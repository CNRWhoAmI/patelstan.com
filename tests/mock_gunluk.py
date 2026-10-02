"""gunluk.main()'i durumu hatırlayan sahte bir Instagram'a karşı çalıştırır.

Gerçek profile, kayıtlara ya da internete dokunmaz: instagram.com istekleri buradaki sahte
sayfalara yönlenir, geri kalan her şey engellenir; dosyalar MOCK_CFG["tmp"] altında.
Her kullanıcının takip durumu tmp/sunucu.json'da tutulur: istek atılınca "Requested",
geri çekilince "Follow" olur ve sonraki ziyarette (ya da sonraki çalıştırmada) öyle görünür.
Başlangıç durumunu kullanıcı adının öneki seçer:
  pend_ isteğimiz bekliyor · acc_ kabul etmiş (Following) · decl_ reddetmiş (Follow)
  new_ henüz istek atılmamış gizli hesap · pub_ açık hesap · block_ "Please wait a few minutes"
"""
import dataclasses, json, os, pathlib, re, sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import withdraw_requests as w  # noqa: E402
import istek_at as ia  # noqa: E402
import gunluk as g  # noqa: E402

cfg = json.loads(os.environ["MOCK_CFG"])
tmp = pathlib.Path(cfg["tmp"])
w.PROFILES_DIR = tmp / "profiles"
w.STATE_FILE = tmp / "state.json"
w.LOG_DIR = tmp / "logs"
w.RATE_LOG = tmp / "logs" / "rate_limit.log"
ia.ISTEK_AT = dataclasses.replace(ia.ISTEK_AT, state_file=tmp / "follow_state.json")
g.DAILY_FILE = tmp / "gunluk_state.json"
CLICKS = tmp / "clicks.log"
SERVER = tmp / "sunucu.json"
INITIAL = {"pend": "requested", "acc": "following"}


def server_state(user, new=None):
    data = json.loads(SERVER.read_text()) if SERVER.exists() else {}
    if new is not None:
        data[user] = new
        SERVER.write_text(json.dumps(data))
    return data.get(user, INITIAL.get(user.split("_")[0], "follow"))


PROFILE = r'''<!doctype html><html><body>
<header><section><div><button>Ankara</button></div><div id="act"></div></section></header>
<main id="main"></main>
<div id="dlg"></div>
<script>
const kind = "__KIND__", user = location.pathname.split('/')[1];
let state = "__STATE__";
const TEXT = {follow: 'Follow', requested: 'Requested', following: 'Following'};
const act = document.getElementById('act'), dlg = document.getElementById('dlg');
const log = (x) => fetch('/__log?' + x + '=' + user);
const save = (s) => fetch('/__set?u=' + user + '&s=' + s);
function show() { act.innerHTML = '<button id="b">' + TEXT[state] + '</button>'; document.getElementById('b').onclick = click; }
function click() {
  log('clicked_' + TEXT[state]);
  if (state === 'follow') { state = kind === 'pub' ? 'following' : 'requested'; save(state); setTimeout(show, 300); return; }
  if (state !== 'requested') return;
  setTimeout(() => {
    dlg.innerHTML = '<div role="dialog"><p>If you change your mind…</p><button id="u">Unfollow</button><button>Cancel</button></div>';
    document.getElementById('u').onclick = () => {
      dlg.innerHTML = ''; state = 'follow'; save(state); log('withdrawn'); setTimeout(show, 300);
    };
  }, 200);
}
setTimeout(show, 400);
setTimeout(() => {
  const main = document.getElementById('main');
  main.innerHTML = (kind === 'pub' || state === 'following') ? '<a href="/' + user + '/p/abc/">post</a>'
                 : '<h2>This profile is private</h2><p>Follow to see their photos and videos.</p>';
}, 700);
</script></body></html>'''


async def handler(route):
    url = route.request.url
    if not re.match(r"https?://([a-z]+\.)?instagram\.com/", url):
        return await route.abort()
    u = urlparse(url)
    if u.path in ("", "/"):
        return await route.fulfill(content_type="text/html", body="<html><body><nav>home</nav></body></html>")
    if u.path.startswith("/__log"):
        with CLICKS.open("a") as f:
            f.write(u.query + "\n")
        return await route.fulfill(status=204, body="")
    if u.path.startswith("/__set"):
        q = parse_qs(u.query)
        server_state(q["u"][0], q["s"][0])
        return await route.fulfill(status=204, body="")
    user = u.path.strip("/").split("/")[0]
    kind = user.split("_")[0]
    if kind == "block":
        return await route.fulfill(content_type="text/html",
                                   body="<html><body><p>Please wait a few minutes before you try again.</p></body></html>")
    body = PROFILE.replace("__KIND__", kind).replace("__STATE__", server_state(user))
    await route.fulfill(content_type="text/html", body=body)


orig = w.open_context


async def patched(pw, account, args):
    args.headless = True  # testte ekranda pencere açılmasın
    ctx = await orig(pw, account, args)
    await ctx.route("**/*", handler)
    return ctx

w.open_context = patched

sys.argv = ["gunluk.py", *cfg["argv"]]
sys.exit(g.main())
