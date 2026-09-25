"""Paketlenmis ve paketlenmemis calisma arasindaki yol farki tek yerde.

PyInstaller exe'yi acinca dosyalari gecici bir klasore aciyor; o klasorun
yolu sys._MEIPASS'ta duruyor. Gelistirirken boyle bir sey yok, proje
kokunu kullaniyoruz.
"""

import sys
from pathlib import Path


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_dir() -> Path:
    """Salt okunur kaynaklarin (derlenmis arayuz) bulundugu klasor."""
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def web_dist() -> Path | None:
    """Derlenmis arayuz; yoksa None (arka uc yine de calisir)."""
    candidate = resource_dir() / "web" / "dist"
    return candidate if (candidate / "index.html").exists() else None


def data_dir() -> Path:
    """Veritabani gibi yazilabilir dosyalarin yeri.

    Paketlenmis exe kendi icine yazamaz, bu yuzden kullanicinin AppData
    klasorunu kullaniyoruz.

    Onemli: gelistirme modu da AYNI klasoru kullaniyor. Boylece bagladiginiz
    hesap her iki modda da gecerli olur -- kimlik bilgilerini bir kez
    girersiniz, exe'yi yeniden uretseniz bile tekrar sormaz. Ayri klasor
    kullanilsaydi ayni hesabi iki kez baglamak gerekirdi.
    """
    import os

    # Testler icin: ayar dosyasi, log ve anahtar gercek klasore
    # dokunmasin. Arayuz testinde gercek ayar (otomatik fis) kullanilinca
    # oto kabul edilen test siparisleri gercek yaziciya basilmisti.
    ozel = os.environ.get("TEKEAT_VERI_KLASORU")
    if ozel:
        base = Path(ozel)
        base.mkdir(parents=True, exist_ok=True)
        return base

    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Tekeat"
    base.mkdir(parents=True, exist_ok=True)
    return base
