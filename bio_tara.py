#!/usr/bin/env python3
"""
bio_tara.py — listedeki profillerin bio'sunu okur, şehre (ya da herhangi bir kelimeye) göre ayırır.

İki adım:
  1) Tarama: export'taki pending_follow_requests listesindeki her profili açar,
     başlıktaki yazıyı (ad, kategori, bio, link) bio_state.json'a kaydeder.
     Hiçbir şeye tıklamaz, kimseye istek atmaz. Kaldığı yerden devam eder.
  2) Ayırma: kayıtlı bio'lar içinde arar. Tarayıcı açmaz; farklı şehirlerle
     istediğin kadar tekrar çalıştırabilirsin.

Kullanım:
    ./bio_tara.sh --limit 5                 # önce birkaç profille dene
    ./bio_tara.sh                           # 1) bütün listeyi tara
    ./bio_tara.sh --hedef-dk 120            #    listeyi ~120 dakikaya yay (sekme sayısı otomatik)
    ./bio_tara.sh --parallel 2              #    2 sekmeyle
    ./bio_tara.sh --sehir ankara            # 2) ayır → hedefler-ankara.txt
    ./bio_tara.sh --sehir bingöl kars       #    birden fazla kelime
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import signal
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.async_api import Error as PWError, Page, async_playwright

from ig_export import LOG_DIR, ROOT, log
from withdraw_requests import (
    BLOCKED_RE,
    FOLLOW_RE,
    FOLLOWING_RE,
    RATE_LOG,
    REQUESTED_RE,
    STATE_FILE as WITHDRAW_STATE_FILE,
    STOP_RESULTS,
    STOP_TITLES,
    body_text,
    ensure_logged_in,
    find_pending_file,
    fmt_duration,
    load_pending,
    open_context,
    owner_of,
    pick_account,
    rel,
    wait_profile,
)

BIO_FILE = ROOT / "bio_state.json"
ALIAS_FILE = ROOT / "sehirler.txt"
# Tek sekmede bir profilin yaklaşık süresi (sayfa + bio okuma), --hedef-dk hesabı için
PER_ITEM_SEC = 5.0

# Başlıkta bio olmayan satırlar: sayılar, butonlar, ortak takipçiler
STAT_RE = re.compile(r"^[\d.,]+\s*[KMB]?\s*(posts?|followers?|following|gönderi|takipçi|takip)?$", re.I)
UI_RE = re.compile(r"^(message|mesaj gönder|posts?|followers?|options|more|devamı|…|\.\.\.)$", re.I)
FOLLOWED_BY_RE = re.compile(r"^followed by\b|takip ediyor$", re.I)
# Uzun bio "… more" ile kısalıyor
MORE_RE = re.compile(r"^\s*(…|\.\.\.)?\s*(more|devamı)\s*$", re.I)

# Ankaralı, Bingöllü, Karsta, Ankarada, Bingölden… (kesme işaretli "Kars'ta" zaten ayrı sayılır)
SUFFIX = r"(?:l[iu](?:y[iu][mz])?|[dt][ae]n?)?"

DURUM = {
    "follow": "istek atılabilir",
    "requested": "istek hâlâ bekliyor",
    "following": "zaten takiptesin",
    "unknown": "buton tanınmadı",
    "not_found": "hesap yok/kapalı",
    "failed": "okunamadı, sonra tekrar denenir",
    "blocked": "Instagram engelledi",
    "checkpoint": "doğrulama istendi",
    "logged_out": "oturum düştü",
}


# --------------------------------------------------------------------------
# Profil okuma
# --------------------------------------------------------------------------


def clean_header(raw: str, username: str) -> str:
    """Başlık metninden ad/kategori/bio/link satırlarını bırakır."""
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if (
            not line
            or line.lower() == username.lower()
            or line in lines
            or STAT_RE.match(line)
            or UI_RE.match(line)
            or FOLLOWED_BY_RE.search(line)
            or any(p.match(line) for p in (REQUESTED_RE, FOLLOWING_RE, FOLLOW_RE))
        ):
            continue
        lines.append(line)
    return "\n".join(lines)


async def read_profile(page: Page, username: str) -> tuple[str, str]:
    """(durum, bio). durum: follow/requested/following/unknown/not_found/failed
    ya da bütün sekmeleri durduran blocked/checkpoint/logged_out."""
    await page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=45_000)
    state = await wait_profile(page, timeout=15)
    if state in STOP_RESULTS or state == "not_found":
        return state, ""

    header = page.locator("header")
    try:
        more = header.get_by_text(MORE_RE)
        if await more.count():
            await more.first.click(timeout=2_000)
            await page.wait_for_timeout(300)
    except PWError:
        pass
    try:
        raw = "\n".join(await header.all_inner_texts())
    except PWError:
        raw = ""
    if not raw.strip():
        return "failed", ""
    return state, clean_header(raw, username)


# --------------------------------------------------------------------------
# Tarama
# --------------------------------------------------------------------------


@dataclass
class Scan:
    total: int
    state: dict
    bios: dict
    parallel: int
    interval: float | None = None  # --hedef-dk: profiller arası ortak aralık (sn)
    counts: dict = field(default_factory=dict)
    done: int = 0
    fails: int = 0
    stop_reason: str | None = None
    announced: bool = False
    started: float = field(default_factory=time.monotonic)
    next_slot: float = 0.0

    def rate_per_min(self) -> float:
        elapsed = time.monotonic() - self.started
        return self.done / elapsed * 60 if elapsed > 0 else 0.0

    def record(self, n: int, username: str, durum: str, bio: str) -> None:
        self.done += 1
        self.counts[durum] = self.counts.get(durum, 0) + 1
        if durum == "failed" or durum in STOP_RESULTS:
            self.fails += durum == "failed"
            log(f"[{n}/{self.total}] @{username}: {DURUM.get(durum, durum)}")
            return
        self.fails = 0
        self.bios[username] = {"durum": durum, "bio": bio, "zaman": datetime.now().isoformat(timespec="seconds")}
        preview = bio.replace("\n", " | ")[:90] or "— bio boş —"
        log(f"[{n}/{self.total}] @{username} ({DURUM.get(durum, durum)}): {preview}")
        if self.done % 10 == 0:
            save_bios(self.state)
        if self.done % 50 == 0:
            elapsed = time.monotonic() - self.started
            eta = (self.total - self.done) / (self.done / elapsed)
            log(
                f"— ilerleme {self.done}/{self.total} · hız {self.rate_per_min():.1f} profil/dk · "
                f"geçen {fmt_duration(elapsed)} · kalan ~{fmt_duration(eta)}"
            )

    async def stop(self, reason: str, page: Page | None = None, username: str = "") -> None:
        """Bütün sekmeleri durdurur ve terminale bir kez rapor basar."""
        if self.announced:
            return
        self.announced = True
        self.stop_reason = reason
        message, shot = "", None
        if page is not None:
            text = await body_text(page)
            m = BLOCKED_RE.search(text)
            if m:
                message = re.sub(r"\s+", " ", text[m.start() : m.end() + 120]).strip()
            try:
                LOG_DIR.mkdir(exist_ok=True)
                shot = LOG_DIR / f"bio-engel-{datetime.now():%Y%m%d-%H%M%S}.png"
                await page.screenshot(path=str(shot))
            except PWError:
                shot = None

        bar = "=" * 64
        lines = [
            "",
            bar,
            f"  ⛔ {STOP_TITLES.get(reason, reason)}",
            bar,
            f"  saat            : {datetime.now():%H:%M:%S}",
            f"  son profil      : @{username}" if username else None,
            f"  bu çalıştırmada : {self.done} profil, {fmt_duration(time.monotonic() - self.started)}",
            f"  hız             : {self.rate_per_min():.1f} profil/dk  ({self.parallel} sekme)",
            f"  sayfadaki mesaj : \"{message}\"" if message else None,
            f"  ekran görüntüsü : {rel(shot)}" if shot else None,
            "  Okunanlar kaydedildi; birkaç saat sonra aynı komut kaldığı yerden devam eder.",
            bar,
            "",
        ]
        print("\a" + "\n".join(l for l in lines if l is not None), flush=True)

    async def take_slot(self) -> None:
        """--hedef-dk: sekmeler ortak bir sıradan zaman alır; böylece toplam hız sabit kalır."""
        if not self.interval:
            return
        slot = max(time.monotonic(), self.next_slot)
        self.next_slot = slot + self.interval * random.uniform(0.8, 1.2)
        await self.sleep_until(slot)

    async def sleep_until(self, when: float) -> None:
        while self.stop_reason is None and (left := when - time.monotonic()) > 0:
            await asyncio.sleep(min(0.25, left))


async def worker(scan: Scan, ctx, queue: asyncio.Queue, args) -> None:
    page = await ctx.new_page()
    fail_limit = max(3, scan.parallel + 1)
    try:
        while scan.stop_reason is None:
            try:
                n, username = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await scan.take_slot()
            if scan.stop_reason is not None:
                break
            try:
                durum, bio = await read_profile(page, username)
            except PWError as exc:
                log(f"[{n}/{scan.total}] @{username}: hata {str(exc).splitlines()[0][:120]}")
                durum, bio = "failed", ""
            scan.record(n, username, durum, bio)

            if durum in STOP_RESULTS:
                await scan.stop(durum, page, username)
                break
            if scan.fails >= fail_limit:
                await scan.stop("silent", page, username)
                break
            if not scan.interval:
                await scan.sleep_until(time.monotonic() + random.uniform(args.min_delay, args.max_delay))
    finally:
        try:
            await page.close()
        except PWError:
            pass


async def run_scan(scan: Scan, account: str, todo: list[str], args) -> None:
    loop = asyncio.get_running_loop()

    def on_sigint() -> None:
        log("durduruluyor — açık sekmeler eldeki profili bitiriyor (hemen çıkmak için tekrar Ctrl+C)…")
        scan.stop_reason = scan.stop_reason or "user"
        loop.remove_signal_handler(signal.SIGINT)

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    except (NotImplementedError, RuntimeError):
        pass

    async with async_playwright() as pw:
        ctx = await open_context(pw, account, args)
        try:
            first = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if not await ensure_logged_in(first, account, args.headless):
                scan.stop_reason = "logged_out"
                log("HATA: giriş yapılamadı.")
                return
            queue: asyncio.Queue = asyncio.Queue()
            for n, username in enumerate(todo, 1):
                queue.put_nowait((n, username))
            scan.started = scan.next_slot = time.monotonic()
            await asyncio.gather(*(worker(scan, ctx, queue, args) for _ in range(scan.parallel)))
        finally:
            try:
                await ctx.close()
            except PWError:
                pass


def scan_mode(account: str, state: dict, bios: dict, args) -> int:
    source = Path(args.file).expanduser().resolve() if args.file else find_pending_file(account)
    if not source or not source.exists():
        sys.exit(f"HATA: @{account} için pending_follow_requests dosyası bulunamadı; --file ile ver.")
    owner = owner_of(source)
    if owner and owner.lower() != account.lower():
        sys.exit(f"HATA: {source.name} @{owner} hesabına ait ama @{account} seçildi.")

    names = [u for u, _ in load_pending(source)]
    # Geri çekerken "hesap yok" çıkanları tekrar açmaya gerek yok
    withdraw_state = json.loads(WITHDRAW_STATE_FILE.read_text(encoding="utf-8")) if WITHDRAW_STATE_FILE.exists() else {}
    gone = {u for u, r in withdraw_state.get(account, {}).get("skipped", {}).items() if r == "not_found"}
    todo = [u for u in names if u not in gone and (args.yeniden or u not in bios)]

    log(f"hesap: @{account} · kaynak: {rel(source)}")
    log(f"{len(names)} kişi listede, {len(names) - len(todo)} önceden okunmuş ya da hesabı yok, {len(todo)} okunacak.")
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        log("okunacak profil yok. Ayırmak için: ./bio_tara.sh --sehir ankara")
        return 0

    # Hız ayarı (geri_cek.sh --hedef-dk ile aynı mantık)
    interval = None
    if args.hedef_dk:
        interval = args.hedef_dk * 60 / len(todo)
        parallel = args.parallel or min(10, max(1, math.ceil(PER_ITEM_SEC / interval) + 1))
    else:
        parallel = args.parallel or 1
    parallel = max(1, min(parallel, len(todo)))
    if interval:
        reachable = parallel / PER_ITEM_SEC * 60  # profil/dk
        wanted = 60 / interval
        log(f"hedef ~{args.hedef_dk:g} dk: {parallel} sekme, ~{interval:.1f} sn'de bir profil (~{wanted:.0f} profil/dk)")
        if reachable < wanted * 0.9:
            log(f"UYARI: {parallel} sekmeyle bu hıza yetişilemez; tahmini süre ~{fmt_duration(len(todo) / reachable * 60)}.")
    else:
        per_item = PER_ITEM_SEC + (args.min_delay + args.max_delay) / 2
        log(f"{parallel} sekme, tahmini süre ~{fmt_duration(len(todo) * per_item / parallel)}.")
    log("Hiçbir şeye tıklanmaz. Ctrl+C ile durdurabilirsin (okunanlar kaydedilir).")

    scan = Scan(total=len(todo), state=state, bios=bios, parallel=parallel, interval=interval)
    try:
        asyncio.run(run_scan(scan, account, todo, args))
    except KeyboardInterrupt:
        scan.stop_reason = scan.stop_reason or "user"
        log("hemen durduruldu.")
    finally:
        save_bios(state)
        append_rate_log(scan, account, args)

    print("\n" + "=" * 64)
    print("ÖZET")
    print("=" * 64)
    for key, value in sorted(scan.counts.items(), key=lambda kv: -kv[1]):
        print(f"  {DURUM.get(key, key):<34} {value}")
    filled = sum(1 for info in bios.values() if info.get("bio"))
    print(f"\n  süre: {fmt_duration(time.monotonic() - scan.started)} · hız: {scan.rate_per_min():.1f} profil/dk")
    if scan.stop_reason and scan.stop_reason != "user":
        print(f"  durma sebebi: {STOP_TITLES.get(scan.stop_reason, scan.stop_reason)}")
    print(f"  Kayıtlı: {len(bios)}/{len(names)} profil, {filled} tanesinde bio var → {rel(BIO_FILE)}")
    print("  Ayırmak için:  ./bio_tara.sh --sehir ankara bingöl kars")
    return 1 if scan.stop_reason in STOP_RESULTS | {"silent"} else 0


# --------------------------------------------------------------------------
# Ayırma
# --------------------------------------------------------------------------


def norm(text: str) -> str:
    """Büyük/küçük harf, Türkçe karakter ve süslü font (𝐀𝐧𝐤𝐚𝐫𝐚) farkını siler: BİNGÖL → bingol."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("ı", "i")


