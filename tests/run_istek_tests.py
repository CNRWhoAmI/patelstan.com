"""istek_at.py için uçtan uca testler — sahte Instagram'a karşı (run_tests.py'nin karşılığı).

    .venv/bin/python tests/run_istek_tests.py          # hepsi (~4 dk)
    .venv/bin/python tests/run_istek_tests.py 2 5      # sadece 2. ve 5. senaryo
"""
import json, os, pathlib, re, shutil, signal, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
PY_BIN = str(HERE.parent / ".venv" / "bin" / "python")
ok = []


def check(name, cond, detail=""):
    ok.append(bool(cond))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def setup(name, lines, owner="test.hesap"):
    """Listenin ilk satırına bio_tara gibi '# hesap: @…' yazar (owner=None ise yazmaz)."""
    tmp = HERE / "runs" / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    header = f"# hesap: @{owner}\n" if owner else ""
    (tmp / "hedefler.txt").write_text(header + "".join(f"{x}\n" for x in lines))
    return tmp


def run(tmp, argv, timeout=180):
    env = {**os.environ, "MOCK_CFG": json.dumps({"tmp": str(tmp), "argv": [str(tmp / "hedefler.txt"), *argv]})}
    start = time.time()
    p = subprocess.Popen([PY_BIN, str(HERE / "mock_istek.py")], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.kill(p.pid, signal.SIGUSR1)  # mock_istek'teki faulthandler yığını basar
        time.sleep(1.5)
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        out += "\n<<TAKILDI — yukarıda yığın dökümü>>"
    return p.returncode, out, time.time() - start


def state(tmp):
    f = tmp / "follow_state.json"
    return json.loads(f.read_text())["test.hesap"] if f.exists() else {"sent": {}, "skipped": {}}


def clicks(tmp):
    f = tmp / "clicks.log"
    return f.read_text().split() if f.exists() else []


def clicked(tmp, button):
    return [x.split("=", 1)[1] for x in clicks(tmp) if x.startswith(f"clicked_{button}=")]


def ratelog(tmp):
    f = tmp / "logs" / "rate_limit.log"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


wanted = set(sys.argv[1:]) or {str(i) for i in range(1, 9)}
FAST = ["--min-delay", "0", "--max-delay", "0"]

if "1" in wanted:
    print("\n[1] karışık liste, --hedef-dk 0.5 (27 profil ~30 sn)")
    users = ([f"priv_{i}" for i in range(12)] + [f"pub_{i}" for i in range(4)]
             + ["req_a", "req_b", "fol_a", "fol_b", "gone_a", "flash_a", "flash_b"]
             + [f"priv_{i}" for i in range(12, 16)])
    tmp = setup("i1", users)
    rc, out, dur = run(tmp, ["--hedef-dk", "0.5"])
    st = state(tmp)
    sent = set(st["sent"])
    check("çıkış kodu 0", rc == 0, rc)
    check("16 gizli hesaba istek atıldı", len(sent) == 16 and all(f"priv_{i}" in sent for i in range(16)), len(sent))
    check("4 açık hesap atlandı, hiçbirine tıklanmadı",
          all(st["skipped"].get(f"pub_{i}") == "public" for i in range(4))
          and not any(u.startswith("pub_") for u in clicked(tmp, "Follow")), clicked(tmp, "Follow"))
    check("sadece 'Follow'a tıklandı, Requested/Following'e hiç",
          not clicked(tmp, "Requested") and not clicked(tmp, "Following"), clicks(tmp)[:5])
    check("tıklananlar = istek atılanlar (fazla/eksik tıklama yok)", set(clicked(tmp, "Follow")) == sent)
    check("flash (önce Follow görünen ama isteği bekleyen) tıklanmadı",
          st["skipped"].get("flash_a") == "requested" and "flash_a" not in clicked(tmp, "Follow"))
    check("req → requested, fol → following, gone → not_found",
          all(st["skipped"].get(k) == v for k, v in [("req_a", "requested"), ("fol_a", "following"), ("gone_a", "not_found")]))
    m = re.search(r"hedef ~0.5 dk: (\d+) sekme", out)
    check("sekme sayısı otomatik seçildi", m, m.group(0) if m else out[:300])
    check("süre hedefe yakın (25–70 sn)", 25 <= dur <= 70, f"{dur:.0f} sn")
    rl = ratelog(tmp)
    check("rate_limit.log: istek_at, 16 istek, tamamlandi",
          rl and rl[-1].get("islem") == "istek_at" and rl[-1].get("istek_atilan") == 16 and rl[-1]["sonuc"] == "tamamlandi",
          rl[-1] if rl else None)
    check("özet: toplam istek atılan 16/27", "Toplam istek atılan (tüm çalıştırmalar): 16/27" in out)
    rc, out, _ = run(tmp, ["--hedef-dk", "0.5"])
    check("tekrar çalıştırma: yapılacak bir şey yok, ikinci istek atılmadı",
          "yapılacak bir şey yok" in out and len(clicked(tmp, "Follow")) == 16)

if "2" in wanted:
    print("\n[2] engel: 8. sırada 'Try Again Later', 3 sekme")
    users = [f"priv_{i}" for i in range(7)] + ["block_x"] + [f"priv_{i}" for i in range(7, 20)]
    tmp = setup("i2", users)
    rc, out, _ = run(tmp, ["--parallel", "3", *FAST])
    st = state(tmp)
    check("çıkış kodu 1", rc == 1, rc)
    check("banner: INSTAGRAM ENGELLEDİ + sayfa mesajı", "INSTAGRAM ENGELLEDİ" in out and "Try Again Later" in out)
    check("banner'da istek sayısı", re.search(r"bu çalıştırmada : \d+ profil işlendi, \d+ istek atıldı", out))
    c = clicks(tmp)
    after = c[c.index("blocked=block_x") + 1:] if "blocked=block_x" in c else []
    late = [x for x in after if x.startswith("clicked_Follow")]
    check("engelden sonra yeni tıklama yok (≤1)", len(late) <= 1, late)
    check("engel öncesi 7 istek atıldı", all(f"priv_{i}" in st["sent"] for i in range(7)), sorted(st["sent"]))
    check("block_x sonra yeniden denenecek", st["skipped"].get("block_x") == "blocked")
    check("ekran görüntüsü alındı", list((tmp / "logs").glob("engel-*.png")))
    rl = ratelog(tmp)
    check("rate_limit.log: blocked", rl and rl[-1]["sonuc"] == "blocked")

if "3" in wanted:
    print("\n[3] sessiz engel: 'Requested' olup 0,8 sn sonra 'Follow'a geri dönüyor")
    tmp = setup("i3", [f"silent_{i}" for i in range(6)])
    rc, out, _ = run(tmp, FAST)
    check("3 başarısızdan sonra durdu", "sessiz engel" in out and out.count("✗ başarısız") == 3, out.count("✗ başarısız"))
    check("geri alınan istekler 'atıldı' sayılmadı", not state(tmp)["sent"], sorted(state(tmp)["sent"]))
    check("çıkış kodu 1", rc == 1, rc)

if "4" in wanted:
    print("\n[4] doğrulama (checkpoint) sayfası")
    tmp = setup("i4", ["priv_a", "priv_b", "chk_a", "priv_c", "priv_d"])
    rc, out, _ = run(tmp, FAST)
    check("DOĞRULAMA banner'ı", "DOĞRULAMA İSTİYOR" in out)
    check("checkpoint'ten sonrası işlenmedi", set(state(tmp)["sent"]) == {"priv_a", "priv_b"}, sorted(state(tmp)["sent"]))

if "5" in wanted:
    print("\n[5] dry-run, 2 sekme")
    tmp = setup("i5", ["priv_a", "pub_a", "req_a", "fol_a", "gone_a"])
    rc, out, _ = run(tmp, ["--dry-run", "--parallel", "2", *FAST])
    check("hiçbir tıklama yok", clicks(tmp) == [], clicks(tmp))
    check("state'e istek yazılmadı", not state(tmp)["sent"])
    check("özet: 1 gizli atılabilir, 1 açık atlandı, 1 zaten bekliyor",
          f"  {'gizli hesap, istek atılabilir':<34} 1" in out and f"  {'açık hesap, atlandı':<34} 1" in out
          and f"  {'istek zaten bekliyor':<34} 1" in out,
          out[out.find("ÖZET"):][:400])

if "6" in wanted:
    print("\n[6] liste başka hesaba ait / hesap satırı yok")
    tmp = setup("i6a", ["priv_a"], owner="baska.hesap")
    rc, out, _ = run(tmp, ["--account", "test.hesap", *FAST])
    check("başka hesabın listesiyle durdu", rc != 0 and "istek atacak hesap @baska.hesap yazıyor" in out, out[-300:])
    check("tarayıcı hiç açılmadı", not (tmp / "profiles").exists() and clicks(tmp) == [])
    tmp = setup("i6b", ["@priv_z", "priv_z", "bad name!", "", "# yorum"], owner=None)
    rc, out, _ = run(tmp, ["--account", "test.hesap", *FAST])
    check("hesap satırı yoksa --account ile çalıştı ve uyardı",
          rc == 0 and "'# hesap:' satırı yok" in out and set(state(tmp)["sent"]) == {"priv_z"}, out[-400:])
    check("@'lı/tekrarlı satır tek kişi, geçersiz satır sayıldı", "1 satır kullanıcı adı değil" in out
          and clicked(tmp, "Follow") == ["priv_z"])

if "7" in wanted:
    print("\n[7] --watch (tek sekme, işaretlemeli)")
    tmp = setup("i7", ["priv_a", "fol_a", "pub_a"])
    rc, out, dur = run(tmp, ["--watch", "--parallel", "4", *FAST])
    check("watch'ta sekme 1'e indi", "sekme sayısı 1'e indirildi" in out)
    check("gizliye istek atıldı, açık hesap atlandı, Following'e dokunulmadı",
          set(state(tmp)["sent"]) == {"priv_a"} and state(tmp)["skipped"].get("pub_a") == "public"
          and clicked(tmp, "Follow") == ["priv_a"] and not clicked(tmp, "Following"), clicks(tmp))
    check("işaretleme takılmadı (< 60 sn)", dur < 60, f"{dur:.0f} sn")

if "8" in wanted:
    print("\n[8] gizli mi açık mı anlaşılamazsa tıklamaz; üst üste 3 kez olursa durur")
    tmp = setup("i8", ["pubempty_a", "priv_b", "nosig_a", "nosig_b", "nosig_c", "priv_c"])
    rc, out, _ = run(tmp, FAST)
    st = state(tmp)
    check("gönderisi olmayan açık hesap ('No posts yet') atlandı", st["skipped"].get("pubempty_a") == "public")
    check("gizli hesaba istek atıldı", "priv_b" in st["sent"], sorted(st["sent"]))
    check("anlaşılamayanlara tıklanmadı, sonra yeniden denenecek",
          not any(u.startswith("nosig_") for u in clicked(tmp, "Follow"))
          and all(st["skipped"].get(f"nosig_{c}") == "failed" for c in "abc"), clicks(tmp))
    check("3 anlaşılamayandan sonra durdu, sonrası işlenmedi",
          out.count("gizli mi açık mı anlaşılamadı") == 3 and "ÜST ÜSTE BAŞARISIZ" in out and "priv_c" not in st["sent"],
          out.count("anlaşılamadı"))
    check("çıkış kodu 1", rc == 1, rc)

print(f"\nSONUÇ: {sum(ok)}/{len(ok)}")
sys.exit(0 if all(ok) else 1)
