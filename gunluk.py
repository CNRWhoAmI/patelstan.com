#!/usr/bin/env python3
"""
gunluk.py — günlük tur: süresi dolan istekleri geri çek, sıradaki partiye istek at, istatistiği yaz.

Her sabah tek komut (zamanlayıcıya bağlı değil, sen çalıştırırsın):
    ./gunluk.sh hedefler-ankara.txt                 # 20 saatten eski bekleyenleri geri çek, 90 yeni istek
    ./gunluk.sh hedefler-ankara.txt --dry-run       # tıklamadan neler yapılacağını göster
    ./gunluk.sh hedefler-ankara.txt --istatistik    # Instagram'a bağlanmadan parti parti sonuçlar

Adımlar:
  1) istek_at ile gönderilmiş, en az --saat (varsayılan 20) saat önce gönderilmiş ve henüz
     sonuçlanmamış isteklerin profiline bakar: kabul edilmişse (Following) dokunmaz, "kabul"
     yazar; hâlâ bekliyorsa geri çeker; istek düşmüşse "reddetti ya da düştü" yazar. Sonuçlar
     gunluk_state.json'a yazılır, o kişiye bir daha bakılmaz.
  2) 1. adımda engel gelmediyse listede sıradaki --parti (varsayılan 90) kişiye istek atar
     (istek_at ile aynı: sadece gizli hesaplar, kaldığı yerden).
  3) Gönderim gününe göre parti parti istatistiği yazar.
Engel gelirse, oturum düşerse ya da Ctrl+C'ye basılırsa o adımda durur, sonrakine geçmez.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import istek_at as ia
import withdraw_requests as wr
from ig_export import ROOT, log
from withdraw_requests import Run, execute, load_state, pick_account, plan_speed, rel

DAILY_FILE = ROOT / "gunluk_state.json"
# Geri çekme turunda kesinleşen sonuçlar; o kişiye bir daha bakılmaz
FINAL = {"withdrawn": "geri_cekildi", "following": "kabul", "no_request": "yok", "not_found": "hesap_yok"}


def load_daily() -> dict:
    return json.loads(DAILY_FILE.read_text(encoding="utf-8")) if DAILY_FILE.exists() else {}


def due_requests(account: str, hours: float, now: datetime) -> list[tuple[str, str]]:
    """En az `hours` saat önce gönderilmiş, henüz sonuçlanmamış istekler: [(kullanıcı, gönderilme)]."""
    sent = load_state(ia.ISTEK_AT.state_path).get(account, {}).get("sent", {})
    withdrawn = load_state(wr.GERI_CEK.state_path).get(account, {}).get("withdrawn", {})
    done = load_daily().get(account, {})
    limit = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    due = [(u, t) for u, t in sent.items() if t <= limit and withdrawn.get(u, "") <= t and u not in done]
    return sorted(due, key=lambda kv: kv[1])


def queue(account: str, names: list[str]) -> tuple[dict, dict, list[tuple[str, str]]]:
    """istek_at'in sırası: (kayıt, hesabın kaydı, [(kullanıcı, "")]) — gönderilmemiş ve kesin atlanmamış."""
    state = load_state(ia.ISTEK_AT.state_path)
    acc_state = state.setdefault(account, {"sent": {}, "skipped": {}})
    todo = [
        (u, "") for u in names
        if u not in acc_state["sent"] and acc_state["skipped"].get(u) not in ia.ISTEK_AT.final_skips
    ]
    return state, acc_state, todo


def withdraw_step(account: str, due: list[tuple[str, str]], args) -> str | None:
    """Süresi dolanları kontrol edip bekleyenleri geri çeker; durma sebebini döner."""
    state = load_state(wr.GERI_CEK.state_path)
    acc_state = state.setdefault(account, {"withdrawn": {}, "skipped": {}})
    parallel, interval = plan_speed(args, len(due))
    run = Run(args=args, account=account, total=len(due), state=state, acc_state=acc_state,
              islem=wr.GERI_CEK, parallel=parallel, interval=interval)
    execute(run, due, None)
    if not args.dry_run:
        daily = load_daily()
        acc = daily.setdefault(account, {})
        now = datetime.now().isoformat(timespec="seconds")
        for username, result in run.results.items():
            if result in FINAL:
                acc[username] = {"sonuc": FINAL[result], "zaman": now}
        DAILY_FILE.write_text(json.dumps(daily, indent=2, ensure_ascii=False), encoding="utf-8")
    return run.stop_reason


def send_step(account: str, names: list[str], args) -> str | None:
    """Listede sıradaki `--parti` kişiye istek atar; durma sebebini döner."""
    state, acc_state, todo = queue(account, names)
    todo = todo[: args.parti]
    if not todo:
        log("liste bitti, istek atılacak kimse kalmadı.")
        return None
    parallel, interval = plan_speed(args, len(todo))
    run = Run(args=args, account=account, total=len(todo), state=state, acc_state=acc_state,
              islem=ia.ISTEK_AT, parallel=parallel, interval=interval)
    execute(run, todo, len(names))
    return run.stop_reason