def word_pattern(word: str) -> re.Pattern:
    """Kelimenin parçasını saymaz (kars ≠ karşıyaka, karşı); ekleri sayar (Karslı, Ankara'da).
    Rakamla biten kelimede (plaka kodu 06) sınır rakamdır: ankara06 tutar, 2006 tutmaz.
    Sonu * olan öntakıdır (gazi uni* → Gazi Üniversitesi). Boşluk yerine . _ - ya da
    bitişik yazım da tutar (haci bayram → Hacıbayram)."""
    prefix = word.endswith("*")
    w = norm(word.rstrip("*")).strip()
    body = r"[\s._-]*".join(map(re.escape, w.split()))
    before = "[0-9]" if w[:1].isdigit() else "[a-z]"
    if prefix:
        return re.compile(rf"(?<!{before}){body}")
    after = "[0-9]" if w[-1:].isdigit() else "[a-z]"
    suffix = SUFFIX if w[-1:].isalpha() else ""
    return re.compile(rf"(?<!{before}){body}{suffix}(?!{after})")


def load_aliases() -> dict[str, list[str]]:
    """sehirler.txt: [şehir] satırının altındaki her kelime o şehir sayılır (üniler, kısaltmalar)."""
    aliases: dict[str, list[str]] = {}
    if not ALIAS_FILE.exists():
        return aliases
    group = None
    for line in ALIAS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            group = aliases.setdefault(norm(m.group(1)), [])
        elif group is not None:
            group.append(line)
    return aliases


