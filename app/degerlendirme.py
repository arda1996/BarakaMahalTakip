"""Musteri degerlendirmelerini platformdan cekip kaydeder, cevaplari gonderir.

Siparis yoklamasindan AYRI bir dongu: yorumlar dakikalik degil ve
siparis turunu yavaslatmamali. 10 dakikada bir, ayrica sayfadan
"Yenile" ile.

Pencereler:
  * Musteri siparisten sonra 7 gun icinde puan verebiliyor ve restoran
    cevabinin onay durumu sonradan degisiyor. Her turda kayittaki en son
    yorumdan 8 gun geriden bugune tekrar cekiliyor (ustune yaziliyor).
  * Hic kayit yoksa son 60 gun.
  * createdDate araligi 7 gunluk pencerelerle; platformun aralik siniri
    dokumanda yok, siparis aktarimindaki temkin burada da gecerli.

Trendyol'da Uber Eats'e gecen subelerde yorum servisleri 403 donuyor ve
dokuman "istek atilmamali" diyor: o sube gunde bir kez denenip
atlaniyor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import hatalar, havuz, store
from .adapters.base import AuthError, PlatformError, ReviewsUnsupported
from .models import Review

log = logging.getLogger("tekeat.degerlendirme")

ARALIK_SN = 600.0
_PENCERE = timedelta(days=7)
_ILK = timedelta(days=60)
_TEKRAR = timedelta(days=8)
_DESTEKSIZ_BEKLE_SN = 24 * 3600.0
# Cevap gonderildikten sonra platformun cevabi gostermesini bekleme.
_CEVAP_TEYIT = (0.6, 1.2, 2.0)

_desteksiz: dict[tuple[int, str], tuple[float, str]] = {}
_durum: dict[int, dict] = {}
_kilit = asyncio.Lock()


async def _sube_senkron(cid: int, adapter, sube_id: str) -> int:
    anahtar = (cid, sube_id)
    kayit = _desteksiz.get(anahtar)
    if kayit and time.monotonic() - kayit[0] < _DESTEKSIZ_BEKLE_SN:
        return 0
    simdi = datetime.now(timezone.utc)
    son = await asyncio.to_thread(store.latest_review_time, cid, sube_id)
    bas = max(son - _TEKRAR if son else simdi - _ILK, simdi - _ILK)
    yeni = 0
    try:
        while bas < simdi:
            bitis = min(bas + _PENCERE, simdi)
            yorumlar = await adapter.fetch_reviews(sube_id, since=bas, until=bitis)
            for r in yorumlar:
                r.connection_id = cid
            yeni += await asyncio.to_thread(store.save_reviews, yorumlar)
            bas = bitis
    except ReviewsUnsupported as exc:
        _desteksiz[anahtar] = (time.monotonic(), str(exc))
        log.info("Degerlendirme desteklenmiyor [%s/%s]: %s", cid, sube_id, exc)
        return yeni
    _desteksiz.pop(anahtar, None)
    return yeni


async def _baglanti_senkron(cid: int, user_id: int) -> None:
    d = _durum.setdefault(cid, {"son_basari": None, "hata": None})
    try:
        adapter = await havuz.edin(cid, user_id)
        subeler = await adapter.fetch_stores()
        yeni = 0
        for s in subeler:
            yeni += await _sube_senkron(cid, adapter, s.id)
        d.update(son_basari=datetime.now(timezone.utc), hata=None)
        if yeni:
            log.info("Yeni degerlendirme [%s]: %d", cid, yeni)
    except KeyError:
        return
    except AuthError as exc:
        await havuz.unut(cid)
        d["hata"] = hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] degerlendirme")
    except Exception as exc:
        d["hata"] = hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] degerlendirme")


async def senkron() -> None:
    async with _kilit:
        for info, user_id in await asyncio.to_thread(store.all_connections):
            await _baglanti_senkron(info.id, user_id)


async def calistir() -> None:
    # Acilista siparis yoklamasi once calissin.
    await asyncio.sleep(20)
    while True:
        try:
            await senkron()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Degerlendirme turu coktu")
        await asyncio.sleep(ARALIK_SN)


def durum(connection_ids: list[int]) -> dict:
    """Arayuz icin: son basarili senkron, hata, desteklenmeyen subeler."""
    hatalar, zamanlar, desteksiz = [], [], []
    for cid in connection_ids:
        d = _durum.get(cid)
        if d:
            if d["hata"]:
                hatalar.append(f"[baglanti {cid}] {d['hata']}")
            if d["son_basari"]:
                zamanlar.append(d["son_basari"])
        for (c, sube), (_, neden) in _desteksiz.items():
            if c == cid:
                desteksiz.append({"connection_id": c, "store_id": sube, "neden": neden})
    return {
        "hata": " | ".join(hatalar) or None,
        "son_basari": min(zamanlar).isoformat() if zamanlar else None,
        "desteklenmeyen": desteksiz,
    }


async def cevapla(user_id: int, review_id: str, metin: str) -> tuple[Review, bool]:
    """Cevabi platforma gonderir; (platformun gosterdigi hal, teyit) doner.

    Iyimser degil: cevap platformda gorunene kadar yorum tekrar cekiliyor.
    Trendyol cevabi once onaya aliyor (WAITING_FOR_APPROVE); gorunmezse
    teyitsiz donuyoruz ve bir sonraki senkron durumu guncelliyor.
    """
    r = await asyncio.to_thread(store.get_review, review_id)
    if r is None or r.connection_id is None or not r.store_id:
        raise KeyError(review_id)
    adapter = await havuz.edin(r.connection_id, user_id)
    await adapter.answer_review(r.store_id, review_id, metin)

    son = r
    for bekle in _CEVAP_TEYIT:
        await asyncio.sleep(bekle)
        try:
            bulunan = await adapter.fetch_reviews(
                r.store_id,
                since=(r.created_at - timedelta(days=1)) if r.created_at else None,
                until=datetime.now(timezone.utc) + timedelta(minutes=5),
                order_number=r.order_number,
            )
        except PlatformError:
            continue
        eslesen = next((x for x in bulunan if x.review_id == review_id), None)
        if eslesen is None:
            continue
        eslesen.connection_id = r.connection_id
        await asyncio.to_thread(store.save_reviews, [eslesen])
        son = eslesen
        if eslesen.answer_text:
            return eslesen, True
    return son, False
