"""Baglantilarin (kimlik bilgileri dahil) saklandigi yer.

Simdilik SQLite. Baraka Mahal'a baglanirken PostgreSQL'e tasinacak; disari
acilan fonksiyon imzalari ayni kalsin diye erisim tek dosyada toplandi.

Kimlik bilgileri diskte SIFRELI durur. Acik metin sadece bellekte, adapter
insa edilirken olusur.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .crypto import decrypt, encrypt
from .models import ConnectionInfo, Credentials, Order, OrderStatus, Platform, Review, UserInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name   TEXT,
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_login_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- Kurye atamasi YEREL: platform "kendi kuryenizle teslimat" diyor
-- ama hangi kuryenin gittigini bilmiyor. Siparis platformdan her
-- cekildiginde bu tablodan zenginlestiriliyor.
CREATE TABLE IF NOT EXISTS order_couriers (
    order_id     TEXT PRIMARY KEY,
    courier      TEXT NOT NULL,
    assigned_at  TEXT NOT NULL
);

-- Platformdan gorulen HER siparis. Eskiden siparisler yalnizca
-- ekranin belleginde yasiyordu: uygulama kapaninca durum, "gordum"
-- bilgisi ve gecmis ucup gidiyordu. Platform yalnizca son degisenleri
-- donduruyor; gecmisi biz tutmazsak kimse tutmuyor.
--
-- Sik sorgulanan alanlar sutunda, siparisin tamami `veri` icinde
-- (Order JSON). Ham platform cevabi ayri: musteri/adres kaydi
-- genisletildiginde (gecmis siparisler) yeniden islenebilsin.
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    connection_id   INTEGER NOT NULL,
    platform        TEXT NOT NULL,
    order_code      TEXT,
    store_id        TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT,
    modified_at     TEXT,
    customer_name   TEXT,
    customer_phone  TEXT,
    address_text    TEXT,
    total_price     REAL,
    veri            TEXT NOT NULL,
    ham             TEXT,
    ilk_gorulme     TEXT NOT NULL,
    son_guncelleme  TEXT NOT NULL,
    -- Yeni siparis uyarisinda "Gordum" denildi mi. Veritabaninda ki
    -- uygulama kapaliyken gelen siparis acilista alarm calsin.
    gorundu         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created_at);

-- Musteriler. Her musteri BIR KEZ kaydediliyor ve platformun kalici
-- musteri kimligiyle taniniyor (Trendyol customer.id). Ad tek basina
-- kimlik olamaz: platform soyadi tek harfe kisaltiyor ("Israfil C."),
-- ayni adli iki kisi birlesirdi. Kimlik gelmeyen platform/siparis icin
-- yedek anahtar ad + telefon/adres.
CREATE TABLE IF NOT EXISTS customers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform             TEXT NOT NULL,
    anahtar              TEXT NOT NULL,
    platform_musteri_id  TEXT,
    ad                   TEXT,
    telefon              TEXT,
    -- Restoranin kendi notu ("kapiyi calmayin", "sik musteri").
    not_metni            TEXT,
    -- Musterinin ilk ve son SIPARIS zamani (created_at). Ad/telefon
    -- yalnizca daha yeni bir siparisten guncelleniyor: gecmis aktarimi
    -- eski siparisi sonradan yazarsa yeni bilgiyi ezmesin.
    ilk_siparis          TEXT,
    son_siparis          TEXT,
    UNIQUE (platform, anahtar)
);

-- Musterinin kullandigi adresler; ayni adres bir kez.
CREATE TABLE IF NOT EXISTS customer_addresses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   INTEGER NOT NULL,
    anahtar       TEXT NOT NULL,
    adres         TEXT NOT NULL,
    mahalle       TEXT,
    ilce          TEXT,
    il            TEXT,
    enlem         REAL,
    boylam        REAL,
    ilk_kullanim  TEXT,
    son_kullanim  TEXT,
    UNIQUE (customer_id, anahtar)
);

-- Musteri degerlendirmeleri (siparis basina bir tane). Platformdan
-- duzenli cekiliyor; restoran cevabinin onay durumu degisebildigi icin
-- son gunler tekrar tekrar guncelleniyor. Siparise order_number ile
-- baglaniyor (Trendyol orderParentId = paketteki orderNumber).
CREATE TABLE IF NOT EXISTS reviews (
    review_id       TEXT PRIMARY KEY,
    connection_id   INTEGER NOT NULL,
    platform        TEXT NOT NULL,
    store_id        TEXT,
    order_number    TEXT,
    created_at      TEXT,
    average         REAL,
    answer_status   TEXT NOT NULL,
    has_comment     INTEGER NOT NULL DEFAULT 0,
    veri            TEXT NOT NULL,
    ham             TEXT,
    ilk_gorulme     TEXT NOT NULL,
    son_guncelleme  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_created ON reviews(created_at);
CREATE INDEX IF NOT EXISTS ix_reviews_order ON reviews(order_number);

-- ---------------------------------------------------------------
-- STOK (app/stok.py). Birimler temel: g, ml, adet (kg/lt arayuzde).
-- Maliyet her kalemde FIFO (partiler en eskiden). takip yalnizca miktari
-- belirler: 'net' kesin, 'destek' recetede aralik (ust sinir dusulur).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stok_kalemleri (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ad                TEXT NOT NULL,
    birim             TEXT NOT NULL,
    kategori          TEXT,
    min_seviye        REAL,
    takip             TEXT NOT NULL DEFAULT 'net',
    -- Bagli platform malzemeleri ("tgo:2"): siparisteki "cikar" (sogansiz)
    -- bu kalemi o urunun recetesinden dusurur.
    platform_malzeme  TEXT NOT NULL DEFAULT '[]',
    olusturma         TEXT NOT NULL,
    UNIQUE (ad)
);

-- FIFO partileri. kalan > 0 olanlar tuketilebilir stok.
CREATE TABLE IF NOT EXISTS stok_katmanlari (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kalem_id     INTEGER NOT NULL,
    tarih        TEXT NOT NULL,
    miktar       REAL NOT NULL,
    kalan        REAL NOT NULL,
    birim_fiyat  REAL NOT NULL,
    hareket_id   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_katman_kalem ON stok_katmanlari(kalem_id, tarih);

-- Her giris/cikis. miktar isaretli (+ giris, - cikis); tutar o hareketin
-- maliyeti (TL, pozitif). eksik: stok yetmedigi icin tahmini fiyatla
-- maliyetlenen miktar (eksi stok).
CREATE TABLE IF NOT EXISTS stok_hareketleri (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kalem_id     INTEGER NOT NULL,
    tarih        TEXT NOT NULL,
    tur          TEXT NOT NULL,
    miktar       REAL NOT NULL,
    tutar        REAL NOT NULL DEFAULT 0,
    eksik        REAL NOT NULL DEFAULT 0,
    aciklama     TEXT,
    order_id     TEXT,
    -- Elle girilen hareket geri alininca: orijinalde geri_alan = duzeltme
    -- hareketinin id'si, duzeltmede geri_aldigi = orijinalin id'si. Satir
    -- silinmez; hangi girisin neden geri alindigi izlenebilir kalsin.
    geri_alan    INTEGER,
    geri_aldigi  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_hareket_kalem ON stok_hareketleri(kalem_id, tarih);
CREATE INDEX IF NOT EXISTS ix_hareket_siparis ON stok_hareketleri(order_id);

-- Urun receteleri. anahtar: "tgo:id:<urunId>" ya da (kimlik yoksa)
-- "tgo:ad:<katlanmis ad>". Opsiyon ve ekstralar da menude urun oldugu
-- icin kendi receteleri var.
CREATE TABLE IF NOT EXISTS receteler (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    anahtar     TEXT NOT NULL UNIQUE,
    urun_id     TEXT,
    urun_adi    TEXT NOT NULL,
    tur         TEXT NOT NULL DEFAULT 'urun',
    guncelleme  TEXT NOT NULL
);
-- miktar: stoktan DUSULEN porsiyon miktari (destek kalemde araligin UST
-- siniri). miktar_min yalnizca bilgi (destek kalemin alt siniri; aradaki
-- fark kabul edilen fire). ekstra_miktar: musteri bu malzemeyi ekstra
-- isterse BU urunde eklenecek miktar (bos: ekstranin kendi recetesi).
CREATE TABLE IF NOT EXISTS recete_satirlari (
    recete_id      INTEGER NOT NULL,
    kalem_id       INTEGER NOT NULL,
    miktar         REAL NOT NULL,
    miktar_min     REAL,
    ekstra_miktar  REAL,
    PRIMARY KEY (recete_id, kalem_id)
);

-- Elle girilmis bir hareketin YERINDE duzeltilmesi (yanlis tutar/miktar).
-- Hareket ayni tarihte kalir (o alis gercekten o gun yapildi), yalnizca
-- degerleri degisir; eski degerler burada durur ki ne degistigi izlensin.
CREATE TABLE IF NOT EXISTS hareket_duzeltmeleri (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hareket_id   INTEGER NOT NULL,
    tarih        TEXT NOT NULL,
    eski_miktar  REAL NOT NULL,
    eski_tutar   REAL NOT NULL,
    yeni_miktar  REAL NOT NULL,
    yeni_tutar   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hareket_duzeltme ON hareket_duzeltmeleri(hareket_id);

-- STOK DENETIM KAYDI (audit trail). Stoktaki her duzeltme, geri alma,
-- malzeme guncelleme ve silme: kim, ne zaman, hangi kayit, alan alan
-- once/sonra, sebep (duzeltme/geri alma/silmede zorunlu), etki.
-- DEGISTIRILEMEZ: asagidaki tetikleyiciler UPDATE/DELETE'i reddeder; bir
-- denetim kaydi sonradan "duzeltilemez", yanlissa yeni kayitla aciklanir.
-- Malzeme silinse de kayit kalir (kalem_adi kopyasi burada).
CREATE TABLE IF NOT EXISTS stok_denetim (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tarih          TEXT NOT NULL,
    kullanici      TEXT,
    islem          TEXT NOT NULL,
    kalem_id       INTEGER,
    kalem_adi      TEXT NOT NULL,
    birim          TEXT,
    hareket_id     INTEGER,
    hareket_tur    TEXT,
    hareket_tarih  TEXT,
    degisiklikler  TEXT NOT NULL DEFAULT '[]',
    sebep          TEXT,
    etki           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_denetim_tarih ON stok_denetim(tarih);
CREATE INDEX IF NOT EXISTS ix_denetim_hareket ON stok_denetim(hareket_id);
CREATE TRIGGER IF NOT EXISTS stok_denetim_degismez
BEFORE UPDATE ON stok_denetim
BEGIN SELECT RAISE(ABORT, 'Denetim kaydi degistirilemez.'); END;
CREATE TRIGGER IF NOT EXISTS stok_denetim_silinmez
BEFORE DELETE ON stok_denetim
BEGIN SELECT RAISE(ABORT, 'Denetim kaydi silinemez.'); END;

-- Recetenin ONCEKI halleri: her kayitta eski satirlar buraya yazilir,
-- yanlis duzenleme tek tikla eski surume donulebilsin.
CREATE TABLE IF NOT EXISTS recete_gecmisi (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recete_id  INTEGER NOT NULL,
    tarih      TEXT NOT NULL,
    satirlar   TEXT NOT NULL,
    aciklama   TEXT
);
CREATE INDEX IF NOT EXISTS ix_recete_gecmisi ON recete_gecmisi(recete_id, tarih);

-- Siparis basina stok dusumu (bir kez). durum: dusuldu / iade.
CREATE TABLE IF NOT EXISTS siparis_stok (
    order_id          TEXT PRIMARY KEY,
    connection_id     INTEGER,
    durum             TEXT NOT NULL,
    tarih             TEXT NOT NULL,
    malzeme_maliyeti  REAL NOT NULL,
    tahmini_maliyet   REAL NOT NULL DEFAULT 0,
    recetesiz         TEXT NOT NULL DEFAULT '[]',
    detay             TEXT NOT NULL DEFAULT '[]'
);

-- Yeni urun taslaklari. Trendyol Yemek API'si urun OLUSTURMAYA izin
-- vermiyor (yalnizca okuma, ac/kapa, fiyat); urun satici panelinden
-- ekleniyor. Taslak platformun yapisini birebir tutuyor ki panele
-- aynen girilsin; panelde eklenince menu cekiminde adiyla eslesiyor.
CREATE TABLE IF NOT EXISTS urun_taslaklari (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id    INTEGER NOT NULL,
    store_id         TEXT NOT NULL,
    section_id       TEXT,
    section_name     TEXT,
    name             TEXT NOT NULL,
    description      TEXT,
    price            REAL NOT NULL,
    icerik           TEXT NOT NULL,
    eslesen_urun_id  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Platformdaki eski siparislerin geriye dogru aktarimi (baglanti basina).
-- Uygulama kapanirsa kaldigi pencereden devam etsin diye ilerleme burada.
CREATE TABLE IF NOT EXISTS gecmis_aktarim (
    connection_id   INTEGER PRIMARY KEY,
    durum           TEXT NOT NULL,       -- calisiyor / bitti / hata
    -- Buraya kadar (dahil degil) geriye dogru tarandi.
    taranan_sinir   TEXT NOT NULL,
    en_eski_siparis TEXT,
    aktarilan       INTEGER NOT NULL DEFAULT 0,
    bos_seri_gun    INTEGER NOT NULL DEFAULT 0,
    hata            TEXT,
    baslangic       TEXT NOT NULL,
    guncelleme      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    platform          TEXT NOT NULL,
    label             TEXT NOT NULL,
    sandbox           INTEGER NOT NULL DEFAULT 0,
    credentials_enc   TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    last_verified_at  TEXT
);
"""


