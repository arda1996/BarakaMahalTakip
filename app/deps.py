"""Rotalarin paylastigi bagimliliklar."""

from fastapi import HTTPException

from . import store
from .models import UserInfo


def current_user() -> UserInfo:
    """Aktif oturumun kullanicisi; yoksa 401.

    Uygulama yalnizca 127.0.0.1'e bagli oldugu icin oturum bilgisi
    istekte tasinmiyor: acik oturum varsa sahibi bu kullanicidir.
    """
    user = store.active_user()
    if user is None:
        raise HTTPException(401, "Once giris yapmalisiniz.")
    return user
