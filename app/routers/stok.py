"""Stok, receteler, maliyet ayarlari ve siparis basina kar.

Bu modul platforma HICBIR SEY YAZMAZ. Recete, malzeme, alis, fiyat
yalnizca Tekeat'in yerel veritabaninda. Platformla tek temas "Menuden
aktar": menuyu OKUR (fetch_menu). Buraya platforma yazan bir cagri
eklenmemeli; test_stok_platform.py bunu sahte adapterle denetliyor.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import ayarlar, havuz, stok, store
from ..hatalar import kullanici_mesaji
from ..adapters.base import AuthError, PlatformError
from ..deps import current_user
from ..models import UserInfo

router = APIRouter(prefix="/stock", tags=["stok"])


def _cevir(fn, *a):
    """stok.py hatalarini HTTP'ye cevirir."""
    try:
        return fn(*a)
    except KeyError as exc:
        raise HTTPException(404, "Kayit bulunamadi.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class KalemIstek(BaseModel):
    ad: str = Field(min_length=1, max_length=80)
    birim: str = Field(pattern="^(g|ml|adet)$")
    # net: kesin miktar. destek: recetede aralik, ust sinir dusulur. Maliyet ikisinde de FIFO.
    takip: str = Field(default="net", pattern="^(net|destek)$")
    kategori: str | None = Field(default=None, max_length=40)
    min_seviye: float | None = Field(default=None, ge=0)
    platform_malzeme: list[str] = Field(default_factory=list)


def _kim(user: UserInfo) -> str:
    """Denetim kaydinda gorunen kullanici: gorunen ad, yoksa kullanici adi."""
    return user.display_name or user.username


@router.get("/items")
async def kalemler(user: UserInfo = Depends(current_user)) -> list[dict]:
    return await asyncio.to_thread(stok.kalemler)


@router.post("/items")
async def kalem_ekle(body: KalemIstek, user: UserInfo = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_cevir, stok.kalem_kaydet, body.model_dump(), None, _kim(user))


@router.put("/items/{kalem_id}")
async def kalem_guncelle(kalem_id: int, body: KalemIstek, user: UserInfo = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_cevir, stok.kalem_kaydet, body.model_dump(), kalem_id, _kim(user))


@router.delete("/items/{kalem_id}", status_code=204)
async def kalem_sil(kalem_id: int, sebep: str | None = None, user: UserInfo = Depends(current_user)) -> None:
    """Hareketi olan malzemede sebep zorunlu (denetim kaydi)."""
    await asyncio.to_thread(_cevir, stok.kalem_sil, kalem_id, sebep, _kim(user))


@router.get("/audit")
async def denetim(
    kalem_id: int | None = None, hareket_id: int | None = None, limit: int = 500,
    user: UserInfo = Depends(current_user),
) -> list[dict]:
    """Stok denetim kaydi (degistirilemez), en yenisi once."""
    return await asyncio.to_thread(stok.denetim_kayitlari, kalem_id, hareket_id, min(max(limit, 1), 5000))


class HareketIstek(BaseModel):
    tur: str = Field(pattern="^(alis|cikis|fire|sayim)$")
    # Temel birimde (g/ml/adet). Sayimda sayilan TOPLAM miktar.
    miktar: float = Field(ge=0, le=10_000_000)
    # Temel birimin fiyati (TL/g, TL/adet). Alista zorunlu.
    birim_fiyat: float | None = Field(default=None, ge=0, le=1_000_000)
    aciklama: str | None = Field(default=None, max_length=200)


@router.post("/items/{kalem_id}/movements")
async def hareket(kalem_id: int, body: HareketIstek, user: UserInfo = Depends(current_user)) -> dict:
    return await asyncio.to_thread(
        _cevir, stok.hareket_ekle, kalem_id, body.tur, body.miktar, body.birim_fiyat, body.aciklama, _kim(user)
    )


@router.get("/items/{kalem_id}/batches")
async def partiler(kalem_id: int, user: UserInfo = Depends(current_user)) -> list[dict]:
    """Malzemenin FIFO partileri (tukenenler dahil), en eskisi once."""
    return await asyncio.to_thread(_cevir, stok.partiler, kalem_id)


@router.post("/recalculate")
async def yeniden_hesapla(kalem_id: int | None = None, user: UserInfo = Depends(current_user)) -> dict:
    """FIFO'yu bastan oynatir: partiler, tuketim tutarlari ve siparis maliyetleri."""
    return await asyncio.to_thread(_cevir, stok.yeniden_hesapla, kalem_id)


class DuzeltIstek(BaseModel):
    # Temel birimde, pozitif. Alista birim_fiyat (TL/g, TL/adet) zorunlu.
    miktar: float = Field(gt=0, le=10_000_000)
    birim_fiyat: float | None = Field(default=None, ge=0, le=1_000_000)
    # Denetim kaydi icin zorunlu (bos/kisa ise stok.py 400 doner).
    sebep: str | None = Field(default=None, max_length=300)


class GeriAlIstek(BaseModel):
    sebep: str | None = Field(default=None, max_length=300)


@router.put("/movements/{hareket_id}")
async def hareket_duzelt(hareket_id: int, body: DuzeltIstek, user: UserInfo = Depends(current_user)) -> dict:
    """Yanlis girilmis alis/cikis/fire'yi yerinde duzeltir ve yeniden hesaplar."""
    return await asyncio.to_thread(
        _cevir, stok.hareket_duzelt, hareket_id, body.miktar, body.birim_fiyat, body.sebep, _kim(user)
    )


@router.post("/movements/{hareket_id}/undo")
async def hareket_geri_al(
    hareket_id: int, body: GeriAlIstek | None = None, user: UserInfo = Depends(current_user)
) -> dict:
    """Elle girilen hareketi ters duzeltmeyle geri alir (kayit silinmez). Sebep zorunlu."""
    return await asyncio.to_thread(
        _cevir, stok.hareket_geri_al, hareket_id, body.sebep if body else None, _kim(user)
    )


@router.get("/movements")
async def hareketler(
    kalem_id: int | None = None, limit: int = 200, user: UserInfo = Depends(current_user)
) -> list[dict]:
    return await asyncio.to_thread(stok.hareketler, kalem_id, min(max(limit, 1), 1000))


# ---------------- receteler


@router.get("/recipes")
async def receteler(user: UserInfo = Depends(current_user)) -> list[dict]:
    return await asyncio.to_thread(stok.receteler)


class ReceteIstek(BaseModel):
    urun_adi: str = Field(min_length=1, max_length=120)
    tur: str = Field(default="urun", pattern="^(urun|opsiyon)$")
    platform: str = "tgo"


@router.post("/recipes")
async def recete_ekle(body: ReceteIstek, user: UserInfo = Depends(current_user)) -> dict:
    rid = await asyncio.to_thread(stok.recete_olustur, body.platform, None, body.urun_adi.strip(), body.tur)
    return {"id": rid}


class ReceteSatiri(BaseModel):
    kalem_id: int
    # Stoktan dusulen porsiyon miktari; destek kalemde araligin ust siniri.
    miktar: float = Field(gt=0, le=1_000_000)
    miktar_min: float | None = Field(default=None, gt=0, le=1_000_000)
    ekstra_miktar: float | None = Field(default=None, ge=0, le=1_000_000)


class ReceteSatirlari(BaseModel):
    satirlar: list[ReceteSatiri] = Field(max_length=100)


@router.put("/recipes/{recete_id}")
async def recete_kaydet(recete_id: int, body: ReceteSatirlari, user: UserInfo = Depends(current_user)) -> dict:
    await asyncio.to_thread(
        _cevir, stok.recete_satirlarini_kaydet, recete_id, [s.model_dump() for s in body.satirlar]
    )
    return {"ok": True}


@router.get("/recipes/{recete_id}/history")
async def recete_gecmisi(recete_id: int, user: UserInfo = Depends(current_user)) -> list[dict]:
    return await asyncio.to_thread(stok.recete_gecmisi, recete_id)


@router.post("/recipes/{recete_id}/history/{gecmis_id}/restore")
async def recete_geri_yukle(recete_id: int, gecmis_id: int, user: UserInfo = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_cevir, stok.recete_geri_yukle, recete_id, gecmis_id)


@router.delete("/recipes/{recete_id}", status_code=204)
async def recete_sil(recete_id: int, user: UserInfo = Depends(current_user)) -> None:
    await asyncio.to_thread(stok.recete_sil, recete_id)


@router.post("/recipes/{recete_id}/ready-made")
async def hazir_urun(recete_id: int, user: UserInfo = Depends(current_user)) -> dict:
    """Aynen satilan urun (kutu icecek vb.): kendi adiyla 'adet' kalemi, recete 1 adet."""
    return await asyncio.to_thread(_cevir, stok.hazir_urun, recete_id)


class MenuAktarIstek(BaseModel):
    connection_id: int
    store_id: str


@router.post("/import-menu")
async def menuden_aktar(body: MenuAktarIstek, user: UserInfo = Depends(current_user)) -> dict:
    """Menudeki malzemeleri stok kalemi, urunleri bos recete olarak ekler (tekrar guvenli)."""
    if body.connection_id not in {c.id for c in store.list_connections(user.id)}:
        raise HTTPException(404, "Baglanti bulunamadi.")
    try:
        adapter = await havuz.edin(body.connection_id, user.id)
        menu = await adapter.fetch_menu(body.store_id)
    except AuthError as exc:
        await havuz.unut(body.connection_id)
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc
    return await asyncio.to_thread(stok.menuden_aktar, adapter.platform.value, menu)


# ---------------- maliyet ayarlari ve siparis kari


class MaliyetAyari(BaseModel):
    komisyon: dict[str, float] = Field(default_factory=dict)
    ambalaj_tl: float = Field(default=0.0, ge=0, le=10_000)
    stok_baslangic: str | None = None


@router.get("/settings", response_model=MaliyetAyari)
async def maliyet_ayari(user: UserInfo = Depends(current_user)) -> MaliyetAyari:
    return MaliyetAyari(**ayarlar.oku().get("maliyet", {}))


@router.put("/settings", response_model=MaliyetAyari)
async def maliyet_ayari_kaydet(body: MaliyetAyari, user: UserInfo = Depends(current_user)) -> MaliyetAyari:
    for p, oran in body.komisyon.items():
        if p not in ("tgo", "yemeksepeti", "migros") or not 0 <= oran <= 100:
            raise HTTPException(400, "Komisyon platform basina 0-100 arasi yuzde olmali.")
    mevcut = ayarlar.oku().get("maliyet", {})
    # stok_baslangic yalnizca ilk kalemle atanir; buradan geri alinmasin.
    yeni = {"komisyon": {**mevcut.get("komisyon", {}), **body.komisyon}, "ambalaj_tl": body.ambalaj_tl}
    return MaliyetAyari(**ayarlar.yaz({"maliyet": yeni})["maliyet"])


@router.get("/order-cost/{order_id}")
async def siparis_kari(order_id: str, user: UserInfo = Depends(current_user)) -> dict:
    o = await asyncio.to_thread(store.get_order, order_id)
    if o is None or o.connection_id not in {c.id for c in store.list_connections(user.id)}:
        raise HTTPException(404, "Siparis bulunamadi.")
    return await asyncio.to_thread(stok.siparis_kari, o)
