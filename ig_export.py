#!/usr/bin/env python3
"""
ig_export.py — Meta "Bilgilerini indir" (Download Your Information / DYI)
toplu indirici.

Accounts Center'daki hazır export dosyalarını indirir, açar ve durumu
kaydeder. Kullanıcı adı ve şifre çalışırken sorulur — hiçbir yere yazılmaz.

Kullanım:
    ./run.sh                      # sorar, indirir (tarayıcı görünür)
    ./run.sh --list               # indirme yapma, sadece ne var göster
    ./run.sh --dump-links         # linkleri çıkar (wget ile manuel indirmek için)
    ./run.sh --headless           # arka planda (ilk login yapılmışsa)

İstersen birden fazla hesap girebilirsin: ilk hesabı girdikten sonra
kullanıcı adı sorusunu boş geçersen indirmeye başlar.

Her hesabın çerezleri profiles/<hesap>/ altında kalıcı tutulur; ilk
girişten sonra tekrar login/2FA istemez.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from getpass import getpass
from pathlib import Path

from playwright.sync_api import (
    Download,
    Error as PWError,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "accounts.json"
PROFILES_DIR = ROOT / "profiles"
DOWNLOADS_DIR = ROOT / "downloads"
STATE_FILE = ROOT / "state.json"
LOG_DIR = ROOT / "logs"

DYI_URL = "https://accountscenter.instagram.com/info_and_permissions/dyi/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"

# Meta arayüzü dil bazlı değiştiği için context'i en-US'e sabitliyoruz;
# yine de TR karşılıkları yedek olarak duruyor.
DOWNLOAD_WORDS = r"(download|i̇ndir|indir|descargar|herunterladen)"
PASSWORD_WORDS = r"(continue|confirm|submit|devam|onayla|g[oö]nder|log in|giri[sş])"

# Yeni Meta arayüzündeki sekmeler — istekler bunların içinde
TAB_NAMES = ["Current activity", "Past activity"]
EXPIRED_RE = re.compile(r"(have expired|has expired|s[uü]resi dol)", re.I)
ENTRY_RE = re.compile(r"\((Instagram|Facebook|Messenger)\)\s*$", re.I)

# Signed CDN linkleri ve accountscenter indirme uçları
LINK_PATTERNS = [
    re.compile(r"fbcdn\.net/.*dyi", re.I),
    re.compile(r"/download/(file|dyi)", re.I),
    re.compile(r"dyi.*[?&](job_id|jobid|archive)", re.I),
    re.compile(r"instagram\.com/download/", re.I),
]


def log(msg: str, *, account: str | None = None) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{stamp}]" + (f" [{account}]" if account else "")
    print(f"{prefix} {msg}", flush=True)


# --------------------------------------------------------------------------
# Hesap / durum yönetimi
# --------------------------------------------------------------------------


@dataclass
class Account:
    username: str
    password: str | None = None
    label: str | None = None

    @property
    def name(self) -> str:
        return self.label or self.username


@dataclass
class AccountResult:
    username: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    expired: int = 0
    error: str | None = None


def prompt_accounts() -> list[Account]:
    """Kullanıcı adı + şifreyi çalışırken sorar. Şifre ekrana yazılmaz."""
    if not sys.stdin.isatty():
        sys.exit(
            "HATA: manuel giriş için gerçek bir terminal lazım.\n"
            "VSCode'da Terminal > New Terminal açıp ./run.sh çalıştır."
        )

    accounts: list[Account] = []
    print("Instagram hesap bilgileri (şifre ekranda görünmez):\n")
    while True:
        if accounts:
            label = f"  {len(accounts) + 1}. hesap kullanıcı adı (boş bırak = devam et): "
        else:
            label = "  Kullanıcı adı: "
        username = input(label).strip().lstrip("@")

        if not username:
            if accounts:
                break
            print("  Kullanıcı adı boş olamaz.")
            continue

        password = getpass(f"  {username} şifresi: ").strip()
        if not password:
            print("  Şifre boş — bu hesap için sonra sorulacak.")
        accounts.append(Account(username=username, password=password or None))
        print()

    print()
    return accounts


def load_accounts(only: list[str] | None, ask: bool) -> list[Account]:
    if ask or not ACCOUNTS_FILE.exists():
        return prompt_accounts()

    raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("accounts", [])

    accounts: list[Account] = []
    for item in raw:
        if isinstance(item, str):
            accounts.append(Account(username=item))
        else:
            accounts.append(
                Account(
                    username=item["username"],
                    password=item.get("password") or None,
                    label=item.get("label"),
                )
            )
    if only:
        wanted = {u.strip().lower() for u in only}
        accounts = [a for a in accounts if a.username.lower() in wanted]
        if not accounts:
            sys.exit(f"HATA: --accounts ile eşleşen hesap yok: {', '.join(only)}")
    if not accounts:
        sys.exit("HATA: accounts.json boş.")
    return accounts


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("UYARI: state.json bozuk, sıfırdan başlanıyor.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def resolve_password(acc: Account, interactive: bool) -> str | None:
    """Şifreyi sırayla: accounts.json -> ortam değişkeni -> soru."""
    if acc.password:
        return acc.password
    env_key = "IG_PW_" + re.sub(r"\W", "_", acc.username).upper()
    if os.environ.get(env_key):
        return os.environ[env_key]
    if interactive and sys.stdin.isatty():
        return getpass(f"  {acc.username} şifresi (boş geç = atla): ") or None
    return None


# --------------------------------------------------------------------------
# Sayfa yardımcıları
# --------------------------------------------------------------------------


def is_logged_out(page: Page) -> bool:
    url = page.url.lower()
    if "/accounts/login" in url or "login.php" in url or "/login/" in url:
        return True
    try:
        return page.locator("input[name='username'], input[name='email']").count() > 0
    except PWError:
        return False


def do_login(page: Page, acc: Account, password: str | None, headless: bool) -> bool:
    """Otomatik login dener; 2FA/checkpoint çıkarsa kullanıcıyı bekler."""
    log("oturum kapalı, giriş deneniyor…", account=acc.name)
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeout:
        pass

    if password:
        try:
            page.fill("input[name='username']", acc.username, timeout=20_000)
            page.fill("input[name='password']", password, timeout=20_000)
            page.press("input[name='password']", "Enter")
        except PWError as exc:
            log(f"login formu doldurulamadı: {exc}", account=acc.name)
    else:
        log("şifre yok — elle giriş bekleniyor.", account=acc.name)

    if headless:
        page.wait_for_timeout(12_000)
        return not is_logged_out(page)

    # Görünür modda 2FA / "şüpheli giriş" ekranı için kullanıcıya süre tanı.
    log("2FA veya doğrulama varsa tarayıcıda tamamla — 3 dk bekleniyor…", account=acc.name)
    deadline = time.time() + 180
    while time.time() < deadline:
        page.wait_for_timeout(3_000)
        if not is_logged_out(page):
            log("giriş başarılı.", account=acc.name)
            return True
    log("giriş zaman aşımına uğradı.", account=acc.name)
    return False


def confirm_password_if_asked(page: Page, password: str | None, account: str) -> None:
    """İndirmeden önce çıkan 'şifreni gir' modalını geçer."""
    try:
        pw_input = page.locator("input[type='password']:visible").first
        if pw_input.count() == 0:
            return
    except PWError:
        return

    if not password:
        log("şifre onayı istendi ama şifre yok — elle gir (90 sn).", account=account)
        page.wait_for_timeout(90_000)
        return

    log("şifre onayı isteniyor, dolduruluyor…", account=account)
    try:
        pw_input.fill(password, timeout=15_000)
        button = page.get_by_role(
            "button", name=re.compile(PASSWORD_WORDS, re.I)
        ).first
        if button.count() > 0:
            button.click(timeout=10_000)
        else:
            pw_input.press("Enter")
    except PWError as exc:
        log(f"şifre onayı geçilemedi: {exc}", account=account)


def harvest_links(page: Page) -> list[str]:
    """DOM'daki indirme linklerini toplar (tıklamaya gerek kalmadan)."""
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )
    except PWError:
        return []
    found: list[str] = []
    for href in hrefs:
        if any(p.search(href) for p in LINK_PATTERNS) and href not in found:
            found.append(href)
    return found


