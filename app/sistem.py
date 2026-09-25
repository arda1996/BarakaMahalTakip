"""Isletim sistemi ile ilgili isler: uyanik kalma ve pencereyi one alma.

Neden gerekli: bu bir siparis terminali. Windows uykuya gecerse ya da
ekran kararirsa gelen siparis kimseye gorunmez; uyandiginda da
yoklamanin kaldigi yerden devam etmesi yetmez, hemen bakmasi gerekir.
Satici panelinin masaustu uygulamasinin kayitlarinda ayni ihtiyacin
izleri var (uyanma tetikleyicisi ve guc tasarrufunun kapatilmasi).
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("tekeat.sistem")

# SetThreadExecutionState bayraklari (winbase.h)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# Bayrak IS PARCACIGINA bagli ve o parcacik yasadigi surece gecerli.
# Bu yuzden adanmis bir parcacikta tutuyor ve duzenli olarak
# tazeliyoruz: bazi guc profillerinde tek seferlik cagri, uzun
# sureli oturumda dusebiliyor.
_TAZELEME_SN = 45.0

# Saat sicramasi bu esigi gecerse makine uyumus demektir.
_UYANMA_ESIGI_SN = 30.0

_durum = {
    "uyanik_tutuluyor": False,
    "ekran_aciik": False,
    "son_uyanma": None,   # datetime | None
    "uyku_sayisi": 0,
}
_kilit = threading.Lock()


def _windows_mu() -> bool:
    return hasattr(ctypes, "windll")


def _bayrak_uygula(ekran: bool) -> bool:
    if not _windows_mu():
        return False
    bayrak = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if ekran:
        bayrak |= ES_DISPLAY_REQUIRED
    try:
        # 0 donerse istek reddedilmis demektir.
        sonuc = ctypes.windll.kernel32.SetThreadExecutionState(bayrak)
        return sonuc != 0
    except Exception as exc:  # pragma: no cover - platforma bagli
        log.warning("Uyanik tutma bayragi uygulanamadi: %s", exc)
        return False


def _dongu(ekran: bool) -> None:
    """Bayragi tazeler ve ayni parcacikta uyku/uyanma tespiti yapar."""
    onceki = time.time()
    while True:
        tamam = _bayrak_uygula(ekran)
        with _kilit:
            _durum["uyanik_tutuluyor"] = tamam
            _durum["ekran_aciik"] = tamam and ekran

        time.sleep(_TAZELEME_SN)

        # Duvar saati beklenenden cok ilerlediyse makine uyumustur.
        simdi = time.time()
        sapma = simdi - onceki - _TAZELEME_SN
        if sapma > _UYANMA_ESIGI_SN:
            with _kilit:
                _durum["son_uyanma"] = datetime.now(timezone.utc)
                _durum["uyku_sayisi"] += 1
            log.info("Sistem uykudan dondu (yaklasik %.0f sn).", sapma)
        onceki = simdi


def uyanik_tut(ekran: bool = True) -> None:
    """Uygulama acikken Windows'un uyumasini (ve ekranin kararmasini) engeller.

    Uygulama kapaninca bayrak kendiliginden dusuyor: parcacik daemon
    oldugu icin ayrica geri almaya gerek yok.
    """
    if not _windows_mu():
        log.info("Uyanik tutma yalnizca Windows'ta calisiyor; atlandi.")
        return
    t = threading.Thread(target=_dongu, args=(ekran,), daemon=True, name="uyanik")
    t.start()


def durum() -> dict:
    with _kilit:
        d = dict(_durum)
    d["son_uyanma"] = d["son_uyanma"].isoformat() if d["son_uyanma"] else None
    return d


# ---------------------------------------------------------------
# Pencere
# ---------------------------------------------------------------

_pencere_basligi: str | None = None
_hwnd: int | None = None

# Pencere islerini YAPAN tek parcacik.
#
# Neden kuyruk: /system/focus ucu yeni siparis geldiginde cagriliyor;
# pencere cagrisini istek icinde yapmak olay dongusunu durduruyordu.
# Uc kuyruga birakip aninda donuyor.
_kuyruk: "queue.Queue[str]" = queue.Queue(maxsize=8)
_isci_basladi = False

# Pencereyi pywebview ile DEGIL, dogrudan Win32 ile one aliyoruz.
#
# HATA KAYNAGIYDI (pes pese gelen siparislerde "program dondu"):
# pywebview'in `on_top` ayari WinForms'ta `TopMost` ozelligini arayuz
# parcacigina aktarmadan, cagiran parcaciktan yaziyor. pythonnet ozellik
# yazarken GIL'i birakmiyor; TopMost ise SetWindowPos ile arayuz
# parcacigini bekliyor. Arayuz parcacigi o sirada pencere olayini
# (Activated) Python'da islemek icin GIL'i bekliyorsa ikisi birbirini
# sonsuza kadar bekliyor: pencere, arka uc, yoklama -- hepsi duruyor.
# Windows olay gunlugunde "AppHang" olarak goruldu; kucult/one al
# dongusuyle birkac turda yeniden uretildi. Ayrica pywebview'in
# restore()'u tam ekran pencereyi de kucuk boyuta indiriyordu.
#
# ctypes dis cagrilarda GIL'i birakir ve asagidaki cagrilarin hicbiri
# arayuz parcacigini beklemez (ShowWindowAsync, SWP_ASYNCWINDOWPOS).
SW_SHOW = 5
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_ASYNCWINDOWPOS = 0x4000
_SWP = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
FLASHW_ALL = 0x3
FLASHW_TIMERNOFG = 0xC


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("uCount", ctypes.c_uint),
        ("dwTimeout", ctypes.c_uint),
    ]


def _user32():
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    u.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    return u


def _pencere_iscisi() -> None:
    while True:
        is_ = _kuyruk.get()
        try:
            if is_ == "one_al":
                _one_al_gercek()
            elif is_ == "ustten_indir":
                _ustten_indir()
        except Exception as exc:  # pragma: no cover - platforma bagli
            log.warning("Pencere islemi basarisiz: %s", exc)
        finally:
            _kuyruk.task_done()


def _isciyi_baslat() -> None:
    global _isci_basladi
    if _isci_basladi:
        return
    threading.Thread(
        target=_pencere_iscisi, daemon=True, name="pencere"
    ).start()
    _isci_basladi = True


def pencereyi_kaydet(pencere) -> None:
    """Pencerenin basligini aklimizda tutuyoruz; tutamaci basliktan buluyoruz.

    pywebview nesnesinin kendisini saklamiyoruz ki kimse yanlislikla
    onun pencere cagrilarini kullanmasin (bkz. yukaridaki kilitlenme).
    """
    global _pencere_basligi
    _pencere_basligi = pencere.title
    _isciyi_baslat()


def _tutamac() -> int | None:
    """Bu surecin, basligi bizim pencere olan ust duzey penceresi.

    FindWindow tek basina yetmiyor: eski bir Tekeat surecinin (ya da
    ayni adli baska bir uygulamanin) penceresini bulabilir. Surec
    kimligiyle eslestiriyoruz.
    """
    global _hwnd
    if not _windows_mu() or not _pencere_basligi:
        return None
    u = _user32()
    if _hwnd and u.IsWindow(_hwnd):
        return _hwnd

    import os
    from ctypes import wintypes

    bulunan: list[int] = []
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def gez(hwnd, _):
        sahip = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(sahip))
        if sahip.value != pid:
            return True
        tampon = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, tampon, 256)
        if tampon.value == _pencere_basligi:
            bulunan.append(hwnd)
            return False
        return True

    u.EnumWindows(gez, 0)
    _hwnd = bulunan[0] if bulunan else None
    return _hwnd


def one_al() -> bool:
    """Pencereyi one getirmeyi KUYRUGA birakir ve hemen doner.

    Donus degeri "istek alindi" demek; pencere bir an sonra one
    geliyor. Cagiran taraf (yeni siparis uyarisi) sonucu beklemiyor
    zaten, beklemesi de dogru degil.
    """
    if _pencere_basligi is None:
        return False
    _isciyi_baslat()
    try:
        _kuyruk.put_nowait("one_al")
        return True
    except queue.Full:
        # Ust uste gelen istekler birikmisse zaten one aliniyor.
        return True


def _one_al_gercek() -> None:
    """Asil pencere cagrilari; YALNIZCA pencere parcaciginda calisir.

    Kalici olarak ustte tutmuyoruz; bir an ustte tutup birakmak
    pencereyi one cikarmaya yetiyor ve kullanicinin baska bir isi
    varsa onu kilitlemiyoruz. HWND_NOTOPMOST pencereyi yine de diger
    normal pencerelerin ustunde birakiyor.

    Odak calmiyoruz (SWP_NOACTIVATE): kasiyer baska bir programa bir
    sey yazarken klavyesi bizim pencereye kaymasin. Gorev cubugu
    dugmesi pencere one gelene kadar yanip sonuyor.
    """
    hwnd = _tutamac()
    if hwnd is None:
        return
    u = _user32()
    # SW_RESTORE simge durumundaki pencereyi ONCEKI haline dondurur:
    # tam ekrandaysa tam ekran kalir.
    if u.IsIconic(hwnd):
        u.ShowWindowAsync(hwnd, SW_RESTORE)
    elif not u.IsWindowVisible(hwnd):
        u.ShowWindowAsync(hwnd, SW_SHOW)
    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, _SWP)

    bilgi = _FLASHWINFO(
        ctypes.sizeof(_FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0
    )
    u.FlashWindowEx(ctypes.byref(bilgi))

    time.sleep(1.2)
    _ustten_indir()


def _ustten_indir() -> None:
    hwnd = _tutamac()
    if hwnd is not None:
        _user32().SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, _SWP)