# s-cedilla, dotless i, I, I-dot, g-breve, u/o-umlaut, c-cedilla (kucuk/buyuk)
_KATLA = str.maketrans(
    "\u015f\u015e\u0131I\u0130\u011f\u011e\u00fc\u00dc\u00f6\u00d6\u00e7\u00c7",
    "ssiiigguuoocc",
)


def ara_bicim(metin: str | None) -> str:
    """Arama icin katlanmis metin: kucuk harf, Turkce karakterler ASCII.

    SQLite LIKE yalnizca ASCII'de buyuk/kucuk harf duyarsiz; "besiktas"
    yazan kasiyer Turkce karakterli "Besiktas"i, "ISTANBUL" yazan
    noktali I ile yazilmis "Istanbul"u bulamiyordu.
    Klavyede Turkce karakter kullanmayan da bulsun diye iki taraf da
    ASCII'ye katlaniyor.
    """
    if not metin:
        return ""
    return str(metin).translate(_KATLA).lower()


def _migrate(con: sqlite3.Connection) -> None:
    """Eski veritabanlarini yeni semaya tasir.

    connections tablosu once user_id olmadan olusturulmustu; CREATE TABLE
    IF NOT EXISTS mevcut tabloya sutun eklemez, elle eklemek gerekiyor.
    """
    sutunlar = {r["name"] for r in con.execute("PRAGMA table_info(connections)")}
    if sutunlar and "user_id" not in sutunlar:
        con.execute("ALTER TABLE connections ADD COLUMN user_id INTEGER")

    # Siparisin musteriye ve kullandigi adrese bagi (musteri kaydi
    # sonradan geldi). Indeks sutundan SONRA: SCHEMA icinde olsaydi eski
    # veritabaninda "no such column" ile patlardi.
    siparis = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
    if "customer_id" not in siparis:
        con.execute("ALTER TABLE orders ADD COLUMN customer_id INTEGER")
    if "address_id" not in siparis:
        con.execute("ALTER TABLE orders ADD COLUMN address_id INTEGER")
    con.execute("CREATE INDEX IF NOT EXISTS ix_orders_customer ON orders(customer_id)")

    # Hibrit stok (net/destek) sonradan geldi; eski kalemler 'net' (FIFO)
    # kalir, yani onceki maliyetler degismez.
    kalem = {r["name"] for r in con.execute("PRAGMA table_info(stok_kalemleri)")}
    if kalem and "takip" not in kalem:
        con.execute("ALTER TABLE stok_kalemleri ADD COLUMN takip TEXT NOT NULL DEFAULT 'net'")
    satir = {r["name"] for r in con.execute("PRAGMA table_info(recete_satirlari)")}
    if satir and "miktar_min" not in satir:
        con.execute("ALTER TABLE recete_satirlari ADD COLUMN miktar_min REAL")
    if satir and "ekstra_miktar" not in satir:
        con.execute("ALTER TABLE recete_satirlari ADD COLUMN ekstra_miktar REAL")
    hareket = {r["name"] for r in con.execute("PRAGMA table_info(stok_hareketleri)")}
    if hareket and "geri_alan" not in hareket:
        con.execute("ALTER TABLE stok_hareketleri ADD COLUMN geri_alan INTEGER")
    if hareket and "geri_aldigi" not in hareket:
        con.execute("ALTER TABLE stok_hareketleri ADD COLUMN geri_aldigi INTEGER")
    # Hareketi kimin girdigi (denetim kaydi geldikten sonra).
    if hareket and "kullanici" not in hareket:
        con.execute("ALTER TABLE stok_hareketleri ADD COLUMN kullanici TEXT")
    _denetime_tasi(con)


