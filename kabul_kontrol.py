#!/usr/bin/env python3
"""
kabul_kontrol.py — istek_at ile gönderilen isteklerden kaçı kabul edildi?

follow_state.json'da "istek atıldı" diye kayıtlı herkes için Instagram'ın profil
bilgisini okur (sitenin kendi kullandığı web_profile_info isteği); hiçbir şeye
tıklamaz. Her çalıştırma kabul_kontrol.json'a tarihli bir ölçüm olarak eklenir,
böylece "1. gün %X, 7. gün %Y" diye karşılaştırılabilir. Özet sadece toplu sayıdır.

Kullanım:
    ./kabul_kontrol.sh --account ornek.hesap
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from collections import Counter
from datetime import datetime

from playwright.async_api import Error as PWError, async_playwright

import istek_at as ia
import withdraw_requests as wr
from ig_export import ROOT, log
from withdraw_requests import ensure_logged_in, load_state, rel

RESULT_FILE = ROOT / "kabul_kontrol.json"
APP_ID = "936619743392459"  # instagram.com web istemcisinin kendi kimliği
FETCH_JS = """async ([u, appId]) => {
  const r = await fetch('/api/v1/users/web_profile_info/?username=' + encodeURIComponent(u),
                        {headers: {'x-ig-app-id': appId}});
  return {status: r.status, text: await r.text()};
}"""

LABELS = {
    "kabul": "kabul etti",
    "bekliyor": "hâlâ bekliyor",
    "yok": "reddetti ya da istek düştü",
    "acik": "açık hesap (doğrudan takip)",
    "hesap_yok": "hesap yok/kapalı",
}


def classify(status: int, text: str) -> str:
    """kabul / bekliyor / yok / acik / hesap_yok, ya da durduran 'engel' / 'bozuk'."""
    if status == 404:
        return "hesap_yok"
    if status in (401, 403, 429):
        return "engel"
    if status != 200:
        return "bozuk"
    try:
        user = json.loads(text)["data"]["user"]
    except (ValueError, KeyError, TypeError):
        return "bozuk"  # oturum düştüyse giriş sayfasının HTML'i gelir
    if user is None:
        return "hesap_yok"
    if any(user.get(k) is None for k in ("is_private", "followed_by_viewer", "requested_by_viewer")):
        return "bozuk"
    if user["followed_by_viewer"]:
        return "kabul" if user["is_private"] else "acik"
    return "bekliyor" if user["requested_by_viewer"] else "yok"


async def check_all(account: str, users: list[str], results: dict, args) -> str | None:
    """Sonuçları `results`'a yazar (Ctrl+C'de eldekiler kalsın); durma sebebini döner."""
    async with async_playwright() as pw:
        ctx = await wr.open_context(pw, account, args)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if not await ensure_logged_in(page, account, args.headless):
                return "oturum açılamadı"
            for n, username in enumerate(users, 1):
                try:
                    r = await page.evaluate(FETCH_JS, [username, APP_ID])
                    result = classify(r["status"], r["text"])
                except PWError as exc:
                    r, result = {"status": str(exc).splitlines()[0][:80]}, "bozuk"
                if result == "engel":
                    log(f"[{n}/{len(users)}] Instagram engelledi (HTTP {r['status']}) — durdu.")
                    return "engel"
                if result == "bozuk":
                    log(f"[{n}/{len(users)}] Instagram'ın yanıtı beklenen gibi değil (HTTP {r['status']}) — durdu.")
                    return "bozuk"
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
    lines = [
        f"ÖLÇÜM — @{account}: {len(results)}/{len(sent)} istek kontrol edildi"
        + (f", istekler ortalama ~{statistics.median(hours):.0f} saat önce gönderilmişti" if hours else ""),
        *(f"  {LABELS[k]:<32} {c[k]}" for k in LABELS),
    ]
    if private:
        lines.append(f"  Kabul oranı (gizli hesaplara giden istekler): {c['kabul']}/{private} = %{100 * c['kabul'] / private:.0f}")
    return "\n".join(lines)


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

    log(f"@{account}: {len(sent)} gönderilmiş istek kontrol edilecek. Hiçbir şeye tıklanmaz.")
    results: dict[str, str] = {}
    try:
        stop = asyncio.run(check_all(account, list(sent), results, args))
    except KeyboardInterrupt:
        stop = "kullanıcı durdurdu"

    now = datetime.now()
    if results:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8")) if RESULT_FILE.exists() else {}
        data.setdefault(account, []).append(
            {"zaman": now.isoformat(timespec="seconds"), "tamamlandi": stop is None, "sonuclar": results}
        )
        RESULT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n" + "=" * 64)
    print(summary(account, results, sent, now))
    if stop:
        print(f"  Yarım kaldı: {stop}. Kontrol edilenler kaydedildi; tekrar çalıştırınca baştan ölçer.")
    if results:
        print(f"  Kayıt: {rel(RESULT_FILE)}")
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())
