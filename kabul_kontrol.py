#!/usr/bin/env python3
"""
kabul_kontrol.py — istek_at ile gönderilen isteklerden kaçı kabul edildi?

follow_state.json'da "istek atıldı" diye kayıtlı herkesin profil sayfasını açar,
hiçbir şeye tıklamaz. İsteğin durumunu takip butonundan (Following / Requested /
Follow), hesabın gizli olup olmadığını da sayfanın açılırken kendi yüklediği profil
bilgisinden okur. Her çalıştırma kabul_kontrol.json'a tarihli bir ölçüm olarak
eklenir, böylece "1. gün %X, 7. gün %Y" diye karşılaştırılabilir: özet bir önceki
ölçüme göre değişenleri ve bütün ölçümlerin geçmişini de gösterir. Özet sadece toplu
sayıdır; isteği reddeden ya da düşenlerin listesi ayrıca reddedenler-<hesap>.txt'ye yazılır.

Kullanım:
    ./kabul_kontrol.sh --account ornek.hesap
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import textwrap
from collections import Counter
from datetime import datetime

from playwright.async_api import Error as PWError, Page, Response, async_playwright

import istek_at as ia
import withdraw_requests as wr
from ig_export import ROOT, log
from withdraw_requests import BLOCKED_RE, STOP_RESULTS, body_text, ensure_logged_in, load_state, rel, wait_profile

RESULT_FILE = ROOT / "kabul_kontrol.json"
INSTAGRAM_RE = re.compile(r"https?://([a-z]+\.)?instagram\.com/")
# Sayfanın HTML'ine gömülü veri blokları
SCRIPT_JSON_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
# Takip edilen hesabın gizli mi açık mı olduğunu sayfanın verilerinde bu kadar ara
PRIVACY_WAIT_SEC = 6.0
# Üst üste bu kadar profil sayfası tanınmazsa dur (sayfa değişmiş ya da sessiz engel)
UNKNOWN_LIMIT = 3

LABELS = {
    "kabul": "kabul etti",
    "bekliyor": "hâlâ bekliyor",
    "yok": "reddetti ya da istek düştü",
    "acik": "açık hesap (doğrudan takip)",
    "belirsiz": "takipte, gizli mi anlaşılamadı",
    "hesap_yok": "hesap yok/kapalı",
}
STOPS = {
    "blocked": "Instagram engelledi",
    "checkpoint": "Instagram doğrulama istiyor",
    "logged_out": "oturum düştü",
    "tanınmadı": "profil sayfası üst üste tanınmadı",
}


def json_docs(text: str):
    """Bir yanıttaki JSON nesneleri: düz JSON, satır satır JSON ya da HTML'deki veri blokları."""
    text = text.strip().removeprefix("for (;;);")
    parts = SCRIPT_JSON_RE.findall(text) if text.startswith("<") else [text, *text.splitlines()]
    for part in parts:
        try:
            yield json.loads(part)
        except ValueError:
            continue


def find_user(doc, username: str) -> dict | None:
    """JSON ağacında bu kullanıcıya ait, is_private alanı olan nesne. Kullanıcı adı birebir
    eşleşmeli: "Suggested for you" gibi başka hesapların nesneleri karışmasın."""
    stack = [doc]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if str(cur.get("username", "")).lower() == username.lower() and isinstance(cur.get("is_private"), bool):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


async def private_flag(responses: list[Response], username: str) -> bool | None:
    """Sayfanın kendi yüklediği verilerde hesabın gizli olup olmadığı; bulunamazsa None."""
    loop = asyncio.get_running_loop()
    end, seen = loop.time() + PRIVACY_WAIT_SEC, 0
    while True:
        for resp in responses[seen:]:
            seen += 1
            try:
                text = await resp.text()
            except PWError:
                continue
            for doc in json_docs(text):
                user = find_user(doc, username)
                if user is not None:
                    return user["is_private"]
        if loop.time() >= end:
            return None
        await asyncio.sleep(0.5)


async def read_status(page: Page, username: str, responses: list[Response]) -> str:
    """kabul / bekliyor / yok / acik / belirsiz / hesap_yok, ya da durduran
    blocked / checkpoint / logged_out; tanınmayan sayfa için tanınmadı."""
    responses.clear()
    await page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=45_000)
    state = await wait_profile(page)
    if state in STOP_RESULTS:
        return state
    if state == "not_found":
        return "hesap_yok"
    if state == "requested":
        return "bekliyor"
    if state == "follow":
        return "yok"
    if state != "following":
        return "tanınmadı"
    private = await private_flag(responses, username)
    if private is None:
        return "belirsiz"
    return "kabul" if private else "acik"