def scan_tab(page: Page, tab: str, account: str):
    """Bir sekmeyi açar; istekleri, süresi dolmuşları, linkleri ve butonları döner."""
    try:
        page.get_by_text(tab, exact=True).first.click(timeout=15_000)
        page.wait_for_timeout(4_000)
    except PWError:
        log(f"'{tab}' sekmesi bulunamadı/açılamadı.", account=account)
        return [], 0, [], 0

    # Liste tembel yükleniyor — sonuna kadar kaydır.
    for _ in range(5):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(700)

    try:
        text = page.inner_text("body")
    except PWError:
        text = ""

    # "ornek.hesap (Instagram)" gibi başlıklar her isteğin satır başı.
    entries = [
        line.strip()
        for line in text.splitlines()
        if ENTRY_RE.search(line)
    ]
    expired = len(EXPIRED_RE.findall(text))
    links = harvest_links(page)
    buttons = find_download_buttons(page)
    n_buttons = buttons.count() if buttons else 0

    log(
        f"'{tab}': {len(entries)} istek, {n_buttons} buton, {len(links)} link"
        + (" — SÜRESİ DOLMUŞ" if expired else ""),
        account=account,
    )
    for e in entries:
        log(f"    · {e}", account=account)

    return entries, (len(entries) if expired else 0), links, n_buttons