def split_mode(account: str, bios: dict, args) -> int:
    if not bios:
        sys.exit(f"HATA: @{account} için kayıtlı bio yok; önce ./bio_tara.sh --account {account} ile tara.")

    words = list(dict.fromkeys(args.sehir))
    aliases = load_aliases()
    targets: list[str] = []
    for word in words:
        terms = [word, *aliases.get(norm(word), [])]
        patterns = [(term, word_pattern(term)) for term in terms]
        hits = []
        for username, info in bios.items():
            lines = [username, *info.get("bio", "").splitlines()]
            match = next(((t, l) for l in lines for t, p in patterns if p.search(norm(l))), None)
            if match:
                hits.append((username, info.get("durum", "unknown"), *match))
        sendable = [u for u, durum, _, _ in hits if durum == "follow"]
        extra = f" (+{len(terms) - 1} kelime {rel(ALIAS_FILE)}'den)" if len(terms) > 1 else ""
        print(f"\n{word}{extra} — {len(hits)} kişi, {len(sendable)} tanesine istek atılabilir")
        for username, durum, term, line in hits:
            print(f"  @{username:<30} {DURUM.get(durum, durum):<20} [{term}] {line[:60]}")
        targets += sendable

    targets = list(dict.fromkeys(targets))
    slug = "-".join(re.sub(r"[^a-z0-9]+", "", norm(w)) for w in words)
    out = Path(args.yaz).expanduser() if args.yaz else ROOT / f"hedefler-{slug}.txt"
    # İlk satır istek_at'e listenin hangi hesaba ait olduğunu söyler
    out.write_text(f"# hesap: @{account}\n" + "".join(f"{u}\n" for u in targets), encoding="utf-8")
    print(f"\n{len(bios)} kayıtlı bio içinde arandı.")
    print(f"{len(targets)} kişi → {rel(out.resolve())}  (isteği bekleyenler ve zaten takip ettiklerin hariç)")
    print("Yukarıdaki listeye göz at: yanlış eşleşme varsa dosyadan o satırı sil.")
    return 0


