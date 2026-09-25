"""Sistem uclari: pencereyi one alma ve uyanik kalma durumu."""

from fastapi import APIRouter, Depends

from .. import sistem
from ..deps import current_user
from ..models import UserInfo

router = APIRouter(prefix="/system", tags=["sistem"])


@router.get("/status")
async def status(user: UserInfo = Depends(current_user)) -> dict:
    """Uyanik tutma calisiyor mu, en son ne zaman uykudan donuldu."""
    return sistem.durum()


@router.post("/focus")
async def focus(user: UserInfo = Depends(current_user)) -> dict:
    """Pencereyi one getirir.

    Yeni siparis geldiginde arayuz bunu cagiriyor: uygulama arkada
    kalmissa tam ekran uyari kimseye gorunmuyordu.
    """
    return {"ok": sistem.one_al()}
