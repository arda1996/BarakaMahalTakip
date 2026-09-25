# -*- mode: python ; coding: utf-8 -*-
"""Tekeat'i tek bir exe'ye paketler.

Cikti: cikti/Tekeat.exe
Bu dosyayi betikler/derle.ps1 calistiriyor; elle de olur:
    pyinstaller servis.spec --clean --noconfirm

Onemli: arayuzun onceden derlenmis olmasi gerekiyor (web/dist).
"""

from PyInstaller.utils.hooks import collect_all

# pywebview'in Windows'ta WebView2 icin ihtiyac duydugu her sey.
webview_datas, webview_binaries, webview_hidden = collect_all("webview")

# pywebview Windows'ta WebView2'ye pythonnet (clr) uzerinden ulasiyor.
# Bu paket yalnizca Windows'ta kurulu oldugu icin toplama basarisiz
# olursa sessizce geciyoruz -- baska platformda paketleme yapilmiyor.
extra_datas, extra_binaries, extra_hidden = [], [], []
for _pkg in ("clr_loader", "pythonnet"):
    try:
        _d, _b, _h = collect_all(_pkg)
        extra_datas += _d
        extra_binaries += _b
        extra_hidden += _h
    except Exception:
        pass

a = Analysis(
    ["run_desktop.py"],
    pathex=[],
    binaries=webview_binaries + extra_binaries,
    # Derlenmis arayuz exe'nin icine gomuluyor; app/paths.py oradan okuyor.
    datas=webview_datas + extra_datas + [("web/dist", "web/dist")],
    hiddenimports=webview_hidden
    + extra_hidden
    + [
        "clr",
        # win32print calisma aninda ice aliniyor; statik analiz gormuyor.
        "win32print",
        "win32api",
        "pywintypes",
        # uvicorn bunlari calisma aninda ice aliyor, statik analiz goremiyor.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Tekeat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Arkada konsol penceresi acilmasin.
    console=False,
    disable_windowed_traceback=False,
    icon="web/icon.ico" if __import__("os").path.exists("web/icon.ico") else None,
)
