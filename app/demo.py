"""Test siparisleri.

Gercek bir siparisi beklemeden butun akisi denemek icin: kart ekranda
cikiyor, kabul ediliyor, fisi basiliyor, iptal ediliyor. Platforma
hicbir istek gitmiyor.

Bellekte duruyorlar: uygulama kapaninca kayboluyorlar ki gercek
siparislerin arasinda unutulmus test kaydi kalmasin.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timedelta, timezone

from .models import (
    Order,
    OrderItem,
    OrderItemOption,
    OrderStatus,
    Platform,
)

# Gercek paket kimlikleri 64 karakterlik alfanumerik; bu onek onlarla
# karismaz ve rotalar test siparisini bakisla ayirt edebilir.
ONEK = "demo-"

_kilit = threading.Lock()
_siparisler: dict[str, Order] = {}
_sayac = itertools.count(1)


def test_siparisi_mi(order_id: str) -> bool:
    return order_id.startswith(ONEK)


def _ornek(no: int, kapida: bool) -> Order:
    """Zor durumlari bilerek iceren bir siparis.

    Turkce karakter, uzun urun adi, cikarilan/eklenen malzeme, satir
    notu, kapida odeme ve musteri notu -- fiste ne bozulabilirse
    hepsi burada.
    """
    return Order(
        connection_id=0,
        platform=Platform.TGO,
        platform_order_id=f"{ONEK}{no}",
        order_code=f"TEST{no}",
        store_id="test",
        status=OrderStatus.NEW,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=20),
        modified_at=datetime.now(timezone.utc),
        customer_name="Ayşe Yılmaz",
        customer_phone="5554443322",
        call_center_phone="0212 365 34 03",
        pin_code="673985557",
        address_text="Caferağa Mah. Güneşli Bahçe Sk. No:12 D:4, Kadıköy/İstanbul",
        # Gercek koordinat: mesafe ve paket ucreti hesabi da denenebilsin.
        latitude=40.9903,
        longitude=29.0270,
        is_pickup=False,
        courier_by_platform=False,
        order_number="1199521762",
        items=[
            OrderItem(
                name="Adana Kebap Menü (Büyük Porsiyon)",
                quantity=2,
                unit_price=145.0,
                total_price=290.0,
                options=[
                    OrderItemOption(name="Patates Kızartması", price=30.0),
                    OrderItemOption(name="Ayran", price=0.0),
                ],
                removed=["Soğan", "Turşu"],
                extras=["Ekstra peynir"],
                note="Az acılı olsun",
                item_ids=[f"{ONEK}{no}-i1", f"{ONEK}{no}-i2"],
            ),
            OrderItem(
                name="Çiğ Köfte Dürüm",
                quantity=1,
                unit_price=75.0,
                total_price=75.0,
                item_ids=[f"{ONEK}{no}-i3"],
            ),
            OrderItem(
                name="Şalgam Suyu (Acılı)",
                quantity=3,
                unit_price=25.0,
                total_price=75.0,
                item_ids=[f"{ONEK}{no}-i4", f"{ONEK}{no}-i5", f"{ONEK}{no}-i6"],
            ),
        ],
        total_price=425.99,
        delivery_price=15.99,
        discount_total=30.0,
        payment_type="PAY_WITH_ON_DELIVERY" if kapida else "PAY_WITH_CARD",
        payment_on_delivery="Nakit" if kapida else None,
        is_on_delivery=kapida,
        note="Zile basmayın, bebek uyuyor. Kapıyı çalın lütfen.",
        eta_text="15 - 36 dk",
        is_test=True,
        app_name="TrendyolGo",
    )


def olustur(kapida: bool = True) -> Order:
    from . import ayarlar, mesafe

    with _kilit:
        no = next(_sayac)
        siparis = _ornek(no, kapida)
        # Sahte bir sube koordinati (Besiktas) ile mesafe/ucret de
        # ekranda gorunsun; test siparisi tum akisi denemek icin.
        siparis.store_latitude, siparis.store_longitude = 41.0422, 29.0093
        siparis.distance_km, siparis.courier_fee = mesafe.hesapla(
            41.0422, 29.0093, siparis.latitude, siparis.longitude,
            ayarlar.oku().get("teslimat", {}),
        )
        _siparisler[siparis.platform_order_id] = siparis
        return siparis


def listele(statuler: list[OrderStatus] | None = None) -> list[Order]:
    with _kilit:
        hepsi = list(_siparisler.values())
    if statuler is None:
        return hepsi
    return [o for o in hepsi if o.status in statuler]


def getir(order_id: str) -> Order | None:
    with _kilit:
        return _siparisler.get(order_id)


def durum_degistir(order_id: str, durum: OrderStatus) -> Order | None:
    with _kilit:
        siparis = _siparisler.get(order_id)
        if siparis is None:
            return None
        siparis.status = durum
        siparis.gorundu = True
        siparis.modified_at = datetime.now(timezone.utc)
        return siparis


def gorundu(order_id: str) -> None:
    with _kilit:
        siparis = _siparisler.get(order_id)
        if siparis is not None:
            siparis.gorundu = True


def kurye_ata(order_id: str, ad: str) -> Order | None:
    with _kilit:
        siparis = _siparisler.get(order_id)
        if siparis is None:
            return None
        siparis.courier_name = ad or None
        return siparis


def iptal(order_id: str, item_ids: list[str]) -> Order | None:
    """Kismi iptalde yalnizca secilenler, tam iptalde siparisin tamami."""
    with _kilit:
        siparis = _siparisler.get(order_id)
        if siparis is None:
            return None
        istenen = set(item_ids)
        tum = {i for satir in siparis.items for i in satir.item_ids}
        for satir in siparis.items:
            if set(satir.item_ids) <= istenen:
                satir.cancelled = True
        if not istenen or istenen >= tum:
            siparis.status = OrderStatus.CANCELLED
        siparis.modified_at = datetime.now(timezone.utc)
        return siparis


def temizle() -> int:
    with _kilit:
        sayi = len(_siparisler)
        _siparisler.clear()
        return sayi