def _denetime_tasi(con: sqlite3.Connection) -> None:
    """Denetim kaydindan ONCE yapilmis duzeltme ve geri almalari kayda
    bir kez tasir; sebep ve kullanici o zaman tutulmuyordu, oyle yazilir.
    Denetim tablosu doluysa ya da tasinacak bir sey yoksa hicbir sey yapmaz
    (her baglantida calisiyor, ucuz olmali)."""
    if con.execute("SELECT 1 FROM stok_denetim LIMIT 1").fetchone():
        return
    eski_sebep = "Denetim kaydi oncesi yapildi; sebep ve kullanici tutulmuyordu."
    satirlar = []
    for d in con.execute(
        "SELECT d.*, h.kalem_id, h.tur, h.tarih AS h_tarih, k.ad, k.birim FROM hareket_duzeltmeleri d "
        "JOIN stok_hareketleri h ON h.id = d.hareket_id LEFT JOIN stok_kalemleri k ON k.id = h.kalem_id"
    ).fetchall():
        degisen = []
        if abs(d["eski_miktar"] - d["yeni_miktar"]) > 1e-9:
            degisen.append({"alan": "miktar", "once": abs(d["eski_miktar"]), "sonra": abs(d["yeni_miktar"])})
        if abs(d["eski_tutar"] - d["yeni_tutar"]) > 1e-9:
            degisen.append({"alan": "tutar", "once": d["eski_tutar"], "sonra": d["yeni_tutar"]})
        satirlar.append((d["tarih"], None, "hareket_duzeltme", d["kalem_id"], d["ad"] or "?", d["birim"],
                         d["hareket_id"], d["tur"], d["h_tarih"], json.dumps(degisen), eski_sebep, "{}"))
    for h in con.execute(
        "SELECT o.id, o.kalem_id, o.tur, o.tarih, o.miktar, o.tutar, g.tarih AS g_tarih, k.ad, k.birim "
        "FROM stok_hareketleri o JOIN stok_hareketleri g ON g.id = o.geri_alan "
        "LEFT JOIN stok_kalemleri k ON k.id = o.kalem_id WHERE o.geri_alan IS NOT NULL"
    ).fetchall():
        degisen = [{"alan": "durum", "once": "gecerli", "sonra": "geri alindi"}]
        satirlar.append((h["g_tarih"], None, "hareket_geri_alma", h["kalem_id"], h["ad"] or "?", h["birim"],
                         h["id"], h["tur"], h["tarih"], json.dumps(degisen), eski_sebep, "{}"))
    # Bos listede bile executemany ortuk bir islem aciyordu ve init_db'deki
    # "PRAGMA journal_mode=WAL" "within a transaction" ile patliyordu.
    if not satirlar:
        return
    con.executemany(
        "INSERT INTO stok_denetim (tarih, kullanici, islem, kalem_id, kalem_adi, birim, hareket_id, hareket_tur, "
        "hareket_tarih, degisiklikler, sebep, etki) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        sorted(satirlar, key=lambda s: s[0]),
    )
    con.commit()


def _conn() -> sqlite3.Connection:
    """Baglanti acar ve semanin var oldugundan emin olur.

    Sema olusturma CREATE TABLE IF NOT EXISTS oldugu icin her acilista
    calistirmak ucuz; karsiliginda "no such table" hatasi hic olusmuyor --
    CLI once calissa da, servis once calissa da fark etmiyor.
    """
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.create_function("ara_bicim", 1, ara_bicim, deterministic=True)
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def init_db() -> None:
    """Acilista bir kez cagrilir; _conn zaten semayi garantiliyor.

    WAL: yoklayici yazarken arayuzun okumasi beklemesin. Ayar
    veritabani dosyasinda kalici, bir kez yeterli.
    """
    con = _conn()
    con.execute("PRAGMA journal_mode=WAL")
    con.close()
    # Musteri kaydindan once gelen siparisleri bagla (ilk acilista hepsi,
    # sonrasinda yalnizca musteri bilgisi hic olmayanlar -- ucuz).
    link_unassigned_customers()


def _row_to_info(row: sqlite3.Row) -> ConnectionInfo:
    return ConnectionInfo(
        id=row["id"],
        platform=Platform(row["platform"]),
        label=row["label"],
        sandbox=bool(row["sandbox"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_verified_at=(
            datetime.fromisoformat(row["last_verified_at"])
            if row["last_verified_at"]
            else None
        ),
    )


def add_connection(
    platform: Platform, label: str, credentials: Credentials, user_id: int
) -> ConnectionInfo:
    now = datetime.now(timezone.utc)
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO connections "
            "(user_id, platform, label, sandbox, credentials_enc, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                platform.value,
                label,
                int(credentials.sandbox),
                encrypt(credentials.model_dump()),
                now.isoformat(),
            ),
        )
        row = con.execute(
            "SELECT * FROM connections WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_info(row)


def list_connections(user_id: int) -> list[ConnectionInfo]:
    with _conn() as con:
        rows = con.execute(
            # user_id NULL olanlar giris sisteminden onceki kayitlar;
            # ilk kullaniciya gorunsunler ki veri kaybolmasin.
            "SELECT * FROM connections WHERE user_id = ? OR user_id IS NULL "
            "ORDER BY id",
            (user_id,),
        ).fetchall()
    return [_row_to_info(r) for r in rows]


def all_connections() -> list[tuple[ConnectionInfo, int]]:
    """Butun baglantilar ve sahipleri.

    Yoklayici oturumdan bagimsiz calisiyor: kimse giris yapmamis olsa
    da siparis kaydi tutulmali. Sahibi olmayan eski kayitlar icin 0
    donuyor; get_connection onlari zaten herkese aciyor.
    """
    with _conn() as con:
        rows = con.execute("SELECT * FROM connections ORDER BY id").fetchall()
    return [(_row_to_info(r), r["user_id"] or 0) for r in rows]


def get_connection(
    connection_id: int, user_id: int
) -> tuple[ConnectionInfo, Credentials] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM connections WHERE id = ? "
            "AND (user_id = ? OR user_id IS NULL)",
            (connection_id, user_id),
        ).fetchone()
    if row is None:
        return None
    return _row_to_info(row), Credentials(**decrypt(row["credentials_enc"]))


