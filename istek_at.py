#!/usr/bin/env python3
"""
istek_at.py — bir listedeki herkese takip isteği atar (ör. bio_tara'nın çıkardığı hedefler-ankara.txt).

Geri çekmeyle aynı çalıştırıcıyı kullanır: --hedef-dk ile süre verirsin, sekme sayısı ona
göre açılır. Instagram engellerse bütün sekmeler durur; aynı komut kaldığı yerden devam eder.

Kullanım:
    ./istek_at.sh hedefler-ankara.txt --dry-run            # tıklamaz, durumları gösterir
    ./istek_at.sh hedefler-ankara.txt --limit 5 --watch    # 5 kişiyle izleyerek dene
    ./istek_at.sh hedefler-ankara.txt --hedef-dk 60        # listeyi ~60 dakikaya yay
    ./istek_at.sh hedefler-ankara.txt                      # tek sekme, işlemler arası 4–6 sn

Güvenlik:
    - Sadece gizli hesaplara istek atar. Açık hesapları atlar: sayfada "This profile is
      private" yazısını görmediği hesaba tıklamaz (anlaşılamazsa da tıklamaz).
    - Sadece butonu "Follow" olana tıklar; "Requested" ya da "Following" olanlara dokunmaz.
    - Tıkladıktan sonra butonun "Requested"/"Following"de kaldığını 2 sn izler. Instagram
      isteği sessizce geri alırsa başarısız sayılır; üst üste olursa bütün sekmeler durur.
    - İsteği atacak hesap listenin ilk satırında yazar: "# hesap: @ornek.hesap" (bio_tara bunu
      listeyi çıkardığı hesap olarak yazar). Başka hesaptan atmak için o satırı değiştir;
      --account satırla çelişirse yanlış hesaptan istek gitmesin diye durur.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import Error as PWError, Page

import withdraw_requests as wr
from ig_export import ROOT, log
from withdraw_requests import (
    AMBER,
    BLOCKED_RE,
    FOLLOW_RE,
    FOLLOWING_RE,
    GRAY,
    GREEN,
    RED,
    REQUESTED_RE,
    STOP_RESULTS,
    USERNAME_RE,
    Islem,
    Run,
    body_text,
    execute,
    header_state,
    highlight,
    load_state,
    pick_account,
    plan_speed,
    rel,
    show_step,
    url_problem,
    wait_profile,
)

FOLLOW_STATE_FILE = ROOT / "follow_state.json"
# bio_tara'nın hedefler dosyasına yazdığı ilk satır
LIST_OWNER_RE = re.compile(r"^#\s*hesap:\s*@?([A-Za-z0-9._]{1,30})", re.I)
# Tıkladıktan sonra butonun yeni hâlinde bu kadar kalması gerekiyor
STABLE_SEC = 2.0
# Takip etmediğin gizli hesabın profilinde gönderilerin yerinde bu yazı çıkar
# (2026-09'da gerçek sayfada: "This profile is private / Follow to see their photos and videos.")
PRIVATE_RE = re.compile(r"this (account|profile) is private|bu hesap gizli", re.I)
# Açık hesap: gönderi ızgarası ya da "No posts yet"
NO_POSTS_RE = re.compile(r"no posts yet|henüz (hiç )?gönderi yok", re.I)
# Bütün sayfada aranır: gizli profilde (menü, "Suggested for you", alt linkler) /p/ ya da /reel/ linki yok
POST_LINKS = "a[href*='/p/'], a[href*='/reel/']"
PRIVACY_WAIT_SEC = 8.0


async def privacy(page: Page, timeout: float = PRIVACY_WAIT_SEC) -> str:
    """Takip etmediğin hesap gizli mi açık mı: private / public / unknown.
    Gizli sayılması için "This account is private" yazısını görmek şart; göremezsek
    açık hesabı yanlışlıkla takip etmemek için tıklanmaz."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        text = await body_text(page)
        if PRIVATE_RE.search(text):
            return "private"
        try:
            if NO_POSTS_RE.search(text) or await page.locator(POST_LINKS).count():
                return "public"
        except PWError:
            pass
        await asyncio.sleep(0.25)
    return "unknown"


