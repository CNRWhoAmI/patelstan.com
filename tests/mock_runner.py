"""withdraw_requests.main()'i sahte bir Instagram'a karşı çalıştırır.

Gerçek profile, kayıtlara ya da internete dokunmaz: bütün instagram.com
istekleri buradaki sahte sayfalara yönlenir, geri kalan her şey engellenir;
profil/state/log dosyaları MOCK_CFG["tmp"] altında oluşur.
Kullanıcı adının öneki sayfanın davranışını seçer:
  req_ normal istek · fol_ kabul etmiş · nr_ istek yok · gone_ hesap yok
  flash_ önce "Follow" görünüp sonra "Requested" olur · block_ Try Again Later
  silent_ tıklama sessizce geri alınır · chk_ doğrulama sayfasına yönlenir
"""
import faulthandler, json, os, pathlib, re, signal, sys
from urllib.parse import urlparse

faulthandler.register(signal.SIGUSR1, all_threads=True)  # takılırsa yığın dökümü
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import withdraw_requests as w  # noqa: E402

cfg = json.loads(os.environ["MOCK_CFG"])
tmp = pathlib.Path(cfg["tmp"])
w.DOWNLOADS_DIR = tmp / "downloads"
w.PROFILES_DIR = tmp / "profiles"
w.STATE_FILE = tmp / "state.json"
w.LOG_DIR = tmp / "logs"
w.RATE_LOG = tmp / "logs" / "rate_limit.log"
CLICKS = tmp / "clicks.log"

PROFILE = r'''<!doctype html><html><body>
<header><section><div><button>Ankara</button></div><div id="act"></div></section></header>
<div id="dlg"></div>
<script>
const kind = "__KIND__", act = document.getElementById('act'), dlg = document.getElementById('dlg');
const user = location.pathname.split('/')[1];
const log = (x) => fetch('/__log?' + x + '=' + user);
function btn(t) { act.innerHTML = '<button id="b">' + t + '</button>'; document.getElementById('b').onclick = onClick; }
function onClick() {
  const t = document.getElementById('b').textContent;
  if (t !== 'Requested') { log('clicked_' + t); return; }
  log('clicked_requested');
  setTimeout(() => {
    dlg.innerHTML = '<div role="dialog"><p>If you change your mind…</p><button id="u">Unfollow</button><button>Cancel</button></div>';
    document.getElementById('u').onclick = () => {
      dlg.innerHTML = '';
      if (kind === 'block') { dlg.innerHTML = '<div role="dialog"><h3>Try Again Later</h3><p>We restrict certain activity to protect our community.</p><button>OK</button></div>'; log('blocked'); return; }
      if (kind === 'silent') { log('silent'); return; }
      setTimeout(() => { btn('Follow'); log('withdrawn'); }, 300);
    };
  }, 200);
}
const initial = {fol: 'Following', nr: 'Follow'}[kind] || 'Requested';
if (kind === 'flash') { setTimeout(() => btn('Follow'), 150); setTimeout(() => btn('Requested'), 700); }
else setTimeout(() => btn(initial), 400);
</script></body></html>'''


async def handler(route):
    url = route.request.url
    if not re.match(r"https?://([a-z]+\.)?instagram\.com/", url):
        return await route.abort()
    u = urlparse(url)
    if u.path.startswith("/__log"):
        with CLICKS.open("a") as f:
            f.write(u.query + "\n")
        return await route.fulfill(status=204, body="")
    if u.path in ("", "/"):
        return await route.fulfill(content_type="text/html", body="<html><body><nav>home</nav></body></html>")
    if u.path.startswith("/challenge"):
        return await route.fulfill(content_type="text/html", body="<html><body><h2>Confirm it's you</h2></body></html>")
    user = u.path.strip("/").split("/")[0]
    kind = user.split("_")[0]
    if kind == "chk":
        return await route.fulfill(status=302, headers={"Location": "https://www.instagram.com/challenge/x/"}, body="")
    if kind == "gone":
        return await route.fulfill(content_type="text/html", body="<html><body><h2>Sorry, this page isn't available.</h2></body></html>")
    await route.fulfill(content_type="text/html", body=PROFILE.replace("__KIND__", kind))


orig = w.open_context


async def patched(pw, account, args):
    args.headless = True  # testte ekranda pencere açılmasın
    ctx = await orig(pw, account, args)
    await ctx.route("**/*", handler)
    return ctx

w.open_context = patched

sys.argv = ["withdraw_requests.py", *cfg["argv"]]
sys.exit(w.main())
