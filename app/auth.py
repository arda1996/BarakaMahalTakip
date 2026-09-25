"""Kullanici hesaplari ve oturum.

Sifreler asla duz metin saklanmiyor: PBKDF2-HMAC-SHA256, hesap basina
rastgele tuz, 600.000 tur. Ek bagimlilik yok, hepsi standart kutuphane.

Oturum modeli bilerek basit: uygulama yalnizca 127.0.0.1'e bagli, tek
bilgisayarda calisan bir masaustu programi. Giris yapilinca veritabanina
bir oturum kaydi yaziliyor; uygulama acildiginda o kayit duruyorsa
kullanici zaten girmis sayiliyor. "Beni hatirla" isaretliyse oturum
1 yil, degilse 12 saat yasiyor.
"""

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16

# Oturum sureleri (saniye)
SESSION_SHORT = 12 * 60 * 60        # 12 saat
SESSION_REMEMBER = 365 * 24 * 60 * 60  # 1 yil

MIN_PASSWORD_LENGTH = 8


class AuthError(Exception):
    """Kullaniciya gosterilebilir kimlik hatasi."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """'algoritma$tur$tuz$ozet' bicimini dondurur."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Sabit zamanli karsilastirma; bicim bozuksa sessizce False."""
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt_b64), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, _unb64(digest_b64))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_credentials(username: str, password: str) -> None:
    """Kayit sirasindaki kurallar. Ihlalde AuthError."""
    username = username.strip()
    if len(username) < 3:
        raise AuthError("Kullanici adi en az 3 karakter olmali.")
    if len(username) > 40:
        raise AuthError("Kullanici adi en fazla 40 karakter olabilir.")
    if not all(c.isalnum() or c in "._-@" for c in username):
        raise AuthError(
            "Kullanici adi yalnizca harf, rakam ve . _ - @ icerebilir."
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Sifre en az {MIN_PASSWORD_LENGTH} karakter olmali."
        )
