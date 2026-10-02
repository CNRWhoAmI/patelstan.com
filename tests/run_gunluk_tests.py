"""gunluk.py için uçtan uca testler — durumu hatırlayan sahte Instagram'a karşı (~3 dk).

    .venv/bin/python tests/run_gunluk_tests.py
"""
import json, os, pathlib, shutil, subprocess, sys
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
PY_BIN = str(HERE.parent / ".venv" / "bin" / "python")
ok = []
FAST = ["--min-delay", "0", "--max-delay", "0"]


def check(name, cond, detail=""):
    ok.append(bool(cond))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def ago(hours):
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def setup(name, sent, rest):
    """sent: {kullanıcı: kaç saat önce gönderildi}; liste = gönderilenler + rest."""
    tmp = HERE / "runs" / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    state = {"test.hesap": {"sent": {u: ago(h) for u, h in sent.items()}, "skipped": {}}}
    (tmp / "follow_state.json").write_text(json.dumps(state))
    (tmp / "hedefler.txt").write_text("# hesap: @test.hesap\n" + "".join(f"{u}\n" for u in [*sent, *rest]))
    return tmp


def run(tmp, argv, timeout=240):
    cfg = {"tmp": str(tmp), "argv": [str(tmp / "hedefler.txt"), *argv]}
    p = subprocess.run([PY_BIN, str(HERE / "mock_gunluk.py")], env={**os.environ, "MOCK_CFG": json.dumps(cfg)},
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def load(tmp, name, key=None):
    f = tmp / name
    data = json.loads(f.read_text())["test.hesap"] if f.exists() else {}
    return data.get(key, {}) if key else data


def clicks(tmp):
    f = tmp / "clicks.log"
    return f.read_text().split() if f.exists() else []


SENT = {"pend_old": 40, "acc_old": 40, "decl_old": 40, "pend_new": 2}
REST = ["new_1", "pub_1", "new_2", "new_3", "new_4", "new_5"]

print("\n[1] tam tur: eskileri sonuçlandır, sıradaki 4 kişiye istek at")
tmp = setup("g1", SENT, REST)
rc, out = run(tmp, ["--parti", "4", *FAST])
daily = load(tmp, "gunluk_state.json")
c = clicks(tmp)
check("çıkış kodu 0", rc == 0, out[-500:] if rc else rc)
check("40 saatlik üç istek sonuçlandı: bekleyen geri çekildi, kabul eden ve reddeden kaydedildi",
      {u: v["sonuc"] for u, v in daily.items()} == {"pend_old": "geri_cekildi", "acc_old": "kabul", "decl_old": "yok"},
      daily)
check("sadece bekleyen isteğe tıklandı (Following'e ve Follow'a geri çekme turunda dokunulmadı)",
      "clicked_Requested=pend_old" in c and "clicked_Following=acc_old" not in c and "clicked_Follow=decl_old" not in c, c)
check("2 saatlik isteğe dokunulmadı", not any(x.endswith("=pend_new") for x in c))
sent = load(tmp, "follow_state.json", "sent")
check("sıradaki 4 profil işlendi: 3 gizliye istek, açık hesap atlandı",
      {"new_1", "new_2", "new_3"} <= set(sent) and "pub_1" not in sent and "new_4" not in sent
      and load(tmp, "follow_state.json", "skipped").get("pub_1") == "public", sorted(sent))
check("geri çekilenlere bir daha istek atılmadı", c.count("clicked_Follow=pend_old") == 0)
check("istatistik: eski parti kabul 1 (%33), geri çekilen 1, reddeden 1; sırada 2 kişi",
      "kabul 1 (%33) · bekliyordu, geri çekildi 1 · reddetti ya da düştü 1" in out and "sırada 2 kişi" in out,
      out[out.find("PARTİLER"):][:700])

print("\n[2] ertesi gün gibi ikinci tur: sonuçlananlara bir daha bakılmaz, liste biter")
rc, out = run(tmp, ["--parti", "4", "--saat", "0", *FAST])
c2 = clicks(tmp)[len(c):]
check("dünkü sonuçlananlara bakılmadı", not any(x.split("=")[-1] in ("pend_old", "acc_old", "decl_old") for x in c2), c2)
check("dünkü gönderilenler (saat 0 ile süresi doldu) geri çekildi",
      all(load(tmp, "gunluk_state.json").get(u, {}).get("sonuc") == "geri_cekildi" for u in ("new_1", "new_2", "new_3", "pend_new")),
      load(tmp, "gunluk_state.json"))
check("kalan 2 kişiye istek atıldı, liste bitti",
      {"new_4", "new_5"} <= set(load(tmp, "follow_state.json", "sent")) and "sırada 0 kişi" in out, out[-400:])

print("\n[3] geri çekme adımında engel: yeni istek atılmaz")
tmp = setup("g3", {"pend_a": 40, "block_x": 39, "pend_b": 38}, ["new_1", "new_2"])
rc, out = run(tmp, ["--parti", "2", *FAST])
check("çıkış kodu 1, engel ve 'bugün yeni istek atılmıyor'",
      rc == 1 and "INSTAGRAM ENGELLEDİ" in out and "bugün yeni istek atılmıyor" in out, out[-500:])
check("hiç yeni istek atılmadı", not any(x.startswith("clicked_Follow=new") for x in clicks(tmp)), clicks(tmp))
check("engelden önceki sonuç kaydedildi, sonrası yarın denenecek",
      load(tmp, "gunluk_state.json").get("pend_a", {}).get("sonuc") == "geri_cekildi"
      and "pend_b" not in load(tmp, "gunluk_state.json"), load(tmp, "gunluk_state.json"))

print("\n[4] dry-run: hiçbir şey değişmez")
tmp = setup("g4", SENT, REST)
rc, out = run(tmp, ["--parti", "4", "--dry-run", *FAST])
check("tıklama yok, kayıt değişmedi",
      clicks(tmp) == [] and not (tmp / "gunluk_state.json").exists()
      and set(load(tmp, "follow_state.json", "sent")) == set(SENT), clicks(tmp))

print("\n[5] --istatistik: Instagram'a bağlanmaz")
tmp = setup("g5", SENT, REST)
rc, out = run(tmp, ["--istatistik"])
check("tarayıcı açılmadı, istatistik basıldı",
      rc == 0 and "PARTİLER" in out and "henüz bekliyor" in out and not (tmp / "profiles").exists(), out[-400:])

print(f"\nSONUÇ: {sum(ok)}/{len(ok)}")
sys.exit(0 if all(ok) else 1)
