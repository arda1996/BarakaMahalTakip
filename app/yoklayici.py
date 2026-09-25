"""Platformlari arka ucta yoklayip siparisleri veritabanina yazan dongu.

Neden arayuzde degil:
  * Arayuz kapaninca ya da yeniden acilinca bellekteki her sey
    kayboluyordu; siparisin durumu ve gecmisi hic yazilmiyordu.
  * WebView2 arkada kalan sayfanin zamanlayicilarini dakikada bire
    indiriyor. Pencere arkadayken 12 saniyelik yoklama 60 saniyeye
    cikiyordu (servis.log'da gorulen desen); yeni siparis bir dakika
    gec gorunuyordu.

Arayuz artik yalnizca yerel veritabanini okuyor. Platforma giden
istek sayisi eskisiyle ayni: baglanti basina 12 sn'de bir paket
listesi.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import ayarlar, demo, hatalar, havuz, mesafe, sistem, stok, store
from .adapters.base import AuthError
from .models import Order, OrderStatus

log = logging.getLogger("tekeat.yoklayici")

YOKLAMA_SN = 12.0

# Aktif liste icin platform sorgusunun penceresi. Bu pencereden eski
# ama hala suren siparisler asagidaki tekil sorguyla takip ediliyor.
_AKTIF_PENCERE = timedelta(minutes=240)
_AKTIF = [
    OrderStatus.NEW,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.ON_THE_WAY,
]

# Biten siparisler (teslim/iptal) gecmis ve gunluk ozet icin. Her
# yoklamada sormaya gerek yok; ilk seferde son 24 saat, sonra son
# senkrondan bu yana.
_BITEN = [OrderStatus.DELIVERED, OrderStatus.CANCELLED]
_BITEN_ARALIK_SN = 300.0
_BITEN_ILK_PENCERE = timedelta(hours=24)
_BITEN_PAY = timedelta(minutes=10)
# Acilis telafisi: kapaliyken gecen sure bu pencerelerle, en fazla bu
# kadar geriye (daha eskisini gecmis aktarimi zaten tariyor).
_TELAFI_PENCERE = timedelta(days=7)
_TELAFI_EN_FAZLA = timedelta(days=60)

# Aktif listede artik gorunmeyen bir siparisin ne oldugunu tek tek
# soruyoruz (Model 2'de platform kuryesi teslim edince bize kimse
# haber vermiyor). Limit dostu olsun diye sinirli.
_TEKIL_EN_FAZLA = 10
_TEKIL_ARALIK_SN = 60.0

# Sube koordinatlari siparis basina degismiyor; her yoklamada
# /stores cagirmak hem yavas hem limit yiyor.
_SUBE_TTL = 600.0

# Arayuzun "Yenile"si ve pencere odagi platformu hemen yoklatiyor;
# ust uste gelen odak olaylari limiti yemesin.
_TAZELEME_EN_AZ_SN = 3.0


# Oto onayda platform reddederse (or. siparis o arada panelden
# iptal edildi) sonsuza kadar denemeyelim; teyit gelmezse de bir
# sonraki turlarda tekrar.
_OTO_EN_FAZLA = 3
_OTO_ARALIK_SN = 30.0
# Kabulden sonra platform statuyu hemen yansitmiyor (orders.py'deki
# teyit ile ayni ders).
_OTO_TEYIT = (0.4, 0.8, 1.2, 2.0)


class _Durum:
    def __init__(self) -> None:
        self.son_basari: datetime | None = None
        self.hata: str | None = None
        self.biten_son: datetime | None = None
        self.biten_son_deneme = 0.0


_durumlar: dict[int, _Durum] = {}
_sube_onbellek: dict[int, tuple[float, dict]] = {}
_tekil_son: dict[str, float] = {}
_oto_deneme: dict[str, tuple[int, float]] = {}
_arka_gorevler: set[asyncio.Task] = set()
_kilit = asyncio.Lock()
_son_tur = 0.0
_son_baslangic = 0.0


def _durum(cid: int) -> _Durum:
    return _durumlar.setdefault(cid, _Durum())


async def _sube_konumlari(adapter, cid: int) -> dict:
    kayit = _sube_onbellek.get(cid)
    if kayit and time.monotonic() - kayit[0] < _SUBE_TTL:
        return kayit[1]
    try:
        subeler = await adapter.fetch_stores()
    except Exception:
        # Konum alinamazsa mesafe gosterilmez; siparis akisi durmaz.
        return kayit[1] if kayit else {}
    harita = {s.id: (s.latitude, s.longitude) for s in subeler}
    _sube_onbellek[cid] = (time.monotonic(), harita)
    return harita


async def _zenginlestir(adapter, cid: int, siparisler: list[Order]) -> None:
    """Baglanti kimligi, mesafe ve paket ucreti.

    Mesafe yalnizca kendi kuryemizle giden siparislerde
    hesaplanabiliyor: Model 2'de platform musteri koordinatini
    maskeliyor.
    """
    konumlar = await _sube_konumlari(adapter, cid)
    teslimat = ayarlar.oku().get("teslimat", {})
    for o in siparisler:
        o.connection_id = cid
        sube = konumlar.get(o.store_id or "", (None, None))
        o.store_latitude, o.store_longitude = sube
        o.distance_km, o.courier_fee = mesafe.hesapla(
            sube[0], sube[1], o.latitude, o.longitude, teslimat
        )


async def kaydet(siparisler: list[Order]) -> list[str]:
    """Veritabanina yazar (ayri parcacikta); ilk kez gorulen yenileri doner.

    Ardindan teslim edilenleri stoktan dusuyor (bkz. stok.py). Stok hatasi
    siparis akisini DURDURMAMALI: kayit yazildi, dusum bir sonraki turda
    tekrar denenir (siparis_stok bir kez yazildigi icin cift dusmez).
    """
    yeniler = await asyncio.to_thread(store.save_orders, siparisler)
    try:
        await asyncio.to_thread(stok.siparisleri_isle, siparisler)
    except Exception:
        log.exception("Stok dusumu basarisiz")
    return yeniler


def _oto_ayar() -> tuple[bool, int]:
    ayar = ayarlar.oku().get("siparis", {})
    return bool(ayar.get("oto_onay")), int(ayar.get("hazirlik_dk") or 20)


def _denenebilir(oid: str) -> bool:
    deneme, son = _oto_deneme.get(oid, (0, 0.0))
    return deneme < _OTO_EN_FAZLA and time.monotonic() - son >= _OTO_ARALIK_SN


async def _fis_bas(order: Order) -> None:
    """Oto kabul edilen siparisin fisi.

    Elle kabulde fisi arayuz basiyor; oto kabulde arayuz arkada ya da
    kapali olabilir, o yuzden burada. Yazici hatasi siparisi geri
    almaz; kayda yazilir.
    """
    if not ayarlar.oku().get("yazici", {}).get("otomatik"):
        return
    from .routers.yazici import _bas

    try:
        await asyncio.to_thread(_bas, order)
    except Exception as exc:
        log.warning("Oto kabul fisi basilamadi (%s): %s", order.order_code,
                    getattr(exc, "detail", exc))


async def _otomatik_kabul(adapter, cid: int, siparisler: list[Order]) -> list[Order]:
    """Oto onay acikken "yeni" siparisleri kabul eder; teyitli halleri doner.

    Iyimser degil: kabul istegi sonrasi platform statuyu degistirene
    kadar tek paket sorgusuyla bekliyoruz ve donen, PLATFORMUN
    bildirdigi hal. Teyit gelmezse siparis "yeni" kalir, alarm calar ve
    bir sonraki turda tekrar denenir.

    Kayit cagiranda: siparis once "yeni" diye yazilip sonra kabul
    edilirse arayuz aradaki anda alarmi bir an calip susturuyordu.
    """
    acik, dk = _oto_ayar()
    kabul: list[Order] = []
    if not acik:
        return kabul
    bekleyen = [
        o for o in siparisler
        if o.status == OrderStatus.NEW and _denenebilir(o.platform_order_id)
    ]
    gonderilen: list[str] = []
    for o in bekleyen:
        oid = o.platform_order_id
        deneme, _ = _oto_deneme.get(oid, (0, 0.0))
        _oto_deneme[oid] = (deneme + 1, time.monotonic())
        try:
            await adapter.accept_order(oid, dk)
            gonderilen.append(oid)
        except Exception as exc:
            log.warning("Oto kabul reddedildi (%s): %s", o.order_code, exc)

    for bekle in _OTO_TEYIT:
        if not gonderilen:
            break
        await asyncio.sleep(bekle)
        for oid in list(gonderilen):
            try:
                son = await adapter.fetch_order(oid)
            except Exception:
                continue
            if son is None or son.status == OrderStatus.NEW:
                continue
            gonderilen.remove(oid)
            son.oto_kabul = True
            _oto_deneme.pop(oid, None)
            log.info("Oto kabul: %s (%d dk)", son.order_code, dk)
            kabul.append(son)
    for oid in gonderilen:
        log.warning("Oto kabul teyit edilmedi: %s; tekrar denenecek", oid)
    return kabul


async def demo_otomatik_kabul() -> None:
    """Test siparisleri de switch'e uysun: akis gercek siparis beklemeden denensin."""
    acik, dk = _oto_ayar()
    if not acik:
        return
    for o in demo.listele([OrderStatus.NEW]):
        son = demo.durum_degistir(o.platform_order_id, OrderStatus.PREPARING)
        if son is None:
            continue
        son.preparation_minutes = dk
        son.oto_kabul = True
        await _fis_bas(son)