def mark_verified(connection_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE connections SET last_verified_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), connection_id),
        )


def delete_connection(connection_id: int, user_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM connections WHERE id = ? "
            "AND (user_id = ? OR user_id IS NULL)",
            (connection_id, user_id),
        )
    return cur.rowcount > 0


# --------------------------------------------------------------- kullanicilar


def _row_to_user(row: sqlite3.Row) -> UserInfo:
    return UserInfo(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_login_at=(
            datetime.fromisoformat(row["last_login_at"])
            if row["last_login_at"]
            else None
        ),
    )


def user_count() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(username: str, display_name: str, password_hash: str) -> UserInfo:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO users (username, display_name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, display_name or username, password_hash, now),
        )
        row = con.execute(
            "SELECT * FROM users WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_user(row)


def find_user(username: str) -> tuple[UserInfo, str] | None:
    """Kullaniciyi ve sifre ozetini dondurur."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    return (_row_to_user(row), row["password_hash"]) if row else None


def touch_login(user_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


# ------------------------------------------------------------------ oturumlar


def create_session(user_id: int, token: str, ttl_seconds: int) -> None:
    now = datetime.now(timezone.utc)
    with _conn() as con:
        # Tek bilgisayarda tek aktif oturum: eskiler temizlensin.
        con.execute("DELETE FROM sessions")
        con.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (
                token,
                user_id,
                now.isoformat(),
                (now + timedelta(seconds=ttl_seconds)).isoformat(),
            ),
        )


def active_user() -> UserInfo | None:
    """Suresi dolmamis oturumun kullanicisi; yoksa None."""
    now = datetime.now(timezone.utc)
    with _conn() as con:
        row = con.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "ORDER BY s.created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        session = con.execute(
            "SELECT expires_at FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if datetime.fromisoformat(session["expires_at"]) < now:
            con.execute("DELETE FROM sessions")
            return None
    return _row_to_user(row)


def clear_sessions() -> None:
    with _conn() as con:
        con.execute("DELETE FROM sessions")


# ---------------------------------------------------------------
# Kurye atamalari
# ---------------------------------------------------------------


def set_order_courier(order_id: str, courier: str) -> None:
    """Kuryeyi atar; bos ad atamayi kaldirir."""
    with _conn() as con:
        if not courier:
            con.execute("DELETE FROM order_couriers WHERE order_id = ?", (order_id,))
            return
        con.execute(
            "INSERT INTO order_couriers (order_id, courier, assigned_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(order_id) DO UPDATE SET courier = excluded.courier, "
            "assigned_at = excluded.assigned_at",
            (order_id, courier, datetime.now(timezone.utc).isoformat()),
        )


def get_order_couriers(order_ids: list[str]) -> dict[str, str]:
    """Birden cok siparisin kuryesini tek sorguda getirir."""
    if not order_ids:
        return {}
    isaret = ",".join("?" * len(order_ids))
    with _conn() as con:
        satirlar = con.execute(
            f"SELECT order_id, courier FROM order_couriers WHERE order_id IN ({isaret})",
            order_ids,
        ).fetchall()
    return {r["order_id"]: r["courier"] for r in satirlar}


# ---------------------------------------------------------------
# Siparisler
# ---------------------------------------------------------------

AKTIF_STATULER = frozenset(
    {
        OrderStatus.NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.PREPARING,
        OrderStatus.READY,
        OrderStatus.ON_THE_WAY,
    }
)

# Mesafe ve paket ucreti bizim hesabimiz. Statu teyidi icin cekilen tek
# paket bunlari tasimiyor; ustune yazilirsa kartta mesafe kayboluyordu.
_HESAPLANAN = ("distance_km", "courier_fee", "store_latitude", "store_longitude")

# courier_name ayri tabloda (order_couriers), gorundu ise sutunda.
# veri'ye yazilirlarsa iki yerde iki farkli deger olusur.
_VERI_DISI = {"raw", "gorundu", "courier_name", "customer_id"}

# Siparis satirini Order'a cevirmek icin okunan sutunlar.
_SIPARIS_SUTUNLARI = "veri, gorundu, customer_id"


def _utc(dt: datetime | None) -> str | None:
    """Tarihler UTC ve ayni bicimde: metin olarak siralanip karsilastirilabilsin."""
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _row_to_order(row: sqlite3.Row) -> Order:
    o = Order.model_validate_json(row["veri"])
    o.gorundu = bool(row["gorundu"])
    if "customer_id" in row.keys():
        o.customer_id = row["customer_id"]
    return o


def _telefon(metin: str | None) -> str | None:
    """Yalnizca gercek bir numara; maskeli ("TGO Yemek") degerler degil."""
    if not metin:
        return None
    rakam = sum(ch.isdigit() for ch in metin)
    return metin.strip() if rakam >= 7 else None


def _musteri_bagla(con: sqlite3.Connection, o: Order) -> tuple[int | None, int | None]:
    """Siparisin musterisini ve adresini kayda yazar; (customer_id, address_id).

    Musteri platformun kalici kimligiyle bulunuyor. Ad/telefon yalnizca
    bu siparis musterinin bilinen en yeni siparisiyse guncelleniyor:
    gecmis aktarimi yillar onceki siparisi sonradan yazdiginda guncel ad
    ve telefonu ezmemeli.
    """
    ad = (o.customer_name or "").strip() or None
    telefon = _telefon(o.customer_phone)
    if o.customer_platform_id:
        anahtar = f"id:{o.customer_platform_id}"
    elif ad and (telefon or o.address_text):
        # Kimlik yok: ad + (telefon ya da adres). Yalnizca ad, ayni adli
        # iki kisiyi birlestirirdi.
        anahtar = "yedek:" + ara_bicim(ad) + "|" + ara_bicim(telefon or o.address_text)
    else:
        return None, None

    zaman = _utc(o.created_at) or datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO customers (platform, anahtar, platform_musteri_id, ad, telefon, "
        "ilk_siparis, son_siparis) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(platform, anahtar) DO UPDATE SET "
        "ad = CASE WHEN excluded.son_siparis >= COALESCE(customers.son_siparis, '') "
        "     THEN COALESCE(excluded.ad, customers.ad) ELSE COALESCE(customers.ad, excluded.ad) END, "
        "telefon = CASE WHEN excluded.son_siparis >= COALESCE(customers.son_siparis, '') "
        "     THEN COALESCE(excluded.telefon, customers.telefon) "
        "     ELSE COALESCE(customers.telefon, excluded.telefon) END, "
        "ilk_siparis = MIN(COALESCE(customers.ilk_siparis, excluded.ilk_siparis), excluded.ilk_siparis), "
        "son_siparis = MAX(COALESCE(customers.son_siparis, excluded.son_siparis), excluded.son_siparis)",
        (o.platform.value, anahtar, o.customer_platform_id, ad, telefon, zaman, zaman),
    )
    musteri_id = con.execute(
        "SELECT id FROM customers WHERE platform = ? AND anahtar = ?",
        (o.platform.value, anahtar),
    ).fetchone()["id"]

    adres_id = None
    adres = (o.address_text or "").strip()
    if adres and not o.is_pickup:
        adres_anahtari = " ".join(ara_bicim(adres).split())
        con.execute(
            "INSERT INTO customer_addresses (customer_id, anahtar, adres, mahalle, ilce, il, "
            "enlem, boylam, ilk_kullanim, son_kullanim) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(customer_id, anahtar) DO UPDATE SET "
            # Ayni adres farkli yazimla gelebiliyor; en son kullanilan
            # yazim gosterilsin (eski siparis sonradan gelirse degil).
            "adres = CASE WHEN excluded.son_kullanim >= COALESCE(customer_addresses.son_kullanim, '') "
            "     THEN excluded.adres ELSE customer_addresses.adres END, "
            "mahalle = COALESCE(excluded.mahalle, customer_addresses.mahalle), "
            "ilce = COALESCE(excluded.ilce, customer_addresses.ilce), "
            "il = COALESCE(excluded.il, customer_addresses.il), "
            "enlem = COALESCE(excluded.enlem, customer_addresses.enlem), "
            "boylam = COALESCE(excluded.boylam, customer_addresses.boylam), "
            "ilk_kullanim = MIN(COALESCE(customer_addresses.ilk_kullanim, excluded.ilk_kullanim), excluded.ilk_kullanim), "
            "son_kullanim = MAX(COALESCE(customer_addresses.son_kullanim, excluded.son_kullanim), excluded.son_kullanim)",
            (musteri_id, adres_anahtari, adres, o.address_neighborhood, o.address_district,
             o.address_city, o.latitude, o.longitude, zaman, zaman),
        )
        adres_id = con.execute(
            "SELECT id FROM customer_addresses WHERE customer_id = ? AND anahtar = ?",
            (musteri_id, adres_anahtari),
        ).fetchone()["id"]
    return musteri_id, adres_id


def _hamdan_tamamla(o: Order, ham: str | None) -> None:
    """Musteri kaydindan ONCE yazilmis siparislerin eksiklerini ham cevaptan doldurur.

    Yalnizca gecis icin: yeni siparislerde bu alanlari adapter dolduruyor.
    Ham cevap platforma ozel (su an Trendyol); tanimadigi yapida bir
    sey yapmiyor.
    """
    if not ham or o.customer_platform_id:
        return
    try:
        h = json.loads(ham)
    except ValueError:
        return
    musteri = h.get("customer") if isinstance(h, dict) else None
    if isinstance(musteri, dict) and musteri.get("id") is not None:
        o.customer_platform_id = str(musteri["id"])
    adres = h.get("address") if isinstance(h, dict) else None
    if isinstance(adres, dict):
        def parca(k):
            v = adres.get(k)
            return v if isinstance(v, str) and v.strip() and v not in ("TGO Yemek", "Trendyol Yemek") else None
        o.address_neighborhood = o.address_neighborhood or parca("neighborhood")
        o.address_district = o.address_district or parca("district")
        o.address_city = o.address_city or parca("city")


def link_unassigned_customers() -> int:
    """Musteriye baglanmamis siparisleri baglar; baglanan sayisini doner.

    Musteri kaydi eklenmeden once gelen siparisler icin (acilista bir kez
    calisir). Musteri bilgisi olmayan (gel-al, maskeli) siparisler NULL
    kaliyor; her acilista yeniden bakilmasi ucuz.
    """
    baglanan = 0
    with _conn() as con:
        rows = con.execute(
            "SELECT order_id, veri, ham FROM orders WHERE customer_id IS NULL"
        ).fetchall()
        for r in rows:
            o = Order.model_validate_json(r["veri"])
            _hamdan_tamamla(o, r["ham"])
            mid, aid = _musteri_bagla(con, o)
            if mid is None:
                continue
            con.execute(
                "UPDATE orders SET customer_id = ?, address_id = ?, veri = ? WHERE order_id = ?",
                (mid, aid, o.model_dump_json(exclude=_VERI_DISI), r["order_id"]),
            )
            baglanan += 1
    return baglanan


def save_orders(orders: list[Order]) -> list[str]:
    """Siparisleri yazar ya da gunceller.

    Platform her zaman gercegin kaynagi: statu ve icerik gelen haliyle
    yaziliyor. Korunanlar yalnizca bizim urettigimiz bilgiler (ilk
    gorulme, gordum, mesafe/ucret).

    Donus: ILK KEZ gorulen ve hala "yeni" olan siparislerin kimlikleri --
    alarm ve pencereyi one alma bunlar icin.
    """
    if not orders:
        return []
    simdi = datetime.now(timezone.utc).isoformat()
    yeniler: list[str] = []
    with _conn() as con:
        for o in orders:
            eski = con.execute(
                f"SELECT {_SIPARIS_SUTUNLARI} FROM orders WHERE order_id = ?",
                (o.platform_order_id,),
            ).fetchone()
            gorundu = 0
            if eski is not None:
                onceki = json.loads(eski["veri"])
                for alan in _HESAPLANAN:
                    if getattr(o, alan) is None and onceki.get(alan) is not None:
                        setattr(o, alan, onceki[alan])
                # Platform bu bilgiyi hic dondurmuyor; her guncellemede
                # sifirlanmasin.
                if onceki.get("oto_kabul"):
                    o.oto_kabul = True
                gorundu = eski["gorundu"]
            elif o.status == OrderStatus.NEW:
                yeniler.append(o.platform_order_id)
            # "Yeni"den cikan siparis (bizden ya da satici panelinden
            # kabul edildi) artik duyurulacak bir sey degil.
            if o.status != OrderStatus.NEW:
                gorundu = 1

            musteri_id, adres_id = _musteri_bagla(con, o)
            o.customer_id = musteri_id

            con.execute(
                "INSERT INTO orders (order_id, connection_id, platform, order_code, "
                "store_id, status, created_at, modified_at, customer_name, "
                "customer_phone, address_text, total_price, veri, ham, "
                "ilk_gorulme, son_guncelleme, gorundu, customer_id, address_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET "
                "connection_id = excluded.connection_id, "
                "order_code = excluded.order_code, store_id = excluded.store_id, "
                "status = excluded.status, created_at = excluded.created_at, "
                "modified_at = excluded.modified_at, "
                "customer_name = excluded.customer_name, "
                "customer_phone = excluded.customer_phone, "
                "address_text = excluded.address_text, "
                "total_price = excluded.total_price, veri = excluded.veri, "
                # Tek paket cevabi bos ham donebilir; oncekini silmesin.
                "ham = COALESCE(excluded.ham, orders.ham), "
                "son_guncelleme = excluded.son_guncelleme, "
                "gorundu = excluded.gorundu, "
                # Tek paket cevabinda musteri eksik gelirse bag kopmasin.
                "customer_id = COALESCE(excluded.customer_id, orders.customer_id), "
                "address_id = COALESCE(excluded.address_id, orders.address_id)",
                (
                    o.platform_order_id,
                    o.connection_id or 0,
                    o.platform.value,
                    o.order_code,
                    o.store_id,
                    o.status.value,
                    _utc(o.created_at),
                    _utc(o.modified_at),
                    o.customer_name,
                    o.customer_phone,
                    o.address_text,
                    o.total_price,
                    o.model_dump_json(exclude=_VERI_DISI),
                    json.dumps(o.raw, ensure_ascii=False) if o.raw else None,
                    simdi,
                    simdi,
                    gorundu,
                    musteri_id,
                    adres_id,
                ),
            )
    return yeniler


def get_order(order_id: str) -> Order | None:
    with _conn() as con:
        row = con.execute(
            f"SELECT {_SIPARIS_SUTUNLARI} FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
    return _row_to_order(row) if row else None


def list_orders(
    connection_ids: list[int],
    statuses: list[OrderStatus],
    final_since: datetime,
) -> list[Order]:
    """Kayitli siparisler.

    Aktif siparisler yasina bakilmadan geliyor: eskiden platform
    sorgusu "son 240 dakikada degisenler" oldugu icin uc saattir
    yolda/hazirlikta duran siparis ekrandan dusuyordu. Tarih penceresi
    yalnizca teslim/iptal gibi bitmis siparislere uygulaniyor.
    """
    if not connection_ids or not statuses:
        return []
    aktif = [s.value for s in statuses if s in AKTIF_STATULER]
    biten = [s.value for s in statuses if s not in AKTIF_STATULER]
    kosullar: list[str] = []
    degerler: list = list(connection_ids)
    if aktif:
        kosullar.append(f"status IN ({','.join('?' * len(aktif))})")
        degerler += aktif
    if biten:
        kosullar.append(
            f"(status IN ({','.join('?' * len(biten))}) "
            "AND COALESCE(modified_at, created_at, ilk_gorulme) >= ?)"
        )
        degerler += biten + [_utc(final_since)]
    sql = (
        f"SELECT {_SIPARIS_SUTUNLARI} FROM orders "
        f"WHERE connection_id IN ({','.join('?' * len(connection_ids))}) "
        f"AND ({' OR '.join(kosullar)})"
    )
    with _conn() as con:
        rows = con.execute(sql, degerler).fetchall()
    return [_row_to_order(r) for r in rows]


def active_order_ids(connection_id: int) -> list[str]:
    """Bizce hala suren siparisler; platformun aktif listesinde yoksa ne oldu diye sorulur."""
    durumlar = [s.value for s in AKTIF_STATULER]
    with _conn() as con:
        rows = con.execute(
            "SELECT order_id FROM orders WHERE connection_id = ? "
            f"AND status IN ({','.join('?' * len(durumlar))})",
            (connection_id, *durumlar),
        ).fetchall()
    return [r["order_id"] for r in rows]


def mark_orders_seen(order_ids: list[str]) -> None:
    if not order_ids:
        return
    with _conn() as con:
        con.execute(
            f"UPDATE orders SET gorundu = 1 WHERE order_id IN "
            f"({','.join('?' * len(order_ids))})",
            order_ids,
        )


# ---------------------------------------------------------------
# Gecmis siparisler ve musteriler
# ---------------------------------------------------------------

def _gecmis_kosullari(
    connection_ids: list[int],
    baslangic: datetime | None,
    bitis: datetime | None,
    statuses: list[OrderStatus] | None,
    arama: str | None,
    musteri: int | None,
) -> tuple[str, list]:
    kosullar = [f"connection_id IN ({','.join('?' * len(connection_ids))})"]
    degerler: list = list(connection_ids)
    if baslangic:
        kosullar.append("COALESCE(created_at, ilk_gorulme) >= ?")
        degerler.append(_utc(baslangic))
    if bitis:
        kosullar.append("COALESCE(created_at, ilk_gorulme) < ?")
        degerler.append(_utc(bitis))
    if statuses:
        kosullar.append(f"status IN ({','.join('?' * len(statuses))})")
        degerler += [s.value for s in statuses]
    if arama:
        # Kasada en cok aranan: onay kodu, musteri adi, telefon, adres.
        desen = f"%{ara_bicim(arama.strip())}%"
        kosullar.append(
            "(ara_bicim(order_code) LIKE ? OR ara_bicim(order_id) LIKE ? "
            "OR ara_bicim(customer_name) LIKE ? OR ara_bicim(customer_phone) LIKE ? "
            "OR ara_bicim(address_text) LIKE ?)"
        )
        degerler += [desen] * 5
    if musteri is not None:
        kosullar.append("customer_id = ?")
        degerler.append(musteri)
    return " AND ".join(kosullar), degerler


def order_history(
    connection_ids: list[int],
    baslangic: datetime | None = None,
    bitis: datetime | None = None,
    statuses: list[OrderStatus] | None = None,
    arama: str | None = None,
    musteri: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Filtrelenmis siparisler + ozet (adet, ciro, iptal).

    Ciro yalnizca teslim edilenlerden: iptal edilen ya da hala suren
    siparisin tutari kasaya girmemis para.
    """
    if not connection_ids:
        return {"orders": [], "toplam": 0, "ozet": {
            "adet": 0, "teslim_adet": 0, "ciro": 0.0, "iptal_adet": 0,
            "ortalama_sepet": 0.0}}
    where, degerler = _gecmis_kosullari(
        connection_ids, baslangic, bitis, statuses, arama, musteri
    )
    with _conn() as con:
        ozet = con.execute(
            "SELECT COUNT(*) AS adet, "
            "SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS teslim_adet, "
            "COALESCE(SUM(CASE WHEN status = 'delivered' THEN total_price END), 0) AS ciro, "
            "SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS iptal_adet "
            f"FROM orders WHERE {where}",
            degerler,
        ).fetchone()
        rows = con.execute(
            f"SELECT {_SIPARIS_SUTUNLARI} FROM orders WHERE {where} "
            "ORDER BY COALESCE(created_at, ilk_gorulme) DESC LIMIT ? OFFSET ?",
            [*degerler, limit, offset],
        ).fetchall()
    teslim = ozet["teslim_adet"] or 0
    ciro = round(ozet["ciro"] or 0.0, 2)
    return {
        "orders": [_row_to_order(r) for r in rows],
        "toplam": ozet["adet"] or 0,
        "ozet": {
            "adet": ozet["adet"] or 0,
            "teslim_adet": teslim,
            "ciro": ciro,
            "iptal_adet": ozet["iptal_adet"] or 0,
            "ortalama_sepet": round(ciro / teslim, 2) if teslim else 0.0,
        },
    }


# Musteri istatistikleri siparislerden HESAPLANIYOR (ayri sayac yok):
# sayac tutsaydik iptal/iade ya da yeniden aktarimda siparisle
# birbirinden kopabilirdi. Yalnizca kullanicinin baglantilarindaki
# siparisler sayiliyor.
_MUSTERI_OZETI = (
    "COUNT(o.order_id) AS siparis_adet, "
    "SUM(CASE WHEN o.status = 'delivered' THEN 1 ELSE 0 END) AS teslim_adet, "
    "SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) AS iptal_adet, "
    "COALESCE(SUM(CASE WHEN o.status = 'delivered' THEN o.total_price END), 0) AS toplam_harcama, "
    "MIN(COALESCE(o.created_at, o.ilk_gorulme)) AS ilk_siparis, "
    "MAX(COALESCE(o.created_at, o.ilk_gorulme)) AS son_siparis"
)


def _musteri_satiri(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "platform": r["platform"],
        "platform_musteri_id": r["platform_musteri_id"],
        "ad": r["ad"],
        "telefon": r["telefon"],
        "not_metni": r["not_metni"],
        "siparis_adet": r["siparis_adet"] or 0,
        "teslim_adet": r["teslim_adet"] or 0,
        "iptal_adet": r["iptal_adet"] or 0,
        "toplam_harcama": round(r["toplam_harcama"] or 0.0, 2),
        "ilk_siparis": r["ilk_siparis"],
        "son_siparis": r["son_siparis"],
        "adres_adet": r["adres_adet"] or 0,
        "son_adres": r["son_adres"],
    }


def customers(
    connection_ids: list[int], arama: str | None = None, limit: int = 200
) -> list[dict]:
    """Kayitli musteriler, en son siparis verene gore.

    Arama ad, telefon, Trendyol musteri no, not ve TUM adreslerinde.
    """
    if not connection_ids:
        return []
    baglanti = f"o.connection_id IN ({','.join('?' * len(connection_ids))})"
    degerler: list = list(connection_ids)
    filtre = ""
    if arama:
        desen = f"%{ara_bicim(arama.strip())}%"
        filtre = (
            "WHERE (ara_bicim(c.ad) LIKE ? OR ara_bicim(c.telefon) LIKE ? "
            "OR c.platform_musteri_id LIKE ? OR ara_bicim(c.not_metni) LIKE ? "
            "OR EXISTS (SELECT 1 FROM customer_addresses a WHERE a.customer_id = c.id "
            "AND ara_bicim(a.adres) LIKE ?))"
        )
        degerler += [desen] * 5
    sql = (
        f"SELECT c.*, {_MUSTERI_OZETI}, "
        "(SELECT COUNT(*) FROM customer_addresses a WHERE a.customer_id = c.id) AS adres_adet, "
        "(SELECT a.adres FROM customer_addresses a WHERE a.customer_id = c.id "
        " ORDER BY a.son_kullanim DESC LIMIT 1) AS son_adres "
        f"FROM customers c JOIN orders o ON o.customer_id = c.id AND {baglanti} "
        f"{filtre} GROUP BY c.id ORDER BY son_siparis DESC LIMIT ?"
    )
    with _conn() as con:
        rows = con.execute(sql, [*degerler, limit]).fetchall()
    return [_musteri_satiri(r) for r in rows]


def customer_detail(connection_ids: list[int], musteri_id: int) -> dict | None:
    """Musteri karti: ozet + adresler (her adresin kac sipariste kullanildigi).

    Kullanicinin baglantilarinda bu musterinin siparisi yoksa None --
    baska bir hesabin musterisi gorunmesin.
    """
    if not connection_ids:
        return None
    baglanti = f"o.connection_id IN ({','.join('?' * len(connection_ids))})"
    with _conn() as con:
        r = con.execute(
            f"SELECT c.*, {_MUSTERI_OZETI}, "
            "(SELECT COUNT(*) FROM customer_addresses a WHERE a.customer_id = c.id) AS adres_adet, "
            "(SELECT a.adres FROM customer_addresses a WHERE a.customer_id = c.id "
            " ORDER BY a.son_kullanim DESC LIMIT 1) AS son_adres "
            f"FROM customers c JOIN orders o ON o.customer_id = c.id AND {baglanti} "
            "WHERE c.id = ? GROUP BY c.id",
            [*connection_ids, musteri_id],
        ).fetchone()
        if r is None:
            return None
        adresler = con.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM orders o WHERE o.address_id = a.id "
            f"AND {baglanti}) AS siparis_adet "
            "FROM customer_addresses a WHERE a.customer_id = ? ORDER BY a.son_kullanim DESC",
            [*connection_ids, musteri_id],
        ).fetchall()
    d = _musteri_satiri(r)
    d["adresler"] = [
        {
            "id": a["id"],
            "adres": a["adres"],
            "mahalle": a["mahalle"],
            "ilce": a["ilce"],
            "il": a["il"],
            "enlem": a["enlem"],
            "boylam": a["boylam"],
            "siparis_adet": a["siparis_adet"],
            "ilk_kullanim": a["ilk_kullanim"],
            "son_kullanim": a["son_kullanim"],
        }
        for a in adresler
    ]
    return d


