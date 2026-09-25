"""Kullanici ayarlari: kucuk bir JSON dosyasi.

Veritabanina tablo acmak yerine dosya: ayar sayisi az, semasi sik
degisiyor ve bozulursa varsayilana donmesi yeterli.
"""

from __future__ import annotations

import json
import logging
import threading

from .paths import data_dir

log = logging.getLogger("tekeat.ayarlar")

VARSAYILAN = {
    "yazici": {
        "ad": "",              # bos: varsayilan Windows yazicisi
        "genislik_mm": 80,     # 58 veya 80
        "kopya": 1,
        "otomatik": True,      # siparis kabul edilince fis bassin
        "baslik": "TEKEAT",
        # 0 = ASCII. Hangi kod sayfasinin calistigini yazici
        # kalibrasyon fisinden ogreniyoruz.
        "kod_sayfasi": 0,
        # Kendi kuryemizle giden sipariste fisin altina Google Maps rota
        # QR'i. QR desteklemeyen yazicida kapatilir.
        "rota_qr": True,
    },
    # Siparis ekrani tercihleri. Eskiden localStorage'daydi; pywebview
    # gizli modda acildigi icin her acilista siliniyor, hazirlik suresi
    # 20'ye donuyordu.
    "siparis": {
        "hazirlik_dk": 20,
        # Acikken yoklayici yeni siparisi hazirlik_dk ile kendisi
        # kabul ediyor (bkz. yoklayici.otomatik_kabul).
        "oto_onay": False,
        "alarm_sessiz": False,
    },
    # Siparis basina net kar: satis - malzeme (FIFO) - komisyon - kurye -
    # ambalaj. Komisyon platform basina yuzde; ambalaj siparis basina TL.
    "maliyet": {
        "komisyon": {"tgo": 0.0, "yemeksepeti": 0.0, "migros": 0.0},
        "ambalaj_tl": 0.0,
        # Bu andan SONRA teslim edilen siparisler stoktan dusulur. Gecmis
        # aktarimiyla gelen eski siparisler bugunku stoku eritmesin.
        # Ilk stok kalemi olusturulunca kendiliginden atanir.
        "stok_baslangic": None,
    },
    # Kendi kuryelerimiz; siparise atanabilsin diye.
    "kuryeler": [],
    # Paket ucreti: taban_km'ye kadar taban_ucret, sonrasinda her
    # baslayan kilometre icin km_basi. Platform mesafeyi vermiyor,
    # kus ucusu hesapliyoruz; yol_carpani gercek yolu yaklasiklamak
    # icin (1.3 sehir ici icin makul bir baslangic).
    "teslimat": {
        "taban_km": 3.0,
        "taban_ucret": 100.0,
        "km_basi": 25.0,
        # Kus ucusu gercek yoldan kisadir; sehir ici icin ~1.3 makul bir
        # yaklasiklama. Ucret esigine yakin siparisleri eksik
        # hesaplamamak icin varsayilan bu.
        "yol_carpani": 1.3,
    },
}

_kilit = threading.Lock()


def _dosya():
    return data_dir() / "ayarlar.json"


def _birlestir(varsayilan: dict, gelen: dict) -> dict:
    """Eksik anahtarlar varsayilandan gelsin (surum atlamalarinda onemli)."""
    sonuc = dict(varsayilan)
    for k, v in (gelen or {}).items():
        if isinstance(v, dict) and isinstance(sonuc.get(k), dict):
            sonuc[k] = _birlestir(sonuc[k], v)
        else:
            sonuc[k] = v
    return sonuc


# Okunan ayarin bellekteki kopyasi.
#
# oku() sicak yolda: her siparis yoklamasinda (teslimat kurali) ve her
# fiste cagriliyor. Diskten okumak tek basina ucuz ama olay dongusunu
# bloklayan senkron bir islem; 12 saniyede bir, baglanti basina bir kez
# yapmanin anlami yok. Dosya disaridan degisirse mtime'dan anliyoruz.
_onbellek: tuple[float, dict] | None = None


def oku() -> dict:
    """Ayarlar. Dosya degismediyse bellekten doner."""
    global _onbellek
    with _kilit:
        dosya = _dosya()
        try:
            damga = dosya.stat().st_mtime_ns
        except OSError:
            _onbellek = None
            return dict(VARSAYILAN)

        if _onbellek is not None and _onbellek[0] == damga:
            # Cagiran taraf uzerinde oynayabilir; kopya veriyoruz.
            return json.loads(json.dumps(_onbellek[1]))

        try:
            ham = json.loads(dosya.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Ayar dosyasi okunamadi, varsayilana donuluyor: %s", exc)
            _onbellek = None
            return dict(VARSAYILAN)

        birlesik = _birlestir(VARSAYILAN, ham)
        _onbellek = (damga, birlesik)
        return json.loads(json.dumps(birlesik))


def yaz(yeni: dict) -> dict:
    global _onbellek
    mevcut = oku()
    birlesik = _birlestir(mevcut, yeni)
    with _kilit:
        dosya = _dosya()
        dosya.write_text(
            json.dumps(birlesik, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            _onbellek = (dosya.stat().st_mtime_ns, birlesik)
        except OSError:
            _onbellek = None
    return birlesik
