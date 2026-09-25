# Tekeat

Trendyol Go, Yemeksepeti ve Migros Yemek siparislerini tek ekrandan yoneten
Windows masaustu uygulamasi. Tek surec: FastAPI arka ucu bir is parcaciginda
calisir, pywebview penceresi ayni adresi acar. Arayuz ile API ayni origin'de.

Klasor adi `BarakaMahal_Servis`; urun adi **Tekeat**. Klasor bilerek
degistirilmedi.

## Komutlar

| Ne | Nasil |
|----|-------|
| Kurulum (bir kere) | `1-KURULUM.cmd` |
| Gelistirme | `2-GELISTIRME.cmd` |
| Exe uret | `3-EXE-OLUSTUR.cmd` (once `npm run build`, sonra PyInstaller) |
| Yazici testi | `4-YAZICI-TESTI.cmd` |
| Arayuz tip kontrolu | `cd web && npx tsc -b` |
| Arayuz derleme | `cd web && npx vite build` |

Exe derlemesi acik Tekeat penceresini kendisi kapatir (eskiden `WinError 5`
veriyordu); ayrica dosya yazilabilir olana kadar bekler.

## Dil ve isimlendirme

- Kod, degisken, fonksiyon ve yorumlar **Turkce**, ancak kaynak dosyalarda
  **Turkce karakter kullanilmiyor** (`sube`, `gecikme`, `yoklama`). Kullaniciya
  gorunen metinler de ayni sekilde ASCII.
- Yorumlar "ne yaptigini" degil **neden oyle yapildigini** anlatir. Bir sinir,
  bir platform kurali ya da yasanmis bir hata varsa yorumda o yazar.
- Dosya adlari Turkce: `ayarlar.py`, `mesafe.py`, `sistem.py`, `yazici.py`,
  `havuz.py`, `siparisIzleyici.ts`, `saat.ts`.

## Mimari kurallar

- **Olay dongusu bloklanmaz.** Bu uygulamada bloklayan bir cagri "program
  dondu" demek. Windows yazici API'si (`EnumPrinters`, `StartDocPrinter`) ve
  pywebview pencere cagrilari (`show/restore/on_top`) bloklayandir:
  - Yazici isleri `asyncio.to_thread` ile calisir (`app/routers/yazici.py`).
  - Pencere isleri `app/sistem.py` icindeki kuyruga birakilir, ayri bir
    parcacik isler. `/system/focus` aninda doner.
  - pywebview pencere metotlari (`show/restore/on_top`) **baska parcaciktan
    cagrilmaz**. `on_top` WinForms `TopMost`'u Invoke'suz yaziyor, pythonnet
    GIL'i birakmiyor -> arayuz parcacigiyla karsilikli kilitlenme (sahada
    "AppHang", peş pese siparislerde). Pencere one alma ctypes/Win32 ile
    (`ShowWindowAsync`, `SWP_ASYNCWINDOWPOS`).
  - pywebview `private_mode=True` varsayilaniyla aciliyor: localStorage her
    acilista siliniyor. Kalici olmasi gereken ayar sunucuda (`ayarlar.json`).
- **Adapter havuzu** (`app/havuz.py`): baglanti basina tek httpx istemcisi
  yasar. Her yoklamada yeni istemci acmak her seferinde TLS el sikismasi
  demekti. Kimlik hatasinda ve baglanti silininde `havuz.unut(cid)`.
- **Siparisleri arka uc yoklar** (`app/yoklayici.py`, 12 sn) ve SQLite
  `orders` tablosuna yazar; arayuz yalnizca yerel kaydi okur (`GET
  /orders`, baglanti hatasi `/orders/sync-status`). Uygulama kapanip
  acilinca durum, "Gordum" ve gecmis korunur; pencere arkadayken WebView2
  zamanlayicilari yavaslasa da siparis gecikmez. Aktif listeden dusen
  siparis tek paket sorgusuyla takip edilir; teslim/iptal 5 dk'da bir cekilir.
