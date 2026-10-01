"""kabul_kontrol.py için uçtan uca testler — sahte Instagram'a karşı.

    .venv/bin/python tests/run_kabul_tests.py
"""
import json, os, pathlib, shutil, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
PY_BIN = str(HERE.parent / ".venv" / "bin" / "python")
ok = []


def check(name, cond, detail=""):
    ok.append(bool(cond))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def setup(name, sent):
    tmp = HERE / "runs" / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    state = {"test.hesap": {"sent": {u: "2026-09-30T22:00:00" for u in sent}, "skipped": {}}}
    (tmp / "follow_state.json").write_text(json.dumps(state))
    return tmp


def run(tmp, argv, timeout=120):
    env = {**os.environ, "MOCK_CFG": json.dumps({"tmp": str(tmp), "argv": [*argv, "--min-delay", "0", "--max-delay", "0"]})}
    p = subprocess.run([PY_BIN, str(HERE / "mock_kabul.py")], env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def measurements(tmp):
    f = tmp / "kabul_kontrol.json"
    return json.loads(f.read_text())["test.hesap"] if f.exists() else []


print("\n[1] karışık sonuçlar")
tmp = setup("k1", ["acc_1", "acc_2", "acc_3", "pend_1", "pend_2", "decl_1", "pub_1", "gone_1"])
rc, out = run(tmp, ["--account", "test.hesap"])
m = measurements(tmp)
check("çıkış kodu 0", rc == 0, out[-400:] if rc else rc)
check("kategoriler doğru", m and m[-1]["sonuclar"] == {
    "acc_1": "kabul", "acc_2": "kabul", "acc_3": "kabul", "pend_1": "bekliyor", "pend_2": "bekliyor",
    "decl_1": "yok", "pub_1": "acik", "gone_1": "hesap_yok"}, m[-1]["sonuclar"] if m else None)
check("kabul oranı sadece gizli hesaplar üzerinden: 3/6 = %50", "3/6 = %50" in out, out[out.find("ÖLÇÜM"):][:500])
check("ölçüm tamamlandı olarak kaydedildi", m and m[-1]["tamamlandi"] is True)
rc, out = run(tmp, [])
check("tek hesap varsa --account'suz çalışır, ikinci ölçüm eklenir", rc == 0 and len(measurements(tmp)) == 2)

print("\n[2] Instagram engeli (429): durur, eldekiler kaydedilir")
tmp = setup("k2", ["acc_1", "block_x", "acc_2"])
rc, out = run(tmp, ["--account", "test.hesap"])
m = measurements(tmp)
check("çıkış kodu 1, engel mesajı ve Instagram'ın kendi mesajı",
      rc == 1 and 'Instagram engelledi (HTTP 429): "Please wait a few minutes' in out, out[-300:])
check("engelden sonrası sorulmadı, yarım ölçüm kaydedildi",
      m and m[-1]["sonuclar"] == {"acc_1": "kabul"} and m[-1]["tamamlandi"] is False, m)

print("\n[3] oturum düşmüş (JSON yerine HTML): durur")
tmp = setup("k3", ["login_x", "acc_1"])
rc, out = run(tmp, ["--account", "test.hesap"])
check("çıkış kodu 1, 'beklenen gibi değil' mesajı, kayıt yok",
      rc == 1 and "beklenen gibi değil" in out and not measurements(tmp), out[-300:])

print("\n[4] gönderilmiş istek yok")
tmp = setup("k4", [])
rc, out = run(tmp, ["--account", "test.hesap"])
check("tarayıcı açmadan hata verdi", rc != 0 and "gönderilmiş istek kaydı yok" in out and not (tmp / "profiles").exists(), out[-200:])

print(f"\nSONUÇ: {sum(ok)}/{len(ok)}")
sys.exit(0 if all(ok) else 1)
