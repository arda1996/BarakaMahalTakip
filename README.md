# Tekeat

Trendyol Go, Yemeksepeti ve Migros Yemek siparislerini tek ekrandan yoneten
Windows masaustu uygulamasi. Tarayici acmadan, panele girmeden; her platforma
dogrudan kendi API'si uzerinden baglanir.

## Hizli baslangic

Klasordeki dosyalara sirayla **cift tiklayin**. Baska bir sey gerekmez.

| Dosya | Ne yapar | Ne kadar surer |
|-------|----------|----------------|
| `1-KURULUM.cmd` | Python ve Node.js kurar, `.venv` ve `.env` hazirlar | 3-5 dk (bir kere) |
| `2-GELISTIRME.cmd` | Uygulamayi canli yenilemeli acar | ~1 dk |
| `3-EXE-OLUSTUR.cmd` | Tek dosyalik exe uretir | 2-4 dk |

Kurulum bitince `.env` dosyasini acip `TGO_EXECUTOR_USER` satirina kendi
e-posta adresinizi yazin.

Uretilen dosya: `cikti\Tekeat.exe` -- tek dosya, kurulum
gerektirmez, istediginiz yere kopyalayip cift tiklayabilirsiniz.

**Derleyici, Rust ya da Visual Studio araclari gerekmiyor.** Pencere motoru
olarak Windows'un kendi WebView2'si kullaniliyor (Windows 11'de hazir gelir).

## Hesap baglama

Uygulama acilinca "Hesap bagla" ekrani gelir. Trendyol Go icin gereken
`supplierId`, `API Key` ve `API Secret Key`, satici panelinde
**Hesap Bilgilerim -> Entegrasyon Bilgileri** sayfasindadir. Anahtarlar
`supplierId` basina tektir, tum subeler icin ayni.

Bilgiler once platforma gercek bir istekle dogrulanir; yalnizca dogrulanirsa
cihazda **sifreli** olarak kaydedilir. Ilk denemede "Test ortami (stage)"
secenegini acik birakin.

Sifreleme anahtari ilk calistirmada otomatik uretilir; `.env` sart degil.

Anahtarlari **bir kez** girersiniz. Kayit
`C:\Users\<kullanici>\AppData\Local\Tekeat\connections.db`
icinde durur; gelistirme modu ve exe ayni kaydi kullanir, exe'yi yeniden
uretmek de silmez. Anahtari degistirdiginizde baglantiyi silip yeniden
eklemeniz yeterli.

Arayuz olmadan denemek isterseniz:

```
.venv\Scripts\python.exe -m app.cli connect
.venv\Scripts\python.exe -m app.cli orders 1
```

## Baska bir bilgisayara tasima

Kaynak paketi (`Tekeat-kaynak.zip`) yalnizca kodu ve baslaticilari
icerir: `node_modules`, `.venv`, `cikti\` ve kayitli baglantilar
DISARIDA birakilir. Hepsi yeni makinede yeniden uretilir.

1. ZIP'i acin (ornegin `C:\Tekeat\`). Klasor adi onemli degil.
2. `1-KURULUM.cmd` -- Python ve Node.js kurar, `.venv` ve `.env`
   hazirlar. 3-5 dakika.
3. `.env` icindeki `TGO_EXECUTOR_USER` satirina kendi e-postanizi yazin.
4. `2-GELISTIRME.cmd` ile calistirip hesabi yeniden baglayin.

Kayitli Trendyol baglantisi TASINMAZ, tasinmasi da ise yaramaz:
kimlik bilgileri makineye ozel bir anahtarla sifrelenir ve o anahtar
`AppData` altinda kalir. Yeni makinede anahtarlari bir kere yeniden
girmek gerekir.

Yazici ayari (secili yazici, kagit genisligi, kod sayfasi) da makineye
baglidir; yeni makinede Yazici sayfasindan bir kere ayarlanir. Turkce
karakter icin once "Turkce karakter testi" basilip kagitta duzgun
gorunen satirin numarasi secilir.

Paket `.git` klasorunu icerir; `git log` ile butun gecmis yeni makinede
de durur.

## Sorun giderme

Uygulama beklenmedik bir hata verirse ayrinti su dosyada:

```
C:\Users\<kullanici>\AppData\Local\Tekeat\servis.log
```

Baglanti kurulamiyorsa ham istek/cevabi gorun:

```
.venv\Scripts\python.exe -m app.cli tani
```

## Mimari

Tek surec. Arka uc bir is parcaciginda calisir, pencere ayni adresi acar:

```
Tekeat.exe
  ├── FastAPI (127.0.0.1, bos bulunan ilk port)
  │     ├── /connections, /orders     API
  │     └── /                          derlenmis arayuz
  └── pywebview penceresi -> ayni adres
```

Arayuz ile API ayni origin'de oldugu icin CORS, port aktarimi ve ayri
sunucu derdi yok.

## Klasor duzeni

```
BarakaMahal_Servis\
  1-KURULUM.cmd          cift tiklanabilir baslaticilar
  2-GELISTIRME.cmd
  3-EXE-OLUSTUR.cmd
  betikler\              baslaticilarin cagirdigi PowerShell betikleri
  app\                   arka uc (FastAPI)
    routers\menu.py      menu: icerik, fiyat, satisa acma/kapama
    models.py            platformdan bagimsiz Order / Credentials
    crypto.py            kimlik bilgilerinin sifrelenmesi
    store.py             baglanti deposu (SQLite -> ileride PostgreSQL)
    ratelimit.py         endpoint basina kayan pencere limiti
    paths.py             paketlenmis / paketlenmemis yol farki
    desktop.py           masaustu giris noktasi (pencere + sunucu)
    adapters\
      base.py            butun adapterlarin uydugu sozlesme
      tgo.py             Trendyol Go
    routers\
      connections.py     hesap baglama
      orders.py          siparis listesi ve statu gecisleri
  web\                   arayuz (React + Vite)
    src\styles\theme.css PALET BURADA -- tek yer
  cikti\                 URETILEN EXE BURADA
  servis.spec            exe paketleme tarifi
```

Yeni platform eklemek `app\adapters\` altina bir dosya yazip
`registry.py`'ye kaydetmek demek. Servisin geri kalani degismez.

## Kimlik dogrulama sekilleri

| Platform | Yontem | Gereken bilgi |
|----------|--------|---------------|
| Trendyol Go | Basic auth | supplierId + API Key + API Secret Key |
| Yemeksepeti | OAuth2 | client_id + client_secret (adapter yaziliyor) |
| Migros Yemek | belirsiz | resmi API basvurusu bekleniyor |

## Trendyol Go servis ozeti

Base URL: `https://api.tgoapis.com` (test: `https://stageapi.tgoapis.com`)
Yol onegi: `/integrator/order/meal/suppliers/{supplierId}`

| Islem | Metot | Yol |
|-------|-------|-----|
| Siparisleri cek | GET | `/packages` |
| Tek paket | GET | `/packages/{packageId}` |
| Siparisi kabul et | PUT | `/packages/picked` |
| Hazirlik bitti | PUT | `/packages/invoiced` |
| Yola cikti (Model 1) | PUT | `/packages/{packageId}/manual-shipped` |
| Teslim edildi (Model 1) | PUT | `/packages/{packageId}/manual-delivered` |
| Iptal / kismi iptal | PUT | `/packages/unsupplied` |

### Menu ve restoran servisleri

Not: bu servisler farkli onekler kullaniyor -- `product` ve `store`.

| Islem | Metot | Yol |
|-------|-------|-----|
| Menuyu al | GET | `/product/meal/suppliers/{s}/stores/{st}/products` |
| Urun satisa ac/kapa | PUT | `/product/meal/.../products/{id}/status` |
| Kategori satisa ac/kapa | PUT | `/product/meal/.../sections/{id}/status` |
| Fiyat guncelle (kuyruk) | POST | `/product/meal/suppliers/{s}/products/price` |
| Toplu islem sonucu | GET | `/product/meal/suppliers/{s}/batch-requests/{id}` |
| Restoranlar | GET | `/store/meal/suppliers/{s}/stores` |
| Sube ac/kapa | PUT | `/store/meal/.../stores/{id}/status` |

Fiyat guncelleme **asenkron**: istek kuyruga alinir, `batchRequestId`
doner ve sonuc ayrica sorulur (4 saat goruntulenebilir). Tek istekte en
fazla 1000 urun; ayni govdeyle tekrar istek atmak hata verir, bu yuzden
yalnizca degisen fiyatlar gonderilir. `restaurantId` her zaman
gonderilir -- bos birakilirsa fiyat TUM restoranlarda degisir.

Urun/kategori durum degisiminde 409 "es zamanli degisiklik" demektir;
kod bir kez otomatik tekrar dener.

Zorunlu header'lar: `User-Agent: "{supplierId} - SelfIntegration"` (yoksa 403),
`x-agentname`, `x-executor-user`.
Limit: ayni endpoint'e 10 saniyede 50 istek; asilirsa 429. Kod 45'te tutuyor.
