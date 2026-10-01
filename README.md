# Instagram DYI toplu indirici

Meta Accounts Center'daki **"Bilgilerini indir"** (Download Your Information)
export dosyalarını otomatik indirir, açar ve indirdiklerini hatırlar.

Kullanıcı adı ve şifre **çalışırken sorulur**, hiçbir dosyaya yazılmaz.

## Kullanım

```bash
cd ~/Desktop/patelstan.com
./run.sh
```

Sorduklarını doldur:

```
Instagram hesap bilgileri (şifre ekranda görünmez):

  Kullanıcı adı: ornek.hesap
  ornek.hesap şifresi: ********

  2. hesap kullanıcı adı (boş bırak = devam et):     <-- ENTER'a bas
```

Şifre yazarken ekranda hiçbir şey görünmez, bu normal. İkinci hesap sorusunu
boş geçince indirmeye başlar.

### Bayraklar

```bash
./run.sh --list         # indirme, sadece kaç dosya var göster (kuru deneme)
./run.sh --dump-links   # links.txt çıkar, wget -i links.txt ile indirmek için
./run.sh --headless     # arka planda (ilk login yapıldıysa)
```

## İlk çalıştırma

**İlk seferi görünür modda çalıştır** (`--headless` verme) ve `--list` ile
başla. Hesapta 2FA veya "şüpheli giriş" doğrulaması varsa script 3 dakika
bekler, sen tarayıcıda onaylarsın.

Çerezler `profiles/<hesap>/` altında kalıcı tutulduğu için sonraki
çalıştırmalarda login istemez — o zaman `--headless` kullanabilirsin.

## Nasıl çalışıyor

