"""Platform adapter havuzu.

Neden: siparis yoklamasi 12 saniyede bir calisiyor ve her seferinde
sifirdan adapter kuruluyordu -- baglanti kaydini oku, kimlik bilgisini
coz, YENI bir httpx istemcisi ac, istegi at, istemciyi kapat. Yeni
istemci demek her yoklamada yeni bir TLS el sikismasi demek; bagli her
hesap icin yarim saniyeye yakin bekleme, bos yere harcanan CPU ve
gereksiz baglanti trafigi.

Havuz adapteri (ve icindeki istemciyi) baglanti basina bir kere kurup
saklyor. Kimlik bilgisi degisirse ya da baglanti silinirse `unut`
cagriliyor; oturum dusmesi gibi durumlarda da cagiran taraf unutup
yeniden istiyor.
"""

from __future__ import annotations

import asyncio
import logging

from .adapters.base import PlatformAdapter
from .adapters.registry import build_adapter
from . import store

log = logging.getLogger("tekeat.havuz")

_havuz: dict[int, PlatformAdapter] = {}
_kilit = asyncio.Lock()


async def edin(connection_id: int, user_id: int) -> PlatformAdapter:
    """Baglantinin adapteri; yoksa kurar. Bulunamazsa KeyError."""
    mevcut = _havuz.get(connection_id)
    if mevcut is not None:
        return mevcut

    async with _kilit:
        # Kilidi beklerken baskasi kurmus olabilir.
        mevcut = _havuz.get(connection_id)
        if mevcut is not None:
            return mevcut

        found = store.get_connection(connection_id, user_id)
        if not found:
            raise KeyError(connection_id)
        info, creds = found
        adapter = build_adapter(info.platform, creds)
        await adapter.authenticate()
        _havuz[connection_id] = adapter
        return adapter


async def unut(connection_id: int) -> None:
    """Adapteri havuzdan dusur ve istemcisini kapat.

    Kimlik bilgisi degistiginde, baglanti silindiginde ve kimlik
    hatasi alindiginda cagriliyor.
    """
    adapter = _havuz.pop(connection_id, None)
    if adapter is None:
        return
    try:
        await adapter.aclose()
    except Exception as exc:  # pragma: no cover - kapanis hatasi akisi bozmasin
        log.debug("Adapter kapatilamadi: %s", exc)


async def hepsini_kapat() -> None:
    for cid in list(_havuz):
        await unut(cid)
