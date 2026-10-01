"""kabul_kontrol.main()'i sahte bir Instagram'a karşı çalıştırır.

Gerçek profile, kayıtlara ya da internete dokunmaz: instagram.com istekleri buradaki
sahte cevaplara yönlenir, geri kalan her şey engellenir; dosyalar MOCK_CFG["tmp"] altında.
web_profile_info cevabını kullanıcı adının öneki seçer:
  acc_ gizli, kabul etmiş · pend_ gizli, istek bekliyor · decl_ gizli, reddetmiş
  pub_ açık hesap, takip ediliyor · gone_ 404 · block_ 429 "Please wait a few minutes"
  login_ oturum düşmüş gibi HTML döner
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

USERS = {
    "acc": dict(is_private=True, followed_by_viewer=True, requested_by_viewer=False),
    "pend": dict(is_private=True, followed_by_viewer=False, requested_by_viewer=True),
    "decl": dict(is_private=True, followed_by_viewer=False, requested_by_viewer=False),
    "pub": dict(is_private=False, followed_by_viewer=True, requested_by_viewer=False),
}


async def handler(route):
    url = route.request.url
    if not re.match(r"https?://([a-z]+\.)?instagram\.com/", url):
        return await route.abort()
    u = urlparse(url)
    if u.path in ("", "/"):
        return await route.fulfill(content_type="text/html", body="<html><body><nav>home</nav></body></html>")
    if u.path == "/api/v1/users/web_profile_info/":
        if route.request.headers.get("x-ig-app-id") != kk.APP_ID:
            return await route.fulfill(status=400, body='{"message": "useragent mismatch"}')
        user = parse_qs(u.query)["username"][0]
        kind = user.split("_")[0]
        if kind == "gone":
            return await route.fulfill(status=404, body="{}")
        if kind == "block":
            return await route.fulfill(status=429, content_type="application/json",
                                       body='{"message": "Please wait a few minutes before you try again.", "status": "fail"}')
        if kind == "login":
            return await route.fulfill(status=200, content_type="text/html", body="<html><body>Log in</body></html>")
        body = {"data": {"user": {"username": user, **USERS[kind]}}, "status": "ok"}
        return await route.fulfill(content_type="application/json", body=json.dumps(body))
    await route.fulfill(status=404, body="")


orig = w.open_context


async def patched(pw, account, args):
    args.headless = True  # testte ekranda pencere açılmasın
    ctx = await orig(pw, account, args)
    await ctx.route("**/*", handler)
    return ctx

w.open_context = patched

sys.argv = ["kabul_kontrol.py", *cfg["argv"]]
sys.exit(kk.main())
