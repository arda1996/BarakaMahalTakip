"""Gecmis siparisler ve musteri kaydi.

Kaynak yerel veritabani (yoklayicinin yazdigi `orders` tablosu).
Platform yalnizca son degisenleri donduruyor; gecmis ve musteri
bilgisi bizde tutulmazsa hic kimsede yok.
"""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import gecmis_aktarim, hatalar, store
from ..deps import current_user
from ..models import Order, OrderStatus, UserInfo

router = APIRouter(prefix="/history", tags=["gecmis"])


class Ozet(BaseModel):
    adet: int
    teslim_adet: int
    ciro: float
    iptal_adet: int
    ortalama_sepet: float


class GecmisCevabi(BaseModel):
    orders: list[Order]
    toplam: int
    ozet: Ozet


class Musteri(BaseModel):
    """Kayitli musteri. Platformun kalici musteri kimligiyle tekil."""

    id: int
    platform: str
    # Trendyol musteri no; ad maskeli oldugu icin kisiyi ayirt eden bu.
    platform_musteri_id: str | None = None
    ad: str | None = None
    telefon: str | None = None
    not_metni: str | None = None
    siparis_adet: int
    teslim_adet: int
    iptal_adet: int
    toplam_harcama: float
    ilk_siparis: str | None = None
    son_siparis: str | None = None
    adres_adet: int = 0
    son_adres: str | None = None


class MusteriAdresi(BaseModel):
    id: int
    adres: str
    mahalle: str | None = None
    ilce: str | None = None
    il: str | None = None
    enlem: float | None = None
    boylam: float | None = None
    siparis_adet: int = 0
    ilk_kullanim: str | None = None
    son_kullanim: str | None = None


class MusteriDetay(Musteri):
    adresler: list[MusteriAdresi] = []


class NotIstek(BaseModel):
    not_metni: str | None = Field(default=None, max_length=1000)


def _baglantilar(user: UserInfo) -> list[int]:
    return [c.id for c in store.list_connections(user.id)]


@router.get("/orders", response_model=GecmisCevabi)
async def gecmis_siparisler(
    baslangic: datetime | None = None,
    bitis: datetime | None = None,
    statuses: list[OrderStatus] = Query(default=[]),
    arama: str | None = Query(default=None, max_length=100),
    musteri: int | None = Query(default=None, description="Kayitli musteri (customers.id)"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: UserInfo = Depends(current_user),
) -> GecmisCevabi:
    sonuc = await asyncio.to_thread(
        store.order_history,
        _baglantilar(user),
        baslangic,
        bitis,
        statuses or None,
        arama or None,
        musteri,
        limit,
        offset,
    )
    kuryeler = store.get_order_couriers([o.platform_order_id for o in sonuc["orders"]])
    for o in sonuc["orders"]:
        o.courier_name = kuryeler.get(o.platform_order_id)
    return GecmisCevabi(**sonuc)


class AktarimDurumu(BaseModel):
    connection_id: int
    durum: str                     # bekliyor / calisiyor / bitti / hata
    taranan_sinir: str | None = None
    en_eski_siparis: str | None = None
    aktarilan: int = 0
    hata: str | None = None


@router.get("/import", response_model=list[AktarimDurumu])
async def aktarim_durumu(user: UserInfo = Depends(current_user)) -> list[AktarimDurumu]:
    """Platformdaki eski siparislerin aktarim ilerlemesi (baglanti basina)."""
    sonuc = []
    for cid in _baglantilar(user):
        k = await asyncio.to_thread(store.get_import, cid)
        if k is None:
            sonuc.append(AktarimDurumu(connection_id=cid, durum="bekliyor"))
            continue
        durum = k["durum"]
        # Kayitta "calisiyor" yazip gorev yoksa (uygulama kapanmis) bir
        # sonraki baslatmada devam edecek.
        if durum == "calisiyor" and not gecmis_aktarim.calisiyor_mu():
            durum = "bekliyor"
        sonuc.append(AktarimDurumu(
            connection_id=cid,
            durum=durum,
            taranan_sinir=k["taranan_sinir"],
            en_eski_siparis=k["en_eski_siparis"],
            aktarilan=k["aktarilan"],
            # Eski surumlerin kaydettigi ham platform cevabi ekrana gitmesin.
            hata=hatalar.temizle(k["hata"]),
        ))
    return sonuc


@router.post("/import")
async def aktarim_baslat(
    bastan: bool = False, user: UserInfo = Depends(current_user)
) -> dict:
    """Aktarimi baslatir. bastan=true: ilerlemeyi silip en bastan tarar.

    Kayitli siparisler silinmiyor; tekrar gelenler ustune yaziliyor.
    """
    if bastan:
        await gecmis_aktarim.yeniden_tara(_baglantilar(user))
        return {"ok": True, "basladi": True}
    return {"ok": True, "basladi": gecmis_aktarim.baslat()}


@router.get("/customers", response_model=list[Musteri])
async def musteriler(
    arama: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, ge=1, le=1000),
    user: UserInfo = Depends(current_user),
) -> list[Musteri]:
    satirlar = await asyncio.to_thread(
        store.customers, _baglantilar(user), arama or None, limit
    )
    return [Musteri(**s) for s in satirlar]


@router.get("/customers/{musteri_id}", response_model=MusteriDetay)
async def musteri_karti(
    musteri_id: int, user: UserInfo = Depends(current_user)
) -> MusteriDetay:
    d = await asyncio.to_thread(store.customer_detail, _baglantilar(user), musteri_id)
    if d is None:
        raise HTTPException(404, "Musteri bulunamadi.")
    return MusteriDetay(**d)


@router.put("/customers/{musteri_id}/note", response_model=MusteriDetay)
async def musteri_notu(
    musteri_id: int, body: NotIstek, user: UserInfo = Depends(current_user)
) -> MusteriDetay:
    """Restoranin musteri hakkindaki notu (yalnizca bizde; platforma gitmez)."""
    cids = _baglantilar(user)
    # Once gorebildigi bir musteri mi: baska hesabin musterisine not yazilmasin.
    if await asyncio.to_thread(store.customer_detail, cids, musteri_id) is None:
        raise HTTPException(404, "Musteri bulunamadi.")
    await asyncio.to_thread(store.set_customer_note, musteri_id, body.not_metni)
    return MusteriDetay(**await asyncio.to_thread(store.customer_detail, cids, musteri_id))
