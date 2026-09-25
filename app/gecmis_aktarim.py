"""Platformdaki eski siparisleri geriye dogru cekip kaydeder.

Neden: yerel kayit uygulamanin yoklamaya basladigi andan itibaren
tutuluyor; hesabin ondan onceki siparisleri de Gecmis'te ve musteri
listesinde gorunsun istendi.

Nasil:
  * Bugunden geriye 7 gunluk pencerelerle ilerliyor (son degisiklik
    tarihine gore, TUM statuler). Dokuman en genis araligi ve ne kadar
    geriye gidilebildigini yazmiyor: platform araligi reddederse (400)
    pencere yariya iniyor; 1 gunluk pencere de reddedilirse "platform
    daha eskisini vermiyor" deyip bitiyor.
  * Art arda _BOS_SERI_GUN boyunca hic siparis yoksa hesabin baslangicina
    ulasildi sayiliyor.
  * Ilerleme veritabaninda (gecmis_aktarim): uygulama kapanirsa kaldigi
    pencereden devam ediyor.
  * "Yeni" statudeki siparislere DOKUNMUYOR: onlari canli yoklayici
    aliyor. Aktarim onlari yazarsa alarm/oto onay akisiyla yarisirdi.
  * Istekler arasinda bekleme var; limit (10 sn'de 45) canli yoklamayla
    ortak, canli siparis beklemesin.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import hatalar, havuz, store
from .adapters.base import AuthError, PlatformError
from .models import OrderStatus

log = logging.getLogger("tekeat.aktarim")

_PENCERE = timedelta(days=7)
_EN_KUCUK_PENCERE = timedelta(days=1)
_BOS_SERI_GUN = 120
_MUTLAK_SINIR = timedelta(days=365 * 5)
_ARA_SN = 0.5
_GECICI_HATA_DENEME = 5
_GECICI_HATA_BEKLE_SN = 30.0

_gorev: asyncio.Task | None = None


def _tarih(metin: str) -> datetime:
    return datetime.fromisoformat(metin)


async def _baglanti_aktar(cid: int, user_id: int) -> None:
    kayit = await asyncio.to_thread(store.get_import, cid)
    if kayit and kayit["durum"] == "bitti":
        return
    if kayit is None:
        kayit = await asyncio.to_thread(store.save_import, cid)
    elif kayit["durum"] != "calisiyor":
        kayit = await asyncio.to_thread(store.save_import, cid, durum="calisiyor", hata=None)

    from .yoklayici import _zenginlestir, kaydet

    simdi = datetime.now(timezone.utc)
    sinir = _tarih(kayit["taranan_sinir"])
    aktarilan = kayit["aktarilan"]
    bos_seri = kayit["bos_seri_gun"]
    en_eski = kayit["en_eski_siparis"]
    pencere = _PENCERE
    gecici_hata = 0

    while True:
        if sinir <= simdi - _MUTLAK_SINIR:
            await asyncio.to_thread(store.save_import, cid, durum="bitti")
            log.info("Gecmis aktarimi [%s]: mutlak sinira ulasildi.", cid)
            return
        bas = sinir - pencere
        try:
            adapter = await havuz.edin(cid, user_id)
            siparisler = await adapter.fetch_orders(statuses=None, since=bas, until=sinir)
        except KeyError:
            return
        except AuthError as exc:
            await havuz.unut(cid)
            await asyncio.to_thread(store.save_import, cid, durum="hata",
                                    hata=hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] gecmis"))
            return
        except PlatformError as exc:
            metin = str(exc)
            if metin.startswith("400"):
                if pencere > _EN_KUCUK_PENCERE:
                    pencere = max(pencere / 2, _EN_KUCUK_PENCERE)
                    continue
                # Hata degil, platformun siniri: bilgi olarak, ham cevapsiz.
                # (Eskiden "(400: {"exception": ...})" ekrana gidiyordu.)
                log.info("Gecmis aktarimi [%s]: platform %s oncesini vermiyor: %s", cid, bas, metin[:300])
                await asyncio.to_thread(
                    store.save_import, cid, durum="bitti",
                    hata=f"Trendyol {sinir:%d.%m.%Y} oncesindeki siparisleri vermiyor.",
                )
                return
            gecici_hata += 1
            if gecici_hata > _GECICI_HATA_DENEME:
                await asyncio.to_thread(store.save_import, cid, durum="hata",
                                        hata=hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] gecmis"))
                return
            await asyncio.sleep(_GECICI_HATA_BEKLE_SN)
            continue
        except Exception as exc:
            # Ag kopmasi vb.: bekleyip ayni pencereyi tekrar dene.
            gecici_hata += 1
            if gecici_hata > _GECICI_HATA_DENEME:
                await asyncio.to_thread(
                    store.save_import, cid, durum="hata",
                    hata=hatalar.kullanici_mesaji(exc, f"[baglanti {cid}] gecmis"),
                )
                return
            await asyncio.sleep(_GECICI_HATA_BEKLE_SN)
            continue

        gecici_hata = 0
        eski = [o for o in siparisler if o.status != OrderStatus.NEW]
        if eski:
            await _zenginlestir(adapter, cid, eski)
            await kaydet(eski)
            aktarilan += len(eski)
            tarihler = [o.created_at for o in eski if o.created_at]
            if tarihler:
                aday = min(tarihler).astimezone(timezone.utc).isoformat()
                en_eski = aday if en_eski is None or aday < en_eski else en_eski
        bos_seri = 0 if siparisler else bos_seri + max(1, round(pencere / timedelta(days=1)))
        # Kuculen pencere korunuyor: platformun kabul ettigi en genis
        # araligi gosteriyor, 7 gune donmek her adimda bosa bir 400 demek.
        sinir = bas
        bitti = bos_seri >= _BOS_SERI_GUN
        await asyncio.to_thread(
            store.save_import, cid,
            durum="bitti" if bitti else "calisiyor",
            taranan_sinir=sinir.isoformat(),
            aktarilan=aktarilan,
            bos_seri_gun=bos_seri,
            en_eski_siparis=en_eski,
            hata=None,
        )
        if bitti:
            log.info("Gecmis aktarimi [%s] bitti: %d siparis, en eski %s", cid, aktarilan, en_eski)
            return
        await asyncio.sleep(_ARA_SN)


async def _hepsini_aktar() -> None:
    for info, user_id in await asyncio.to_thread(store.all_connections):
        try:
            await _baglanti_aktar(info.id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Gecmis aktarimi [%s] coktu", info.id)
            await asyncio.to_thread(
                store.save_import, info.id, durum="hata", hata="Beklenmeyen hata; kayda bakin."
            )


def baslat() -> bool:
    """Calismiyorsa aktarimi arka planda baslatir; zaten calisiyorsa False."""
    global _gorev
    if _gorev is not None and not _gorev.done():
        return False
    _gorev = asyncio.create_task(_hepsini_aktar(), name="gecmis-aktarim")
    return True


def calisiyor_mu() -> bool:
    return _gorev is not None and not _gorev.done()


async def durdur() -> None:
    global _gorev
    if _gorev is not None and not _gorev.done():
        _gorev.cancel()
        try:
            await _gorev
        except asyncio.CancelledError:
            pass
    _gorev = None


async def yeniden_tara(connection_ids: list[int]) -> None:
    """Bu baglantilarin ilerlemesini silip bastan tarar (kayitlar korunur)."""
    await durdur()
    for cid in connection_ids:
        await asyncio.to_thread(store.delete_import, cid)
    baslat()
