"""Tek ekrandan siparis yonetimi.

Butun platformlar ayni ucu paylasir; fark adapter'da kalir.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import ayarlar, demo, havuz, store, yoklayici
from ..hatalar import kullanici_mesaji
from ..adapters.base import AuthError, PlatformError
from ..deps import current_user
from ..models import CANCEL_REASONS, Order, OrderStatus, UserInfo

router = APIRouter(prefix="/orders", tags=["siparisler"])


async def _adapter(connection_id: int, user_id: int):
    """Havuzdaki adapter.

    Kapatmiyoruz: istemci (ve TLS baglantisi) baglanti basina bir kere
    kuruluyor, yoklamalar arasinda yasiyor. Kimlik hatasi alinirsa
    cagiran taraf havuz.unut ile dusuruyor.
    """
    try:
        return await havuz.edin(connection_id, user_id)
    except KeyError as exc:
        raise HTTPException(404, "Baglanti bulunamadi.") from exc


@router.get("", response_model=list[Order])
async def list_orders(
    connection_id: int | None = None,
    statuses: list[OrderStatus] = Query(
        default=[OrderStatus.NEW, OrderStatus.PREPARING]
    ),
    minutes: int = Query(
        default=180,
        description="Teslim/iptal gibi BITMIS siparisler icin: son kac dakika. "
        "Aktif siparisler yasina bakilmadan gelir.",
    ),
    tazele: bool = Query(
        default=False, description="Platformu once hemen yoklat (Yenile, odak)."
    ),
    user: UserInfo = Depends(current_user),
) -> list[Order]:
    """Yerel kayittaki siparisler.

    Platformu artik bu istek yoklamiyor; arka plandaki yoklayici
    yokluyor ve yaziyor (bkz. yoklayici.py). Boylece uygulama
    acildiginda ya da internet koptugunda son bilinen durum ekranda
    kaliyor. Yoklama hatasi ayrica /orders/sync-status'ta.
    """
    if tazele:
        await yoklayici.tazele()

    # Test siparisleri once: platforma hic gitmeden ekranda cikarlar.
    result: list[Order] = demo.listele(statuses)

    targets = (
        [connection_id]
        if connection_id is not None
        else [c.id for c in store.list_connections(user.id)]
    )
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    result.extend(
        await asyncio.to_thread(store.list_orders, targets, statuses, since)
    )

    # Kurye atamasi yerel: platformdan gelen siparisi kendi
    # kaydimizla zenginlestiriyoruz.
    gercekler = [
        o.platform_order_id
        for o in result
        if not demo.test_siparisi_mi(o.platform_order_id)
    ]
    kuryeler = store.get_order_couriers(gercekler)
    for o in result:
        # Test siparisinin kuryesi bellekte duruyor; uzerine yazmayalim.
        if not demo.test_siparisi_mi(o.platform_order_id):
            o.courier_name = kuryeler.get(o.platform_order_id)

    result.sort(key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
    return result


@router.get("/cancel-reasons")
async def cancel_reasons(user: UserInfo = Depends(current_user)) -> list[dict]:
    """Iptal sebepleri.

    Dokuman: UnSupplied servisinde YALNIZCA bu sebepler kullanilabilir;
    platform kaynakli sebepler (musteri iptali vb.) buraya girmez.
    """
    return CANCEL_REASONS


@router.get("/sync-status")
async def sync_status(user: UserInfo = Depends(current_user)) -> dict:
    """Platform yoklamasinin durumu.

    Liste artik yerel kayittan geldigi icin her zaman doluyor; baglanti
    koptugunu arayuz buradan ogreniyor (eskiden liste isteginin hata
    vermesinden anliyordu).
    """
    return yoklayici.durum([c.id for c in store.list_connections(user.id)])


class GordumRequest(BaseModel):
    order_ids: list[str] = Field(default_factory=list, max_length=200)


@router.post("/seen")
async def gordum(body: GordumRequest, user: UserInfo = Depends(current_user)) -> dict:
    """Yeni siparis uyarisindaki "Gordum". Kalici: yeniden acilista alarm tekrar calmaz."""
    gercekler = [i for i in body.order_ids if not demo.test_siparisi_mi(i)]
    for i in body.order_ids:
        if demo.test_siparisi_mi(i):
            demo.gorundu(i)
    await asyncio.to_thread(store.mark_orders_seen, gercekler)
    return {"ok": True}


@router.post("/demo", response_model=Order)
async def test_siparisi(
    kapida: bool = True, user: UserInfo = Depends(current_user)
) -> Order:
    """Ekrana bir test siparisi dusurur.

    Gercek siparis beklemeden butun akis denenebilsin diye: kart
    ekranda cikar, kabul edilir, fisi basilir, iptal edilir. Platforma
    hicbir istek gitmez, "TEST" rozetiyle isaretlidir.
    """
    siparis = demo.olustur(kapida=kapida)
    # Oto onay aciksa gercek sipariste oldugu gibi "yeni" asamasini hic
    # gostermeden kabul et; yoksa 12 sn'lik turu beklerken alarm calip
    # susuyordu ve test gercegi yansitmiyordu.
    await yoklayici.demo_otomatik_kabul()
    return demo.getir(siparis.platform_order_id) or siparis


@router.delete("/demo")
async def test_siparislerini_sil(user: UserInfo = Depends(current_user)) -> dict:
    return {"ok": True, "silinen": demo.temizle()}


class TeslimatAyari(BaseModel):
    taban_km: float = Field(default=3.0, gt=0, le=50)
    taban_ucret: float = Field(default=100.0, ge=0, le=10000)
    km_basi: float = Field(default=25.0, ge=0, le=1000)
    # 1.0 = ham kus ucusu. Sehir ici gercek yol icin ~1.3.
    yol_carpani: float = Field(default=1.3, ge=1.0, le=3.0)


@router.get("/delivery-settings", response_model=TeslimatAyari)
async def teslimat_ayari(user: UserInfo = Depends(current_user)) -> TeslimatAyari:
    return TeslimatAyari(**ayarlar.oku().get("teslimat", {}))


@router.put("/delivery-settings", response_model=TeslimatAyari)
async def teslimat_ayari_kaydet(
    body: TeslimatAyari, user: UserInfo = Depends(current_user)
) -> TeslimatAyari:
    """Paket ucreti kurallari.

    Kural: taban_km'ye kadar taban_ucret, sonrasinda her BASLAYAN
    kilometre icin km_basi. Mesafe kus ucusu hesaplanip yol_carpani
    ile carpiliyor -- platform mesafeyi hic vermiyor.
    """
    ayarlar.yaz({"teslimat": body.model_dump()})
    # Onbellek mesafeyi degil konumu tutuyor; yeni kural bir sonraki
    # yoklamada zaten uygulaniyor.
    return body


class SiparisAyari(BaseModel):
    hazirlik_dk: int = Field(default=20, ge=1, le=180)
    oto_onay: bool = False
    alarm_sessiz: bool = False


@router.get("/settings", response_model=SiparisAyari)
async def siparis_ayari(user: UserInfo = Depends(current_user)) -> SiparisAyari:
    return SiparisAyari(**ayarlar.oku().get("siparis", {}))


class SiparisAyariDegisikligi(BaseModel):
    """Yalnizca gonderilen alanlar degisir.

    Alarm dugmesi ile sure secimi ayri yerlerden kaydediliyor; tam
    govde isteseydik biri digerini varsayilana dondururdu.
    """

    hazirlik_dk: int | None = Field(default=None, ge=1, le=180)
    oto_onay: bool | None = None
    alarm_sessiz: bool | None = None


@router.put("/settings", response_model=SiparisAyari)
async def siparis_ayari_kaydet(
    body: SiparisAyariDegisikligi, user: UserInfo = Depends(current_user)
) -> SiparisAyari:
    """Hazirlik suresi, oto onay ve alarm. Sunucuda: yeniden acilista kaybolmasin."""
    mevcut = SiparisAyari(**ayarlar.oku().get("siparis", {}))
    yeni = mevcut.model_copy(update=body.model_dump(exclude_none=True))
    ayarlar.yaz({"siparis": yeni.model_dump()})
    if yeni.oto_onay and not mevcut.oto_onay:
        # Switch acilir acilmaz bekleyen yeni siparisler de alinsin;
        # 12 saniyelik turu beklemeye gerek yok.
        yoklayici.arkada_tazele()
    return yeni


class KuryeListesi(BaseModel):
    couriers: list[str] = []


@router.get("/couriers", response_model=KuryeListesi)
async def kuryeler(user: UserInfo = Depends(current_user)) -> KuryeListesi:
    return KuryeListesi(couriers=ayarlar.oku().get("kuryeler", []))


@router.put("/couriers", response_model=KuryeListesi)
async def kuryeleri_kaydet(
    body: KuryeListesi, user: UserInfo = Depends(current_user)
) -> KuryeListesi:
    """Kendi kurye listemiz. Bos adlar ve tekrarlar ayiklaniyor."""
    temiz: list[str] = []
    for ad in body.couriers:
        ad = ad.strip()[:60]
        if ad and ad not in temiz:
            temiz.append(ad)
    if len(temiz) > 50:
        raise HTTPException(400, "En fazla 50 kurye tanimlanabilir.")
    ayarlar.yaz({"kuryeler": temiz})
    return KuryeListesi(couriers=temiz)


class CourierRequest(BaseModel):
    # Bos ad atamayi kaldirir.
    courier: str = ""


@router.put("/{connection_id}/{order_id}/courier")
async def set_courier(
    connection_id: int,
    order_id: str,
    body: CourierRequest,
    user: UserInfo = Depends(current_user),
) -> dict:
    """Siparise kendi kuryemizi atar.

    Platforma gonderilmiyor: platform kendi kurye havuzunu yonetmiyor
    (Model 1'de teslimat bizde). Bu kayit bizim icin -- fise basiliyor
    ve ekranda gorunuyor.
    """
    ad = body.courier.strip()[:60]
    if demo.test_siparisi_mi(order_id):
        demo.kurye_ata(order_id, ad)
    else:
        store.set_order_courier(order_id, ad)
    return {"ok": True, "courier": ad or None}


class AcceptRequest(BaseModel):
    preparation_minutes: int = 20


class CancelRequest(BaseModel):
    # Bos birakilirsa siparisin TAMAMI iptal edilir.
    item_ids: list[str] = []
    reason_id: int


class ActionResponse(BaseModel):
    """Islem sonucu -- ekranda gosterilecek deger platformdan gelir."""

    ok: bool = True
    confirmed: bool
    status: OrderStatus
    order: Order | None = None


# Statu degisikligi platformda aninda gorunmuyor; menu tarafinda
# ogrendigimiz ders burada da gecerli: iyimser gosterip sonra
# duzeltmek yerine platform istenen statuyu bildirene kadar
# bekliyoruz. Tek paket sorgusu ucuz oldugu icin bu maliyetli degil.
_BEKLEME = (0.4, 0.8, 1.2, 2.0)


def _statu(hedef: set[OrderStatus]) -> Callable[[Order], bool]:
    return lambda o: o.status in hedef


async def _dogrula(
    adapter, order_id: str, oldu_mu: Callable[[Order], bool]
) -> tuple[OrderStatus, Order | None, bool]:
    """Istenen degisiklik platformda gorunene kadar bekler.

    Olcut disaridan geliyor: statu degisikliklerinde "statu su oldu
    mu", kismi iptalde "su urunler iptal gorunuyor mu". Kismi iptalde
    siparis statusu hic degismeyebiliyor -- statuye bakmak yanlis
    alarm uretiyordu.
    """
    son: Order | None = None
    durum = OrderStatus.UNKNOWN
    for bekle in _BEKLEME:
        await asyncio.sleep(bekle)
        try:
            son = await adapter.fetch_order(order_id)
        except PlatformError:
            continue
        if son is None:
            return durum, None, False
        durum = son.status
        if oldu_mu(son):
            return durum, son, True
    return durum, son, False


# Test siparisinde hangi islem hangi statuye goturur.
_DEMO_GECIS = {
    "accept_order": OrderStatus.PREPARING,
    "mark_ready": OrderStatus.READY,
    "mark_shipped": OrderStatus.ON_THE_WAY,
    "mark_delivered": OrderStatus.DELIVERED,
}


def _demo_islem(fn_name: str, args: tuple) -> ActionResponse:
    """Test siparisini yerel olarak ilerletir; platforma gitmez."""
    order_id = args[0]
    if fn_name == "cancel_order":
        siparis = demo.iptal(order_id, list(args[1]))
    else:
        hedef = _DEMO_GECIS.get(fn_name)
        siparis = demo.durum_degistir(order_id, hedef) if hedef else demo.getir(order_id)
    if siparis is None:
        raise HTTPException(404, "Test siparisi bulunamadi.")
    if fn_name == "accept_order" and len(args) > 1:
        siparis.preparation_minutes = int(args[1])
    return ActionResponse(confirmed=True, status=siparis.status, order=siparis)


async def _islem(
    connection_id: int,
    user_id: int,
    fn_name: str,
    args: tuple,
    oldu_mu: Callable[[Order], bool],
) -> ActionResponse:
    if demo.test_siparisi_mi(str(args[0])):
        return _demo_islem(fn_name, args)

    adapter = await _adapter(connection_id, user_id)
    try:
        await getattr(adapter, fn_name)(*args)
        durum, order, tamam = await _dogrula(adapter, args[0], oldu_mu)
        if order is not None:
            order.connection_id = connection_id
            # Yeni durum hemen kayda gecsin; bir sonraki yoklamayi
            # beklersek liste bir an eski statuyu gosterir. Kayittan
            # geri okuyoruz: mesafe/ucret ve kurye oradan geliyor.
            await yoklayici.kaydet([order])
            kayitli = await asyncio.to_thread(store.get_order, order.platform_order_id)
            if kayitli is not None:
                kayitli.courier_name = store.get_order_couriers(
                    [kayitli.platform_order_id]
                ).get(kayitli.platform_order_id)
                order = kayitli
        return ActionResponse(confirmed=tamam, status=durum, order=order)
    except AuthError as exc:
        await havuz.unut(connection_id)
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc


@router.put("/{connection_id}/{order_id}/accept", response_model=ActionResponse)
async def accept(
    connection_id: int,
    order_id: str,
    body: AcceptRequest,
    user: UserInfo = Depends(current_user),
) -> ActionResponse:
    if not 1 <= body.preparation_minutes <= 180:
        raise HTTPException(400, "Hazirlik suresi 1-180 dakika arasinda olmali.")
    return await _islem(
        connection_id,
        user.id,
        "accept_order",
        (order_id, body.preparation_minutes),
        _statu({OrderStatus.PREPARING, OrderStatus.ACCEPTED}),
    )


@router.put("/{connection_id}/{order_id}/ready", response_model=ActionResponse)
async def ready(
    connection_id: int,
    order_id: str,
    user: UserInfo = Depends(current_user),
) -> ActionResponse:
    return await _islem(
        connection_id, user.id, "mark_ready", (order_id,), _statu({OrderStatus.READY})
    )


@router.put("/{connection_id}/{order_id}/shipped", response_model=ActionResponse)
async def shipped(
    connection_id: int,
    order_id: str,
    user: UserInfo = Depends(current_user),
) -> ActionResponse:
    return await _islem(
        connection_id, user.id, "mark_shipped", (order_id,), _statu({OrderStatus.ON_THE_WAY})
    )


@router.put("/{connection_id}/{order_id}/delivered", response_model=ActionResponse)
async def delivered(
    connection_id: int,
    order_id: str,
    user: UserInfo = Depends(current_user),
) -> ActionResponse:
    return await _islem(
        connection_id, user.id, "mark_delivered", (order_id,), _statu({OrderStatus.DELIVERED})
    )


@router.put("/{connection_id}/{order_id}/cancel", response_model=ActionResponse)
async def cancel(
    connection_id: int,
    order_id: str,
    body: CancelRequest,
    user: UserInfo = Depends(current_user),
) -> ActionResponse:
    """Siparisi (kismen) iptal eder.

    item_ids bos gelirse siparisteki TUM packageItemId'ler gonderilir --
    dokumanin "full iptal" tarifi bu. Kismi iptalde siparis UnSupplied
    olmayabilecegi icin hedef statuye Picking/Invoiced de dahil.
    """
    gecerli = {r["id"] for r in CANCEL_REASONS}
    if body.reason_id not in gecerli:
        raise HTTPException(
            400,
            "Bu iptal sebebi restoran kaynakli iptallerde kullanilamaz. "
            f"Gecerli sebepler: {sorted(gecerli)}",
        )

    item_ids = body.item_ids
    tam_iptal = False
    if not item_ids and demo.test_siparisi_mi(order_id):
        mevcut = demo.getir(order_id)
        if mevcut is None:
            raise HTTPException(404, "Test siparisi bulunamadi.")
        item_ids = [i for line in mevcut.items if not line.cancelled for i in line.item_ids]
        tam_iptal = True
    elif not item_ids:
        adapter = await _adapter(connection_id, user.id)
        mevcut = await adapter.fetch_order(order_id)
        if mevcut is None:
            raise HTTPException(404, "Siparis bulunamadi.")
        # Zaten iptal edilmis satirlari tekrar gondermiyoruz;
        # platform bunu hata sayabilir.
        item_ids = [
            i for line in mevcut.items if not line.cancelled for i in line.item_ids
        ]
        tam_iptal = True
        if not item_ids:
            raise HTTPException(400, "Sipariste iptal edilebilecek urun yok.")

    if tam_iptal:
        oldu_mu = _statu({OrderStatus.CANCELLED})
    else:
        # Kismi iptalde siparis yasamaya devam ediyor; statu hic
        # degismeyebilir. Dogru olcut: bu urunler iptal gorunuyor mu.
        istenen = set(item_ids)

        def oldu_mu(o: Order, _istenen: set[str] = istenen) -> bool:
            iptalli = {
                i for line in o.items if line.cancelled for i in line.item_ids
            }
            return _istenen <= iptalli

    return await _islem(
        connection_id,
        user.id,
        "cancel_order",
        (order_id, item_ids, body.reason_id),
        oldu_mu,
    )