- **Iyimser arayuz yok.** Statu degisikliginden sonra sunucu platformu
  yoklayip GERCEK statuyu doner (`confirmed`). Teyit gelmezse siparis
  "supheli" isaretlenir. Switchlerin acik/kapali arasinda zipladigi hata
  buradan cikti.
- Yeni platform eklemek = `app/adapters/` altina bir dosya + `registry.py`
  kaydi. Servisin geri kalani degismez.
- **Hata metinleri** (`app/hatalar.py`): ekrana yalnizca ne oldu + ne
  yapilmali. Ham platform cevabi, istisna adi, JSON, URL ASLA ekrana gitmez
  (sahada Gecmis'te "400: {exception...}" gorundu); `kullanici_mesaji(exc)`
  ceviriyor ve teknigi log'a yaziyor, kayitli eski metinler `temizle()`.
  Router'larda `HTTPException(502, str(exc))` yazilmaz. Arayuzde son kontrol
  `temizMetin()` (`web/src/lib/api.ts`): 422 dizisi "[object Object]" olmaz.
  Birden fazla hesapta hesap ADI on ek (teknik "[baglanti N]" degil).
- **Arayuz metni:** klavye kisayollari ekranda GOSTERILMEZ (kullanici
  istemedi; davranis calisir). Formlarda uzun aciklama paragrafi yok;
  gerekiyorsa `title` ipucu, uyari yalnizca eylem gerektiginde.
- **Sade duzen** (2026-09, kullanici onayladi; Stok sayfasi ilk ornek):
  sayfa = sekmeler (+ gerekirse kucuk durum etiketi) -> tiklanabilir ozet
  kartlari -> arama/filtre + sagda "Diger" menusu + TEK ana dugme -> tablo.
  Satirda sik islem tek dugme, gerisi "..." (`components/IslemMenusu.tsx`,
  `position: fixed`; tablo kutusu tasmayi kesiyor). Ekranda ic jargon yok
  ("Net/Destek" yerine "Kesin/Aralikli miktar", yalnizca ilgili pencerede).
  Listede durum rozeti (nokta + kisa metin: Stokta/Azaldi/Tukendi/Eksi
  stok/Alis yok/Fiyati kontrol edin). Rakamlar kod yazi tipiyle DEGIL,
  normal yazi tipi + `tabular-nums` (kod yazi tipinde "TL/kg" bozuk
  duruyordu). `text-transform: uppercase` KULLANILMAZ: sayfa dili tr ve
  kaynak ASCII oldugu icin "Sinir" noktali buyuk I ile yaziliyordu; basliklar cumle
  duzeninde. Bir pencerede tek birincil dugme (Kaydet); Kapat ikincil.
- **Okunabilirlik:** her yazi rengi en koyu yuzeyde (surface-3) WCAG 4.5:1'i
  gecmeli; renk `theme.css` disinda tanimlanmaz. Beyaz yazili kirmizi dolgu
  icin `--danger-strong`. `color-scheme: dark` ve dugme/kutularda
  `color: inherit` global: eksikken dugmeler koyu zeminde SIYAH yaziyla
  cikiyordu (Ana Sayfa sayilari 1.2:1).

## Platform (Trendyol Go) kurallari -- kodda bunlara guveniliyor

- Basic auth. `User-Agent: "{supplierId} - SelfIntegration"` yoksa 403.
  `x-agentname` ve `x-executor-user` (gecerli e-posta) zorunlu.
- Limit: ayni endpoint'e 10 sn'de 50 istek. Kod 45'te tutuyor
  (`app/ratelimit.py`).
- Uc farkli servis oneki: `/integrator/order/meal/...`,
  `/integrator/store/meal/...`, `/integrator/product/meal/...`.
- Ayni subede es zamanli statu degisikliginde **409** doner. `menu.py` sube
  basina yazma kilidi tutar ve tekrar dener.
- Tutar alanlari **sayi olmayabilir**: `coupon.amount` gibi alanlar
  `{"seller": 0.18}` seklinde bir nesne gelebiliyor. `tgo.py` icindeki
  `_sayi/_metin/_tam_sayi/_liste/_harita/_koordinat` donusturucaleri HER
  alanda kullanilir. Tek bozuk siparis butun listeyi cokertmisti.
- Model 1 (STORE, kendi kuryemiz) ve Model 2 (GO, platform kuryesi) farki:
  Model 2'de musteri adresi ve koordinati "TGO Yemek" ile maskeleniyor,
  dolayisiyla mesafe ve paket ucreti hesaplanamiyor.
- Iptal sebepleri: 621, 622, 623, 627 her modelde; 624 ve 626 yalnizca
  Model 1. Iptalde `packageItemId` listesi gonderilir.
- Fiyat guncelleme asenkron: `batchRequestId` doner, sonuc ayrica sorulur.
  `restaurantId` bos birakilirsa fiyat TUM restoranlarda degisir.
- Yemek menu API'si (developers.tgoapps.com, 2026-09 itibariyla) yalnizca 6
  servis: menuyu cekme, kategori ac/kapa, urun ac/kapa, fiyat, toplu islem
  sonucu, hata kodlari. **Urun/kategori/opsiyon OLUSTURMA yok**; satici
  panelinden yapiliyor. Menu cevabinda `ingredients`, `modifierGroups`
  (min/max, modifierProducts) ve urun basina `ingredients`/
  `extraIngredients`/`modifierGroups` referanslari var.
- Kurye KONUMU veren servis yok. Model 1'de yalnizca `manual-shipped` ve
  `manual-delivered`; Model 2'de pakette `pickupEtaState`,
  `estimatedPickupTimeMin/Max`, `isCourierNearby`.
- Restoran teslimat suresi: `PUT /integrator/store/meal/.../stores/{id}/average-delivery-time`
  `{"min","max"}`, yalnizca Model 1, 5'in kati, min 15-85 / max 20-90.
- Docs sitesi SPA: WebFetch icerigi goremiyor, tarayiciyla okunuyor.
- Degerlendirme (service `review`): `GET .../stores/{id}/reviews/filter`
  (startDate/endDate createdDate'e gore, orderParentId, hasComment,
  restaurantAnswerStatus), `POST .../reviews/{reviewId}/answer {"text"}`.
  Musteri 7 gun icinde puan verir, degistiremez; cevap once
  WAITING_FOR_APPROVE, sonra APPROVED/REJECTED (+rejectedReason).
- **Uber Eats gecisi** (su an yalnizca Bilecik subeleri, yil sonuna kadar):
  yorum servisleri 403 doner ve istek atilmamali; `customer.id` degisir
  (ayni musteri yeni kimlikle gelir -- musteri kaydi bu subelerde ikiye
  bolunur); appName "UberEats"; coupon bos, indirim promotions'ta.
- Yemeksepeti acik Partner API'si (v2.0.2, developer.yemeksepeti.com) ve
  Delivery Hero POS API'si: degerlendirme servisi YOK. Migros Yemek:
  herkese acik dokuman yok.

## Mesafe ve paket ucreti

Platform mesafe vermiyor. Sube koordinati (`/stores` -> `location`) ile
musteri koordinatindan kus ucusu (haversine) hesaplanip **yol carpani**
ile carpiliyor (varsayilan 1.3). Ucret: taban_km'ye kadar taban_ucret,
sonrasinda her **baslayan** kilometre icin km_basi. Varsayilan 3 km / 100 TL
/ +25 TL. Kural Kuryeler sayfasindan degistirilebiliyor.

## Yazici

ESC/POS, `win32print` ile RAW is. Turkce karakter sorunu: yazicilarin cogu
PC857'yi uygulamiyor. Varsayilan **ASCII** (kod sayfasi 0); dogru sayfa
"Turkce karakter testi" fisi basilip kagitta duzgun gorunen satirdan
seciliyor. Ayar makineye ozel.

## Test

Testler depoda degil, ayri bir calisma klasorunde tutuluyor: sahte bir
FastAPI/http.server arka uc + Playwright. Bir davranis duzeltildiginde ona
karsilik gelen bir test yaziliyor (`test_donma.py` bloklamayi dogrudan
olcuyor: yazici 1.5 sn tutarken olay dongusu donmeye devam ediyor mu).

Arayuz degisikliginden sonra en az `npx tsc -b` calistirilir.

Arka ucu elle/arayuzle test ederken `TEKEAT_VERI_KLASORU` (ayar, log,
anahtar) ve `DB_PATH` gecici klasore verilir. Aksi halde gercek
`ayarlar.json` degisir ve "otomatik fis" acik oldugu icin test siparisleri
gercek yaziciya basilir (yasandi).

## Guvenlik

API anahtarlari (supplierId / API Key / Secret) **hicbir zaman** sohbete,
koda, log'a ya da commit'e girmez. Kullanici anahtarlari yalnizca uygulamanin
"Hesap bagla" ekranina kendi girer; orada dogrulanip cihazda sifreli saklanir.
Anahtar dosyasi `AppData` altinda, depoda degil.

## Siparis akisi (2026-09)

- Oto onay switch'i `ayarlar.json` > `siparis.oto_onay`; kabul yoklayicida,
  teyitten SONRA kayda yaziliyor (once "yeni" yazilirsa alarm bir an caliyor).
  Siparis basina en fazla 3 deneme, 30 sn arayla.
- Gecmis: `/history/orders`. Arama `ara_bicim` ile Turkce karakter
  katlanarak yapilir.
- **Musteri kaydi** (`customers`, `customer_addresses`): musteri platformun
  KALICI kimligiyle tekil (`anahtar = "id:<customer.id>"`). Trendyol soyadi
  tek harfe kisaltiyor ("Israfil C."); ad kimlik degil. Kimlik yoksa yedek
  anahtar ad + telefon/adres. `orders.customer_id` / `address_id` baglar;
  istatistikler (sayi, harcama) siparislerden HESAPLANIR, sayac tutulmaz.
  Ad/telefon/adres yazimi yalnizca daha YENI siparisten guncellenir (gecmis
  aktarimi eskiyi sonradan yazar). Eski siparisler `link_unassigned_customers`
  ile acilista ham cevaptan baglanir. Uclar: `/history/customers[/{id}]`,
  `PUT /history/customers/{id}/note` (restoranin notu, platforma gitmez).
- **Stok ve maliyet** (`app/stok.py`, kullanici kararlari 2026-09):
  **Maliyet her malzemede FIFO**: her alis kendi birim maliyetiyle ayri
  parti, tuketim en eskiden, parti bitince birim maliyet siradakine
  kendiliginden gecer (`/stock/items/{id}/batches` partileri gosterir).
  Malzeme basina `takip` yalnizca MIKTARI belirler: **net** (gramaji kesin:
  et g, peynir adet), **destek** (porsiyonu degisken: domates, tursu).
  (Destek icin once agirlikli ortalama maliyet yapilmisti; kullanici
  "ortalama"yla miktar tahminini kastediyordu, 2026-09 FIFO'ya alindi.)
  **Yeniden hesaplama** (`_yeniden`, `POST /stock/recalculate`): bir
  malzemenin hareketleri tarih sirasiyla bastan oynatilir; partiler,
  tuketim tutarlari ve teslim edilmis siparislerin malzeme maliyeti
  yeniden kurulur (kullanici karari: gecmis siparis de guncellenir).
  Geri alinan hareket + duzeltmesi atlanir; eksi stokta tuketilen miktar
  sonraki alisin GERCEK fiyatini alir; iade siparisin odedigi birim
  maliyetle. Her elle hareket ve geri almadan sonra o malzeme icin
  otomatik calisir. Recete satirinda
  `miktar` = DUSULEN miktar (destekte araligin UST siniri, her zaman o
  dusulur; fark kabul edilen fire), `miktar_min` yalnizca bilgi,
  `ekstra_miktar` = musteri o malzemeyi ekstra isterse BU urunde eklenen
  (yoksa ekstra urunun kendi recetesi; o da yoksa addan eslesme, "Ekstra
  Domates" -> Domates). Ekstralar platformda malzeme degil URUN kimligiyle
  gelir. Hicbiri platforma gitmez. Menuden aktarilan malzemeler "destek".
  Partiler (`stok_katmanlari`), dusum siparis TESLIM edilince (olcut
  son degisiklik zamani >= `maliyet.stok_baslangic`; gecmis aktarimi eski
  siparisleri dusurmez), teslimden sonra iptalde ayni maliyetle iade, eksi
  stoka izin (yetmeyen miktar son alis fiyatiyla "tahmini", sonraki alis
  once acigi kapatir). Birimler temel g/ml/adet; kg/lt yalnizca arayuzde
  (`web/src/lib/birim.ts`). Recete eslesmesi platform urun kimligi, yoksa
  katlanmis ad; siparisteki "cikar" (removed_ids -> stok_kalemleri.
  platform_malzeme) dusulmez, opsiyon/ekstra kendi recetesiyle eklenir.
  Dusum `yoklayici.kaydet` sonrasinda; `siparis_stok` bir kez -> cift
  dusum yok. Net kar = satis - malzeme - komisyon% - kurye - ambalaj.
- **Stok platforma YAZMAZ** (kullanici sarti): `/stock` uclarinda tek
  platform temasi `fetch_menu` (okuma). Yazan bir cagri eklenmez; test
  (`test_stok_duzeltme.py`) sahte adapterle her yazma metodunu yakalar.
  Sayfanin ustunde bu surekli yaziyor.
- **Duzeltme** (2026-09): elle hareket (alis/cikis/fire/sayim) SILINMEZ,
  "Geri al" ters bir `duzeltme` hareketi yazar ve ikisi baglanir
  (`geri_alan` / `geri_aldigi`); ardindan malzeme yeniden hesaplanir ve
  ikili hic olmamis gibi atlanir (geri alinan alisin partisi kalmaz; bos
  parti "son alis fiyati" sayilip yanlis fiyati gosteriyordu). Siparis/iade
  hareketi buradan geri alinmaz. **Yerinde duzeltme** (`PUT
  /stock/movements/{id}`): alisin miktari/tutari, cikis-fire'nin miktari
  ayni tarihte degistirilir, malzeme yeniden hesaplanir, eski degerler
  `hareket_duzeltmeleri`'nde. Yanlis fiyatli alistan sonra tuketim
  varsa geri almak yerine BU kullanilir (sahada: domates 1 kg 11.111 TL
  girildi, ustune fire; geri alinca fire alissiz kalip eksiye dusuyordu).
  Onleme: alis penceresi onceki alisa gore 5 kattan fazla farkli fiyatta
  uyarir ve ikinci onay ister; listede ve partilerde "fiyati kontrol edin"
  (`supheli_fiyat`).
- **Stok denetim kaydi** (`stok_denetim`, audit trail, 2026-09): duzeltme,
  geri alma, malzeme guncelleme ve silme icin kim (oturumdaki kullanici),
  ne zaman, hangi kayit (hareketin KENDI tarihi ayri), alan alan
  once/sonra, sebep ve etki (stok miktar/deger once-sonra, degisen siparis
  maliyeti). Sebep duzeltme, geri alma ve hareketli malzeme silmede
  ZORUNLU (sunucu 400). Tablo tetikleyiciyle DEGISTIRILEMEZ/SILINEMEZ;
  malzeme silinse de kayit kalir. Hareketlerde `kullanici` (giren) tutulur.
  Eski `hareket_duzeltmeleri` ve geri almalar acilista bir kez "sebep
  tutulmuyordu" notuyla tasinir. Arayuz: Hareketler > "Degisiklik kaydi";
  satirdaki "duzeltildi" etiketi o hareketin kaydini acar. Malzeme silme: siparis hareketi yoksa
  elle hareketleriyle birlikte silinir; recetedeyse hangi recete oldugu
  soylenir. Her recete kaydi onceki hali `recete_gecmisi`'ne yazar
  (degismediyse yazmaz); "Bu hale don" da once mevcut hali yazar, yani
  o da geri alinabilir.
- **Klavyeyle giris** (`web/src/lib/klavye.ts`): formlarda Tab/Enter
  sonraki alan, son alanda Enter kaydeder, Ctrl+S her yerden kaydet,
  pencerede odak tuzagi (Tab arkadaki sayfaya kacmaz), sayi alani odakta
  secilir. Satir ici "sil" dugmeleri `tabIndex=-1`. Yeni formlar da buna uyar.
- **Degerlendirmeler** (`app/degerlendirme.py`, ayri dongu 10 dk):
  `reviews` tablosu, siparise `order_number` ile bagli. Her turda son
  yorumdan 8 gun geri (cevap durumu degisiyor), ilk seferde 60 gun. 403
  (Uber Eats) sube gunde bir denenir. Cevap platforma gider ve musteriye
  gorunur: arayuzde onay penceresi zorunlu, onaylanmis/bekleyen cevaba
  ikinci cevap 409, reddedilene yeniden yazilabilir.
- Harita: anahtarsiz Google gomme (`output=embed`) + `maps/dir/?api=1`.
- **Gecmis aktarimi** (`app/gecmis_aktarim.py`): platformdaki eski
  siparisler BIR KEZ geriye dogru, 7 gunluk pencerelerle (TUM statuler)
  cekilir. Ilerleme `gecmis_aktarim` tablosunda; yarim kaldiysa kaldigi
  pencereden devam, bittiyse tekrar taramaz. 400 -> pencere yariya
  (1 gune kadar), 1 gunde de 400 -> "platform daha eskisini vermiyor".
  120 gun bos -> hesabin basi. "Yeni" statuye dokunmaz (canli yoklayicinin).
- **Acilis telafisi** (yoklayici, baglanti basina ilk tur): kayittaki en
  son `son_guncelleme`'den bugune TUM statuler (en fazla 60 gun). Uygulama
  kapaliyken olanlar boylece eksiksiz; her acilista sifirdan cekilmez.
- `packageStatuses` icinde "Cancelled" isteniyorsa "UnSupplied" da
  gonderilir (restoran kaynakli iptal ayri statu).
- Test yazarken gercek ayar/yaziciya dokunmamak sart: kullanicinin
  ayarinda oto onay + otomatik fis acik; ayar yonlendirilmeyen test
  sahte siparisleri gercek yaziciya basti (2 kez yasandi).

## Sirada ne var

- **Raporlama** (stok modulunun ustune): donem bazli satis, malzeme
  maliyeti, komisyon, kurye, ambalaj, net kar; urun bazli marj; stok
  degeri ve fire raporu.

- **Men Express (menexpresskurye.com, Menemen) kurye entegrasyonu** -- kritik.
  Hedef: kurye "aldim" deyince Trendyol'a `manual-shipped`, "teslim ettim"
  deyince `manual-delivered`. Herkese acik API dokumani yok; SepetTakip
  kurye firmalariyla API uzerinden bagli (SepetFast altyapisi) ama belge
  satis ekibinden. Kullanicinin Men Express restoran profili uzerinden
  incelenecek. Not: uygulama 127.0.0.1'de; disaridan webhook almak icin
  tunel ya da Men Express API'sini yoklamak gerekecek.
- Gercek PROD Trendyol Go baglantisiyla ilk canli deneme.
- Yemeksepeti adapteri (OAuth2), sonra Migros Yemek.
- Baraka Mahal renk paleti (`web/src/styles/theme.css` tek yer).
- Secilmeyi bekleyen ozellik gruplari: para/raporlama, mutfak operasyonu,
  musteri iliskisi, platform kontrolu.