async def check_all(account: str, users: list[str], results: dict, args) -> str | None:
    """Sonuçları `results`'a yazar (Ctrl+C'de eldekiler kalsın); durma sebebini döner."""
    async with async_playwright() as pw:
        ctx = await wr.open_context(pw, account, args)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if not await ensure_logged_in(page, account, args.headless):
                return "logged_out"
            responses: list[Response] = []

            def keep(resp: Response) -> None:
                if resp.request.resource_type in ("document", "xhr", "fetch") and INSTAGRAM_RE.match(resp.url):
                    responses.append(resp)

            page.on("response", keep)
            unknown = 0
            for n, username in enumerate(users, 1):
                try:
                    result = await read_status(page, username, responses)
                except PWError as exc:
                    log(f"[{n}/{len(users)}] {username}: hata {str(exc).splitlines()[0][:120]}")
                    result = "tanınmadı"
                if result in STOP_RESULTS:
                    message = ""
                    if result == "blocked":
                        text = await body_text(page)
                        m = BLOCKED_RE.search(text)
                        message = re.sub(r"\s+", " ", text[m.start() : m.end() + 80]).strip() if m else ""
                    log(f"[{n}/{len(users)}] {STOPS[result]}" + (f': "{message}"' if message else "") + " — durdu.")
                    return result
                if result == "tanınmadı":
                    unknown += 1
                    log(f"[{n}/{len(users)}] {username}: profil sayfası tanınmadı, atlandı")
                    if unknown >= UNKNOWN_LIMIT:
                        return "tanınmadı"
                    continue
                unknown = 0
                results[username] = result
                log(f"[{n}/{len(users)}] {username}: {LABELS[result]}")
                await asyncio.sleep(random.uniform(args.min_delay, args.max_delay))
        finally:
            try:
                await ctx.close()
            except PWError:
                pass
    return None


def summary(account: str, results: dict, sent: dict, now: datetime) -> str:
    c = Counter(results.values())
    private = c["kabul"] + c["bekliyor"] + c["yok"]
    hours = [(now - datetime.fromisoformat(sent[u])).total_seconds() / 3600 for u in results]

    def pct(k: str) -> str:
        return f"  (%{100 * c[k] / private:.0f})" if k in ("kabul", "bekliyor", "yok") and private else ""

    lines = [
        f"ÖLÇÜM — @{account}: {len(results)}/{len(sent)} istek kontrol edildi"
        + (f", istekler ortalama ~{statistics.median(hours):.0f} saat önce gönderilmişti" if hours else ""),
        *(f"  {LABELS[k]:<32} {c[k]}{pct(k)}" for k in LABELS),
    ]
    if private:
        lines.append(f"  Kabul oranı (gizli hesaplara giden istekler): {c['kabul']}/{private} = %{100 * c['kabul'] / private:.0f}")
    if c["belirsiz"]:
        lines.append("  Takipte olup gizli mi açık mı anlaşılamayanlar orana dahil değil.")
    return "\n".join(lines)


def changes(previous: dict, results: dict) -> str:
    """Bir önceki ölçüme göre durumu değişenler (ör. bekliyor → reddetti: 3)."""
    before = previous["sonuclar"]
    moved = Counter((before[u], r) for u, r in results.items() if u in before and before[u] != r)
    when = previous["zaman"].replace("T", " ")[:16]
    if not moved:
        return f"  Önceki ölçümden ({when}) bu yana değişen yok."
    return "\n".join(
        [f"  Önceki ölçümden ({when}) bu yana değişenler:"]
        + [f"    {LABELS[a]} → {LABELS[b]}: {n}" for (a, b), n in moved.most_common()]
    )


def trend(history: list[dict]) -> str:
    """Bütün ölçümler alt alta: oranın zamanla nasıl değiştiği."""
    lines = ["  Ölçüm geçmişi:"]
    for m in history:
        c = Counter(m["sonuclar"].values())
        private = c["kabul"] + c["bekliyor"] + c["yok"]
        rate = f"%{100 * c['kabul'] / private:.0f}" if private else "-"
        lines.append(
            f"    {m['zaman'].replace('T', ' ')[:16]}  kabul {c['kabul']}/{private} ({rate}) · "
            f"reddeden/düşen {c['yok']} · bekliyor {c['bekliyor']}" + ("" if m.get("tamamlandi") else " · yarım")
        )
    return "\n".join(lines)


