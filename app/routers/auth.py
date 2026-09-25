"""Kullanici hesabi: kayit, giris, cikis.

Ilk calistirmada hic kullanici yoktur; o durumda kayit ekrani acilir ve
ilk hesap serbestce olusturulur. Sonraki hesaplar yalnizca giris yapmis
bir kullanici tarafindan eklenebilir (personel hesabi acmak icin).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, store
from ..deps import current_user
from ..models import UserInfo

router = APIRouter(prefix="/auth", tags=["hesap"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    display_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = True


class StatusResponse(BaseModel):
    """Acilista arayuzun hangi ekrani gosterecegini belirler."""

    has_users: bool
    user: UserInfo | None = None


@router.get("/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    return StatusResponse(has_users=store.user_count() > 0, user=store.active_user())


@router.post("/register", response_model=UserInfo)
async def register(payload: RegisterRequest) -> UserInfo:
    ilk_kullanici = store.user_count() == 0
    if not ilk_kullanici and store.active_user() is None:
        raise HTTPException(
            401, "Yeni hesap acmak icin once mevcut bir hesapla giris yapin."
        )

    try:
        auth.validate_credentials(payload.username, payload.password)
    except auth.AuthError as exc:
        raise HTTPException(422, str(exc)) from exc

    username = auth.normalize_username(payload.username)
    if store.find_user(username):
        raise HTTPException(409, "Bu kullanici adi zaten kayitli.")

    user = store.create_user(
        username,
        (payload.display_name or "").strip(),
        auth.hash_password(payload.password),
    )

    # Ilk hesap acilir acilmaz giris yapilmis sayilsin; kullaniciyi
    # ayni bilgileri iki kez yazdirmanin anlami yok.
    if ilk_kullanici:
        store.create_session(
            user.id, auth.new_session_token(), auth.SESSION_REMEMBER
        )
        store.touch_login(user.id)
    return user


@router.post("/login", response_model=UserInfo)
async def login(payload: LoginRequest) -> UserInfo:
    found = store.find_user(auth.normalize_username(payload.username))
    # Kullanici yok ile sifre yanlis ayni mesaji verir: hangi
    # kullanici adlarinin kayitli oldugu disari sizmasin.
    if not found or not auth.verify_password(payload.password, found[1]):
        raise HTTPException(401, "Kullanici adi veya sifre hatali.")

    user = found[0]
    store.create_session(
        user.id,
        auth.new_session_token(),
        auth.SESSION_REMEMBER if payload.remember else auth.SESSION_SHORT,
    )
    store.touch_login(user.id)
    return user


@router.post("/logout", status_code=204)
async def logout() -> None:
    store.clear_sessions()


@router.get("/me", response_model=UserInfo)
async def me(user: UserInfo = Depends(current_user)) -> UserInfo:
    return user