def set_customer_note(musteri_id: int, metin: str | None) -> bool:
    with _conn() as con:
        cur = con.execute(
            "UPDATE customers SET not_metni = ? WHERE id = ?",
            ((metin or "").strip() or None, musteri_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------
# Urun taslaklari
# ---------------------------------------------------------------


def _row_to_taslak(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "connection_id": row["connection_id"],
        "store_id": row["store_id"],
        "section_id": row["section_id"],
        "section_name": row["section_name"],
        "name": row["name"],
        "description": row["description"],
        "price": row["price"],
        "icerik": json.loads(row["icerik"]),
        "eslesen_urun_id": row["eslesen_urun_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_drafts(connection_id: int, store_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM urun_taslaklari WHERE connection_id = ? AND store_id = ? "
            "ORDER BY id",
            (connection_id, store_id),
        ).fetchall()
    return [_row_to_taslak(r) for r in rows]


def save_draft(
    connection_id: int, store_id: str, veri: dict, taslak_id: int | None = None
) -> dict | None:
    """Taslagi yazar; taslak_id verilirse gunceller (yoksa None).

    Duzenlenen taslagin eslesmesi siliniyor: ad degismis olabilir, bir
    sonraki menu cekiminde yeniden eslesir.
    """
    simdi = datetime.now(timezone.utc).isoformat()
    alanlar = (
        veri.get("section_id"),
        veri.get("section_name"),
        veri["name"],
        veri.get("description"),
        veri["price"],
        json.dumps(veri.get("icerik") or {}, ensure_ascii=False),
    )
    with _conn() as con:
        if taslak_id is None:
            cur = con.execute(
                "INSERT INTO urun_taslaklari (connection_id, store_id, section_id, "
                "section_name, name, description, price, icerik, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (connection_id, store_id, *alanlar, simdi, simdi),
            )
            taslak_id = cur.lastrowid
        else:
            cur = con.execute(
                "UPDATE urun_taslaklari SET section_id = ?, section_name = ?, name = ?, "
                "description = ?, price = ?, icerik = ?, eslesen_urun_id = NULL, "
                "updated_at = ? WHERE id = ? AND connection_id = ? AND store_id = ?",
                (*alanlar, simdi, taslak_id, connection_id, store_id),
            )
            if cur.rowcount == 0:
                return None
        row = con.execute(
            "SELECT * FROM urun_taslaklari WHERE id = ?", (taslak_id,)
        ).fetchone()
    return _row_to_taslak(row)


def delete_draft(connection_id: int, store_id: str, taslak_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM urun_taslaklari WHERE id = ? AND connection_id = ? AND store_id = ?",
            (taslak_id, connection_id, store_id),
        )
    return cur.rowcount > 0


def match_drafts(connection_id: int, store_id: str, urunler: list[tuple[str, str]]) -> None:
    """Panelde eklenen urunleri taslaklarla adindan eslestirir.

    Ad karsilastirmasi ara_bicim ile: panelde "Tavuk Sis" diye girilen
    urun "tavuk sis" taslagini bulsun. Ayni adda birden cok urun varsa
    eslestirmiyoruz -- yanlis urune fiyat yazmak eslesmemekten kotu.
    Eslesen urun menuden kalkarsa eslesme de kalkar.
    """
    ada_gore: dict[str, list[str]] = {}
    for pid, ad in urunler:
        ada_gore.setdefault(ara_bicim(ad).strip(), []).append(pid)
    with _conn() as con:
        rows = con.execute(
            "SELECT id, name, eslesen_urun_id FROM urun_taslaklari "
            "WHERE connection_id = ? AND store_id = ?",
            (connection_id, store_id),
        ).fetchall()
        mevcut = {pid for pid, _ in urunler}
        for r in rows:
            adaylar = ada_gore.get(ara_bicim(r["name"]).strip(), [])
            yeni = adaylar[0] if len(adaylar) == 1 else None
            if r["eslesen_urun_id"] and r["eslesen_urun_id"] in mevcut and yeni is None:
                continue
            if yeni != r["eslesen_urun_id"]:
                con.execute(
                    "UPDATE urun_taslaklari SET eslesen_urun_id = ? WHERE id = ?",
                    (yeni, r["id"]),
                )

# ---------------------------------------------------------------
# Gecmis aktarimi ilerlemesi
# ---------------------------------------------------------------


def get_import(connection_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM gecmis_aktarim WHERE connection_id = ?", (connection_id,)
        ).fetchone()
    return dict(row) if row else None


def save_import(connection_id: int, **alanlar) -> dict:
    """Ilerlemeyi yazar; kayit yoksa olusturur. Verilmeyen alanlar korunur."""
    simdi = datetime.now(timezone.utc).isoformat()
    mevcut = get_import(connection_id) or {
        "connection_id": connection_id,
        "durum": "calisiyor",
        "taranan_sinir": simdi,
        "en_eski_siparis": None,
        "aktarilan": 0,
        "bos_seri_gun": 0,
        "hata": None,
        "baslangic": simdi,
    }
    mevcut.update(alanlar)
    mevcut["guncelleme"] = simdi
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO gecmis_aktarim (connection_id, durum, taranan_sinir, "
            "en_eski_siparis, aktarilan, bos_seri_gun, hata, baslangic, guncelleme) "
            "VALUES (:connection_id, :durum, :taranan_sinir, :en_eski_siparis, :aktarilan, "
            ":bos_seri_gun, :hata, :baslangic, :guncelleme)",
            mevcut,
        )
    return mevcut


def delete_import(connection_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM gecmis_aktarim WHERE connection_id = ?", (connection_id,))

def last_seen(connection_id: int) -> datetime | None:
    """Bu baglantidan kayda en son dokunulan an (uygulamanin son calistigi an).

    Acilista aradaki boslugu (uygulama kapaliyken biten siparisler)
    kapatmak icin baslangic noktasi.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT MAX(son_guncelleme) AS son FROM orders WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
    return datetime.fromisoformat(row["son"]) if row and row["son"] else None

# ---------------------------------------------------------------
# Degerlendirmeler
# ---------------------------------------------------------------


def save_reviews(reviews: list[Review]) -> int:
    """Degerlendirmeleri yazar/gunceller; ILK KEZ gorulenlerin sayisini doner."""
    if not reviews:
        return 0
    simdi = datetime.now(timezone.utc).isoformat()
    yeni = 0
    with _conn() as con:
        for r in reviews:
            var = con.execute(
                "SELECT 1 FROM reviews WHERE review_id = ?", (r.review_id,)
            ).fetchone()
            yeni += var is None
            con.execute(
                "INSERT INTO reviews (review_id, connection_id, platform, store_id, order_number, "
                "created_at, average, answer_status, has_comment, veri, ham, ilk_gorulme, son_guncelleme) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(review_id) DO UPDATE SET "
                "connection_id = excluded.connection_id, store_id = excluded.store_id, "
                "order_number = excluded.order_number, created_at = excluded.created_at, "
                "average = excluded.average, answer_status = excluded.answer_status, "
                "has_comment = excluded.has_comment, veri = excluded.veri, "
                "ham = COALESCE(excluded.ham, reviews.ham), son_guncelleme = excluded.son_guncelleme",
                (
                    r.review_id, r.connection_id or 0, r.platform.value, r.store_id, r.order_number,
                    _utc(r.created_at), r.average, r.answer_status.value, int(bool(r.comment)),
                    r.model_dump_json(exclude={"raw"}),
                    json.dumps(r.raw, ensure_ascii=False) if r.raw else None,
                    simdi, simdi,
                ),
            )
    return yeni


def get_review(review_id: str) -> Review | None:
    with _conn() as con:
        row = con.execute("SELECT veri FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
    return Review.model_validate_json(row["veri"]) if row else None


def latest_review_time(connection_id: int, store_id: str) -> datetime | None:
    with _conn() as con:
        row = con.execute(
            "SELECT MAX(created_at) AS son FROM reviews WHERE connection_id = ? AND store_id = ?",
            (connection_id, store_id),
        ).fetchone()
    return datetime.fromisoformat(row["son"]) if row and row["son"] else None


# Degerlendirmenin siparisi: ayni baglantida siparis numarasi eslesen.
_YORUM_SIPARIS = (
    "LEFT JOIN orders o ON o.connection_id = r.connection_id "
    "AND r.order_number IS NOT NULL "
    "AND json_extract(o.veri, '$.order_number') = r.order_number"
)


def list_reviews(
    connection_ids: list[int],
    platform: str | None = None,
    filtre: str | None = None,
    arama: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Degerlendirmeler (en yeni once) + siparis/musteri bilgisi.

    filtre: cevapsiz (yorum var, restoran cevap vermemis), dusuk (ortalama
    3 ve alti), yorumlu, reddedilen.
    """
    if not connection_ids:
        return {"reviews": [], "toplam": 0}
    kosul = [f"r.connection_id IN ({','.join('?' * len(connection_ids))})"]
    deger: list = list(connection_ids)
    if platform:
        kosul.append("r.platform = ?")
        deger.append(platform)
    if filtre == "cevapsiz":
        kosul.append("r.has_comment = 1 AND r.answer_status IN ('none', 'rejected')")
    elif filtre == "dusuk":
        kosul.append("r.average IS NOT NULL AND r.average <= 3")
    elif filtre == "yorumlu":
        kosul.append("r.has_comment = 1")
    elif filtre == "reddedilen":
        kosul.append("r.answer_status = 'rejected'")
    if arama:
        desen = f"%{ara_bicim(arama.strip())}%"
        kosul.append(
            "(ara_bicim(json_extract(r.veri, '$.comment')) LIKE ? OR r.order_number LIKE ? "
            "OR ara_bicim(o.customer_name) LIKE ? OR ara_bicim(json_extract(r.veri, '$.products')) LIKE ?)"
        )
        deger += [desen] * 4
    where = " AND ".join(kosul)
    with _conn() as con:
        toplam = con.execute(
            f"SELECT COUNT(*) FROM reviews r {_YORUM_SIPARIS} WHERE {where}", deger
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT r.veri, o.order_id, o.order_code, o.customer_name, o.customer_id, o.total_price "
            f"FROM reviews r {_YORUM_SIPARIS} WHERE {where} "
            "ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
            [*deger, limit, offset],
        ).fetchall()
    sonuc = []
    for row in rows:
        d = json.loads(row["veri"])
        d["siparis"] = (
            {
                "order_id": row["order_id"],
                "order_code": row["order_code"],
                "customer_name": row["customer_name"],
                "customer_id": row["customer_id"],
                "total_price": row["total_price"],
            }
            if row["order_id"]
            else None
        )
        sonuc.append(d)
    return {"reviews": sonuc, "toplam": toplam}


def review_summary(connection_ids: list[int], since: datetime | None = None) -> list[dict]:
    """Platform basina ozet: ortalamalar, dagilim, cevap bekleyenler."""
    if not connection_ids:
        return []
    kosul = f"connection_id IN ({','.join('?' * len(connection_ids))})"
    deger: list = list(connection_ids)
    if since:
        kosul += " AND created_at >= ?"
        deger.append(_utc(since))
    with _conn() as con:
        rows = con.execute(
            "SELECT platform, COUNT(*) AS adet, AVG(average) AS ortalama, "
            "AVG(json_extract(veri, '$.flavor_score')) AS lezzet, "
            "AVG(json_extract(veri, '$.service_score')) AS servis, "
            "AVG(json_extract(veri, '$.delivery_score')) AS teslimat, "
            "SUM(has_comment) AS yorumlu, "
            "SUM(CASE WHEN has_comment = 1 AND answer_status IN ('none','rejected') THEN 1 ELSE 0 END) AS cevapsiz, "
            "SUM(CASE WHEN answer_status = 'waiting' THEN 1 ELSE 0 END) AS onay_bekleyen, "
            "SUM(CASE WHEN average IS NOT NULL AND average <= 3 THEN 1 ELSE 0 END) AS dusuk, "
            "SUM(CASE WHEN ROUND(average) = 1 THEN 1 ELSE 0 END) AS p1, "
            "SUM(CASE WHEN ROUND(average) = 2 THEN 1 ELSE 0 END) AS p2, "
            "SUM(CASE WHEN ROUND(average) = 3 THEN 1 ELSE 0 END) AS p3, "
            "SUM(CASE WHEN ROUND(average) = 4 THEN 1 ELSE 0 END) AS p4, "
            "SUM(CASE WHEN ROUND(average) = 5 THEN 1 ELSE 0 END) AS p5 "
            f"FROM reviews WHERE {kosul} GROUP BY platform",
            deger,
        ).fetchall()
    yuvarla = lambda v: round(v, 2) if v is not None else None
    return [
        {
            "platform": r["platform"],
            "adet": r["adet"],
            "ortalama": yuvarla(r["ortalama"]),
            "lezzet": yuvarla(r["lezzet"]),
            "servis": yuvarla(r["servis"]),
            "teslimat": yuvarla(r["teslimat"]),
            "yorumlu": r["yorumlu"] or 0,
            "cevapsiz": r["cevapsiz"] or 0,
            "onay_bekleyen": r["onay_bekleyen"] or 0,
            "dusuk": r["dusuk"] or 0,
            "dagilim": [r["p1"] or 0, r["p2"] or 0, r["p3"] or 0, r["p4"] or 0, r["p5"] or 0],
        }
        for r in rows
    ]