async def follow_one(page: Page, username: str, dry_run: bool = False, stopping=lambda: False) -> str:
    """
    Dönüş: sent / public / requested / following / not_found / failed /
           blocked / checkpoint / logged_out   (dry-run'da gizli hesap için: follow)
           aborted — başka sekme engel gördüğü için tıklamadan bırakıldı
    """
    await page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=45_000)
    state = await wait_profile(page)

    if state in STOP_RESULTS:
        return state
    if state == "not_found":
        await show_step(page, f"@{username} — hesap yok, atlanıyor", AMBER, 2_000)
        return "not_found"
    await show_step(page, f"@{username} açıldı — takip butonu kontrol ediliyor…", GRAY, 1_200)
    if state == "requested":
        await show_step(page, f"@{username} — istek zaten bekliyor, atlanıyor", AMBER, 2_000)
        return "requested"
    if state == "following":
        await show_step(page, f"@{username} — zaten takip ediyorsun, atlanıyor", AMBER, 2_000)
        return "following"
    if state != "follow":
        _, texts = await header_state(page)
        log(f"  {username}: buton tanınmadı {texts}")
        return "failed"

    kind = await privacy(page)
    if kind == "public":
        await show_step(page, f"@{username} — açık hesap, atlanıyor", AMBER, 2_000)
        return "public"
    if kind != "private":
        log(f"  {username}: gizli mi açık mı anlaşılamadı, tıklanmadı")
        return "failed"
    if dry_run:
        return "follow"
    if stopping():
        return "aborted"

    button = page.locator("header").get_by_role("button", name=FOLLOW_RE).first
    await highlight(page, button, f"@{username} — 'Follow' bulundu → tıklanıyor", RED, 2_000)
    try:
        await button.click(timeout=10_000)
    except PWError:
        await page.locator("header div[role='button']").filter(has_text=FOLLOW_RE).first.click(timeout=10_000)

    # Doğrula: buton "Requested" olmalı ve öyle kalmalı ("Following" olursa hesap açıkmış).
    # Instagram engellediğinde butonu önce değiştirip sonra sessizce geri alabiliyor.
    loop = asyncio.get_running_loop()
    since = None
    for _ in range(48):  # ~12 sn
        await asyncio.sleep(0.25)
        problem = url_problem(page)
        if problem:
            return problem
        if BLOCKED_RE.search(await body_text(page)):
            return "blocked"
        state, _ = await header_state(page)
        if state not in ("requested", "following"):
            since = None
            continue
        since = since or loop.time()
        if loop.time() - since >= STABLE_SEC:
            if state == "following":
                log(f"  UYARI: {username} açık hesapmış, takip edildi (gizli yazısı yanlış okunmuş olabilir)")
            name = REQUESTED_RE if state == "requested" else FOLLOWING_RE
            done = page.locator("header").get_by_role("button", name=name).first
            await highlight(page, done, f"✓ @{username} — istek atıldı (buton artık '{state.title()}')", GREEN, 2_500)
            return "sent"
    return "failed"


LABELS = {
    "sent": "✓ istek atıldı",
    "follow": "gizli hesap, istek atılabilir (dry-run)",
    "public": "atlandı — açık hesap",
    "requested": "atlandı — istek zaten bekliyor",
    "following": "atlandı — zaten takip ediyorsun",
    "not_found": "atlandı — hesap yok/kapalı",
    "failed": "✗ başarısız",
    "blocked": "✗ Instagram engelledi",
    "checkpoint": "✗ Instagram doğrulama istiyor",
    "logged_out": "✗ oturum düştü",
}

SUMMARY = {
    "follow": "gizli hesap, istek atılabilir",
    "sent": "istek atıldı",
    "public": "açık hesap, atlandı",
    "requested": "istek zaten bekliyor",
    "following": "zaten takip ediyorsun",
    "not_found": "hesap yok/kapalı",
    "failed": "başarısız",
    "blocked": "Instagram engelledi",
    "checkpoint": "doğrulama istendi",
    "logged_out": "oturum düştü",
}

