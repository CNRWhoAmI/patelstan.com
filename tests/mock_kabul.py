"""kabul_kontrol.main()'i sahte bir Instagram'a karşı çalıştırır.

Gerçek profile, kayıtlara ya da internete dokunmaz: instagram.com istekleri buradaki
sahte sayfalara yönlenir, geri kalan her şey engellenir; dosyalar MOCK_CFG["tmp"] altında.
Profil sayfası, gerçek site gibi profil bilgisini kendisi yükler (/graphql/query); yanıtta
"Suggested for you" gibi başka bir hesabın nesnesi de var, karışmamalı.
Kullanıcı adının öneki sayfanın davranışını seçer:
  acc_ gizli, takipte (kabul etmiş) · pend_ Requested · decl_ Follow (reddetmiş)
  pub_ açık hesap, takipte · unk_ takipte ama veride hesap yok · ssr_ veri HTML'e gömülü
  gone_ hesap yok · block_ "Please wait a few minutes" · login_ giriş sayfasına yönlenir
  blank_ buton yok (sayfa tanınmaz)
MOCK_CFG["accept"]: bu çalıştırmada kabul etmiş sayılacak kullanıcılar (ölçümler arası değişim).
"""
import dataclasses, json, os, pathlib, re, sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import withdraw_requests as w  # noqa: E402
import istek_at as ia  # noqa: E402
import kabul_kontrol as kk  # noqa: E402

cfg = json.loads(os.environ["MOCK_CFG"])
tmp = pathlib.Path(cfg["tmp"])
w.PROFILES_DIR = tmp / "profiles"
w.STATE_FILE = tmp / "state.json"
w.LOG_DIR = tmp / "logs"
w.RATE_LOG = tmp / "logs" / "rate_limit.log"
ia.ISTEK_AT = dataclasses.replace(ia.ISTEK_AT, state_file=tmp / "follow_state.json")
kk.RESULT_FILE = tmp / "kabul_kontrol.json"
ACCEPT = set(cfg.get("accept", []))


def kind_of(user):
    return "acc" if user in ACCEPT else user.split("_")[0]

PROFILE = r'''<!doctype html><html><head>__SSR__</head><body>
<header><section><div><button>Ankara</button></div><div id="act"></div></section></header>
<main></main>
<script>
const kind = "__KIND__", user = location.pathname.split('/')[1];
const state = {pend: 'Requested', decl: 'Follow'}[kind] || 'Following';
if (kind !== 'ssr') fetch('/graphql/query?u=' + encodeURIComponent(user), {method: 'POST'});
if (kind !== 'blank') setTimeout(() => { document.getElementById('act').innerHTML = '<button>' + state + '</button>'; }, 400);
</script></body></html>'''


def graphql(user, kind):
    """Satır satır JSON: önce başka bir hesabın nesnesi (ters gizlilikle), sonra hedef hesap."""
    private = kind != "pub"
    lines = [{"data": {"suggested": [{"username": "onerilen_hesap", "is_private": not private}]}}]
    if kind != "unk":
        lines.append({"data": {"user": {"username": user, "is_private": private, "friendship_status": {"following": True}}}})
    return "\n".join(json.dumps(x) for x in lines)


def ssr(user):
    blocks = [{"require": [["x", {"user": {"username": "onerilen_hesap", "is_private": False}}]]},
              {"require": [["x", {"user": {"username": user, "is_private": True}}]]}]
    return "".join(f'<script type="application/json" data-sjs>{json.dumps(b)}</script>' for b in blocks)


async def handler(route):
    url = route.request.url
    if not re.match(r"https?://([a-z]+\.)?instagram\.com/", url):
        return await route.abort()
    u = urlparse(url)
    if u.path in ("", "/"):
        return await route.fulfill(content_type="text/html", body="<html><body><nav>home</nav></body></html>")
    if u.path == "/graphql/query":
        user = parse_qs(u.query)["u"][0]
        return await route.fulfill(content_type="application/json", body=graphql(user, kind_of(user)))
    if u.path.startswith("/accounts/login"):
        return await route.fulfill(content_type="text/html", body='<html><body><input name="username"></body></html>')
    user = u.path.strip("/").split("/")[0]
    kind = kind_of(user)
    if kind == "gone":
        return await route.fulfill(content_type="text/html", body="<html><body><h2>Sorry, this page isn't available.</h2></body></html>")
    if kind == "block":
        return await route.fulfill(content_type="text/html",
                                   body="<html><body><p>Please wait a few minutes before you try again.</p></body></html>")
    if kind == "login":
        return await route.fulfill(status=302, headers={"Location": "https://www.instagram.com/accounts/login/"}, body="")
    body = PROFILE.replace("__KIND__", kind).replace("__SSR__", ssr(user) if kind == "ssr" else "")
    await route.fulfill(content_type="text/html", body=body)


orig = w.open_context


async def patched(pw, account, args):
    args.headless = True  # testte ekranda pencere açılmasın
    ctx = await orig(pw, account, args)
    await ctx.route("**/*", handler)
    return ctx

w.open_context = patched

sys.argv = ["kabul_kontrol.py", *cfg["argv"]]
sys.exit(kk.main())
