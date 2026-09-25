"""Kullaniciya gosterilecek hata metinleri.

Kural: ekranda yalnizca NE OLDU ve NE YAPILMALI yazar. Platformun ham
cevabi, istisna adi, JSON, URL ekrana gitmez; bunlar log dosyasina
yazilir. Sahada Gecmis sayfasinda "400: {"exception":"ClientApiBad
RequestException", ...}" gorundu ve kullaniciyi bulandirdi.

Kendi urettigimiz Turkce aciklamalar (ValueError, AuthError metinleri)
zaten kullaniciya yazilmis; onlar korunur, yalnizca eklenen ham platform
cevabi kesilir.
"""

from __future__ import annotations

import logging
import re

import httpx

from .adapters.base import AuthError, PlatformError

log = logging.getLogger("tekeat.hata")

GENEL = "Beklenmeyen bir hata olustu. Tekrar deneyin; surerse ayrintilar kayit dosyasinda."
BAGLANTI = "Trendyol'a ulasilamadi. Internet baglantisini kontrol edip tekrar deneyin."

_DURUM = {
    400: "Trendyol istegi kabul etmedi.",
    403: "Trendyol bu islem icin yetki vermedi. Hesap bilgilerini kontrol edin.",
    404: "Trendyol'da kayit bulunamadi.",
    409: "Ayni anda baska bir degisiklik yapildi. Birkac saniye sonra tekrar deneyin.",
    429: "Trendyol'a cok sik istek gitti. Biraz bekleyip tekrar deneyin.",
}

# Teknik metin isaretleri: bunlardan biri varsa metin ekrana uygun degil.
_TEKNIK = re.compile(r"[{}\[\]<>]|exception|traceback|https?://|Error\b|\bat 0x", re.IGNORECASE)


def teknik_mi(metin: str) -> bool:
    return bool(_TEKNIK.search(metin)) or len(metin) > 220


def temizle(metin: str | None) -> str | None:
    """Kayitli durum metinlerini (eski surumlerin yazdiklari dahil) ekrana
    uygun hale getirir: teknik parantez/ek kisim atilir."""
    if not metin:
        return metin
    # "Platform ... vermiyor (400: {...})" -> parantezli teknik ek at.
    metin = re.sub(r"\s*\((\d{3}:)?[^()]*[{\[][^)]*\)?\s*$", "", metin).strip()
    metin = re.sub(r"^\[baglanti \d+\]\s*", "", metin)
    if " Platform cevabi:" in metin:
        metin = metin.split(" Platform cevabi:")[0]
    return GENEL if teknik_mi(metin) else metin


def kullanici_mesaji(exc: BaseException, baglam: str = "") -> str:
    """Istisnayi ekrana yazilacak kisa Turkce cumleye cevirir; teknik
    ayrintiyi log'a yazar."""
    log.warning("%s%s: %s", f"{baglam} " if baglam else "", type(exc).__name__, exc)
    if isinstance(exc, AuthError):
        return temizle(str(exc)) or GENEL
    if isinstance(exc, PlatformError):
        metin = str(exc).strip()
        kod = re.match(r"^(\d{3})\b", metin)
        if kod:
            durum = int(kod.group(1))
            if durum >= 500:
                return "Trendyol su an yanit vermiyor. Biraz sonra tekrar deneyin."
            # Bizim yazdigimiz aciklama varsa ("409. Ayni sube icin ...") onu kullan.
            geri = metin[len(kod.group(0)):].lstrip(".: ").strip()
            if geri and not teknik_mi(geri):
                return geri
            return _DURUM.get(durum, "Trendyol istegi reddetti.")
        return GENEL if teknik_mi(metin) else metin
    if isinstance(exc, (httpx.TransportError, OSError, TimeoutError)):
        return BAGLANTI
    if isinstance(exc, ValueError):
        metin = str(exc)
        return GENEL if teknik_mi(metin) else metin
    return GENEL
