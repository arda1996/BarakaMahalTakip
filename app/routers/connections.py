"""Giris / hesap baglama ucu.

Kullanicinin gordugu "giris ekrani" bu endpoint'e POST atar. Kimlik bilgileri
once gercek bir istekle dogrulanir, ancak dogrulanirsa sifrelenip kaydedilir.
Yanlis bilgiyle kayit olusmaz.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import store
from ..hatalar import kullanici_mesaji
from ..adapters.base import AuthError, PlatformError
from .. import havuz
from ..adapters.registry import build_adapter, supported_platforms
from ..deps import current_user
from ..models import ConnectionInfo, Credentials, Platform, Store, UserInfo

router = APIRouter(prefix="/connections", tags=["baglantilar"])


class ConnectRequest(BaseModel):
    platform: Platform
    label: str
    credentials: Credentials


class ConnectResponse(BaseModel):
    connection: ConnectionInfo
    details: dict


@router.get("/platforms")
async def platforms() -> list[Platform]:
    return supported_platforms()


@router.post("", response_model=ConnectResponse)
async def connect(
    payload: ConnectRequest, user: UserInfo = Depends(current_user)
) -> ConnectResponse:
    try:
        adapter = build_adapter(payload.platform, payload.credentials)
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        await adapter.authenticate()
        details = await adapter.verify()
    except AuthError as exc:
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc
    finally:
        await adapter.aclose()

    info = store.add_connection(
        payload.platform, payload.label, payload.credentials, user.id
    )
    store.mark_verified(info.id)
    return ConnectResponse(connection=info, details=details)


@router.get("", response_model=list[ConnectionInfo])
async def list_all(user: UserInfo = Depends(current_user)) -> list[ConnectionInfo]:
    return store.list_connections(user.id)


@router.post("/{connection_id}/verify")
async def verify(
    connection_id: int, user: UserInfo = Depends(current_user)
) -> dict:
    found = store.get_connection(connection_id, user.id)
    if not found:
        raise HTTPException(404, "Baglanti bulunamadi.")
    info, creds = found
    adapter = build_adapter(info.platform, creds)
    try:
        await adapter.authenticate()
        details = await adapter.verify()
    except AuthError as exc:
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    finally:
        await adapter.aclose()
    store.mark_verified(connection_id)
    return details


@router.get("/stores", response_model=list[Store])
async def all_stores(user: UserInfo = Depends(current_user)) -> list[Store]:
    """Tum bagli hesaplarin subeleri, tek listede."""
    result: list[Store] = []
    for info in store.list_connections(user.id):
        # Bir hesap cevap vermiyorsa ya da kaydi bozuksa digerlerini
        # engellemesin. Sifre cozme hatasi (RuntimeError) ve eksik alan
        # (AuthError) eskiden bu dongunun DISINA tasiyor, butun sube
        # listesini (ve Menu sayfasini) 500'e dusuruyordu.
        try:
            found = store.get_connection(info.id, user.id)
            if not found:
                continue
            adapter = build_adapter(info.platform, found[1])
        except Exception:
            continue
        try:
            await adapter.authenticate()
            for s_ in await adapter.fetch_stores():
                s_.connection_id = info.id
                result.append(s_)
        except (AuthError, PlatformError):
            continue
        finally:
            await adapter.aclose()
    return result


class StoreStatusRequest(BaseModel):
    open: bool


@router.put("/{connection_id}/stores/{store_id}/status")
async def store_status(
    connection_id: int,
    store_id: str,
    body: StoreStatusRequest,
    user: UserInfo = Depends(current_user),
) -> dict:
    """Subeyi satisa ac / kapat."""
    found = store.get_connection(connection_id, user.id)
    if not found:
        raise HTTPException(404, "Baglanti bulunamadi.")
    info, creds = found
    adapter = build_adapter(info.platform, creds)
    try:
        await adapter.authenticate()
        await adapter.set_store_status(store_id, body.open)
    except AuthError as exc:
        raise HTTPException(401, kullanici_mesaji(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, kullanici_mesaji(exc)) from exc
    finally:
        await adapter.aclose()
    return {"ok": True}


@router.delete("/{connection_id}", status_code=204)
async def remove(
    connection_id: int, user: UserInfo = Depends(current_user)
) -> None:
    if not store.delete_connection(connection_id, user.id):
        raise HTTPException(404, "Baglanti bulunamadi.")
    # Havuzdaki adapter silinen hesabin anahtarini tutuyor; dusurelim.
    await havuz.unut(connection_id)