def find_download_buttons(page: Page):
    """Sayfadaki 'Download' butonlarını bulur (rol + metin, birkaç yedekle)."""
    candidates = [
        page.get_by_role("button", name=re.compile(DOWNLOAD_WORDS, re.I)),
        page.get_by_role("link", name=re.compile(DOWNLOAD_WORDS, re.I)),
        page.locator("div[role='button']:has-text('Download')"),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except PWError:
            continue
    return None


def save_download(dl: Download, target_dir: Path, account: str) -> Path | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    name = dl.suggested_filename or f"dyi-{int(time.time())}.zip"
    dest = target_dir / name
    counter = 1
    while dest.exists():
        dest = target_dir / f"{dest.stem}-{counter}{dest.suffix}"
        counter += 1
    try:
        dl.save_as(dest)
    except PWError as exc:
        log(f"dosya kaydedilemedi ({name}): {exc}", account=account)
        return None
    size_mb = dest.stat().st_size / 1024 / 1024
    log(f"indirildi: {dest.name} ({size_mb:.1f} MB)", account=account)
    return dest


def extract_zip(path: Path, account: str) -> None:
    if path.suffix.lower() != ".zip":
        return
    out = path.with_suffix("")
    try:
        with zipfile.ZipFile(path) as zf:
            zf.extractall(out)
        log(f"açıldı: {out.name}/", account=account)
    except zipfile.BadZipFile:
        log(f"UYARI: {path.name} geçerli bir zip değil (link süresi dolmuş olabilir).", account=account)


# --------------------------------------------------------------------------
# Hesap başına ana akış
# --------------------------------------------------------------------------


def process_account(
    pw,
    acc: Account,
    args,
    state: dict,
) -> AccountResult:
    result = AccountResult(username=acc.username)
    profile_dir = PROFILES_DIR / re.sub(r"\W", "_", acc.username)
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_dir = DOWNLOADS_DIR / re.sub(r"\W", "_", acc.username)
    done = set(state.get(acc.username, {}).get("downloaded", []))

    password = resolve_password(acc, interactive=not args.headless)

    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel="chrome",
        headless=args.headless,
        accept_downloads=True,
        locale="en-US",
        viewport={"width": 1440, "height": 950},
        args=["--disable-blink-features=AutomationControlled"],
    )
    context.set_default_timeout(45_000)

    try:
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(DYI_URL, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            log("sayfa yavaş yüklendi, devam ediliyor.", account=acc.name)
        page.wait_for_timeout(4_000)

        if is_logged_out(page):
            if not do_login(page, acc, password, args.headless):
                result.error = "giriş yapılamadı"
                return result
            page.goto(DYI_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)

        # Meta'nın yeni arayüzünde istekler "Current activity" / "Past activity"
        # sekmelerinin içinde; sayfada doğrudan durmuyor.
        links: list[str] = []
        buttons_total = 0
        for tab in TAB_NAMES:
            entries, expired, tab_links, tab_buttons = scan_tab(page, tab, acc.name)
            result.entries.extend(entries)
            result.expired += expired
            buttons_total += tab_buttons
            for href in tab_links:
                if href not in links:
                    links.append(href)

        result.links = links
        log(
            f"toplam: {len(result.entries)} istek, {result.expired} süresi dolmuş, "
            f"{buttons_total} indirme butonu, {len(links)} link.",
            account=acc.name,
        )

        # Sekmeler açılamasa bile hazır dosya sayfanın kendisinde durabiliyor.
        if buttons_total == 0:
            main_buttons = find_download_buttons(page)
            buttons_total = main_buttons.count() if main_buttons else 0

        if not result.entries and buttons_total == 0 and not links:
            log(
                "hiç export isteği yok — Instagram'da önce 'Create export' ile istek at.",
                account=acc.name,
            )
        elif buttons_total == 0 and not links:
            if result.expired:
                log(
                    "tüm istekler süresi dolmuş; yeniden 'Create export' yapman gerek.",
                    account=acc.name,
                )
            else:
                log("istekler var ama indirilebilir dosya yok (hâlâ hazırlanıyor olabilir).",
                    account=acc.name)

        if args.dump_links or args.list:
            return result

        # 1) Önce doğrudan linkleri çerezli request ile çek (en sağlam yol).
        for href in links:
            key = href.split("?")[0]
            if key in done:
                result.skipped.append(key)
                continue
            try:
                resp = context.request.get(href, timeout=180_000)
                if not resp.ok:
                    continue
                body = resp.body()
                if len(body) < 5_000:  # HTML hata sayfası, dosya değil
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                fname = href.split("/")[-1].split("?")[0] or f"dyi-{int(time.time())}.zip"
                if not fname.endswith(".zip"):
                    fname += ".zip"
                dest = out_dir / fname
                dest.write_bytes(body)
                log(f"indirildi (link): {dest.name} ({len(body)/1024/1024:.1f} MB)", account=acc.name)
                extract_zip(dest, acc.name)
                result.downloaded.append(key)
                done.add(key)
            except PWError as exc:
                log(f"link indirilemedi: {exc}", account=acc.name)

        # 2) Kalanlar için butonlara tıkla ve download olayını yakala.
        buttons = find_download_buttons(page)
        total = buttons.count() if buttons else 0
        log(f"{total} indirme butonu deneniyor.", account=acc.name)

        for i in range(total):
            try:
                btn = find_download_buttons(page).nth(i)
                with page.expect_download(timeout=240_000) as dl_info:
                    btn.click(timeout=20_000)
                    page.wait_for_timeout(2_500)
                    confirm_password_if_asked(page, password, acc.name)
                dest = save_download(dl_info.value, out_dir, acc.name)
                if dest:
                    extract_zip(dest, acc.name)
                    result.downloaded.append(dest.name)
                    done.add(dest.name)
            except PWTimeout:
                log(f"buton #{i+1}: indirme başlamadı (atlanıyor).", account=acc.name)
            except PWError as exc:
                log(f"buton #{i+1} hatası: {exc}", account=acc.name)
            page.wait_for_timeout(1_500)

        if not result.downloaded:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            shot = LOG_DIR / f"{acc.username}-{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                log(f"hiçbir şey inmedi — ekran görüntüsü: {shot}", account=acc.name)
            except PWError:
                pass

    except Exception as exc:  # noqa: BLE001 - tek hesap patlarsa diğerleri devam etsin
        result.error = str(exc)
        log(f"HATA: {exc}", account=acc.name)
    finally:
        state[acc.username] = {
            "downloaded": sorted(done),
            "last_run": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(state)
        try:
            context.close()
        except PWError:
            pass

    return result


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Instagram DYI toplu indirici")
    ap.add_argument("--accounts", help="virgülle ayrılmış kullanıcı adları")
    ap.add_argument("--headless", action="store_true", help="tarayıcıyı gösterme")
    ap.add_argument("--list", action="store_true", help="sadece listele, indirme")
    ap.add_argument("--dump-links", action="store_true", help="linkleri dosyaya yaz")
    ap.add_argument(
        "--ask",
        action="store_true",
        help="accounts.json olsa bile kullanıcı adı/şifreyi elle sor",
    )
    args = ap.parse_args()

    only = args.accounts.split(",") if args.accounts else None
    accounts = load_accounts(only, args.ask)
    state = load_state()

    log(f"{len(accounts)} hesap işlenecek: {', '.join(a.username for a in accounts)}")
    results: list[AccountResult] = []

    with sync_playwright() as pw:
        for acc in accounts:
            log("=" * 60)
            results.append(process_account(pw, acc, args, state))

    # Özet
    print("\n" + "=" * 60)
    print("ÖZET")
    print("=" * 60)
    all_links: list[str] = []
    for r in results:
        status = f"HATA: {r.error}" if r.error else f"{len(r.downloaded)} indirildi"
        if r.skipped:
            status += f", {len(r.skipped)} zaten vardı"
        print(f"  {r.username:<24} {status}")
        all_links.extend(r.links)

    if args.dump_links and all_links:
        dest = ROOT / "links.txt"
        dest.write_text("\n".join(all_links), encoding="utf-8")
        print(f"\n{len(all_links)} link -> {dest}")
        print(f"Manuel indirmek için:  wget -i {dest.name} -P downloads/")

    print(f"\nDosyalar: {DOWNLOADS_DIR}")
    if any(r.downloaded for r in results):
        print("Takip isteklerini geri çekmek için:  ./geri_cek.sh --dry-run  sonra  ./geri_cek.sh")
    return 0 if all(not r.error for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
