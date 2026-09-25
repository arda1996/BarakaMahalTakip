"""Tekeat masaustu uygulamasinin giris noktasi.

Tek surec: arka uc arka planda bir is parcaciginda calisir, pencere de
ayni adresi acar. Derleyici, ayri sunucu, ayri port aktarimi yok --
arayuz ile API ayni origin'de.

Windows'ta pencere motoru olarak WebView2 kullanilir; Windows 11'de
hazir gelir, Windows 10'da Edge ile birlikte gelmistir.
"""

import argparse
import socket
import threading
import time
import urllib.error
import urllib.request

import uvicorn
import webview

from . import sistem
from .main import app

HOST = "127.0.0.1"
PORT_START = 8756

# Sayfa yuklenene kadar pencere beyaz parlamasin; tema rengiyle ayni.
BG = "#0f1115"


def _free_port() -> int:
    """PORT_START'tan baslayarak ilk bos portu bulur."""
    for port in range(PORT_START, PORT_START + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:
                return port
    raise RuntimeError("Bos port bulunamadi.")


def _serve(port: int) -> None:
    uvicorn.run(app, host=HOST, port=port, log_level="warning", access_log=False)


def _wait_until_ready(port: int, timeout: float = 20.0) -> bool:
    """Pencereyi acmadan once arka ucun cevap verdigini dogrula.

    Yoksa kullanici bir an bos/hatali bir sayfa gorur.
    """
    deadline = time.monotonic() + timeout
    url = f"http://{HOST}:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="Tekeat")
    parser.add_argument(
        "--dev",
        metavar="URL",
        nargs="?",
        const="http://localhost:5173",
        help="Pencereyi Vite'in canli sunucusuna baglar (gelistirme icin). "
        "Arka ucu bu modda ayri calistirin.",
    )
    args = parser.parse_args()

    if args.dev:
        webview.create_window(
            "Tekeat (gelistirme)",
            args.dev,
            width=1360,
            height=860,
            min_size=(1024, 640),
            text_select=True,
            background_color=BG,
        )
        webview.start(debug=True)
        return

    port = _free_port()

    # daemon: pencere kapaninca surec beklemeden kapansin.
    threading.Thread(target=_serve, args=(port,), daemon=True).start()

    if not _wait_until_ready(port):
        print("Arka uc baslatilamadi.")
        return

    pencere = webview.create_window(
        "Tekeat",
        f"http://{HOST}:{port}/",
        width=1360,
        height=860,
        min_size=(1024, 640),
        text_select=True,
        background_color=BG,
    )
    sistem.pencereyi_kaydet(pencere)

    # Siparis terminali uyumamali: Windows uykuya gecerse ya da ekran
    # kararirsa gelen siparis kimseye gorunmez. Bayrak uygulama
    # kapaninca kendiliginden dusuyor.
    sistem.uyanik_tut(ekran=True)

    webview.start()


if __name__ == "__main__":
    main()