# --------------------------------------------------------------------------


def load_bios() -> dict:
    if BIO_FILE.exists():
        return json.loads(BIO_FILE.read_text(encoding="utf-8"))
    return {}


def save_bios(state: dict) -> None:
    BIO_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def append_rate_log(scan: Scan, account: str, args) -> None:
    """geri_cek.sh ile aynı dosya; "islem" alanı taramayı geri çekmeden ayırır."""
    entry = {
        "zaman": datetime.now().isoformat(timespec="seconds"),
        "hesap": account,
        "islem": "bio_tara",
        "sonuc": scan.stop_reason or "tamamlandi",
        "sekme": scan.parallel,
        "hedef_dk": args.hedef_dk,
        "bekleme_sn": None if scan.interval else [args.min_delay, args.max_delay],
        "islenen": scan.done,
        "sure_sn": round(time.monotonic() - scan.started),
        "hiz_profil_dk": round(scan.rate_per_min(), 1),
    }
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with RATE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Profil bio'larını tara, şehre göre ayır")
    ap.add_argument("--account", help="kullanıcı adı (tek hesap varsa otomatik)")
    ap.add_argument("--file", help="pending_follow_requests.html/json yolu (varsayılan: en yeni export)")
    ap.add_argument("--sehir", nargs="+", metavar="KELİME",
                    help="taramadan, kayıtlı bio'larda ara (ör. ankara bingöl kars 06)")
    ap.add_argument("--yaz", help="eşleşenlerin yazılacağı dosya (varsayılan: hedefler-<kelimeler>.txt)")
    ap.add_argument("--limit", type=int, help="en fazla bu kadar profil oku")
    ap.add_argument("--yeniden", action="store_true", help="daha önce okunanları da tekrar oku")
    ap.add_argument("--hedef-dk", type=float, help="listeyi yaklaşık bu kadar dakikada bitir (sekme sayısı otomatik)")
    ap.add_argument("--parallel", type=int, help="aynı anda kaç sekme (varsayılan 1; --hedef-dk ile otomatik)")
    ap.add_argument("--min-delay", type=float, default=2, help="profiller arası min bekleme (sn)")
    ap.add_argument("--max-delay", type=float, default=4, help="profiller arası max bekleme (sn)")
    ap.add_argument("--headless", action="store_true", help="tarayıcıyı gösterme")
    args = ap.parse_args()
    args.watch = False  # open_context bekliyor

    account = pick_account(args.account)
    state = load_bios()
    bios = state.setdefault(account, {})
    if args.sehir:
        return split_mode(account, bios, args)
    return scan_mode(account, state, bios, args)


if __name__ == "__main__":
    sys.exit(main())
