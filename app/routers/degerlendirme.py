"""Musteri degerlendirmeleri: liste, ozet, cevap.

Kaynak yerel kayit (app/degerlendirme.py platformdan cekiyor). Cevap
ise dogrudan platforma gidiyor ve MUSTERIYE GORUNUR (platform
onayindan sonra); geri alinamaz.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import degerlendirme, havuz, store
from ..hatalar import kullanici_mesaji
from ..adapters.base import AuthError, PlatformError, ReviewsUnsupported
from ..deps import current_user
from ..models import Review, UserInfo

router = APIRouter(prefix="/reviews", tags=["degerlendirme"])


def _baglantilar(user: UserInfo) -> list[int]:
    return [c.id for c in store.list_connections(user.id)]


class YorumSiparisi(BaseModel):
    order_id: str
    order_code: str | None = None
    customer_name: str | None = None
    customer_id: int | None = None
    total_price: float | None = None


class YorumSatiri(Review):
    siparis: YorumSiparisi | None = None


class YorumListesi(BaseModel):
    reviews: list[YorumSatiri]
    toplam: int


@router.get("", response_model=YorumListesi)
async def yorumlar(
    platform: str | None = None,
    filtre: str | None = Query(default=None, pattern="^(cevapsiz|dusuk|yorumlu|reddedilen)$"),
    arama: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserInfo = Depends(current_user),
) -> YorumListesi:
    d = await asyncio.to_thread(
        store.list_reviews, _baglantilar(user), platform, filtre, arama, limit, offset
    )
    return YorumListesi(**d)


@router.get("/summary")
async def ozet(user: UserInfo = Depends(current_user)) -> list[dict]:
    return await asyncio.to_thread(store.review_summary, _baglantilar(user))


@router.get("/sync-status")
async def senkron_durumu(user: UserInfo = Depends(current_user)) -> dict:
    return degerlendirme.durum(_baglantilar(user))


@router.post("/sync")
async def senkron(user: UserInfo = Depends(current_user)) -> dict:
    """Platformdan hemen cek (sayfadaki Yenile)."""
    await degerlendirme.senkron()
    return degerlendirme.durum(_baglantilar(user))


class CevapIstek(BaseModel):
    # Bos ya da tek karakterlik cevap platformda reddediliyor; ust sinir
    # dokumanda yok, makul bir tavan.
    text: str = Field(min_length=2, max_length=1000)


class CevapSonucu(BaseModel):
    review: Review
    # Platform cevabi gosterdi mi (onay bekliyor olabilir).
    confirmed: bool


@router.post("/{review_id}/answer", response_model=CevapSonucu)
async def cevapla(
    review_id: str, body: CevapIstek, user: UserInfo = Depends(current_user)
) -> CevapSonucu:
    r = await asyncio.to_thread(store.get_review, review_id)
    if r is None or r.connection_id not in _baglantilar(user):
        raise HTTPException(404, "Degerlendirme bulunamadi.")
    if r.answer_text and r.answer_status.value in ("approved", "waiting"):
        # Platform tek cevap kabul ediyor; ikinci gonderim hata ya da
        # kopya uretir. Reddedilen cevap yeniden yazilabiliyor.
        raise HTTPException(409, "Bu yoruma zaten cevap verilmis.")
    try:
        son, tamam = await degerlendirme.cevapla(user.id, review_id, body.text.strip())
    except KeyError as exc:
        raise HTTPException(404, "Degerlendirme bulunamadi.") from exc
    except ReviewsUnsupported as exc:
        raise HTTPException(409, kullanici_mesaji(exc)) from exc
    except AuthError as exc:
        await havuz.unut(r.connection_id)
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc
    return CevapSonucu(review=son, confirmed=tamam)