1. Kalıcı Chrome profiliyle Accounts Center DYI sayfasını açar
2. Oturum kapalıysa giriş yapar (2FA'da bekler)
3. **Current activity** ve **Past activity** sekmelerini tek tek açıp
   kaydırır — Meta'nın yeni arayüzünde istekler bu sekmelerin içinde durur,
   sayfada doğrudan görünmez
4. Her sekmede kaç istek var, kaçının süresi dolmuş, kaç indirilebilir dosya
   var — hepsini tek tek raporlar
5. **Önce** DOM'daki imzalı indirme linklerini çerezli HTTP isteğiyle çeker —
   React butonlarına tıklamaya gerek kalmadığı için en sağlam yol
6. Kalanlar için "Download" butonlarına tıklar, çıkan **şifre onayı**
   modalını doldurur, dosyayı yakalar
7. `.zip` dosyalarını açar
8. `state.json`'a yazar — aynı dosyayı ikinci kez indirmez

## Çıktılar

| Yol | İçerik |
|---|---|
| `downloads/<hesap>/` | inen `.zip` + açılmış klasörler |
| `profiles/<hesap>/` | kalıcı Chrome profili (çerezler) |
| `state.json` | indirilenler, tekrar indirmemek için |
| `logs/*.png` | hiçbir şey inmediyse hata anının ekran görüntüsü |
| `links.txt` | `--dump-links` ile çıkan ham linkler |

## Bilinmesi gerekenler

- **Gerçek terminal lazım.** Şifre sorusu için tty gerekiyor; VSCode'da
  `Terminal > New Terminal` aç. Boru/pipe ile çalıştırırsan uyarı verip çıkar.
- **Önce Instagram'da export isteği atmış olman gerekiyor.** Bu araç hazır
  dosyaları indirir, istek oluşturmaz. Accounts Center > Export your
  information > **Create export**.
- **Linkler ~4 gün sonra ölüyor.** Export hazır bildirimi geldikten sonra
  çalıştır; süresi dolmuşsa zip bozuk iner (script uyarır) ve Instagram'dan
  yeniden istek atman gerekir.
- **Meta arayüzü sık değişiyor.** Buton seçicileri bir gün tutmazsa
  `--dump-links` ile linkleri çıkarıp `wget -i links.txt` ile indirebilirsin;
  bu yol DOM'a çok daha az bağımlı.
- Bundan sonrası için: tek tek küçük export yerine **"Tüm bilgiler"** olarak
  tek istek atmak bu işi baştan gereksiz kılıyor.

## Opsiyonel: dosyadan hesap okuma

Her seferinde elle girmek istemezsen `accounts.example.json`'ı `accounts.json`
olarak kopyalayıp doldurabilirsin — dosya varsa script sormadan onu kullanır
(`--ask` ile yine sormaya zorlarsın). Bu durumda `chmod 600 accounts.json` yap;
dosya `.gitignore`'da.

## Bekleyen takip isteklerini geri çekme

Export indikten sonra zip'teki `pending_follow_requests.html` listesindeki
herkese gönderdiğin takip isteğini iptal eder.

Dosya `downloads/` altında herhangi bir yerde olabilir (zip'ten çıkarıp doğrudan
`downloads/` içine koymak yeterli). Türkçe ve İngilizce export'ların ikisi de
okunur. Script dosyanın hangi hesaba ait olduğunu başlığından okur ve o hesabı
seçer; başka hesabın oturumuyla çalıştırmaya kalkarsan durur. JSON export'larda
sahip yazmadığı için dosyayı `downloads/<hesap>/` altına koy ya da
`--file` ile ver.

```bash
./geri_cek.sh --dry-run     # tıklamaz, her profilin durumunu gösterir
./geri_cek.sh               # hepsini geri çeker
./geri_cek.sh --limit 10    # sadece 10 tane
./geri_cek.sh --watch       # izleme modu: yavaş, butonları renkli işaretler
./geri_cek.sh --hedef-dk 60 # listeyi ~60 dakikaya yay (sekme sayısı otomatik)
./geri_cek.sh --parallel 4  # 4 sekme aynı anda
```

- Butonu hâlâ **Requested** olanlara dokunur. **Following** (kabul etmiş)
  olanları atlar, yani kimseyi yanlışlıkla takipten çıkarmaz.
- Her işlem arası 4–6 sn (ortalama 5) bekler (`--min-delay`, `--max-delay` ile ayarlanır).
- `--hedef-dk` verilince sekmeler ortak bir sıradan zaman alır: toplam hız sabit
  kalır ve liste o sürede biter. `--watch` tek sekmede çalışır.
- Instagram engellerse ("Try Again Later", "birkaç dakika bekle", doğrulama
  sayfası, ya da tıklamalar üst üste sessizce geri alınırsa) **bütün sekmeler
  durur**. Diğer sekmeler de engel görüldükten sonra yeni tıklama yapmaz.
  Terminale saat, kaçıncı profilde ve başlangıçtan kaç dakika sonra olduğu, o
  ana kadarki hız ve sayfadaki mesaj yazılır; ekran görüntüsü `logs/` altına düşer.
- Her çalıştırmanın sonucu (sekme sayısı, hız, işlenen, sonuç) `logs/rate_limit.log`
  dosyasına bir satır olarak eklenir. Farklı hızlarda ne zaman engel geldiğini
  buradan karşılaştırabilirsin.
- İlerleme `withdraw_state.json`'a yazılır; tekrar çalıştırınca kaldığı yerden
  devam eder. Ctrl+C: açık sekmeler eldeki profili bitirip durur (ikinci Ctrl+C
  hemen çıkar).
- Liste export anının görüntüsü. Sonradan attığın istekler için yeni export lazım.

## Bio'dan şehre göre ayırma

Export'taki listede kimin bio'sunda hangi şehir yazdığını bulur. İki adımda çalışır:

```bash
./bio_tara.sh --limit 5            # önce 5 profille dene: bio'lar ekrana düşüyor mu?
./bio_tara.sh                      # bütün listeyi tara (kaldığı yerden devam eder)
./bio_tara.sh --hedef-dk 120       # listeyi ~120 dakikaya yay (sekme sayısı otomatik)
./bio_tara.sh --parallel 2         # 2 sekmeyle
./bio_tara.sh --sehir ankara       # kayıtlı bio'larda ara → hedefler-ankara.txt
./bio_tara.sh --sehir bingöl kars  # birden fazla kelime → hedefler-bingol-kars.txt
```

- Tarama hiçbir şeye tıklamaz, kimseye istek atmaz. Profili açar ve başlıktaki
  yazıyı (ad, kategori, bio, link) `bio_state.json`'a kaydeder. Sayılar ve
  "Followed by …" satırı alınmaz. Engel gelirse geri çekmedeki gibi durur.
- `--hedef-dk` geri çekmedeki gibi çalışır: sekmeler ortak sıradan zaman alır,
  sekme sayısı hedefe göre seçilir. Her çalıştırma `logs/rate_limit.log`'a
  `"islem": "bio_tara"` alanıyla yazılır, geri çekme kayıtlarından ayrılır.
- Ayırma tarayıcı açmaz, kayıtlı bio'larda arar. Farklı şehirlerle istediğin
  kadar tekrar çalıştırabilirsin.
- Büyük/küçük harf, Türkçe karakter ve süslü font (𝐀𝐧𝐤𝐚𝐫𝐚) fark etmez:
  `bingol` = `BİNGÖL`. Ekler tutar (Ankaralı, Bingöllü, Karslı, Kars'ta) ama
  kelime parçası tutmaz: `kars` Karşıyaka'yı ya da "karşı"yı yakalamaz.
- Kullanıcı adına da bakar (`kars_36`). Plaka kodu da aranabilir (`--sehir 06`);
  `ankara06` tutar, `2006` tutmaz, ama tarih/telefonla karışabilir.
- `--sehir ankara` Ankara'daki devlet ve vakıf ünilerini de kısaltmalarıyla
  sayar (ODTÜ/METU, Bilkent, Hacettepe, Gazi Üni, TOBB ETÜ, TEDÜ, AYBÜ…). Liste
  `sehirler.txt`'de: `[şehir]` başlığının altına kelime ekleyerek başka şehirlere
  de genişletebilirsin (ör. `[kars]` altına `kafkas uni*`). Hangi kelimeyle
  eşleştiği ekranda `[odtu]` gibi görünür.
- İlçeleri kendiliğinden bilmez; Keçiören gibi ilçeleri de istiyorsan
  `sehirler.txt`'ye ekle.
- Dosyaya sadece butonu "Follow" olanlar yazılır. İsteği hâlâ bekleyenler ve
  zaten takip ettiklerin girmez. Eşleşmeler ekrana da basılır, yanlış olan
  varsa dosyadan sil.
- `bio_state.json` ve `hedefler-*.txt` başka insanların bilgisi: sadece bu
  bilgisayarda durur, git'e ve sunucuya gitmez. İşin bitince silebilirsin.

## Listedeki herkese takip isteği atma

`hedefler-*.txt` gibi, satır başına bir kullanıcı adı olan listedeki herkese
takip isteği atar. Geri çekmeyle aynı çalışır: aynı bayraklar, aynı engel
davranışı.

```bash
./istek_at.sh hedefler-ankara.txt --dry-run            # tıklamaz, durumları gösterir
./istek_at.sh hedefler-ankara.txt --limit 5 --watch    # 5 kişiyle izleyerek dene
./istek_at.sh hedefler-ankara.txt --hedef-dk 60        # listeyi ~60 dakikaya yay
./istek_at.sh hedefler-ankara.txt                      # tek sekme, işlemler arası 4–6 sn
```

- Sadece **gizli hesaplara** istek atar, açık hesapları atlar (`açık hesap,
  atlandı`). Sayfada "This profile is private" yazısını görmediği hesaba
  tıklamaz; gizli mi açık mı anlaşılamazsa da tıklamaz, üst üste 3 kez olursa
  durur.
- Sadece butonu **Follow** olana tıklar. İsteği zaten bekleyenlere
  (**Requested**) ve zaten takip ettiklerine (**Following**) dokunmaz.
- Tıkladıktan sonra butonun 2 sn boyunca Requested'da kaldığını
  izler. Instagram isteği sessizce geri alırsa "başarısız" sayılır; üst üste
  olursa bütün sekmeler durur.
- İsteği hangi hesabın atacağını listenin ilk satırı belirler:
  `# hesap: @ornek.hesap`. `bio_tara` bu satıra listeyi çıkardığı hesabı yazar;
  başka hesaptan atmak için satırı değiştir (hesabın `profiles/` altında girişi
  yapılmış olmalı). `--account` bu satırla çelişirse yanlış hesaptan istek
  gitmesin diye durur. Elle yazılmış listede bu satır yoksa `--account` ver.
- İlerleme `follow_state.json`'a yazılır; aynı komut kaldığı yerden devam eder.
  Engel gelirse terminale ne zaman geldiği yazılır, `logs/rate_limit.log`'a
  `"islem": "istek_at"` satırı düşer.
- Takip isteği engelleri geri çekmeninkilerden uzun sürebilir; engelden sonra
  tekrar başlatmadan önce daha uzun bekle.
- Sahte Instagram'a karşı testler: `.venv/bin/python tests/run_istek_tests.py`
