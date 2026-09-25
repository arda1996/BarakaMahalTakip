"""Menu yonetimi: icerik, fiyat, satisa acma/kapama.

Bu rotalar platformdaki GERCEK menuyu degistirir. Yerel bir kopya tutup
sonra senkronlamak yerine her degisiklikten sonra menuyu platformdan
yeniden cekiyoruz; ekranda gorulen sey her zaman musterinin gordugu sey.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from .. import havuz, store
from ..hatalar import kullanici_mesaji
from ..adapters.base import AuthError, PlatformAdapter, PlatformError
from ..deps import current_user
from ..models import BatchResult, Menu, PriceChange, UserInfo

router = APIRouter(prefix="/menu", tags=["menu"])


async def _adapter(connection_id: int, user_id: int) -> PlatformAdapter:
    """Havuzdaki adapter; istemci baglantilar arasinda yasiyor."""
    try:
        return await havuz.edin(connection_id, user_id)
    except KeyError as exc:
        raise HTTPException(404, "Baglanti bulunamadi.") from exc


async def _calistir(connection_id: int, user_id: int, islem):
    """Adapter'i ac, isi yap, her durumda kapat; hatalari HTTP'ye cevir."""
    adapter = await _adapter(connection_id, user_id)
    try:
        return await islem(adapter)
    except AuthError as exc:
        await havuz.unut(connection_id)
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc


@router.get("", response_model=Menu)
async def get_menu(
    connection_id: int,
    store_id: str,
    user: UserInfo = Depends(current_user),
) -> Menu:
    menu = await _calistir(
        connection_id, user.id, lambda a: a.fetch_menu(store_id)
    )
    menu.connection_id = connection_id
    # Panelde eklenen urunler taslaklarla burada eslesiyor: menu zaten
    # elimizde, ayrica platforma gitmeye gerek yok.
    urunler = [
        (p.id, p.name)
        for p in [*(p for s in menu.sections for p in s.products), *menu.orphan_products]
    ]
    await asyncio.to_thread(store.match_drafts, connection_id, store_id, urunler)
    return menu


# ---------------------------------------------------------------
# Yeni urun taslaklari
#
# Trendyol Yemek API'sinde urun/kategori/opsiyon OLUSTURMA servisi yok
# (dokumandaki menu servisleri: okuma, ac/kapa, fiyat). Urun satici
# panelinden ekleniyor. Biz urunu platformun yapisiyla birebir
# hazirliyoruz -- var olan malzeme ve opsiyon gruplari KIMLIGIYLE
# baglaniyor ki panelde ayni kayitlar secilsin, yenisi acilmasin.
# ---------------------------------------------------------------


class TaslakMalzeme(BaseModel):
    # Var olan malzeme/urun/grubun platform kimligi; yeniyse bos.
    id: str | None = None
    ad: str = Field(min_length=1, max_length=80)


class TaslakFiyatli(TaslakMalzeme):
    fiyat: float = Field(default=0.0, ge=0, le=10000)


class TaslakGrup(BaseModel):
    id: str | None = None
    ad: str = Field(min_length=1, max_length=80)
    min: int = Field(default=0, ge=0, le=50)
    max: int = Field(default=1, ge=1, le=50)
    secenekler: list[TaslakFiyatli] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _sinirlar(self):
        # Platformdaki kural: musteri en az `min`, en fazla `max` secer.
        # min > max ya da secenekten fazla zorunlu secim panelde de
        # reddediliyor; burada yakalamak panelde vakit kaybettirmiyor.
        if self.min > self.max:
            raise ValueError(f"'{self.ad}': en az ({self.min}) en fazladan ({self.max}) buyuk olamaz.")
        if self.min > len(self.secenekler):
            raise ValueError(f"'{self.ad}': en az {self.min} secim icin yeterli secenek yok.")
        return self


class TaslakIcerik(BaseModel):
    # Musterinin cikarabilecegi malzemeler (siparisteki "cikar").
    malzemeler: list[TaslakMalzeme] = Field(default_factory=list, max_length=100)
    # Ucretli ekstralar (siparisteki "ekle").
    ekstralar: list[TaslakFiyatli] = Field(default_factory=list, max_length=100)
    opsiyon_gruplari: list[TaslakGrup] = Field(default_factory=list, max_length=30)


class TaslakIstek(BaseModel):
    section_id: str | None = None
    section_name: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    price: float = Field(gt=0, le=100000)
    icerik: TaslakIcerik = Field(default_factory=TaslakIcerik)


class Taslak(TaslakIstek):
    id: int
    connection_id: int
    store_id: str
    # Panelde eklenip menude bulunan urun; bos ise henuz eklenmemis.
    eslesen_urun_id: str | None = None
    created_at: str
    updated_at: str


def _baglanti_kontrol(connection_id: int, user: UserInfo) -> None:
    if connection_id not in {c.id for c in store.list_connections(user.id)}:
        raise HTTPException(404, "Baglanti bulunamadi.")


@router.get("/{connection_id}/{store_id}/drafts", response_model=list[Taslak])
async def taslaklar(
    connection_id: int, store_id: str, user: UserInfo = Depends(current_user)
) -> list[Taslak]:
    _baglanti_kontrol(connection_id, user)
    satirlar = await asyncio.to_thread(store.list_drafts, connection_id, store_id)
    return [Taslak(**s) for s in satirlar]


@router.post("/{connection_id}/{store_id}/drafts", response_model=Taslak)
async def taslak_ekle(
    connection_id: int,
    store_id: str,
    body: TaslakIstek,
    user: UserInfo = Depends(current_user),
) -> Taslak:
    _baglanti_kontrol(connection_id, user)
    s = await asyncio.to_thread(
        store.save_draft, connection_id, store_id, body.model_dump()
    )
    return Taslak(**s)


@router.put("/{connection_id}/{store_id}/drafts/{taslak_id}", response_model=Taslak)
async def taslak_guncelle(
    connection_id: int,
    store_id: str,
    taslak_id: int,
    body: TaslakIstek,
    user: UserInfo = Depends(current_user),
) -> Taslak:
    _baglanti_kontrol(connection_id, user)
    s = await asyncio.to_thread(
        store.save_draft, connection_id, store_id, body.model_dump(), taslak_id
    )
    if s is None:
        raise HTTPException(404, "Taslak bulunamadi.")
    return Taslak(**s)


@router.delete("/{connection_id}/{store_id}/drafts/{taslak_id}", status_code=204)
async def taslak_sil(
    connection_id: int,
    store_id: str,
    taslak_id: int,
    user: UserInfo = Depends(current_user),
) -> None:
    _baglanti_kontrol(connection_id, user)
    if not await asyncio.to_thread(store.delete_draft, connection_id, store_id, taslak_id):
        raise HTTPException(404, "Taslak bulunamadi.")


# Ayni sube icin es zamanli statu degisikligi platformda 409 uretiyor
# ("Aynı şube üzerinde eş zamanlı kategori veya ürün statüsü değişikliği
# yapılıyor"). Kullanici iki anahtari arka arkaya cevirdiginde istekler
# ust uste biniyor, biri sessizce dusuyor ve anahtar "takiliyordu".
# Bu yuzden YAZMA isteklerini sube bazinda siraya aliyoruz.
_yazma_kilitleri: dict[str, asyncio.Lock] = {}


def _kilit(connection_id: int, store_id: str) -> asyncio.Lock:
    anahtar = f"{connection_id}:{store_id}"
    kilit = _yazma_kilitleri.get(anahtar)
    if kilit is None:
        kilit = asyncio.Lock()
        _yazma_kilitleri[anahtar] = kilit
    return kilit


class StatusRequest(BaseModel):
    active: bool
    # Toplu islemde her urun icin ayri ayri dogrulama yapmiyoruz;
    # sonunda tek bir menu cekimi hepsini birden teyit ediyor.
    confirm: bool = True


class StatusResponse(BaseModel):
    """Anahtarin ekranda ne gosterecegini ARTIK SUNUCU soyluyor.

    active: platformun su anda bildirdigi deger (tahmin degil).
    confirmed: platform istenen degeri gercekten dondurdu mu.
    """

    ok: bool = True
    active: bool
    confirmed: bool


def _menude_durum(menu: Menu, tip: str, item_id: str) -> bool | None:
    if tip == "section":
        for sec in menu.sections:
            if sec.id == item_id:
                return sec.active
        return None
    for sec in menu.sections:
        for p in sec.products:
            if p.id == item_id:
                return p.active
    for p in menu.orphan_products:
        if p.id == item_id:
            return p.active
    return None


# Platform degisikligi aninda yansitmiyor; PUT 200 dondukten saniyeler
# sonra menu servisi hala eski degeri verebiliyor. Once iyimser
# gosterip sonra "uzlastirmak" anahtarin geri sicramasina yol
# aciyordu. Artik platform istenen degeri dondurene kadar BEKLIYORUZ;
# arayuz gordugu seyden emin olabilsin.
# Ilk kontrol erken: platform cogu zaman ~1 sn icinde yeni degeri
# donduruyor, kullaniciyi bosuna bekletmeyelim. Sonrakiler
# uzuyor ki gec kalan durumlar da yakalansin (toplam ~6.8 sn).
_BEKLEME = (0.5, 0.8, 1.2, 1.8, 2.5)


async def _dogrula(
    adapter: PlatformAdapter, store_id: str, tip: str, item_id: str, istenen: bool
) -> tuple[bool, bool]:
    """(gorulen_deger, dogrulandi_mi) doner."""
    gorulen = istenen
    for bekle in _BEKLEME:
        await asyncio.sleep(bekle)
        try:
            menu = await adapter.fetch_menu(store_id)
        except PlatformError:
            continue
        su_an = _menude_durum(menu, tip, item_id)
        if su_an is None:
            return istenen, False
        gorulen = su_an
        if su_an == istenen:
            return su_an, True
    return gorulen, False


async def _durum_degistir(
    connection_id: int,
    user_id: int,
    store_id: str,
    tip: str,
    item_id: str,
    active: bool,
    confirm: bool,
) -> StatusResponse:
    adapter = await _adapter(connection_id, user_id)
    try:
        # Kilit yalnizca YAZMA suresince tutuluyor: dogrulama okumasi
        # 409 uretmiyor, onu kilidin disinda yapip diger anahtarlari
        # saniyelerce bekletmiyoruz.
        async with _kilit(connection_id, store_id):
            if tip == "section":
                await adapter.set_section_status(store_id, item_id, active)
            else:
                await adapter.set_product_status(store_id, item_id, active)

        if not confirm:
            return StatusResponse(active=active, confirmed=False)

        gorulen, tamam = await _dogrula(adapter, store_id, tip, item_id, active)
        return StatusResponse(active=gorulen, confirmed=tamam)
    except AuthError as exc:
        await havuz.unut(connection_id)
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc


@router.put(
    "/{connection_id}/{store_id}/products/{product_id}/status",
    response_model=StatusResponse,
)
async def product_status(
    connection_id: int,
    store_id: str,
    product_id: str,
    body: StatusRequest,
    user: UserInfo = Depends(current_user),
) -> StatusResponse:
    return await _durum_degistir(
        connection_id, user.id, store_id, "product", product_id, body.active, body.confirm
    )


@router.put(
    "/{connection_id}/{store_id}/sections/{section_id}/status",
    response_model=StatusResponse,
)
async def section_status(
    connection_id: int,
    store_id: str,
    section_id: str,
    body: StatusRequest,
    user: UserInfo = Depends(current_user),
) -> StatusResponse:
    return await _durum_degistir(
        connection_id, user.id, store_id, "section", section_id, body.active, body.confirm
    )


class PriceRequest(BaseModel):
    # Tek istekte en fazla 1000 urun (platform limiti).
    changes: list[PriceChange] = Field(min_length=1, max_length=1000)


class PriceResponse(BaseModel):
    batch_request_id: str


@router.post("/{connection_id}/{store_id}/prices", response_model=PriceResponse)
async def update_prices(
    connection_id: int,
    store_id: str,
    body: PriceRequest,
    user: UserInfo = Depends(current_user),
) -> PriceResponse:
    """Fiyatlari kuyruga atar.

    Sonuc hemen belli olmaz: donen batch_request_id ile /batch ucundan
    takip edilir.
    """
    batch_id = await _calistir(
        connection_id, user.id, lambda a: a.update_prices(store_id, body.changes)
    )
    return PriceResponse(batch_request_id=batch_id)


@router.get("/{connection_id}/batch/{batch_request_id}", response_model=BatchResult)
async def batch(
    connection_id: int,
    batch_request_id: str,
    user: UserInfo = Depends(current_user),
) -> BatchResult:
    return await _calistir(
        connection_id, user.id, lambda a: a.batch_result(batch_request_id)
    )
