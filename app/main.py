import asyncio
import contextlib
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import degerlendirme, gecmis_aktarim, hatalar, havuz, store, yoklayici
from .paths import data_dir, web_dist
from .routers import auth, connections, gecmis, menu, orders, sistem, yazici
from .routers import degerlendirme as degerlendirme_router
from .routers import stok as stok_router

# Paketlenmis uygulamada konsol yok; hatalar dosyaya yazilmazsa kaybolur.
LOG_PATH = data_dir() / "servis.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("tekeat")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    # Siparisler arka ucta yoklaniyor ve kaydediliyor; arayuz kapali ya
    # da arkada olsa da kayit tutulsun (bkz. yoklayici.py).
    gorev = asyncio.create_task(yoklayici.calistir(), name="yoklayici")
    # Platformdaki eski siparisler geriye dogru aktariliyor; bitmisse hemen
    # donuyor, yarim kaldiysa kaldigi yerden devam ediyor.
    gecmis_aktarim.baslat()
    # Musteri degerlendirmeleri ayri dongude (siparis turunu yavaslatmasin).
    yorum_gorevi = asyncio.create_task(degerlendirme.calistir(), name="degerlendirme")
    yield
    yorum_gorevi.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await yorum_gorevi
    await gecmis_aktarim.durdur()
    gorev.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await gorev
    # Havuzdaki adapterler baglantilari acik tutuyor; kapanirken
    # duzgunce birakalim.
    await havuz.hepsini_kapat()


app = FastAPI(
    title="Tekeat",
    description=(
        "Trendyol Go, Yemeksepeti ve Migros Yemek siparislerini tek ekrandan "
        "yonetmek icin ortak API."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(connections.router)
app.include_router(gecmis.router)
app.include_router(degerlendirme_router.router)
app.include_router(stok_router.router)
app.include_router(menu.router)
app.include_router(orders.router)
app.include_router(sistem.router)
app.include_router(yazici.router)


@app.exception_handler(Exception)
async def beklenmeyen_hata(request: Request, exc: Exception) -> JSONResponse:
    """Beklenmeyen hatalar bos bir 500 olarak kaybolmasin.

    Arayuze kisa bir cumle gider; istisna adi ve iz YALNIZCA dosyaya
    yazilir (eskiden "KeyError: 'x'" gibi metin ekranda gorunuyordu).
    """
    log.error("%s %s -> %s\n%s", request.method, request.url.path, exc,
              traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": hatalar.GENEL, "log": str(LOG_PATH)},
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Derlenmis arayuzu ayni sunucudan servis ediyoruz. Boylece pencere ile
# API ayni origin'de olur: CORS yok, port aktarimi yok. Mount en sonda
# duruyor cunku "/" altindaki her seyi yakaliyor; yukaridaki API yollari
# once eslesiyor.
_dist = web_dist()
if _dist is not None:
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="web")
