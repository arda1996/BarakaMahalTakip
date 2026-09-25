"""Restoran-musteri mesafesi ve paket ucreti.

Platform mesafeyi hic vermiyor: sipariste yalnizca sure bilgileri var
(eta, preparationTime, estimatedPickupTime). Mesafeyi restoranin
koordinatiyla musterinin koordinatindan biz hesapliyoruz.

DIKKAT: bu yalnizca Model 1'de (kendi kuryemizle teslimat) mumkun.
Model 2'de platform musteri koordinatini maskeliyor, o yuzden mesafe
de ucret de hesaplanamiyor -- tahmini bir sayi uydurmaktansa bos
birakiyoruz.

Hesap kus ucusu (haversine) + yol carpani. Gercek yol her zaman kus
ucusundan uzundur; sehir ici icin ~1.3 yaygin bir yaklasiklamadir ve
varsayilan budur. Ucret esigine yakin siparisleri eksik hesaplamamak
icin ham kus ucusu kullanilmiyor. Carpan ayardan degistirilebilir;
1.0 ham mesafe demektir.
"""

from __future__ import annotations

import math

DUNYA_YARICAP_KM = 6371.0


def kus_ucusu_km(
    enlem1: float | None,
    boylam1: float | None,
    enlem2: float | None,
    boylam2: float | None,
) -> float | None:
    """Iki nokta arasi kus ucusu mesafe (km). Eksik koordinatta None."""
    if None in (enlem1, boylam1, enlem2, boylam2):
        return None

    p1 = math.radians(float(enlem1))
    p2 = math.radians(float(enlem2))
    dp = p2 - p1
    dl = math.radians(float(boylam2) - float(boylam1))

    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * DUNYA_YARICAP_KM * math.asin(min(1.0, math.sqrt(a)))


def paket_ucreti(
    km: float | None,
    taban_km: float = 3.0,
    taban_ucret: float = 100.0,
    km_basi: float = 25.0,
) -> float | None:
    """Mesafeye gore paket ucreti.

    Kural: taban_km'ye kadar taban_ucret; sonrasinda her BASLAYAN
    kilometre icin km_basi ekleniyor. 3.1 km ile 4.0 km ayni ucrete
    tabi -- kuryeye "yarim kilometre" diye yarim ucret odenmiyor,
    isletmelerin yaptigi da bu.
    """
    if km is None:
        return None
    if km <= taban_km:
        return round(taban_ucret, 2)
    asan = math.ceil(km - taban_km)
    return round(taban_ucret + asan * km_basi, 2)


def hesapla(
    sube_enlem: float | None,
    sube_boylam: float | None,
    musteri_enlem: float | None,
    musteri_boylam: float | None,
    ayar: dict | None = None,
) -> tuple[float | None, float | None]:
    """(mesafe_km, ucret) dondurur."""
    ayar = ayar or {}
    km = kus_ucusu_km(sube_enlem, sube_boylam, musteri_enlem, musteri_boylam)
    if km is None:
        return None, None

    carpan = float(ayar.get("yol_carpani", 1.0) or 1.0)
    km = round(km * carpan, 2)
    ucret = paket_ucreti(
        km,
        taban_km=float(ayar.get("taban_km", 3.0)),
        taban_ucret=float(ayar.get("taban_ucret", 100.0)),
        km_basi=float(ayar.get("km_basi", 25.0)),
    )
    return km, ucret