def write_declined(account: str, results: dict, now: datetime, complete: bool):
    """Reddeden ya da isteği düşenlerin listesi; ölçüm dosyasının yanına, kişi bazlı."""
    names = sorted(u for u, r in results.items() if r == "yok")
    slug = re.sub(r"\W", "_", account)
    path = RESULT_FILE.with_name(f"reddedenler-{slug}.txt")
    header = f"# @{account} — {now:%Y-%m-%d %H:%M} ölçümünde isteği reddeden ya da düşenler"
    path.write_text(header + ("" if complete else " (yarım ölçüm)") + "\n" + "".join(f"{u}\n" for u in names),
                    encoding="utf-8")
    return path, names


def main() -> int:
    ap = argparse.ArgumentParser(description="Gönderilen takip isteklerinden kaçı kabul edildi")
    ap.add_argument("--account", help="istekleri atan hesap (kayıtta tek hesap varsa otomatik)")
    ap.add_argument("--min-delay", type=float, default=2, help="profiller arası min bekleme (sn)")
    ap.add_argument("--max-delay", type=float, default=4, help="profiller arası max bekleme (sn)")
    ap.add_argument("--headless", action="store_true", help="tarayıcıyı gösterme")
    args = ap.parse_args()
    args.watch = False  # open_context bekliyor

    state = load_state(ia.ISTEK_AT.state_path)
    accounts = [a for a, v in state.items() if v.get("sent")]
    account = args.account.lstrip("@") if args.account else (accounts[0] if len(accounts) == 1 else None)
    if not account:
        sys.exit(f"HATA: hangi hesap? --account ile ver. İstek atılmış hesaplar: {', '.join(accounts) or 'yok'}")
    sent = state.get(account, {}).get("sent", {})
    if not sent:
        sys.exit(f"HATA: @{account} için gönderilmiş istek kaydı yok.")
    # Gönderildikten sonra geri çekilen istek artık kabul edilemez; ölçüme katılmaz
    withdrawn = load_state(wr.GERI_CEK.state_path).get(account, {}).get("withdrawn", {})
    pulled = [u for u, t in sent.items() if withdrawn.get(u, "") > t]
    sent = {u: t for u, t in sent.items() if u not in pulled}
    if not sent:
        sys.exit(f"HATA: @{account} için gönderilen isteklerin hepsi sonradan geri çekilmiş, ölçülecek istek yok.")

    log(f"@{account}: {len(sent)} gönderilmiş istek kontrol edilecek. Hiçbir şeye tıklanmaz."
        + (f" Sonradan geri çekilen {len(pulled)} istek ölçüme katılmıyor." if pulled else ""))
    results: dict[str, str] = {}
    try:
        stop = asyncio.run(check_all(account, list(sent), results, args))
    except KeyboardInterrupt:
        stop = "kullanıcı durdurdu"

    now = datetime.now()
    data = json.loads(RESULT_FILE.read_text(encoding="utf-8")) if RESULT_FILE.exists() else {}
    history = data.setdefault(account, [])
    previous = history[-1] if history else None
    if results:
        history.append({"zaman": now.isoformat(timespec="seconds"), "tamamlandi": stop is None, "sonuclar": results})
        RESULT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 64)
    print(summary(account, results, sent, now))
    if pulled:
        print(f"  Gönderildikten sonra geri çekilen {len(pulled)} istek ölçüme katılmadı.")
    if results and previous and set(results) & set(previous["sonuclar"]):
        print(changes(previous, results))
    if results and len(history) > 1:
        print(trend(history))
    if stop:
        saved = " Kontrol edilenler kaydedildi, tekrar" if results else " Tekrar"
        print(f"  Yarım kaldı: {STOPS.get(stop, stop)}.{saved} çalıştırınca baştan ölçer.")
    if results:
        print(f"  Kayıt: {rel(RESULT_FILE)}")
        path, names = write_declined(account, results, now, complete=stop is None)
        print(f"\nReddeden ya da isteği düşen {len(names)} kişi → {rel(path)} (kişisel veri, paylaşma)")
        if names:
            print(textwrap.fill(", ".join(names), width=100, initial_indent="  ", subsequent_indent="  "))
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())
