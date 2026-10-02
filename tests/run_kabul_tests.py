"""kabul_kontrol.py için uçtan uca testler — sahte Instagram'a karşı (~2 dk).

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


def run(tmp, argv, timeout=180, accept=()):
    cfg = {"tmp": str(tmp), "argv": [*argv, "--min-delay", "0", "--max-delay", "0"], "accept": list(accept)}
    env = {**os.environ, "MOCK_CFG": json.dumps(cfg)}
    p = subprocess.run([PY_BIN, str(HERE / "mock_kabul.py")], env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def measurements(tmp):
    f = tmp / "kabul_kontrol.json"
    return json.loads(f.read_text())["test.hesap"] if f.exists() else []


print("\n[1] karışık sonuçlar")
tmp = setup("k1", ["acc_1", "acc_2", "pend_1", "decl_1", "pub_1", "unk_1", "ssr_1", "gone_1"])
rc, out = run(tmp, ["--account", "test.hesap"])
m = measurements(tmp)
check("çıkış kodu 0", rc == 0, out[-400:] if rc else rc)
check("kategoriler doğru", m and m[-1]["sonuclar"] == {
    "acc_1": "kabul", "acc_2": "kabul", "pend_1": "bekliyor", "decl_1": "yok", "pub_1": "acik",
    "unk_1": "belirsiz", "ssr_1": "kabul", "gone_1": "hesap_yok"}, m[-1]["sonuclar"] if m else out[-400:])
check("önerilen hesabın verisi karışmadı (pub_1 açık, acc_1 gizli okundu)",
      m and m[-1]["sonuclar"].get("pub_1") == "acik" and m[-1]["sonuclar"].get("acc_1") == "kabul")
check("HTML'e gömülü veriden de okudu (ssr_1)", m and m[-1]["sonuclar"].get("ssr_1") == "kabul")
check("kabul oranı sadece gizli hesaplar: 3/5 = %60, belirsiz orana dahil değil",
      "3/5 = %60" in out and "orana dahil değil" in out, out[out.find("ÖLÇÜM"):][:600])
check("ölçüm tamamlandı olarak kaydedildi", m and m[-1]["tamamlandi"] is True)
check("yüzdeler: kabul 3 (%60), reddeden 1 (%20)",
      f"  {'kabul etti':<32} 3  (%60)" in out and f"  {'reddetti ya da istek düştü':<32} 1  (%20)" in out,
      out[out.find("ÖLÇÜM"):][:600])
declined = tmp / "reddedenler-test_hesap.txt"
names = [l for l in declined.read_text().splitlines() if not l.startswith("#")] if declined.exists() else None
check("reddedenler listesi dosyada ve ekranda (sadece decl_1)",
      names == ["decl_1"] and "Reddeden ya da isteği düşen 1 kişi" in out and "\n  decl_1" in out, names)
rc, out = run(tmp, [], accept=["pend_1"])
check("tek hesap varsa --account'suz çalışır, ikinci ölçüm eklenir", rc == 0 and len(measurements(tmp)) == 2)
check("önceki ölçüme göre değişim: bekliyor → kabul 1",
      "hâlâ bekliyor → kabul etti: 1" in out, out[out.find("Önceki ölçüm"):][:300])
check("ölçüm geçmişi iki satır: %60 → %80",
      "Ölçüm geçmişi:" in out and "kabul 3/5 (%60)" in out and "kabul 4/5 (%80)" in out,
      out[out.find("Ölçüm geçmişi"):][:300])

print("\n[2] Instagram engeli: durur, sayfadaki mesajı yazar, eldekiler kaydedilir")
tmp = setup("k2", ["acc_1", "block_x", "acc_2"])
rc, out = run(tmp, ["--account", "test.hesap"])
m = measurements(tmp)
check("çıkış kodu 1, engel ve sayfadaki mesaj",
      rc == 1 and 'Instagram engelledi: "Please wait a few minutes' in out, out[-300:])
check("engelden sonrasına bakılmadı, yarım ölçüm kaydedildi",
      m and m[-1]["sonuclar"] == {"acc_1": "kabul"} and m[-1]["tamamlandi"] is False, m)
check("reddedenler dosyası 'yarım ölçüm' diye işaretli",
      "(yarım ölçüm)" in (tmp / "reddedenler-test_hesap.txt").read_text())

print("\n[3] oturum düşmüş (giriş sayfasına yönleniyor): durur")
tmp = setup("k3", ["login_x", "acc_1"])
rc, out = run(tmp, ["--account", "test.hesap"])
check("çıkış kodu 1, 'oturum düştü', kayıt yok",
      rc == 1 and "oturum düştü" in out and not measurements(tmp), out[-300:])

print("\n[4] üst üste 3 tanınmayan profil sayfası: durur")
tmp = setup("k4", ["acc_1", "blank_a", "blank_b", "blank_c", "acc_2"])
rc, out = run(tmp, ["--account", "test.hesap"])
m = measurements(tmp)
check("3 tanınmayandan sonra durdu, sonrasına bakılmadı",
      rc == 1 and out.count("profil sayfası tanınmadı, atlandı") == 3 and m and m[-1]["sonuclar"] == {"acc_1": "kabul"},
      out[-400:])

print("\n[5] gönderilmiş istek yok")
tmp = setup("k5", [])
rc, out = run(tmp, ["--account", "test.hesap"])
check("tarayıcı açmadan hata verdi", rc != 0 and "gönderilmiş istek kaydı yok" in out and not (tmp / "profiles").exists(), out[-200:])

print("\n[6] gönderildikten sonra geri çekilenler ölçüme katılmaz")
tmp = setup("k6", ["acc_1", "pend_1", "pend_2"])
(tmp / "state.json").write_text(json.dumps({"test.hesap": {
    "withdrawn": {"pend_1": "2026-09-01T10:00:00", "pend_2": "2026-10-02T10:00:00"}, "skipped": {}}}))
rc, out = run(tmp, ["--account", "test.hesap"])
m = measurements(tmp)
check("sadece pend_2 (gönderimden sonra çekilen) dışarıda; pend_1'in eski geri çekmesi sayılmadı",
      rc == 0 and m and set(m[-1]["sonuclar"]) == {"acc_1", "pend_1"}, m[-1]["sonuclar"] if m else out[-300:])
check("ekranda 'sonradan geri çekilen 1 istek' notu",
      "Sonradan geri çekilen 1 istek ölçüme katılmıyor" in out and "geri çekilen 1 istek ölçüme katılmadı" in out)

print("\n[7] sonraki partiyi ölçünce: önceki ölçümle ortak kişi yoksa 'değişen' satırı çıkmaz")
tmp = setup("k7", ["acc_1", "decl_1"])
run(tmp, ["--account", "test.hesap"])
state = {"test.hesap": {"sent": {"acc_2": "2026-10-02T22:00:00", "pend_2": "2026-10-02T22:00:00"}, "skipped": {}}}
(tmp / "follow_state.json").write_text(json.dumps(state))
rc, out = run(tmp, ["--account", "test.hesap"])
check("ikinci ölçüm: 'Önceki ölçümden' satırı yok, geçmiş iki satır",
      rc == 0 and "Önceki ölçümden" not in out and "Ölçüm geçmişi:" in out and out.count("kabul 1/") == 2,
      out[out.find("ÖLÇÜM"):][:700])

print(f"\nSONUÇ: {sum(ok)}/{len(ok)}")
sys.exit(0 if all(ok) else 1)