def statistics(account: str, names: list[str]) -> str:
    """Gönderim gününe göre partiler; yerel kayıtlardan, Instagram'a bağlanmadan."""
    sent = load_state(ia.ISTEK_AT.state_path).get(account, {}).get("sent", {})
    withdrawn = load_state(wr.GERI_CEK.state_path).get(account, {}).get("withdrawn", {})
    daily = load_daily().get(account, {})
    by_day: dict[str, Counter] = defaultdict(Counter)
    for u, t in sent.items():
        if u in daily:
            result = daily[u]["sonuc"]
        elif withdrawn.get(u, "") > t:  # günlük turdan önce elle geri çekilenler
            result = "geri_cekildi"
        else:
            result = "bekliyor"
        by_day[t[:10]][result] += 1

    def line(title: str, c: Counter) -> str:
        decided = c["kabul"] + c["geri_cekildi"] + c["yok"]
        rate = f"%{100 * c['kabul'] / decided:.0f}" if decided else "-"
        extra = (f" · henüz bekliyor {c['bekliyor']}" if c["bekliyor"] else "") + (
            f" · hesap yok {c['hesap_yok']}" if c["hesap_yok"] else "")
        return (f"  {title}: {sum(c.values())} istek · kabul {c['kabul']} ({rate}) · "
                f"bekliyordu, geri çekildi {c['geri_cekildi']} · reddetti ya da düştü {c['yok']}{extra}")

    total: Counter = sum(by_day.values(), Counter())
    lines = ["PARTİLER (gönderim gününe göre)"]
    lines += [line(day, by_day[day]) for day in sorted(by_day)]
    if len(by_day) > 1:
        lines.append(line("TOPLAM    ", total))
    lines.append("  Kabul oranı sonuçlanan istekler üzerinden (kabul + geri çekilen + reddeden).")
    lines.append(f"  Listede sırada {len(queue(account, names)[2])} kişi var.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Günlük tur: süresi dolanları geri çek, sıradakilere istek at")
    ap.add_argument("liste", help="istek atılacak liste (ör. hedefler-ankara.txt)")
    ap.add_argument("--account", help="hesap (varsayılan: listedeki '# hesap:' satırı)")
    ap.add_argument("--parti", type=int, default=90, help="bugün kaç kişiye istek atılacak (varsayılan 90)")
    ap.add_argument("--saat", type=float, default=20,
                    help="bu kadar saat önce gönderilip kabul edilmeyenler geri çekilir (varsayılan 20)")
    ap.add_argument("--istatistik", action="store_true", help="Instagram'a bağlanmadan sadece istatistik")
    ap.add_argument("--dry-run", action="store_true", help="tıklama, sadece durumları göster")
    ap.add_argument("--min-delay", type=float, default=4, help="işlemler arası min bekleme (sn)")
    ap.add_argument("--max-delay", type=float, default=6, help="işlemler arası max bekleme (sn)")
    ap.add_argument("--hedef-dk", type=float, help="her adımı yaklaşık bu kadar dakikaya yay (sekme sayısı otomatik)")
    ap.add_argument("--parallel", type=int, help="aynı anda kaç sekme (varsayılan 1; --hedef-dk ile otomatik)")
    ap.add_argument("--headless", action="store_true", help="tarayıcıyı gösterme")
    args = ap.parse_args()
    args.watch = False  # plan_speed / open_context bekliyor

    path = Path(args.liste).expanduser().resolve()
    if not path.is_file():
        sys.exit(f"HATA: {args.liste} bulunamadı.")
    owner, names, _ = ia.load_list(path)
    if not names:
        sys.exit(f"HATA: {rel(path)} içinde kullanıcı adı yok.")
    given = args.account.lstrip("@") if args.account else None
    if owner and given and owner.lower() != given.lower():
        sys.exit(f"HATA: {path.name} listesinin ilk satırında hesap @{owner} yazıyor ama --account @{given} verildi.")
    account = pick_account(given or owner)

    if args.istatistik:
        print(statistics(account, names))
        return 0

    due = due_requests(account, args.saat, datetime.now())
    log(f"@{account} · 1/2: {args.saat:g} saatten eski, sonuçlanmamış {len(due)} istek kontrol edilecek.")
    stop = withdraw_step(account, due, args) if due else None
    if stop:
        log("Geri çekme adımı yarıda kaldı; bugün yeni istek atılmıyor.")
    else:
        log(f"@{account} · 2/2: sıradaki {args.parti} kişiye istek atılacak.")
        stop = send_step(account, names, args)

    print("\n" + statistics(account, names))
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())