ISTEK_AT = Islem(
    name="istek_at",
    fn=follow_one,
    done="sent",
    verb="istek atıldı",
    noun="istek atılan",
    final_skips=frozenset({"public", "requested", "following", "not_found"}),
    labels=LABELS,
    summary=SUMMARY,
    state_file=FOLLOW_STATE_FILE,
    log_key="istek_atilan",
)


def load_list(path: Path) -> tuple[str | None, list[str], int]:
    """(hesap, kullanıcı adları, tanınmayan satır sayısı). Satır başına bir kullanıcı adı;
    '#' ile başlayan satırlar yorum, '# hesap: @x' listenin hangi hesaba ait olduğunu söyler."""
    owner, names, seen, bad = None, [], set(), 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = LIST_OWNER_RE.match(line)
            if m and not owner:
                owner = m.group(1)
            continue
        name = line.lstrip("@")
        if not USERNAME_RE.match(name):
            bad += 1
        elif name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return owner, names, bad


def main() -> int:
    ap = argparse.ArgumentParser(description="Listedeki herkese takip isteği at")
    ap.add_argument("liste", help="satır başına bir kullanıcı adı (ör. hedefler-ankara.txt)")
    ap.add_argument("--account", help="istek atacak hesap; listede '# hesap:' satırı varsa o geçerli")
    ap.add_argument("--dry-run", action="store_true", help="tıklama, sadece durumları göster")
    ap.add_argument("--limit", type=int, help="en fazla bu kadar işlem yap")
    ap.add_argument("--min-delay", type=float, default=4, help="işlemler arası min bekleme (sn)")
    ap.add_argument("--max-delay", type=float, default=6, help="işlemler arası max bekleme (sn)")
    ap.add_argument("--hedef-dk", type=float, help="listeyi yaklaşık bu kadar dakikada bitir (sekme sayısı otomatik)")
    ap.add_argument("--parallel", type=int, help="aynı anda kaç sekme (varsayılan 1; --hedef-dk ile otomatik)")
    ap.add_argument("--headless", action="store_true", help="tarayıcıyı gösterme")
    ap.add_argument("--watch", action="store_true", help="izleme modu: yavaş, butonları işaretler")
    args = ap.parse_args()

    if args.watch:
        wr.WATCH = True
        args.headless = False

    path = Path(args.liste).expanduser().resolve()
    if not path.is_file():
        sys.exit(f"HATA: {args.liste} bulunamadı.")
    owner, names, bad = load_list(path)
    if not names:
        sys.exit(f"HATA: {rel(path)} içinde kullanıcı adı yok.")
    given = args.account.lstrip("@") if args.account else None
    # Yanlış hesaptan istek gitmesin: hesabı listenin ilk satırı belirler
    if owner and given and owner.lower() != given.lower():
        sys.exit(
            f"HATA: {path.name} listesinin ilk satırında istek atacak hesap @{owner} yazıyor "
            f"ama --account @{given} verildi.\n"
            f"@{owner} ile atmak için --account'u kaldır; @{given} ile atmak istiyorsan listenin "
            f"ilk satırını '# hesap: @{given}' yap."
        )
    account = pick_account(given or owner)
    log(f"hesap: @{account}" + ("" if owner else " (listede '# hesap:' satırı yok, doğru hesap olduğundan emin ol)"))

    state = load_state(ISTEK_AT.state_path)
    acc_state = state.setdefault(account, {"sent": {}, "skipped": {}})
    todo = [
        (u, "") for u in names
        if u not in acc_state["sent"] and acc_state["skipped"].get(u) not in ISTEK_AT.final_skips
    ]

    log(f"liste: {rel(path)}" + (f" ({bad} satır kullanıcı adı değil, atlandı)" if bad else ""))
    log(f"{len(names)} kişi, {len(names) - len(todo)} önceden halledilmiş, {len(todo)} kaldı.")
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        log("yapılacak bir şey yok.")
        return 0

    parallel, interval = plan_speed(args, len(todo))
    run = Run(args=args, account=account, total=len(todo), state=state, acc_state=acc_state,
              islem=ISTEK_AT, parallel=parallel, interval=interval)
    return execute(run, todo, len(names))


if __name__ == "__main__":
    sys.exit(main())