async def _baglanti_yokla(cid: int, user_id: int, etiket: str | None = None) -> list[str]:
    durum = _durum(cid)
    # Hata olsa da doner: yeni siparis kayda yazildiktan sonra ayni turun
    # sonraki bir adimi (telafi, biten senkronu) patlarsa pencere yine one
    # gelsin. Eskiden liste kayboluyor ve alarm penceresi one gelmiyordu.
    yeniler: list[str] = []
    try:
        # Adapteri kurmak da try icinde: bozuk kimlik bilgisi olan tek
        # bir baglanti butun turu dusurup DIGER hesaplarin yoklanmasini
        # engelliyordu.
        try:
            adapter = await havuz.edin(cid, user_id)
        except KeyError:
            return []

        simdi = datetime.now(timezone.utc)
        # Telafinin baslangici bu turun kaydindan ONCE okunmali: yoksa
        # acilista gelen tek yeni siparis last_seen'i "simdi"ye cekiyor ve
        # kapali kalinan gunler hic sorulmuyordu (aralikli test hatasi).
        telafi_son = (
            await asyncio.to_thread(store.last_seen, cid)
            if durum.biten_son is None
            else None
        )
        aktif = await adapter.fetch_orders(
            statuses=_AKTIF, since=simdi - _AKTIF_PENCERE
        )
        kabul = await _otomatik_kabul(adapter, cid, aktif)
        if kabul:
            teyitli = {o.platform_order_id: o for o in kabul}
            aktif = [teyitli.get(o.platform_order_id, o) for o in aktif]
        await _zenginlestir(adapter, cid, aktif)
        yeniler += await kaydet(aktif)
        for o in kabul:
            await _fis_bas(o)

        # Bizde suruyor gorunen ama platformun aktif listesinde olmayan
        # siparisler: bitmis olabilir, ya da pencereden eski olabilir.
        gelen = {o.platform_order_id for o in aktif}
        kayitli = await asyncio.to_thread(store.active_order_ids, cid)
        sorulacak = [
            oid
            for oid in kayitli
            if oid not in gelen
            and time.monotonic() - _tekil_son.get(oid, 0.0) >= _TEKIL_ARALIK_SN
        ][:_TEKIL_EN_FAZLA]
        tekiller: list[Order] = []
        for oid in sorulacak:
            _tekil_son[oid] = time.monotonic()
            o = await adapter.fetch_order(oid)
            if o is not None:
                tekiller.append(o)
        if tekiller:
            await _zenginlestir(adapter, cid, tekiller)
            await kaydet(tekiller)

        if durum.biten_son is None:
            # ACILIS TELAFISI: uygulama gunlerce kapali kaldiysa aradaki
            # siparisler (o arada gelip biten) kayda hic girmiyordu --
            # ilk senkron yalnizca son 24 saate bakiyordu. Kayda en son
            # dokunulan andan bugune TUM statuleri pencere pencere cekiyoruz.
            # Daha eskisi gecmis aktariminin isi.
            son = telafi_son
            baslangic = max(
                (son - _BITEN_PAY) if son else simdi - _BITEN_ILK_PENCERE,
                simdi - _TELAFI_EN_FAZLA,
            )
            while baslangic < simdi:
                bitis = min(baslangic + _TELAFI_PENCERE, simdi)
                ara = await adapter.fetch_orders(statuses=None, since=baslangic, until=bitis)
                await _zenginlestir(adapter, cid, ara)
                await kaydet(ara)
                baslangic = bitis
            durum.biten_son = simdi
            durum.biten_son_deneme = time.monotonic()
        elif time.monotonic() - durum.biten_son_deneme >= _BITEN_ARALIK_SN:
            durum.biten_son_deneme = time.monotonic()
            baslangic = durum.biten_son - _BITEN_PAY
            biten = await adapter.fetch_orders(statuses=_BITEN, since=baslangic)
            await _zenginlestir(adapter, cid, biten)
            await kaydet(biten)
            durum.biten_son = simdi

        durum.son_basari = datetime.now(timezone.utc)
        durum.hata = None
        return yeniler
    except AuthError as exc:
        # Oturum/anahtar sorunlu: adapteri tazeleyelim ki bir sonraki
        # yoklama yeniden kursun.
        await havuz.unut(cid)
        durum.hata = _hesap_mesaji(etiket, hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] yoklama"))
    except Exception as exc:
        # Ag kopmasi (getaddrinfo) PlatformError degil; hangi hata
        # olursa olsun dongu yasamali, bir sonraki turda tekrar dener.
        # Ekrana kisa cumle; teknik ayrinti kullanici_mesaji'nda log'a.
        durum.hata = _hesap_mesaji(etiket, hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] yoklama"))
    return yeniler


