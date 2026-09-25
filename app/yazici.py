"""Termal fis yazicisi (ESC/POS) destegi.

Neden HTML degil: satici panelinin masaustu uygulamasi fisi bir tarayici
penceresine yukleyip yazdiriyor. Bizde tarayici motoru yok ve olsa bile
gereksiz -- termal yazicilar metni dogrudan aliyor. RAW ESC/POS hem
aninda basiyor hem de yazicinin kendi yazi tipini kullandigi icin
kagitta net cikiyor.

Windows'ta win32print ile "RAW" is turu gonderiyoruz: surucu araya
girmiyor, baytlar dogrudan yaziciya gidiyor.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timezone

from .models import Order

log = logging.getLogger("tekeat.yazici")

# ---------------------------------------------------------------
# ESC/POS komutlari
# ---------------------------------------------------------------

ESC = b"\x1b"
GS = b"\x1d"

SIFIRLA = ESC + b"@"
KALIN_AC = ESC + b"E\x01"
KALIN_KAPA = ESC + b"E\x00"
SOL = ESC + b"a\x00"
ORTA = ESC + b"a\x01"
SAG = ESC + b"a\x02"
NORMAL_BOY = GS + b"!\x00"
CIFT_BOY = GS + b"!\x01"      # cift yukseklik
CIFT_HEPSI = GS + b"!\x11"    # cift en + cift yukseklik
# Kesme: once kagidi biraz ilerlet, sonra kismi kes. Dogrudan kesersek
# son satirlar bicagin altinda kaliyor.
KES = b"\n" * 4 + GS + b"V\x42\x00"

# ESC t n -> karakter tablosu secer.
#
# ONEMLI: ucuz termal yazicilarin cogu bu tablonun yalnizca birkac
# girdisini gercekten uyguluyor. Desteklenmeyen bir sayfa secilince
# yazici yuksek baytlari BOSLUK basiyor -- "Sogan" yerine "So an"
# cikmasinin sebebi buydu. Bu yuzden sayfa artik ayardan geliyor ve
# hangisinin calistigini kagitta goren kullanici seciyor
# (kalibrasyon fisi). Bilinmeyen durumda ASCII'ye iniyoruz: "Sogan"
# okunur, "So an" okunmaz.
#
# (ESC t n degeri, Python kodlamasi, ekranda gorunen ad)
KOD_SAYFALARI: list[tuple[int, str, str]] = [
    (13, "cp857", "PC857 - Turkce (en yaygin)"),
    (47, "cp1254", "WPC1254 - Windows Turkce"),
    (34, "cp1254", "WPC1254 - alternatif slot"),
    (18, "cp852", "PC852 - Latin-2 (kismi)"),
    (16, "cp1252", "WPC1252 - Bati Avrupa (kismi)"),
]
KODLAMA = {n: kodlama for n, kodlama, _ in KOD_SAYFALARI}

# 0 = kod sayfasi kullanma, her seyi ASCII'ye indir.
ASCII_SAYFA = 0


def sayfa_komutu(sayfa: int) -> bytes:
    if sayfa == ASCII_SAYFA or sayfa not in KODLAMA:
        return b""
    return ESC + b"t" + bytes([sayfa])

# 80 mm yazicida 48, 58 mm'de 32 karakter. Yanlis deger satirlari
# kaydirdigi icin ayardan geliyor.
GENISLIKLER = {58: 32, 80: 48}


def qr_komutu(veri: str, modul: int = 6) -> bytes:
    """ESC/POS QR kodu (GS ( k, model 2).

    Kurye fisteki QR'i telefonuyla okutunca Google Maps rotasi aciliyor.
    Sira Epson'un tarif ettigi gibi: model -> modul boyutu -> hata
    duzeltme -> veriyi sakla -> bas. QR desteklemeyen yazici bu
    komutlari yok sayiyor ya da birkac anlamsiz karakter basiyor; bu
    yuzden ayardan kapatilabiliyor (yazici.rota_qr).
    """
    icerik = veri.encode("ascii", "ignore")
    uzunluk = len(icerik) + 3
    return (
        GS + b"(k\x04\x00\x31\x41\x32\x00"                      # model 2
        + GS + b"(k\x03\x00\x31\x43" + bytes([max(1, min(modul, 16))])  # modul boyutu
        + GS + b"(k\x03\x00\x31\x45\x30"                       # hata duzeltme L
        + GS + b"(k" + bytes([uzunluk % 256, uzunluk // 256]) + b"\x31\x50\x30" + icerik
        + GS + b"(k\x03\x00\x31\x51\x30"                       # bas
    )


def rota_adresi(order: Order) -> str | None:
    """Kurye icin Google Maps yol tarifi; arayuzdeki yolTarifi ile ayni kural.

    Gel-al ve platform kuryesi (Model 2) siparisinde rota bizi
    ilgilendirmiyor; konum da adres de yoksa QR basilmaz.
    """
    from urllib.parse import urlencode

    if order.is_pickup or order.courier_by_platform:
        return None
    if order.latitude is not None and order.longitude is not None:
        hedef = f"{order.latitude},{order.longitude}"
    elif order.address_text:
        hedef = order.address_text
    else:
        return None
    param = {"api": "1", "destination": hedef, "travelmode": "driving"}
    if order.store_latitude is not None and order.store_longitude is not None:
        param["origin"] = f"{order.store_latitude},{order.store_longitude}"
    return "https://www.google.com/maps/dir/?" + urlencode(param)


def _ascii_indir(metin: str) -> str:
    """Kod sayfasinda olmayan karakterleri en yakin ASCII'ye indirir."""
    esle = {"ı": "i", "İ": "I", "ğ": "g", "Ğ": "G", "ş": "s", "Ş": "S",
            "ç": "c", "Ç": "C", "ö": "o", "Ö": "O", "ü": "u", "Ü": "U"}
    metin = "".join(esle.get(k, k) for k in metin)
    return unicodedata.normalize("NFKD", metin).encode("ascii", "ignore").decode()


def _kodla(metin: str, sayfa: int = ASCII_SAYFA) -> bytes:
    """Metni secili kod sayfasina cevirir.

    Karakter karakter deneniyor: tek bir desteklenmeyen harf yuzunden
    butun satiri ASCII'ye indirmek gereksiz olurdu.
    """
    kodlama = KODLAMA.get(sayfa)
    if not kodlama:
        return _ascii_indir(metin).encode("ascii", "replace")

    cikti = bytearray()
    for karakter in metin:
        try:
            cikti += karakter.encode(kodlama)
        except UnicodeEncodeError:
            cikti += _ascii_indir(karakter).encode("ascii", "replace")
    return bytes(cikti)


def _satir(sol: str, sag: str, genislik: int) -> str:
    """Solu sola, sagi saga yaslar; sigmazsa solu kirpar."""
    sag = sag[:genislik]
    yer = genislik - len(sag)
    if len(sol) > yer - 1:
        sol = sol[: max(0, yer - 2)] + ".."
    return sol.ljust(yer) + sag


def _sar(metin: str, genislik: int, girinti: int = 0) -> list[str]:
    """Kelime bazli satira boler; uzun urun adlari kesilmesin."""
    if not metin:
        return []
    pay = max(8, genislik - girinti)
    satirlar: list[str] = []
    gecerli = ""
    for kelime in metin.split():
        if not gecerli:
            gecerli = kelime
        elif len(gecerli) + 1 + len(kelime) <= pay:
            gecerli += " " + kelime
        else:
            satirlar.append(gecerli)
            gecerli = kelime
    if gecerli:
        satirlar.append(gecerli)
    return [" " * girinti + s for s in satirlar]


def _para(n: float) -> str:
    return f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


PLATFORM_ADI = {"tgo": "Trendyol Go", "yemeksepeti": "Yemeksepeti",
                "migros": "Migros Yemek"}


def fis_olustur(
    order: Order,
    genislik_mm: int = 80,
    baslik: str = "TEKEAT",
    sayfa: int = ASCII_SAYFA,
    kurye: str | None = None,
    rota_qr: bool = False,
    onizleme: bool = False,
) -> bytes:
    """Siparisten ESC/POS fisi uretir.

    onizleme: QR baytlari yerine okunur bir yer tutucu yazilir (metin
    onizlemesinde ikili veri anlamsiz gorunuyordu).
    """
    g = GENISLIKLER.get(genislik_mm, 48)
    parcalar: list[bytes] = [SIFIRLA, sayfa_komutu(sayfa)]

    def kod(metin: str) -> bytes:
        return _kodla(metin, sayfa)

    def yaz(metin: str = "", bicim: bytes | None = None) -> None:
        if bicim:
            parcalar.append(bicim)
        parcalar.append(kod(metin) + b"\n")
        if bicim:
            parcalar.append(NORMAL_BOY + KALIN_KAPA + SOL)

    def cizgi(karakter: str = "-") -> None:
        parcalar.append(kod(karakter * g) + b"\n")

    # ---- baslik
    parcalar.append(ORTA)
    yaz(baslik, KALIN_AC)
    yaz(PLATFORM_ADI.get(order.platform.value, order.platform.value))
    parcalar.append(SOL)
    cizgi("=")

    # ---- siparis kodu: kagitta en buyuk sey bu olmali
    parcalar.append(ORTA + CIFT_HEPSI + KALIN_AC)
    parcalar.append(kod(order.order_code or "-") + b"\n")
    parcalar.append(NORMAL_BOY + KALIN_KAPA + SOL)

    ne_zaman = (order.created_at or datetime.now(timezone.utc)).astimezone()
    parcalar.append(ORTA)
    yaz(ne_zaman.strftime("%d.%m.%Y  %H:%M"))
    parcalar.append(SOL)
    cizgi("=")

    # ---- teslimat tipi: mutfak ve kurye icin en kritik ayrim
    if order.is_pickup:
        parcalar.append(ORTA)
        yaz("*** GEL-AL ***", KALIN_AC)
        parcalar.append(SOL)
    elif order.courier_by_platform:
        yaz("Teslimat: Platform kuryesi")
    else:
        yaz("Teslimat: Kendi kuryemiz")
        if kurye:
            # Fisi kim goturuyor: mutfak ve kasa ayni kagittan gorsun.
            parcalar.append(KALIN_AC)
            parcalar.append(kod(f"Kurye: {kurye}") + b"\n")
            parcalar.append(KALIN_KAPA)

    if order.is_test:
        parcalar.append(ORTA)
        yaz("--- TEST SIPARISI ---", KALIN_AC)
        parcalar.append(SOL)

    # ---- odeme: kuryenin para alip almayacagi
    if order.is_on_delivery:
        cizgi()
        parcalar.append(ORTA + KALIN_AC)
        parcalar.append(kod("KAPIDA ODEME") + b"\n")
        parcalar.append(kod(order.payment_on_delivery or "-") + b"\n")
        parcalar.append(kod(f"Tahsil edilecek: {_para(order.total_price)} TL") + b"\n")
        parcalar.append(KALIN_KAPA + SOL)
    else:
        yaz(f"Odeme: {order.payment_type or '-'} (online)")

    cizgi("=")

    # ---- urunler
    for u in order.items:
        if u.cancelled:
            continue
        ad = f"{u.quantity}x {u.name}"
        satirlar = _sar(ad, g - 10)
        ilk = satirlar[0] if satirlar else ad
        parcalar.append(KALIN_AC)
        parcalar.append(kod(_satir(ilk, _para(u.total_price), g)) + b"\n")
        parcalar.append(KALIN_KAPA)
        for devam in satirlar[1:]:
            parcalar.append(kod("   " + devam.strip()) + b"\n")

        for o in u.options:
            for s in _sar(f"+ {o.name}", g, girinti=3):
                parcalar.append(kod(s) + b"\n")
        # Cikarilan malzeme kalin: yanlis urun cikmasinin bir numarali sebebi.
        if u.removed:
            parcalar.append(KALIN_AC)
            for s in _sar("CIKAR: " + ", ".join(u.removed), g, girinti=3):
                parcalar.append(kod(s) + b"\n")
            parcalar.append(KALIN_KAPA)
        if u.extras:
            parcalar.append(KALIN_AC)
            for s in _sar("EKLE: " + ", ".join(u.extras), g, girinti=3):
                parcalar.append(kod(s) + b"\n")
            parcalar.append(KALIN_KAPA)
        if u.note:
            for s in _sar("Not: " + u.note, g, girinti=3):
                parcalar.append(kod(s) + b"\n")

    iptaller = [u for u in order.items if u.cancelled]
    if iptaller:
        cizgi()
        yaz("IPTAL EDILENLER:", KALIN_AC)
        for u in iptaller:
            parcalar.append(kod(f"   {u.quantity}x {u.name}") + b"\n")

    cizgi("=")

    # ---- toplamlar
    ara = sum(u.total_price for u in order.items if not u.cancelled)
    parcalar.append(kod(_satir("Ara toplam", _para(ara) + " TL", g)) + b"\n")
    if order.delivery_price:
        parcalar.append(
            kod(_satir("Teslimat", _para(order.delivery_price) + " TL", g)) + b"\n"
        )
    if order.discount_total:
        parcalar.append(
            kod(_satir("Indirim", "-" + _para(order.discount_total) + " TL", g)) + b"\n"
        )
    parcalar.append(KALIN_AC)
    parcalar.append(kod(_satir("TOPLAM", _para(order.total_price) + " TL", g)) + b"\n")
    parcalar.append(KALIN_KAPA)

    # ---- musteri notu
    if order.note:
        cizgi("=")
        yaz("MUSTERI NOTU", KALIN_AC)
        parcalar.append(KALIN_AC)
        for s in _sar(order.note, g):
            parcalar.append(kod(s) + b"\n")
        parcalar.append(KALIN_KAPA)

    # ---- teslimat bilgisi
    if not order.is_pickup:
        cizgi("=")
        if order.customer_name:
            yaz(order.customer_name)
        for s in _sar(order.address_text or "-", g):
            parcalar.append(kod(s) + b"\n")

    if order.call_center_phone:
        cizgi()
        yaz("Musteriyi ara:")
        parcalar.append(KALIN_AC)
        parcalar.append(kod(order.call_center_phone) + b"\n")
        if order.pin_code:
            # IVR: numara tek basina yetmiyor, kod tuslanmasi gerekiyor.
            parcalar.append(kod(f"Kod: {order.pin_code}") + b"\n")
        parcalar.append(KALIN_KAPA)

    rota = rota_adresi(order) if rota_qr else None
    if rota:
        cizgi()
        parcalar.append(ORTA)
        yaz("Yol tarifi icin okutun")
        if onizleme:
            parcalar.append(b"[QR KOD: Google Maps rotasi]\n")
        else:
            parcalar.append(qr_komutu(rota) + b"\n")
        parcalar.append(SOL)

    parcalar.append(KES)
    return b"".join(parcalar)


# ---------------------------------------------------------------
# Windows yazici islemleri
# ---------------------------------------------------------------


def _win32print():
    try:
        import win32print  # type: ignore
    except ImportError as exc:  # pragma: no cover - platforma bagli
        raise RuntimeError(
            "Yazici destegi icin pywin32 gerekiyor. 1-KURULUM.cmd dosyasini "
            "tekrar calistirin."
        ) from exc
    return win32print


def yazicilari_listele() -> list[dict]:
    """Bilgisayara tanimli yazicilar; varsayilan isaretli."""
    w = _win32print()
    try:
        varsayilan = w.GetDefaultPrinter()
    except Exception:
        varsayilan = ""
    bayrak = w.PRINTER_ENUM_LOCAL | w.PRINTER_ENUM_CONNECTIONS
    sonuc = []
    for p in w.EnumPrinters(bayrak, None, 2):
        ad = p["pPrinterName"]
        sonuc.append({
            "name": ad,
            "default": ad == varsayilan,
            "port": p.get("pPortName") or "",
            "driver": p.get("pDriverName") or "",
            "jobs": p.get("cJobs", 0),
        })
    sonuc.sort(key=lambda x: (not x["default"], x["name"].lower()))
    return sonuc


def ham_yazdir(yazici: str, veri: bytes, is_adi: str = "Tekeat fis") -> None:
    """Baytlari RAW is olarak yaziciya gonderir.

    RAW: surucu araya girip sayfayi yeniden cizmiyor, ESC/POS komutlari
    yaziciya oldugu gibi ulasiyor.
    """
    w = _win32print()
    tutamac = w.OpenPrinter(yazici)
    try:
        is_no = w.StartDocPrinter(tutamac, 1, (is_adi, None, "RAW"))
        try:
            w.StartPagePrinter(tutamac)
            w.WritePrinter(tutamac, veri)
            w.EndPagePrinter(tutamac)
        finally:
            w.EndDocPrinter(tutamac)
        log.info("Fis yaziciya gonderildi (%s, is=%s, %d bayt)", yazici, is_no, len(veri))
    finally:
        w.ClosePrinter(tutamac)


def kalibrasyon_fisi(genislik_mm: int = 80) -> bytes:
    """Hangi kod sayfasinin calistigini KAGITTA gosteren test fisi.

    Ucuz termal yazicilarin cogu ESC t tablosunun yalnizca birkac
    girdisini uyguluyor ve desteklenmeyen sayfada yuksek baytlari
    bosluk basiyor. Hangisinin calistigini disaridan bilmenin yolu yok
    -- her adayi ayri ayri basip kullaniciya "hangi satir dogru?"
    diye soruyoruz.
    """
    g = GENISLIKLER.get(genislik_mm, 48)
    ornek = "ÇĞİÖŞÜ çğıöşü"
    parcalar: list[bytes] = [SIFIRLA]

    parcalar.append(ORTA + KALIN_AC)
    parcalar.append(b"TURKCE KARAKTER TESTI\n")
    parcalar.append(KALIN_KAPA + SOL)
    parcalar.append(("-" * g + "\n").encode("ascii"))
    parcalar.append(b"Asagidaki satirlardan HANGISI dogru\n")
    parcalar.append(b"gorunuyorsa, o numarayi Yazici\n")
    parcalar.append(b"sayfasinda secin.\n")
    parcalar.append(b"\nDogrusu su olmali:\n")
    # Referans satiri ASCII: her yazicida ayni cikar.
    parcalar.append(b"  CGIOSU cgiosu (harfler eksiksiz)\n")
    parcalar.append(("-" * g + "\n").encode("ascii"))

    for n, kodlama, ad in KOD_SAYFALARI:
        parcalar.append(sayfa_komutu(n))
        parcalar.append(KALIN_AC)
        parcalar.append(f"[{n}] ".encode("ascii"))
        parcalar.append(KALIN_KAPA)
        try:
            parcalar.append(ornek.encode(kodlama))
        except UnicodeEncodeError:
            parcalar.append(_ascii_indir(ornek).encode("ascii", "replace"))
        parcalar.append(b"\n")
        parcalar.append(f"     {ad}\n".encode("ascii", "replace"))

    # Karsilastirma icin ASCII secenegi.
    parcalar.append(sayfa_komutu(ASCII_SAYFA))
    parcalar.append(KALIN_AC + b"[0] " + KALIN_KAPA)
    parcalar.append(_ascii_indir(ornek).encode("ascii") + b"\n")
    parcalar.append(b"     ASCII - Turkce karakter yok\n")
    parcalar.append(b"     (hicbiri dogru degilse bunu secin)\n")

    parcalar.append(("-" * g + "\n").encode("ascii"))
    parcalar.append(KES)
    return b"".join(parcalar)


def ornek_siparis() -> Order:
    """Yazici testi icin gercekci bir siparis.

    Icinde bilerek zor seyler var: Turkce karakter, uzun urun adi,
    cikarilan/eklenen malzeme, kapida odeme ve musteri notu.
    """
    from .models import OrderItem, OrderItemOption, OrderStatus, Platform

    return Order(
        platform=Platform.TGO,
        platform_order_id="ornek",
        order_code="TEST-1",
        status=OrderStatus.NEW,
        created_at=datetime.now(timezone.utc),
        customer_name="Ayşe Yılmaz",
        address_text="Caferağa Mah. Güneşli Bahçe Sk. No:12 D:4, Kadıköy/İstanbul",
        call_center_phone="0212 365 34 03",
        pin_code="673985557",
        is_pickup=False,
        courier_by_platform=False,
        # Koordinat var ki "Test fisi" rota QR'ini da denesin.
        latitude=40.9903,
        longitude=29.0270,
        store_latitude=41.0422,
        store_longitude=29.0093,
        items=[
            OrderItem(
                name="Adana Kebap Menü (Büyük Porsiyon)",
                quantity=2, unit_price=145.0, total_price=290.0,
                options=[OrderItemOption(name="Patates Kızartması", price=30.0),
                         OrderItemOption(name="Ayran", price=0.0)],
                removed=["Soğan", "Turşu"],
                extras=["Ekstra peynir"],
                note="Az acılı olsun",
                item_ids=["1", "2"],
            ),
            OrderItem(name="Çiğ Köfte Dürüm", quantity=1,
                      unit_price=75.0, total_price=75.0, item_ids=["3"]),
            OrderItem(name="Şalgam Suyu", quantity=3,
                      unit_price=25.0, total_price=75.0, item_ids=["4", "5", "6"]),
        ],
        total_price=425.99,
        delivery_price=15.99,
        discount_total=30.0,
        payment_type="PAY_WITH_ON_DELIVERY",
        payment_on_delivery="Nakit",
        is_on_delivery=True,
        note="Zile basmayın, bebek uyuyor. Kapıyı çalın lütfen.",
        is_test=True,
    )
