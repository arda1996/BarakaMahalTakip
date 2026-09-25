"""Yazici uclari: liste, ayar, test ve fis basma.

Yazici islerinin HEPSI ayri bir parcaciga aktariliyor (asyncio.to_thread).
Windows yazici API'si bloklayan bir API: spooler mesgulse ya da kapali
bir ag yazicisi listedeyse EnumPrinters/StartDocPrinter saniyelerce
donmuyor. Dogrudan istegin icinde cagrilinca olay dongusu duruyor ve
uygulamanin TAMAMI -- siparis yoklamasi dahil -- kilitleniyordu.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import ayarlar, yazici
from ..deps import current_user
from ..models import Order, UserInfo

log = logging.getLogger("tekeat.yazici")
router = APIRouter(prefix="/printer", tags=["yazici"])


class YaziciAyari(BaseModel):
    ad: str = ""
    genislik_mm: int = Field(default=80)
    kopya: int = Field(default=1, ge=1, le=5)
    otomatik: bool = True
    baslik: str = "TEKEAT"
    # 0 = ASCII (her yazicida calisir), digerleri ESC t n degeri.
    kod_sayfasi: int = 0
    # Fisin altina kurye icin rota QR'i.
    rota_qr: bool = True


@router.get("/list")
async def liste(user: UserInfo = Depends(current_user)) -> dict:
    """Yazicilar + kayitli ayar. Yazici katmani yoksa hata metniyle doner."""
    try:
        yazicilar = await asyncio.to_thread(yazici.yazicilari_listele)
        hata = None
    except Exception as exc:
        log.warning("Yazicilar listelenemedi: %s", exc)
        yazicilar, hata = [], "Yazicilar okunamadi. Windows yazici hizmeti calisiyor mu?"
    return {
        "printers": yazicilar,
        "settings": ayarlar.oku()["yazici"],
        "error": hata,
        "code_pages": [
            {"id": yazici.ASCII_SAYFA, "label": "ASCII - Turkce karakter yok (garantili)"},
            *[{"id": n, "label": ad} for n, _, ad in yazici.KOD_SAYFALARI],
        ],
    }


@router.put("/settings")
async def ayar_kaydet(
    body: YaziciAyari, user: UserInfo = Depends(current_user)
) -> dict:
    if body.genislik_mm not in yazici.GENISLIKLER:
        raise HTTPException(400, "Kagit genisligi 58 veya 80 mm olmali.")
    return ayarlar.yaz({"yazici": body.model_dump()})["yazici"]


def _yazici_sec(ayar: dict) -> str:
    hedef = ayar.get("ad") or ""
    if not hedef:
        try:
            varsayilan = [p for p in yazici.yazicilari_listele() if p["default"]]
            hedef = varsayilan[0]["name"] if varsayilan else ""
        except Exception as exc:
            log.warning("Varsayilan yazici okunamadi: %s", exc)
            raise HTTPException(500, "Yazicilar okunamadi. Windows yazici hizmeti calisiyor mu?") from exc
    if not hedef:
        raise HTTPException(
            400, "Yazici secilmemis ve Windows varsayilan yazicisi da yok."
        )
    return hedef


def _gonder(hedef: str, veri: bytes, ad: str, kopya: int = 1) -> dict:
    try:
        for _ in range(kopya):
            yazici.ham_yazdir(hedef, veri, is_adi=ad)
    except Exception as exc:
        log.exception("Yaziciya gonderilemedi")
        # Windows'un ham hata metni (pywintypes.error(...)) ekrana gitmez.
        raise HTTPException(502, "Fis yaziciya gonderilemedi. Yazici acik ve bagli mi?") from exc
    return {"ok": True, "printer": hedef, "bytes": len(veri)}


def _bas(order: Order) -> dict:
    ayar = ayarlar.oku()["yazici"]
    hedef = _yazici_sec(ayar)
    veri = yazici.fis_olustur(
        order,
        genislik_mm=int(ayar.get("genislik_mm", 80)),
        baslik=str(ayar.get("baslik") or "TEKEAT"),
        sayfa=int(ayar.get("kod_sayfasi", 0)),
        kurye=order.courier_name,
        rota_qr=bool(ayar.get("rota_qr", True)),
    )
    return _gonder(
        hedef, veri, f"Tekeat {order.order_code or ''}", int(ayar.get("kopya", 1))
    )


@router.post("/test")
async def test(user: UserInfo = Depends(current_user)) -> dict:
    """Ornek bir siparisi basar; kurulumu dogrulamanin en hizli yolu."""
    return await asyncio.to_thread(_bas, yazici.ornek_siparis())


@router.post("/print")
async def yazdir(order: Order, user: UserInfo = Depends(current_user)) -> dict:
    """Ekrandaki siparisin fisini basar."""
    return await asyncio.to_thread(_bas, order)


@router.post("/calibrate")
async def kalibrasyon(user: UserInfo = Depends(current_user)) -> dict:
    """Turkce karakter testi basar.

    Kagitta her kod sayfasi ayri bir satirda cikiyor; dogru gorunen
    satirin numarasi ayara giriliyor. Yazicinin hangi sayfayi
    destekledigini disaridan bilmenin baska yolu yok.
    """
    def calis() -> dict:
        ayar = ayarlar.oku()["yazici"]
        hedef = _yazici_sec(ayar)
        veri = yazici.kalibrasyon_fisi(genislik_mm=int(ayar.get("genislik_mm", 80)))
        return _gonder(hedef, veri, "Tekeat karakter testi")

    return await asyncio.to_thread(calis)


@router.post("/preview")
async def onizleme(order: Order | None = None, user: UserInfo = Depends(current_user)) -> dict:
    """Fisin metin halini doner.

    Yazici yokken ya da bir sorun ararken kagit harcamadan ne
    basilacagini gormek icin.
    """
    ayar = ayarlar.oku()["yazici"]
    hedef = order or yazici.ornek_siparis()
    veri = yazici.fis_olustur(
        hedef,
        genislik_mm=int(ayar.get("genislik_mm", 80)),
        baslik=str(ayar.get("baslik") or "TEKEAT"),
        sayfa=int(ayar.get("kod_sayfasi", 0)),
        kurye=hedef.courier_name,
        rota_qr=bool(ayar.get("rota_qr", True)),
        onizleme=True,
    )
    # Kontrol baytlarini ayiklayip okunur metne cevir.
    import re

    kodlama = yazici.KODLAMA.get(int(ayar.get("kod_sayfasi", 0)), "cp437")
    metin = veri.decode(kodlama, "replace")
    metin = re.sub(r"\x1b[@Eat].?|\x1d[!V].?.?|\x1b\x61.", "", metin)
    return {"text": metin, "bytes": len(veri)}
