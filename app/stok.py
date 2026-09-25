"""Stok, receteler ve siparis basina maliyet.

Kararlar (kullaniciyla netlesti, 2026-09):
  * MALIYET HER MALZEMEDE FIFO: her stok girisi (alis) kendi birim
    maliyetiyle ayri bir parti; tuketim en eski partiden. Ilk parti
    bitince siradaki partinin fiyati otomatik devreye girer.
  * Takip tipi yalnizca MIKTARIN nasil belirlendigini soyler:
      - "net": gramaji/adedi kesin ana malzeme (et g, peynir adet).
      - "destek": porsiyonu degisken yardimci malzeme (domates, tursu).
        Recetede urun basina bir ARALIK yazilir (or. 40-60 g) ve her zaman
        UST sinir dusulur; aradaki fark bilerek kabul edilen fire.
    (Once destek icin agirlikli ortalama maliyet denendi; kullanici
    "ortalama" ile maliyeti degil miktar tahminini kastediyordu. 2026-09)
  * YENIDEN HESAPLAMA (_yeniden): bir malzemenin butun hareketleri tarih
    sirasiyla bastan oynatilir, partiler ve her tuketimin maliyeti
    yeniden kurulur. Geri alinan hareket ile duzeltmesi atlanir (hic
    olmamis gibi). Eksi stokta tuketilen miktar sonradan gelen alisin
    GERCEK fiyatini alir. Teslim edilmis siparislerin maliyeti de
    guncellenir (kullanici karari). Her elle hareket ve geri almadan
    sonra o malzeme icin otomatik calisir; arayuzde elle de tetiklenir.
  * Ekstra istek: ekstralar menude ayri urun (kendi receteleri var). Ana
    urunun recetesinde ayni malzeme icin "ekstra_miktar" yazildiysa O
    urune ozel miktar kullanilir (ekstra domates Adana'da 40, lahmacunda
    20 g olabilir); yazilmadiysa ekstranin kendi recetesi.
  * Bu ayarlar YALNIZCA programin icinde; platforma (Trendyol) gitmez.
  * Stoktan dusum siparis TESLIM EDILINCE (status delivered). Teslimden
    sonra iptal gelirse dusulen partiler ayni maliyetle geri eklenir.
  * Eksi stoka izin var: platform siparisini durduramayiz. Yetmeyen
    miktar son alis fiyatiyla "tahmini" maliyetlenir, hareket ve siparis
    kaydinda isaretlenir; sonraki alis once bu acigi kapatir ve yeniden
    hesaplama o tuketimi alisin gercek fiyatiyla duzeltir.
  * Net kar = satis - malzeme - platform komisyonu - kurye ucreti - ambalaj.

Birimler temel: g, ml, adet. Arayuz kg/lt ile girebilir, burada hep temel.

Recete eslestirme: once platform urun kimligi, yoksa katlanmis urun adi.
Siparisteki "cikar" (removed) o urunun recetesindeki bagli kalemi
dusurmez; opsiyon ve ekstralar kendi receteleriyle eklenir.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from . import ayarlar
from .models import Order, OrderStatus
from .store import _conn, _utc, ara_bicim

BIRIMLER = ("g", "ml", "adet")
TAKIPLER = ("net", "destek")
HAREKET_TURLERI = ("alis", "cikis", "fire", "sayim", "siparis", "iade", "duzeltme")
_KUCUK = 1e-9


def _simdi() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------
# Kalemler
# ---------------------------------------------------------------


def _stok_baslangicini_ata() -> None:
    """Ilk kalem olusunca atanir; o andan once teslim edilenler dusulmez."""
    m = ayarlar.oku().get("maliyet", {})
    if not m.get("stok_baslangic"):
        ayarlar.yaz({"maliyet": {"stok_baslangic": _simdi()}})


# Ayni malzemenin iki alis fiyati arasinda bu kattan fazla fark varsa
# buyuk ihtimalle yazim hatasi (kg yerine g, fazla sifir). Sahada 250 TL/kg
# domatesin yanina 11.111 TL/kg girildi; fiyatlar dogal olarak bu kadar
# oynamiyor.
SUPHELI_ORAN = 5.0


def _supheli_fiyat(con, kalem_id: int) -> bool:
    r = con.execute(
        "SELECT MIN(h.tutar / h.miktar) AS en_az, MAX(h.tutar / h.miktar) AS en_cok FROM stok_hareketleri h "
        "WHERE h.kalem_id = ? AND h.tur = 'alis' AND h.geri_alan IS NULL AND h.miktar > 0 AND h.tutar > 0",
        (kalem_id,),
    ).fetchone()
    return bool(r["en_az"] and r["en_cok"] and r["en_cok"] / r["en_az"] > SUPHELI_ORAN)


def _kalem_satiri(con: sqlite3.Connection, r: sqlite3.Row) -> dict:
    miktar = con.execute(
        "SELECT COALESCE(SUM(miktar), 0) FROM stok_hareketleri WHERE kalem_id = ?", (r["id"],)
    ).fetchone()[0]
    deger = con.execute(
        "SELECT COALESCE(SUM(kalan * birim_fiyat), 0) FROM stok_katmanlari "
        "WHERE kalem_id = ? AND kalan > 0",
        (r["id"],),
    ).fetchone()[0]
    son = con.execute(
        "SELECT birim_fiyat FROM stok_katmanlari WHERE kalem_id = ? ORDER BY tarih DESC, id DESC LIMIT 1",
        (r["id"],),
    ).fetchone()
    return {
        "id": r["id"],
        "ad": r["ad"],
        "birim": r["birim"],
        "takip": r["takip"],
        "kategori": r["kategori"],
        "min_seviye": r["min_seviye"],
        "platform_malzeme": json.loads(r["platform_malzeme"] or "[]"),
        "miktar": round(miktar, 4),
        "deger": round(deger, 2),
        # Bir sonraki tuketimin birim maliyeti: kalani olan en eski partinin
        # fiyati (FIFO). O parti bitince kendiliginden siradakine gecer.
        "birim_maliyet": _birim_maliyet(con, r["id"]) if son else None,
        # Eldeki (kalani olan) parti sayisi: birden fazlaysa siradaki
        # parti bitince birim maliyet degisecek.
        "parti_sayisi": con.execute(
            "SELECT COUNT(*) FROM stok_katmanlari WHERE kalem_id = ? AND kalan > ?", (r["id"], _KUCUK)
        ).fetchone()[0],
        "son_alis_fiyati": son["birim_fiyat"] if son else None,
        "supheli_fiyat": _supheli_fiyat(con, r["id"]),
        "eksi": miktar < -_KUCUK,
        # Minimum seviye "siparis verme noktasi": esitken de uyar.
        "kritik": r["min_seviye"] is not None and miktar <= r["min_seviye"],
    }


# ---------------------------------------------------------------
# Denetim kaydi (audit trail) -- tablo store.py'de, degistirilemez.
# ---------------------------------------------------------------

DENETIM_ISLEMLERI = ("hareket_duzeltme", "hareket_geri_alma", "kalem_guncelleme", "kalem_silme")


def _sebep_zorunlu(sebep: str | None) -> str:
    s = (sebep or "").strip()
    if len(s) < 3:
        raise ValueError("Degisikligin sebebini yazin (denetim kaydi icin zorunlu).")
    return s[:300]


def _stok_durumu(con, kalem_id: int) -> dict:
    miktar = con.execute("SELECT COALESCE(SUM(miktar), 0) FROM stok_hareketleri WHERE kalem_id = ?",
                         (kalem_id,)).fetchone()[0]
    deger = con.execute("SELECT COALESCE(SUM(kalan * birim_fiyat), 0) FROM stok_katmanlari "
                        "WHERE kalem_id = ? AND kalan > 0", (kalem_id,)).fetchone()[0]
    return {"miktar": round(miktar, 4), "deger": round(deger, 2)}


def _denetim(con, islem: str, kullanici: str | None, kalem_row, degisiklikler: list[dict],
             sebep: str | None, etki: dict | None = None, hareket=None) -> None:
    con.execute(
        "INSERT INTO stok_denetim (tarih, kullanici, islem, kalem_id, kalem_adi, birim, hareket_id, hareket_tur, "
        "hareket_tarih, degisiklikler, sebep, etki) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_simdi(), kullanici, islem, kalem_row["id"], kalem_row["ad"], kalem_row["birim"],
         hareket["id"] if hareket else None, hareket["tur"] if hareket else None,
         hareket["tarih"] if hareket else None,
         json.dumps(degisiklikler, ensure_ascii=False), sebep, json.dumps(etki or {}, ensure_ascii=False)),
    )


def denetim_kayitlari(kalem_id: int | None = None, hareket_id: int | None = None, limit: int = 500) -> list[dict]:
    """Denetim kaydi, en yenisi once."""
    kosul, deger = [], []
    if kalem_id is not None:
        kosul.append("kalem_id = ?"); deger.append(kalem_id)
    if hareket_id is not None:
        kosul.append("hareket_id = ?"); deger.append(hareket_id)
    where = f"WHERE {' AND '.join(kosul)}" if kosul else ""
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM stok_denetim {where} ORDER BY tarih DESC, id DESC LIMIT ?", [*deger, limit]
        ).fetchall()
    return [{**dict(r), "degisiklikler": json.loads(r["degisiklikler"]), "etki": json.loads(r["etki"])} for r in rows]


def kalemler() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM stok_kalemleri ORDER BY ad COLLATE NOCASE").fetchall()
        return [_kalem_satiri(con, r) for r in rows]


def kalem(kalem_id: int) -> dict | None:
    with _conn() as con:
        r = con.execute("SELECT * FROM stok_kalemleri WHERE id = ?", (kalem_id,)).fetchone()
        return _kalem_satiri(con, r) if r else None


_KALEM_ALANLARI = ("ad", "birim", "takip", "kategori", "min_seviye")


def kalem_kaydet(veri: dict, kalem_id: int | None = None, kullanici: str | None = None) -> dict:
    """Kalem olusturur/gunceller. Ad tekil (buyuk/kucuk harf duyarsiz).

    Takip tipi hareketi olan kalemde de degisebilir: partiler ayni kaliyor,
    yalnizca bundan sonraki tuketimin nasil maliyetlenecegi degisiyor.
    """
    if veri["birim"] not in BIRIMLER:
        raise ValueError(f"Birim {BIRIMLER} olmali.")
    takip = veri.get("takip") or "net"
    if takip not in TAKIPLER:
        raise ValueError(f"Takip {TAKIPLER} olmali.")
    with _conn() as con:
        ayni = con.execute(
            "SELECT id FROM stok_kalemleri WHERE ad = ? COLLATE NOCASE AND id IS NOT ?",
            (veri["ad"].strip(), kalem_id),
        ).fetchone()
        if ayni:
            raise ValueError(f"'{veri['ad']}' adinda bir kalem zaten var.")
        baglar = json.dumps(sorted(set(veri.get("platform_malzeme") or [])))
        if kalem_id is None:
            ilk = con.execute("SELECT COUNT(*) FROM stok_kalemleri").fetchone()[0] == 0
            cur = con.execute(
                "INSERT INTO stok_kalemleri (ad, birim, takip, kategori, min_seviye, platform_malzeme, olusturma) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (veri["ad"].strip(), veri["birim"], takip, veri.get("kategori"), veri.get("min_seviye"),
                 baglar, _simdi()),
            )
            kalem_id = cur.lastrowid
        else:
            ilk = False
            hareket = con.execute(
                "SELECT COUNT(*) FROM stok_hareketleri WHERE kalem_id = ?", (kalem_id,)
            ).fetchone()[0]
            eski = con.execute("SELECT * FROM stok_kalemleri WHERE id = ?", (kalem_id,)).fetchone()
            if eski is None:
                raise KeyError(kalem_id)
            # Hareketi olan kalemin birimi degisirse eski miktarlar anlamsizlasir.
            if hareket and eski["birim"] != veri["birim"]:
                raise ValueError("Hareketi olan kalemin birimi degistirilemez.")
            yeni = {"ad": veri["ad"].strip(), "birim": veri["birim"], "takip": takip,
                    "kategori": veri.get("kategori"), "min_seviye": veri.get("min_seviye")}
            con.execute(
                "UPDATE stok_kalemleri SET ad = ?, birim = ?, takip = ?, kategori = ?, min_seviye = ?, "
                "platform_malzeme = ? WHERE id = ?",
                (yeni["ad"], yeni["birim"], takip, yeni["kategori"], yeni["min_seviye"], baglar, kalem_id),
            )
            degisen = [{"alan": a, "once": eski[a], "sonra": yeni[a]} for a in _KALEM_ALANLARI if eski[a] != yeni[a]]
            if degisen:
                # Adi degistiyse kayitta YENI ad gorunsun, eskisi degisiklikte.
                _denetim(con, "kalem_guncelleme", kullanici, {**dict(eski), "ad": yeni["ad"]}, degisen,
                         veri.get("sebep"))
    if ilk:
        _stok_baslangicini_ata()
    return kalem(kalem_id)


def kalem_sil(kalem_id: int, sebep: str | None = None, kullanici: str | None = None) -> None:
    """Malzemeyi ve ELLE girilen hareketlerini siler.

    Siparisten dusulmus bir hareketi varsa silinmez: o siparislerin
    maliyeti bu malzemeye dayaniyor. Yanlis/deneme amacli acilmis ve
    yalnizca elle alis-sayim girilmis malzeme ise temizlenebilmeli.
    Hareketi olan malzemenin silinmesi denetim kaydina sebebiyle yazilir;
    kayit malzemeyle birlikte silinmez.
    """
    with _conn() as con:
        k = con.execute("SELECT * FROM stok_kalemleri WHERE id = ?", (kalem_id,)).fetchone()
        if k is None:
            raise KeyError(kalem_id)
        if con.execute(
            "SELECT COUNT(*) FROM stok_hareketleri WHERE kalem_id = ? AND tur IN ('siparis', 'iade')", (kalem_id,)
        ).fetchone()[0]:
            raise ValueError("Bu malzeme siparislerden dusulmus; silinirse o siparislerin maliyeti bozulur.")
        receteler_ = [r["urun_adi"] for r in con.execute(
            "SELECT r.urun_adi FROM recete_satirlari s JOIN receteler r ON r.id = s.recete_id "
            "WHERE s.kalem_id = ? ORDER BY r.urun_adi", (kalem_id,)).fetchall()]
        if receteler_:
            raise ValueError("Once su recetelerden cikarin: " + ", ".join(receteler_))
        hareket_sayisi = con.execute("SELECT COUNT(*) FROM stok_hareketleri WHERE kalem_id = ?",
                                     (kalem_id,)).fetchone()[0]
        if hareket_sayisi:
            sebep = _sebep_zorunlu(sebep)
        _denetim(con, "kalem_silme", kullanici, k,
                 [{"alan": "durum", "once": "kayitli", "sonra": "silindi"}], (sebep or "").strip() or None,
                 {"silinen_hareket": hareket_sayisi, "once": _stok_durumu(con, kalem_id)})
        con.execute("DELETE FROM stok_katmanlari WHERE kalem_id = ?", (kalem_id,))
        con.execute("DELETE FROM hareket_duzeltmeleri WHERE hareket_id IN "
                    "(SELECT id FROM stok_hareketleri WHERE kalem_id = ?)", (kalem_id,))
        con.execute("DELETE FROM stok_hareketleri WHERE kalem_id = ?", (kalem_id,))
        con.execute("DELETE FROM stok_kalemleri WHERE id = ?", (kalem_id,))


# ---------------------------------------------------------------
# Hareketler (FIFO)
# ---------------------------------------------------------------


def _mevcut(con: sqlite3.Connection, kalem_id: int) -> float:
    return con.execute(
        "SELECT COALESCE(SUM(miktar), 0) FROM stok_hareketleri WHERE kalem_id = ?", (kalem_id,)
    ).fetchone()[0]


def _son_fiyat(con: sqlite3.Connection, kalem_id: int) -> float:
    r = con.execute(
        "SELECT birim_fiyat FROM stok_katmanlari WHERE kalem_id = ? ORDER BY tarih DESC, id DESC LIMIT 1",
        (kalem_id,),
    ).fetchone()
    return r["birim_fiyat"] if r else 0.0


def _giris(con, kalem_id: int, miktar: float, birim_fiyat: float, tur: str,
           aciklama: str | None, order_id: str | None = None, tarih: str | None = None) -> int:
    """Stok girisi. Eksi stok varsa yeni parti once o acigi kapatir.

    Acik, tuketim aninda son fiyatla tahmini maliyetlenmisti; burada
    yalnizca miktar kapatiliyor (gecmis siparisin maliyeti geriye donuk
    degistirilmiyor -- kapanmis bir siparisin kari sonradan oynamasin).
    """
    tarih = tarih or _simdi()
    acik = max(0.0, -_mevcut(con, kalem_id))
    cur = con.execute(
        "INSERT INTO stok_hareketleri (kalem_id, tarih, tur, miktar, tutar, aciklama, order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kalem_id, tarih, tur, miktar, round(miktar * birim_fiyat, 4), aciklama, order_id),
    )
    kalan = max(0.0, miktar - acik)
    con.execute(
        "INSERT INTO stok_katmanlari (kalem_id, tarih, miktar, kalan, birim_fiyat, hareket_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kalem_id, tarih, miktar, kalan, birim_fiyat, cur.lastrowid),
    )
    return cur.lastrowid


def _tuket(con, kalem_id: int, miktar: float) -> tuple[float, float]:
    """FIFO tuketim; (maliyet, karsilanamayan miktar). Hareket YAZMAZ.

    Her malzemede ayni: en eski partiden baslanir, o parti bitince
    siradakinin fiyatina gecilir.
    """
    katmanlar = con.execute(
        "SELECT id, kalan, birim_fiyat FROM stok_katmanlari WHERE kalem_id = ? AND kalan > 0 "
        "ORDER BY tarih, id",
        (kalem_id,),
    ).fetchall()
    kalan_ihtiyac = miktar
    maliyet = 0.0
    for k in katmanlar:
        if kalan_ihtiyac <= _KUCUK:
            break
        al = min(k["kalan"], kalan_ihtiyac)
        con.execute("UPDATE stok_katmanlari SET kalan = kalan - ? WHERE id = ?", (al, k["id"]))
        maliyet += al * k["birim_fiyat"]
        kalan_ihtiyac -= al
    eksik = max(0.0, kalan_ihtiyac)
    if eksik > _KUCUK:
        maliyet += eksik * _son_fiyat(con, kalem_id)
    return maliyet, eksik


def _cikis(con, kalem_id: int, miktar: float, tur: str, aciklama: str | None,
           order_id: str | None = None) -> tuple[float, float]:
    maliyet, eksik = _tuket(con, kalem_id, miktar)
    con.execute(
        "INSERT INTO stok_hareketleri (kalem_id, tarih, tur, miktar, tutar, eksik, aciklama, order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kalem_id, _simdi(), tur, -miktar, round(maliyet, 4), round(eksik, 4), aciklama, order_id),
    )
    return maliyet, eksik


def hareket_ekle(kalem_id: int, tur: str, miktar: float, birim_fiyat: float | None = None,
                 aciklama: str | None = None, kullanici: str | None = None) -> dict:
    """Elle hareket: alis (giris), cikis, fire, sayim (sayilan miktara esitle).

    Donus: kalemin son hali + "hareket_id" (arayuz "Geri al" sunsun diye;
    sayimda fark yoksa None).
    """
    if tur not in ("alis", "cikis", "fire", "sayim"):
        raise ValueError("Tur alis, cikis, fire ya da sayim olmali.")
    if miktar < 0 or (tur != "sayim" and miktar <= 0):
        raise ValueError("Miktar pozitif olmali.")
    hid = None
    with _conn() as con:
        if con.execute("SELECT 1 FROM stok_kalemleri WHERE id = ?", (kalem_id,)).fetchone() is None:
            raise KeyError(kalem_id)
        if tur == "alis":
            if birim_fiyat is None or birim_fiyat < 0:
                raise ValueError("Alista birim fiyat gerekli.")
            hid = _giris(con, kalem_id, miktar, birim_fiyat, "alis", aciklama)
        elif tur in ("cikis", "fire"):
            _cikis(con, kalem_id, miktar, tur, aciklama)
            hid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            fark = miktar - _mevcut(con, kalem_id)
            if fark > _KUCUK:
                fiyat = birim_fiyat if birim_fiyat is not None else _son_fiyat(con, kalem_id)
                hid = _giris(con, kalem_id, fark, fiyat, "sayim", aciklama or "Sayim fazlasi")
            elif fark < -_KUCUK:
                _cikis(con, kalem_id, -fark, "sayim", aciklama or "Sayim eksigi")
                hid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Eksi stoktayken gelen alis: acigi kapatir; o acikla tuketilmis
        # siparislerin tahmini maliyeti bu alisin GERCEK fiyatina oturur.
        if hid is not None:
            con.execute("UPDATE stok_hareketleri SET kullanici = ? WHERE id = ?", (kullanici, hid))
            _siparisleri_guncelle(con, _yeniden(con, kalem_id))
    return {**kalem(kalem_id), "hareket_id": hid}


GERI_ALINAMAZ = ("siparis", "iade", "duzeltme")
_TUR_ADI = {"alis": "alisi", "cikis": "cikisi", "fire": "firesi", "sayim": "sayimi"}


def hareket_geri_al(hareket_id: int, sebep: str | None = None, kullanici: str | None = None) -> dict:
    """Elle girilmis bir hareketi (alis, cikis, fire, sayim) geri alir.

    Silmiyoruz; ters yonde bir "duzeltme" hareketi yaziliyor ve ikisi
    birbirine baglaniyor, kayit izlenebilir kalir. Ardindan malzeme
    yeniden hesaplanir: ikili hic olmamis gibi atlanir, partiler ve
    o arada teslim edilen siparislerin maliyeti dogru fiyatla kurulur
    (yanlis fiyatli alistan tuketen siparis gercek partiye oturur).
    Sebep zorunlu; kim, ne zaman, neden denetim kaydina yazilir.
    """
    with _conn() as con:
        h = con.execute("SELECT * FROM stok_hareketleri WHERE id = ?", (hareket_id,)).fetchone()
        if h is None:
            raise KeyError(hareket_id)
        sebep = _sebep_zorunlu(sebep)
        if h["tur"] in GERI_ALINAMAZ:
            raise ValueError(
                "Siparis ve iptal hareketleri siparisin durumuna bagli; buradan geri alinamaz."
                if h["tur"] != "duzeltme" else "Duzeltme hareketi geri alinamaz."
            )
        if h["geri_alan"]:
            raise ValueError("Bu hareket zaten geri alindi.")
        kid, miktar = h["kalem_id"], h["miktar"]
        # Kullanici hareket numarasini bilmiyor; tarih ve tur ile tanir.
        ne_zaman = datetime.fromisoformat(h["tarih"]).astimezone().strftime("%d.%m %H:%M")
        aciklama = f"Geri alindi: {ne_zaman} {_TUR_ADI.get(h['tur'], h['tur'])}"
        # Parti yazilmiyor: yeniden hesaplama ikiliyi zaten atliyor ve
        # partileri bastan kuruyor. (Eskiden geri alinan alisin bos partisi
        # "son alis fiyati" sayilip yanlis fiyati gostermeye devam ediyordu.)
        onceki_durum, onceki_siparis = _stok_durumu(con, kid), _siparis_maliyetleri(con)
        cur = con.execute(
            "INSERT INTO stok_hareketleri (kalem_id, tarih, tur, miktar, tutar, aciklama, geri_aldigi, kullanici) "
            "VALUES (?, ?, 'duzeltme', ?, ?, ?, ?, ?)",
            (kid, _simdi(), -miktar, h["tutar"], aciklama, h["id"], kullanici),
        )
        con.execute("UPDATE stok_hareketleri SET geri_alan = ? WHERE id = ?", (cur.lastrowid, h["id"]))
        _siparisleri_guncelle(con, _yeniden(con, kid))
        k = con.execute("SELECT * FROM stok_kalemleri WHERE id = ?", (kid,)).fetchone()
        _denetim(
            con, "hareket_geri_alma", kullanici, k,
            [{"alan": "durum", "once": "gecerli", "sonra": "geri alindi"},
             {"alan": "miktar", "once": abs(miktar), "sonra": 0},
             {"alan": "tutar", "once": h["tutar"], "sonra": 0}],
            sebep, _etki(con, kid, onceki_durum, onceki_siparis), hareket=h,
        )
    return kalem(kid)


def _etki(con, kalem_id: int, onceki_durum: dict, onceki_siparis: dict[str, float]) -> dict:
    """Degisikligin stok ve siparis maliyetlerine etkisi (denetim kaydi icin)."""
    sonrasi = _siparis_maliyetleri(con)
    degisen = [o for o in sonrasi if abs(sonrasi[o] - onceki_siparis.get(o, sonrasi[o])) > 0.005]
    return {
        "stok_once": onceki_durum,
        "stok_sonra": _stok_durumu(con, kalem_id),
        "degisen_siparis": len(degisen),
        "maliyet_farki": round(sum(sonrasi[o] - onceki_siparis.get(o, sonrasi[o]) for o in degisen), 2),
    }


DUZELTILEBILIR = ("alis", "cikis", "fire")


def hareket_duzelt(hareket_id: int, miktar: float, birim_fiyat: float | None = None,
                   sebep: str | None = None, kullanici: str | None = None) -> dict:
    """Elle girilmis alis/cikis/fire'nin miktarini (ve alista fiyatini)
    YERINDE duzeltir, sonra malzemeyi yeniden hesaplar.

    Neden geri al + yeniden gir degil: yanlis fiyatli alistan sonra fire
    ya da siparis dusulmusse, geri alinca onlar alissiz kalip eksiye
    dusuyordu (sahada: 1 kg domates 11.111 TL girildi, ardindan fire).
    Yerinde duzeltmede hareket ayni tarihte kalir; ondan tuketen her sey
    FIFO yeniden oynatilinca dogru fiyata oturur. Kim, ne zaman, neden ve
    alan alan once/sonra denetim kaydina (stok_denetim) yazilir.
    """
    if miktar <= 0:
        raise ValueError("Miktar pozitif olmali.")
    with _conn() as con:
        h = con.execute("SELECT * FROM stok_hareketleri WHERE id = ?", (hareket_id,)).fetchone()
        if h is None:
            raise KeyError(hareket_id)
        sebep = _sebep_zorunlu(sebep)
        if h["tur"] not in DUZELTILEBILIR:
            raise ValueError(
                "Yalnizca elle girilen alis, cikis ve fire duzeltilebilir. "
                "Sayimi geri alip yeniden sayin; siparis hareketleri siparise bagli."
            )
        if h["geri_alan"]:
            raise ValueError("Geri alinmis hareket duzeltilemez.")
        if h["tur"] == "alis":
            if birim_fiyat is None or birim_fiyat < 0:
                raise ValueError("Alista birim fiyat gerekli.")
            yeni_miktar, yeni_tutar = miktar, round(miktar * birim_fiyat, 4)
        else:
            # Cikis/fire tutari girilmez, FIFO'dan hesaplanir.
            yeni_miktar, yeni_tutar = -miktar, h["tutar"]
        if abs(yeni_miktar - h["miktar"]) < _KUCUK and abs(yeni_tutar - h["tutar"]) < 0.005:
            raise ValueError("Degisiklik yok.")
        kid = h["kalem_id"]
        onceki_durum, onceki_siparis = _stok_durumu(con, kid), _siparis_maliyetleri(con)
        con.execute("UPDATE stok_hareketleri SET miktar = ?, tutar = ? WHERE id = ?",
                    (yeni_miktar, yeni_tutar, h["id"]))
        _siparisleri_guncelle(con, _yeniden(con, kid))
        degisen = []
        if abs(yeni_miktar - h["miktar"]) >= _KUCUK:
            degisen.append({"alan": "miktar", "once": abs(h["miktar"]), "sonra": abs(yeni_miktar)})
        if h["tur"] == "alis":
            if abs(yeni_tutar - h["tutar"]) >= 0.005:
                degisen.append({"alan": "tutar", "once": h["tutar"], "sonra": yeni_tutar})
            eski_bf = h["tutar"] / h["miktar"] if h["miktar"] else 0.0
            if abs(eski_bf - birim_fiyat) > 1e-9:
                degisen.append({"alan": "birim_fiyat", "once": eski_bf, "sonra": birim_fiyat})
        else:
            # Cikis/fire tutari FIFO'dan hesaplanir; yeni tutar yeniden hesaplamadan sonra belli.
            yeni = con.execute("SELECT tutar FROM stok_hareketleri WHERE id = ?", (h["id"],)).fetchone()["tutar"]
            if abs(yeni - h["tutar"]) >= 0.005:
                degisen.append({"alan": "tutar", "once": h["tutar"], "sonra": yeni})
        k = con.execute("SELECT * FROM stok_kalemleri WHERE id = ?", (kid,)).fetchone()
        etki = _etki(con, kid, onceki_durum, onceki_siparis)
        _denetim(con, "hareket_duzeltme", kullanici, k, degisen, sebep, etki, hareket=h)
    return {**kalem(kid), "degisen_siparis": etki["degisen_siparis"], "maliyet_farki": etki["maliyet_farki"]}


def _siparis_maliyetleri(con) -> dict[str, float]:
    return {r["order_id"]: r["malzeme_maliyeti"] for r in con.execute(
        "SELECT order_id, malzeme_maliyeti FROM siparis_stok WHERE durum = 'dusuldu'").fetchall()}


def hareketler(kalem_id: int | None = None, limit: int = 200) -> list[dict]:
    kosul, deger = "", []
    if kalem_id is not None:
        kosul, deger = "WHERE h.kalem_id = ?", [kalem_id]
    with _conn() as con:
        rows = con.execute(
            "SELECT h.*, k.ad AS kalem_adi, k.birim FROM stok_hareketleri h "
            f"JOIN stok_kalemleri k ON k.id = h.kalem_id {kosul} "
            "ORDER BY h.tarih DESC, h.id DESC LIMIT ?",
            [*deger, limit],
        ).fetchall()
        # Her hareketin denetim kaydi (duzeltme / geri alma), eskisi once.
        denetim: dict[int, list[dict]] = defaultdict(list)
        ids = [r["id"] for r in rows]
        if ids:
            for d in con.execute(
                f"SELECT tarih, kullanici, islem, hareket_id, degisiklikler, sebep FROM stok_denetim "
                f"WHERE hareket_id IN ({','.join('?' * len(ids))}) ORDER BY tarih, id", ids,
            ).fetchall():
                denetim[d["hareket_id"]].append({**dict(d), "degisiklikler": json.loads(d["degisiklikler"])})
    return [
        {
            **dict(r),
            "geri_alinabilir": r["tur"] not in GERI_ALINAMAZ and not r["geri_alan"],
            "duzeltilebilir": r["tur"] in DUZELTILEBILIR and not r["geri_alan"],
            "denetim": denetim.get(r["id"], []),
        }
        for r in rows
    ]


# ---------------------------------------------------------------
# Yeniden hesaplama (FIFO'yu bastan oynat)
# ---------------------------------------------------------------


def _yeniden(con, kalem_id: int) -> set[str]:
    """Malzemenin butun hareketlerini tarih sirasiyla bastan oynatir.

    Miktarlar (hareketlerin toplami) degismez; degisen, partilerin kalani
    ve her tuketimin TUTARI. Kurallar:
      * Geri alinan hareket ve onun duzeltmesi atlanir (hic olmamis gibi).
      * Giris (alis, sayim fazlasi) kendi birim fiyatiyla parti olur.
        Iptal iadesi, o siparisin bu malzemeden odedigi birim maliyetle.
      * Cikis (siparis, fire, cikis, sayim eksigi) en eski partiden.
        Parti yetmezse acik bekler; sonraki giris once acigi kapatir ve
        o tuketime KENDI fiyatini yazar. Sonuna kadar kapanmayan acik son
        bilinen fiyatla tahmini kalir (eksik > 0).
    Donus: maliyeti degisebilecek siparislerin kimlikleri.
    """
    hareketler_ = con.execute(
        "SELECT * FROM stok_hareketleri WHERE kalem_id = ? ORDER BY tarih, id", (kalem_id,)
    ).fetchall()
    miktarlar = {h["id"]: h["miktar"] for h in hareketler_}
    partiler: list[dict] = []
    bekleyen: list[list] = []  # [hareket_id, kapanmamis miktar]
    tutar: dict[int, float] = {}
    eksik: dict[int, float] = {}
    siparis_hareketi: dict[str, list[int]] = defaultdict(list)
    son_fiyat: float | None = None
    siparisler: set[str] = set()

    for h in hareketler_:
        if h["geri_alan"] or h["tur"] == "duzeltme":
            continue
        if h["order_id"]:
            siparisler.add(h["order_id"])
        miktar = h["miktar"]
        if miktar > _KUCUK:
            if h["tur"] == "iade":
                ids = siparis_hareketi.get(h["order_id"], [])
                adet = sum(-miktarlar[i] for i in ids)
                deger = sum(tutar[i] + eksik[i] * (son_fiyat or 0.0) for i in ids)
                fiyat = deger / adet if adet > _KUCUK else (son_fiyat or 0.0)
                tutar[h["id"]] = miktar * fiyat
            else:
                fiyat = h["tutar"] / miktar
            kalan = miktar
            while bekleyen and kalan > _KUCUK:
                b = bekleyen[0]
                al = min(b[1], kalan)
                tutar[b[0]] += al * fiyat
                eksik[b[0]] -= al
                b[1] -= al
                kalan -= al
                if b[1] <= _KUCUK:
                    bekleyen.pop(0)
            partiler.append({"tarih": h["tarih"], "miktar": miktar, "kalan": kalan, "fiyat": fiyat, "hareket_id": h["id"]})
            son_fiyat = fiyat
        elif miktar < -_KUCUK:
            ihtiyac = -miktar
            maliyet = 0.0
            for p in partiler:
                if ihtiyac <= _KUCUK:
                    break
                if p["kalan"] <= _KUCUK:
                    continue
                al = min(p["kalan"], ihtiyac)
                p["kalan"] -= al
                maliyet += al * p["fiyat"]
                ihtiyac -= al
            tutar[h["id"]] = maliyet
            eksik[h["id"]] = max(0.0, ihtiyac)
            if eksik[h["id"]] > _KUCUK:
                bekleyen.append([h["id"], eksik[h["id"]]])
            if h["tur"] == "siparis" and h["order_id"]:
                siparis_hareketi[h["order_id"]].append(h["id"])

    fiyat_son = son_fiyat or 0.0
    con.execute("DELETE FROM stok_katmanlari WHERE kalem_id = ?", (kalem_id,))
    con.executemany(
        "INSERT INTO stok_katmanlari (kalem_id, tarih, miktar, kalan, birim_fiyat, hareket_id) VALUES (?, ?, ?, ?, ?, ?)",
        [(kalem_id, p["tarih"], p["miktar"], max(p["kalan"], 0.0), p["fiyat"], p["hareket_id"]) for p in partiler],
    )
    con.executemany(
        "UPDATE stok_hareketleri SET tutar = ?, eksik = ? WHERE id = ?",
        [(round(tutar[i] + eksik.get(i, 0.0) * fiyat_son, 4), round(eksik.get(i, 0.0), 4), i) for i in tutar],
    )
    return siparisler


def _siparisleri_guncelle(con, siparisler: set[str]) -> None:
    """Siparisin malzeme maliyeti = stok hareketlerinin toplami (tum malzemeler).

    Tahmini kisim: eksi stoktan dusulen ve henuz alisla kapanmamis miktar.
    """
    for oid in siparisler:
        r = con.execute(
            "SELECT COALESCE(SUM(tutar), 0) AS tutar, "
            "COALESCE(SUM(CASE WHEN miktar < 0 THEN eksik * tutar / -miktar ELSE 0 END), 0) AS tahmini "
            "FROM stok_hareketleri WHERE order_id = ? AND tur = 'siparis'",
            (oid,),
        ).fetchone()
        con.execute(
            "UPDATE siparis_stok SET malzeme_maliyeti = ?, tahmini_maliyet = ? WHERE order_id = ?",
            (round(r["tutar"], 4), round(r["tahmini"], 4), oid),
        )


def yeniden_hesapla(kalem_id: int | None = None) -> dict:
    """Bir malzemeyi ya da hepsini yeniden hesaplar; ne degistigini doner."""
    with _conn() as con:
        if kalem_id is not None and con.execute(
            "SELECT 1 FROM stok_kalemleri WHERE id = ?", (kalem_id,)
        ).fetchone() is None:
            raise KeyError(kalem_id)
        kimlikler = [kalem_id] if kalem_id is not None else [
            r["id"] for r in con.execute("SELECT id FROM stok_kalemleri").fetchall()
        ]
        oncesi = {r["order_id"]: r["malzeme_maliyeti"] for r in con.execute(
            "SELECT order_id, malzeme_maliyeti FROM siparis_stok WHERE durum = 'dusuldu'").fetchall()}
        deger_oncesi = con.execute(
            "SELECT COALESCE(SUM(kalan * birim_fiyat), 0) FROM stok_katmanlari").fetchone()[0]
        siparisler: set[str] = set()
        for kid in kimlikler:
            siparisler |= _yeniden(con, kid)
        _siparisleri_guncelle(con, siparisler)
        sonrasi = {r["order_id"]: r["malzeme_maliyeti"] for r in con.execute(
            "SELECT order_id, malzeme_maliyeti FROM siparis_stok WHERE durum = 'dusuldu'").fetchall()}
        deger_sonrasi = con.execute(
            "SELECT COALESCE(SUM(kalan * birim_fiyat), 0) FROM stok_katmanlari").fetchone()[0]
    degisen = [o for o in sonrasi if abs(sonrasi[o] - oncesi.get(o, sonrasi[o])) > 0.005]
    return {
        "malzeme": len(kimlikler),
        "siparis": len(siparisler),
        "degisen_siparis": len(degisen),
        "maliyet_farki": round(sum(sonrasi[o] - oncesi.get(o, sonrasi[o]) for o in degisen), 2),
        "stok_degeri_farki": round(deger_sonrasi - deger_oncesi, 2),
    }


def partiler(kalem_id: int) -> list[dict]:
    """Malzemenin butun partileri, en eskisi once; tukenenler de.

    durum: "tukendi" / "kullanimda" (siradaki tuketim buradan) / "sirada".
    """
    with _conn() as con:
        if con.execute("SELECT 1 FROM stok_kalemleri WHERE id = ?", (kalem_id,)).fetchone() is None:
            raise KeyError(kalem_id)
        rows = con.execute(
            "SELECT p.*, h.tur, h.aciklama, h.id AS h_id, h.miktar AS h_miktar, h.tutar AS h_tutar "
            "FROM stok_katmanlari p "
            "LEFT JOIN stok_hareketleri h ON h.id = p.hareket_id "
            "WHERE p.kalem_id = ? ORDER BY p.tarih, p.id",
            (kalem_id,),
        ).fetchall()
    sonuc, kullanimda_var = [], False
    for r in rows:
        if r["kalan"] <= _KUCUK:
            durum = "tukendi"
        elif not kullanimda_var:
            durum, kullanimda_var = "kullanimda", True
        else:
            durum = "sirada"
        sonuc.append({
            "id": r["id"], "tarih": r["tarih"], "tur": r["tur"], "aciklama": r["aciklama"],
            "miktar": round(r["miktar"], 4), "kalan": round(max(r["kalan"], 0.0), 4),
            "tuketilen": round(r["miktar"] - max(r["kalan"], 0.0), 4),
            "birim_fiyat": r["birim_fiyat"], "deger": round(max(r["kalan"], 0.0) * r["birim_fiyat"], 2),
            "durum": durum,
            # Alis partisi yerinde duzeltilebilir (yanlis tutar/miktar).
            "hareket_id": r["h_id"], "duzeltilebilir": r["tur"] == "alis",
            "alis_miktar": r["h_miktar"], "alis_tutar": r["h_tutar"],
        })
    return sonuc


# ---------------------------------------------------------------
# Receteler
# ---------------------------------------------------------------


def recete_anahtari(platform: str, urun_id: str | None, ad: str | None) -> str:
    return f"{platform}:id:{urun_id}" if urun_id else f"{platform}:ad:{' '.join(ara_bicim(ad or '').split())}"


def _recete_bul(con, platform: str, urun_id: str | None, ad: str | None) -> int | None:
    """Once kimlik, sonra ad. Kimligi olan recete adla da bulunabilsin
    (eski siparislerde kimlik yok)."""
    if urun_id:
        r = con.execute("SELECT id FROM receteler WHERE anahtar = ?",
                        (recete_anahtari(platform, urun_id, None),)).fetchone()
        if r:
            return r["id"]
    if ad:
        katli = " ".join(ara_bicim(ad).split())
        r = con.execute("SELECT id FROM receteler WHERE anahtar = ?",
                        (recete_anahtari(platform, None, ad),)).fetchone()
        if r:
            return r["id"]
        # Kimlikli recetelerde ada gore: tek eslesme varsa.
        adaylar = [x["id"] for x in con.execute(
            "SELECT id FROM receteler WHERE platform = ? AND ara_bicim(urun_adi) = ?",
            (platform, katli)).fetchall()]
        if len(adaylar) == 1:
            return adaylar[0]
    return None


def recete_olustur(platform: str, urun_id: str | None, urun_adi: str, tur: str = "urun") -> int:
    with _conn() as con:
        anahtar = recete_anahtari(platform, urun_id, urun_adi)
        con.execute(
            "INSERT INTO receteler (platform, anahtar, urun_id, urun_adi, tur, guncelleme) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(anahtar) DO UPDATE SET urun_adi = excluded.urun_adi",
            (platform, anahtar, urun_id, urun_adi, tur, _simdi()),
        )
        return con.execute("SELECT id FROM receteler WHERE anahtar = ?", (anahtar,)).fetchone()["id"]


def _birim_maliyet(con, kalem_id: int) -> float:
    """Bir sonraki tuketimin birim maliyeti: FIFO'da siradaki (en eski,
    kalani olan) partinin fiyati. O parti bitince bir sonrakine kendiliginden
    gecer; stok tamamen bittiyse son alis fiyati."""
    r = con.execute(
        "SELECT birim_fiyat FROM stok_katmanlari WHERE kalem_id = ? AND kalan > ? ORDER BY tarih, id LIMIT 1",
        (kalem_id, _KUCUK),
    ).fetchone()
    return r["birim_fiyat"] if r else _son_fiyat(con, kalem_id)


def receteler() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM receteler ORDER BY tur, urun_adi COLLATE NOCASE").fetchall()
        sonuc = []
        for r in rows:
            satirlar = con.execute(
                "SELECT s.kalem_id, s.miktar, s.miktar_min, s.ekstra_miktar, k.ad, k.birim, k.takip "
                "FROM recete_satirlari s JOIN stok_kalemleri k ON k.id = s.kalem_id "
                "WHERE s.recete_id = ? ORDER BY k.takip DESC, k.ad",
                (r["id"],),
            ).fetchall()
            kalemler_ = []
            for s in satirlar:
                bm = _birim_maliyet(con, s["kalem_id"])
                kalemler_.append({**dict(s), "birim_maliyet": bm, "maliyet": round(s["miktar"] * bm, 4)})
            sonuc.append({
                "id": r["id"], "platform": r["platform"], "urun_id": r["urun_id"],
                "urun_adi": r["urun_adi"], "tur": r["tur"], "satirlar": kalemler_,
                # Dusulen (ust sinir) miktarlarla: siparis maliyeti de bu.
                "porsiyon_maliyeti": round(sum(k["maliyet"] for k in kalemler_), 2),
            })
        return sonuc


def _recete_satirlari(con, recete_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT kalem_id, miktar, miktar_min, ekstra_miktar FROM recete_satirlari WHERE recete_id = ? "
        "ORDER BY kalem_id", (recete_id,)).fetchall()]


def recete_satirlarini_kaydet(recete_id: int, satirlar: list[dict], aciklama: str | None = None) -> None:
    """Satirlar: kalem_id, miktar (dusulen / ust sinir), istege bagli
    miktar_min (destek kalemde alt sinir) ve ekstra_miktar.

    Degisiklikten once eski hali recete_gecmisi'ne yazilir (bos degilse
    ve gercekten degisiyorsa): yanlis kayit tek tikla geri donulebilsin.
    """
    with _conn() as con:
        if con.execute("SELECT 1 FROM receteler WHERE id = ?", (recete_id,)).fetchone() is None:
            raise KeyError(recete_id)
        eski = _recete_satirlari(con, recete_id)
        temiz = []
        for s in satirlar:
            k = con.execute("SELECT ad, takip FROM stok_kalemleri WHERE id = ?", (s["kalem_id"],)).fetchone()
            if k is None:
                raise ValueError(f"Kalem bulunamadi: {s['kalem_id']}")
            if s["miktar"] <= 0:
                raise ValueError(f"{k['ad']}: miktar pozitif olmali.")
            alt = s.get("miktar_min")
            # Alt sinir yalnizca destek kalemde anlamli; net kalemde miktar kesin.
            if k["takip"] != "destek":
                alt = None
            if alt is not None and not 0 < alt <= s["miktar"]:
                raise ValueError(f"{k['ad']}: alt sinir 0'dan buyuk ve ust sinirdan kucuk ya da esit olmali.")
            ekstra = s.get("ekstra_miktar")
            if ekstra is not None and ekstra <= 0:
                ekstra = None
            temiz.append((recete_id, s["kalem_id"], s["miktar"], alt, ekstra))
        yeni = sorted(
            ({"kalem_id": t[1], "miktar": t[2], "miktar_min": t[3], "ekstra_miktar": t[4]} for t in temiz),
            key=lambda d: d["kalem_id"],
        )
        if eski and eski != yeni:
            # Kalem adlari da saklaniyor: malzeme sonradan silinse bile
            # gecmiste ne oldugu okunabilsin.
            adlar = {r["id"]: (r["ad"], r["birim"]) for r in con.execute("SELECT id, ad, birim FROM stok_kalemleri")}
            con.execute(
                "INSERT INTO recete_gecmisi (recete_id, tarih, satirlar, aciklama) VALUES (?, ?, ?, ?)",
                (recete_id, _simdi(), json.dumps(
                    [{**s, "ad": adlar.get(s["kalem_id"], ("?", ""))[0], "birim": adlar.get(s["kalem_id"], ("?", ""))[1]}
                     for s in eski], ensure_ascii=False), aciklama),
            )
        con.execute("DELETE FROM recete_satirlari WHERE recete_id = ?", (recete_id,))
        con.executemany(
            "INSERT INTO recete_satirlari (recete_id, kalem_id, miktar, miktar_min, ekstra_miktar) "
            "VALUES (?, ?, ?, ?, ?)",
            temiz,
        )
        con.execute("UPDATE receteler SET guncelleme = ? WHERE id = ?", (_simdi(), recete_id))


def recete_gecmisi(recete_id: int) -> list[dict]:
    """Recetenin onceki halleri, en yenisi once."""
    with _conn() as con:
        mevcut = {r["id"] for r in con.execute("SELECT id FROM stok_kalemleri")}
        rows = con.execute(
            "SELECT * FROM recete_gecmisi WHERE recete_id = ? ORDER BY tarih DESC, id DESC LIMIT 30", (recete_id,)
        ).fetchall()
    sonuc = []
    for r in rows:
        satirlar = json.loads(r["satirlar"])
        for s in satirlar:
            s["silinmis"] = s["kalem_id"] not in mevcut
        sonuc.append({"id": r["id"], "tarih": r["tarih"], "aciklama": r["aciklama"], "satirlar": satirlar})
    return sonuc


def recete_geri_yukle(recete_id: int, gecmis_id: int) -> dict:
    """Gecmisteki bir hali geri yukler. Mevcut hal de gecmise yazilir, yani
    geri yukleme de geri alinabilir. Arada silinmis malzemeler atlanir."""
    with _conn() as con:
        r = con.execute("SELECT satirlar FROM recete_gecmisi WHERE id = ? AND recete_id = ?",
                        (gecmis_id, recete_id)).fetchone()
        if r is None:
            raise KeyError(gecmis_id)
        mevcut = {x["id"] for x in con.execute("SELECT id FROM stok_kalemleri")}
    satirlar = json.loads(r["satirlar"])
    kalan = [s for s in satirlar if s["kalem_id"] in mevcut]
    recete_satirlarini_kaydet(recete_id, kalan, "Geri yuklemeden once")
    return {"atlanan": [s["ad"] for s in satirlar if s["kalem_id"] not in mevcut]}


def recete_sil(recete_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM recete_satirlari WHERE recete_id = ?", (recete_id,))
        con.execute("DELETE FROM recete_gecmisi WHERE recete_id = ?", (recete_id,))
        con.execute("DELETE FROM receteler WHERE id = ?", (recete_id,))


def hazir_urun(recete_id: int) -> dict:
    """Ayran, kutu icecek gibi aynen satilan urun: ayni adla 'adet' kalemi
    olustur ve recetesini 1 adet yap."""
    with _conn() as con:
        r = con.execute("SELECT urun_adi FROM receteler WHERE id = ?", (recete_id,)).fetchone()
        if r is None:
            raise KeyError(recete_id)
        var = con.execute("SELECT id FROM stok_kalemleri WHERE ad = ? COLLATE NOCASE", (r["urun_adi"],)).fetchone()
    k = kalem(var["id"]) if var else kalem_kaydet(
        {"ad": r["urun_adi"], "birim": "adet", "takip": "net", "kategori": "hazir"}
    )
    recete_satirlarini_kaydet(recete_id, [{"kalem_id": k["id"], "miktar": 1}], "Hazir urun yapilmadan once")
    return k


def menuden_aktar(platform: str, menu) -> dict:
    """Menudeki malzemelerden stok kalemi, urunlerden bos recete olusturur.

    Var olanlar korunur (ad/anahtar eslesirse dokunulmaz); tekrar
    calistirmak guvenli. Birim varsayilan "g": malzemeler cogunlukla
    agirlikla olculuyor; kullanici degistirir. Takip varsayilan "destek":
    menunun malzeme kutuphanesi musterinin cikarabildigi garnitur
    (sogan, domates, tursu); et gibi ana malzeme orada pek olmuyor.
    """
    yeni_kalem = yeni_recete = 0
    for m in menu.ingredients:
        bag = f"{platform}:{m.id}"
        with _conn() as con:
            var = con.execute("SELECT id, platform_malzeme FROM stok_kalemleri WHERE ad = ? COLLATE NOCASE",
                              (m.name,)).fetchone()
        if var:
            baglar = set(json.loads(var["platform_malzeme"] or "[]"))
            if bag not in baglar:
                with _conn() as con:
                    con.execute("UPDATE stok_kalemleri SET platform_malzeme = ? WHERE id = ?",
                                (json.dumps(sorted(baglar | {bag})), var["id"]))
            continue
        kalem_kaydet({"ad": m.name, "birim": "g", "takip": "destek", "kategori": "malzeme",
                      "platform_malzeme": [bag]})
        yeni_kalem += 1
    with _conn() as con:
        mevcut = {r["anahtar"] for r in con.execute("SELECT anahtar FROM receteler").fetchall()}
    for sec in menu.sections:
        for p in sec.products:
            if recete_anahtari(platform, p.id, p.name) not in mevcut:
                recete_olustur(platform, p.id, p.name, "urun")
                yeni_recete += 1
    for p in menu.orphan_products:
        # Kategorisiz urunler genelde opsiyon/ekstra.
        if recete_anahtari(platform, p.id, p.name) not in mevcut:
            recete_olustur(platform, p.id, p.name, "opsiyon")
            yeni_recete += 1
    return {"yeni_kalem": yeni_kalem, "yeni_recete": yeni_recete}


# ---------------------------------------------------------------
# Siparisten ihtiyac ve dusum
# ---------------------------------------------------------------


def _ham_ile_tamamla(o: Order, ham: str | None) -> Order:
    """Urun kimlikleri gelmeden once kaydedilmis siparisler: ham cevaptan yeniden ayristir."""
    if not ham or any(i.product_id for i in o.items):
        return o
    try:
        if o.platform.value == "tgo":
            from .adapters.tgo import TgoAdapter

            yeni = TgoAdapter._to_order(json.loads(ham))
            for eski_i, yeni_i in zip(o.items, yeni.items):
                eski_i.product_id = yeni_i.product_id
                eski_i.extra_ids = yeni_i.extra_ids
                eski_i.removed_ids = yeni_i.removed_ids
                for eo, yo in zip(eski_i.options, yeni_i.options):
                    eo.product_id = yo.product_id
    except Exception:
        pass
    return o


def ihtiyac(con, o: Order) -> tuple[dict[int, float], list[str], list[dict]]:
    """Siparisin kalem ihtiyaci; (kalem->miktar, recetesiz urunler, satir detayi)."""
    platform = o.platform.value
    toplam: dict[int, float] = defaultdict(float)
    recetesiz: list[str] = []
    detay: list[dict] = []
    # "cikar"daki malzemeler -> bagli stok kalemleri
    baglar: dict[str, set[int]] = defaultdict(set)
    ad_baglari: dict[str, set[int]] = defaultdict(set)
    for r in con.execute("SELECT id, ad, platform_malzeme FROM stok_kalemleri").fetchall():
        for b in json.loads(r["platform_malzeme"] or "[]"):
            baglar[b].add(r["id"])
        ad_baglari[" ".join(ara_bicim(r["ad"]).split())].add(r["id"])

    def satirlar_(rid: int | None) -> list[sqlite3.Row]:
        if rid is None:
            return []
        return con.execute(
            "SELECT kalem_id, miktar, ekstra_miktar FROM recete_satirlari WHERE recete_id = ?", (rid,)
        ).fetchall()

    def urun(urun_id, ad, adet, haric: set[int], etiket: str) -> dict[int, sqlite3.Row]:
        """Receteyi ekler; ana urunun (cikarilmayan) satirlarini doner."""
        satirlar = satirlar_(_recete_bul(con, platform, urun_id, ad))
        if not satirlar:
            recetesiz.append(ad or urun_id or "?")
            return {}
        kalan = {s["kalem_id"]: s for s in satirlar if s["kalem_id"] not in haric}
        for kid, s in kalan.items():
            toplam[kid] += s["miktar"] * adet
        detay.append({"urun": ad, "tur": etiket, "adet": adet})
        return kalan

    def ekstra(eid, ead, adet, ana: dict[int, sqlite3.Row]) -> None:
        """Ekstra istek. Ana urunun recetesinde ayni malzemeye urune ozel
        ekstra miktari yazildiysa o; yoksa ekstranin kendi recetesi."""
        ad_ = ead or eid or "?"
        satirlar = satirlar_(_recete_bul(con, platform, eid, ead))
        if satirlar:
            for s in satirlar:
                a = ana.get(s["kalem_id"])
                m = a["ekstra_miktar"] if a is not None and a["ekstra_miktar"] else s["miktar"]
                toplam[s["kalem_id"]] += m * adet
            detay.append({"urun": ad_, "tur": "ekstra", "adet": adet})
            return
        # Ekstranin recetesi yok: adindan ("Ekstra Domates") ana recetedeki
        # malzemeyi bul; ekstra miktari yoksa bir porsiyon daha (ust sinir).
        if ead:
            for kid in ad_baglari.get(_ekstra_adi(ead), set()):
                if kid in ana:
                    a = ana[kid]
                    toplam[kid] += (a["ekstra_miktar"] or a["miktar"]) * adet
                    detay.append({"urun": ad_, "tur": "ekstra", "adet": adet})
                    return
        recetesiz.append(f"Ekstra: {ad_}")

    for item in o.items:
        if item.cancelled:
            continue
        adet = item.quantity
        haric: set[int] = set()
        for mid in item.removed_ids:
            haric |= baglar.get(f"{platform}:{mid}", set())
        for ad in item.removed:
            haric |= ad_baglari.get(" ".join(ara_bicim(ad).split()), set())
        ana = urun(item.product_id, item.name, adet, haric, "urun")
        for opt in item.options:
            urun(opt.product_id, opt.name, adet, set(), "opsiyon")
        # Ekstralar: kimlik ve ad listeleri ayni siradan uretiliyor ama biri
        # eksik kalabiliyor; uzunluk tutmuyorsa adi eslestirmiyoruz.
        if item.extra_ids:
            adlar = item.extras if len(item.extras) == len(item.extra_ids) else [None] * len(item.extra_ids)
            ciftler = list(zip(item.extra_ids, adlar))
        else:
            ciftler = [(None, ad) for ad in item.extras]
        for eid, ead in ciftler:
            ekstra(eid, ead, adet, ana)
    return dict(toplam), recetesiz, detay


_EKSTRA_ONEKLERI = {"ekstra", "extra", "ek", "fazla", "bol", "ilave"}


def _ekstra_adi(ad: str) -> str:
    """"Ekstra Domates" -> "domates" (stok kalemi adiyla karsilastirmak icin)."""
    kelimeler = ara_bicim(ad).split()
    while kelimeler and kelimeler[0] in _EKSTRA_ONEKLERI:
        kelimeler = kelimeler[1:]
    return " ".join(kelimeler)


def _baslangic() -> str | None:
    return ayarlar.oku().get("maliyet", {}).get("stok_baslangic")


def siparisleri_isle(siparisler: list[Order]) -> int:
    """Teslim edilenleri stoktan dus, dusulmusken iptal edilenleri geri al.

    Kayit yoklayicida yazildiktan sonra cagriliyor; tekrar cagrilmasi
    guvenli (siparis_stok bir kez). Stok baslangicindan ONCE olusan
    siparislere dokunmaz. Donus: islenen siparis sayisi.
    """
    baslangic = _baslangic()
    if not baslangic:
        return 0
    islenen = 0
    with _conn() as con:
        for o in siparisler:
            if o.is_test or not o.platform_order_id:
                continue
            kayit = con.execute("SELECT durum FROM siparis_stok WHERE order_id = ?",
                                (o.platform_order_id,)).fetchone()
            if o.status == OrderStatus.DELIVERED and kayit is None:
                # Olcut TESLIM (son degisiklik) zamani, olusturma degil: stok
                # takibi basladiginda mutfakta olup sonra teslim edilen
                # siparis de dusulmeli. Gecmis aktarimiyla gelen eski
                # teslimler (son degisikligi baslangictan once) dusulmez.
                teslim = _utc(o.modified_at) or _utc(o.created_at) or ""
                if teslim < baslangic:
                    continue
                ham = con.execute("SELECT ham FROM orders WHERE order_id = ?",
                                  (o.platform_order_id,)).fetchone()
                o = _ham_ile_tamamla(o, ham["ham"] if ham else None)
                gerek, recetesiz, detay = ihtiyac(con, o)
                maliyet = tahmini = 0.0
                for kalem_id, miktar in gerek.items():
                    m, eksik = _cikis(con, kalem_id, miktar, "siparis",
                                      f"Siparis {o.order_code or o.platform_order_id}", o.platform_order_id)
                    maliyet += m
                    if eksik > _KUCUK:
                        tahmini += eksik * _son_fiyat(con, kalem_id)
                con.execute(
                    "INSERT INTO siparis_stok (order_id, connection_id, durum, tarih, malzeme_maliyeti, "
                    "tahmini_maliyet, recetesiz, detay) VALUES (?, ?, 'dusuldu', ?, ?, ?, ?, ?)",
                    (o.platform_order_id, o.connection_id, _simdi(), round(maliyet, 4), round(tahmini, 4),
                     json.dumps(recetesiz, ensure_ascii=False), json.dumps(detay, ensure_ascii=False)),
                )
                islenen += 1
            elif o.status == OrderStatus.CANCELLED and kayit and kayit["durum"] == "dusuldu":
                # Teslimden sonra iptal/iade: dusulen partileri ayni maliyetle geri koy.
                for h in con.execute(
                    "SELECT kalem_id, miktar, tutar FROM stok_hareketleri WHERE order_id = ? AND tur = 'siparis'",
                    (o.platform_order_id,),
                ).fetchall():
                    adet = -h["miktar"]
                    if adet > _KUCUK:
                        _giris(con, h["kalem_id"], adet, h["tutar"] / adet, "iade",
                               f"Iptal {o.order_code or o.platform_order_id}", o.platform_order_id)
                con.execute("UPDATE siparis_stok SET durum = 'iade', tarih = ? WHERE order_id = ?",
                            (_simdi(), o.platform_order_id))
                islenen += 1
    return islenen


# ---------------------------------------------------------------
# Siparis basina kar
# ---------------------------------------------------------------


def siparis_kari(o: Order) -> dict:
    """Satis - malzeme - komisyon - kurye - ambalaj.

    Malzeme: siparis stoktan dusulduyse GERCEK FIFO maliyeti; dusulmediyse
    (henuz teslim edilmedi ya da baslangictan once) receteden tahmini.
    """
    m = ayarlar.oku().get("maliyet", {})
    oran = float((m.get("komisyon") or {}).get(o.platform.value, 0) or 0)
    ambalaj = float(m.get("ambalaj_tl") or 0)
    satis = float(o.total_price or 0)
    komisyon = round(satis * oran / 100, 2)
    kurye = float(o.courier_fee or 0) if (not o.is_pickup and not o.courier_by_platform) else 0.0
    with _conn() as con:
        k = con.execute("SELECT * FROM siparis_stok WHERE order_id = ?", (o.platform_order_id,)).fetchone()
        dokum: list[dict] = []
        if k and k["durum"] == "dusuldu":
            malzeme, kaynak = k["malzeme_maliyeti"], "gercek"
            recetesiz = json.loads(k["recetesiz"])
            tahmini_eksik = k["tahmini_maliyet"]
            for h in con.execute(
                "SELECT k.ad, k.birim, k.takip, -h.miktar AS miktar, h.tutar AS maliyet FROM stok_hareketleri h "
                "JOIN stok_kalemleri k ON k.id = h.kalem_id WHERE h.order_id = ? AND h.tur = 'siparis' "
                "ORDER BY k.takip DESC, k.ad",
                (o.platform_order_id,),
            ).fetchall():
                dokum.append(dict(h))
        else:
            ham = con.execute("SELECT ham FROM orders WHERE order_id = ?", (o.platform_order_id,)).fetchone()
            o = _ham_ile_tamamla(o, ham["ham"] if ham else None)
            gerek, recetesiz, _ = ihtiyac(con, o)
            for kid, miktar in gerek.items():
                r = con.execute("SELECT ad, birim, takip FROM stok_kalemleri WHERE id = ?", (kid,)).fetchone()
                dokum.append({**dict(r), "miktar": miktar,
                              "maliyet": miktar * _birim_maliyet(con, kid)})
            dokum.sort(key=lambda d: (d["takip"] != "net", d["ad"]))
            malzeme = sum(d["maliyet"] for d in dokum)
            kaynak, tahmini_eksik = ("iade" if k else "tahmini"), 0.0
    for d in dokum:
        d["miktar"] = round(d["miktar"], 2)
        d["maliyet"] = round(d["maliyet"], 2)
    malzeme = round(malzeme, 2)
    net = round(satis - malzeme - komisyon - kurye - ambalaj, 2)
    return {
        "order_id": o.platform_order_id,
        "satis": round(satis, 2),
        "malzeme": malzeme,
        "malzeme_kaynagi": kaynak,
        "eksi_stoktan_tahmini": round(tahmini_eksik, 2),
        "komisyon": komisyon,
        "komisyon_orani": oran,
        "kurye": round(kurye, 2),
        "ambalaj": round(ambalaj, 2),
        "net": net,
        "marj": round(net / satis * 100, 1) if satis else None,
        "recetesiz": recetesiz,
        # Malzeme basina: net kalemler once, destek kalemler sonra.
        "malzemeler": dokum,
    }