def _hesap_mesaji(etiket: str | None, mesaj: str) -> str:
    """Birden fazla hesap varken hangisinde sorun oldugu anlasilsin: teknik
    "[baglanti 2]" yerine hesabin kullanicinin verdigi adi."""
    return f"{etiket}: {mesaj}" if etiket else mesaj


async def yokla() -> None:
    """Butun baglantilari bir kez yoklar.

    Ayni anda iki tur calismasin. Biri suruyorsa bitmesini bekliyoruz;
    o tur BU cagridan sonra basladiysa cevabi yeterince taze, tekrar
    yoklamiyoruz. Once baslamissa (yeni siparis gelmeden) tekrar
    yokluyoruz -- eskiden bekleyip donuyorduk ve "Yenile" eski listeyi
    getirebiliyordu (testte yakalandi).
    """
    global _son_tur, _son_baslangic
    # perf_counter: Windows'ta monotonic ~15 ms adimla ilerliyor; ayni
    # adimda baslayan onceki tur "sonra basladi" sayilip yoklama
    # atlaniyordu (aralikli test hatasi).
    istek = time.perf_counter()
    async with _kilit:
        if _son_baslangic > istek:
            return
        _son_baslangic = time.perf_counter()
        baglantilar = await asyncio.to_thread(store.all_connections)
        canli = {info.id for info, _ in baglantilar}
        # Silinen baglantinin eski hatasi ekranda kalmasin.
        for cid in list(_durumlar):
            if cid not in canli:
                _durumlar.pop(cid, None)

        yeniler: list[str] = []
        for info, user_id in baglantilar:
            yeniler += await _baglanti_yokla(info.id, user_id, info.label)
        await demo_otomatik_kabul()
        _son_tur = time.monotonic()

    if yeniler:
        log.info("Yeni siparis: %s", ", ".join(yeniler))
        # Pencere arkadaysa arayuzun zamanlayicilari yavaslamis
        # olabilir; one almak hem kullaniciya gosteriyor hem de
        # arayuzu hemen yoklamaya uyandiriyor (odak olayi).
        sistem.one_al()


async def tazele() -> None:
    """Arayuzun istegiyle hemen yokla; cok yeni bir tur varsa atla."""
    if time.monotonic() - _son_tur < _TAZELEME_EN_AZ_SN:
        return
    await yokla()


def arkada_tazele() -> None:
    """Istegi bekletmeden bir tur baslatir (or. oto onay acildiginda).

    Gorev referansini tutuyoruz: tutulmayan gorev yarida cop
    toplayiciya gidebiliyor.
    """
    global _son_tur
    _son_tur = 0.0
    gorev = asyncio.create_task(tazele())
    _arka_gorevler.add(gorev)
    gorev.add_done_callback(_arka_gorevler.discard)


async def calistir() -> None:
    """Uygulama acik kaldigi surece donen gorev."""
    while True:
        try:
            await yokla()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Yoklama turu coktu")
        await asyncio.sleep(YOKLAMA_SN)


def durum(connection_ids: list[int]) -> dict:
    """Arayuz icin: son basarili yoklama ve varsa hata.

    son_basari en ESKI baglantininki: bir hesap cevap vermiyorsa
    "her sey guncel" gorunmesin.
    """
    hatalar: list[str] = []
    zamanlar: list[datetime] = []
    for cid in connection_ids:
        d = _durumlar.get(cid)
        if d is None:
            continue
        if d.hata:
            hatalar.append(d.hata)
        if d.son_basari:
            zamanlar.append(d.son_basari)
    return {
        "hata": " | ".join(hatalar) or None,
        "son_basari": min(zamanlar).isoformat() if zamanlar else None,
    }
