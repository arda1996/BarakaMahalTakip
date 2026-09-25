"""Platformlardan bagimsiz ortak modeller.

Her adapter kendi ham cevabini bu modellere cevirir; servisin geri kalani
Trendyol ile Migros arasindaki farki hic gormez.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Platform(str, Enum):
    TGO = "tgo"          # Trendyol Go / Yemek
    YEMEKSEPETI = "yemeksepeti"
    MIGROS = "migros"


class AuthKind(str, Enum):
    """Platform kimlik dogrulamasini nasil yapiyor."""

    API_KEY = "api_key"          # Trendyol Go: Basic auth, key + secret
    OAUTH2 = "oauth2"            # Yemeksepeti: client credentials -> bearer
    SESSION_LOGIN = "session"    # Resmi API'si olmayanlar: email + sifre -> oturum


class OrderStatus(str, Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY = "ready"
    ON_THE_WAY = "on_the_way"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class OrderItemOption(BaseModel):
    name: str
    price: float = 0.0
    # Platformdaki urun kimligi (opsiyonlar menude ayri urun). Stok
    # recetesiyle eslestirme bununla; eski kayitlarda yok, ada dusulur.
    product_id: str | None = None


class OrderItem(BaseModel):
    name: str
    quantity: int = 1
    unit_price: float = 0.0
    total_price: float = 0.0
    options: list[OrderItemOption] = Field(default_factory=list)
    # Mutfagin gormesi gereken degisiklikler: "sogansiz", "ekstra peynir".
    extras: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    # Stok icin kimlikler: urun, eklenen ekstralar (menude urun) ve
    # cikarilan malzemeler (malzeme kutuphanesi). Adlarla ayni sirada
    # degil; bos olabilir (eski kayit ya da platform vermemis).
    product_id: str | None = None
    extra_ids: list[str] = Field(default_factory=list)
    removed_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    # Iptal servisi packageItemId listesi istiyor; bu olmadan kismi iptal
    # (hatta tam iptal) mumkun degil. Her adet icin bir kimlik var.
    item_ids: list[str] = Field(default_factory=list)
    cancelled: bool = False


class Order(BaseModel):
    # Hangi baglantidan geldigi; statu gecisi bu baglanti uzerinden yapilir.
    connection_id: int | None = None
    platform: Platform
    platform_order_id: str          # Platformdaki benzersiz kimlik
    order_code: str | None = None   # Musteriye/kuryeye gosterilen kisa kod
    order_number: str | None = None  # Uzun siparis numarasi (IVR'de tuslanan)
    store_id: str | None = None
    status: OrderStatus = OrderStatus.UNKNOWN
    created_at: datetime | None = None
    modified_at: datetime | None = None

    customer_name: str | None = None
    customer_phone: str | None = None
    # Platformun KALICI musteri kimligi (Trendyol: customer.id). Soyad
    # platformda tek harfe kisaltiliyor ("Israfil C."); ayni kisiyi
    # guvenilir sekilde taniyan tek bilgi bu.
    customer_platform_id: str | None = None
    # Yerel musteri kaydi (customers.id); kayittan okunurken dolar.
    customer_id: int | None = None
    address_neighborhood: str | None = None
    address_district: str | None = None
    address_city: str | None = None
    # Uber Eats gecisi sonrasi musteriye dogrudan ulasilamiyor: once bu
    # numara araniyor, sonra pin_code tuslaniyor (IVR yonlendirmesi).
    # Numara artik sabit degil, siparis basina havuzdan geliyor.
    call_center_phone: str | None = None
    pin_code: str | None = None
    address_text: str | None = None
    # Model 1'de (kendi kuryemiz) gercek koordinat geliyor; Model 2'de
    # platform bunu maskeliyor ve mesafe hesaplanamiyor.
    latitude: float | None = None
    longitude: float | None = None
    # Restorandan musteriye kus ucusu mesafe (km) ve hesaplanan paket
    # ucreti; platform bu iki bilgiyi hic vermiyor.
    distance_km: float | None = None
    courier_fee: float | None = None
    # Subenin koordinati: haritada yol tarifi restorandan baslasin.
    store_latitude: float | None = None
    store_longitude: float | None = None
    is_pickup: bool = False
    courier_by_platform: bool = False   # Model 2: platform kuryesi tasiyor

    items: list[OrderItem] = Field(default_factory=list)
    total_price: float = 0.0
    delivery_price: float | None = None
    payment_type: str | None = None
    # Kapida odemede kuryenin ne tahsil edecegi: nakit mi, kart mi,
    # hangi yemek karti. Odeme onlineyse bos kalir.
    payment_on_delivery: str | None = None
    is_on_delivery: bool = False
    discount_total: float = 0.0
    note: str | None = None

    # Platform kuryesi (Model 2) durumu.
    eta_text: str | None = None
    courier_eta_state: str | None = None   # SUCCESS / FAILED / ""
    courier_eta_min: datetime | None = None
    courier_eta_max: datetime | None = None
    courier_nearby: bool = False

    preparation_minutes: int | None = None
    # Platform "kendi kuryenizle" diyor ama hangi kurye oldugunu
    # bilmiyor; bu bilgi bizde duruyor.
    courier_name: str | None = None
    # Yeni siparis uyarisinda "Gordum" denildi mi. Yerel kayittan gelir;
    # platform bilmiyor.
    gorundu: bool = False
    # Oto onay switch'i ile yoklayici kabul etti; kartta ve bildirimde
    # ayri gosteriliyor ki "kim kabul etti" sorusu kalmasin.
    oto_kabul: bool = False
    is_test: bool = False
    app_name: str | None = None            # Trendyol / TrendyolGo / Galaxy
    cancel_reason: str | None = None       # cancelInfo

    raw: dict[str, Any] = Field(default_factory=dict)


class CancelReason(BaseModel):
    id: int
    label: str


# Dokuman "Paket Modelleri": UnSupplied servisinde YALNIZCA bu sebepler
# kullanilabilir. Model 1 (kendi kuryesi) disindakiler 624 ve 626'yi
# kullanamiyor, o yuzden isaretliyoruz.
CANCEL_REASONS: list[dict] = [
    {"id": 621, "label": "Tedarik problemi", "model1_only": False},
    {"id": 622, "label": "Magaza kapali", "model1_only": False},
    {"id": 623, "label": "Magaza siparisi hazirlayamiyor", "model1_only": False},
    {"id": 627, "label": "Siparis karisikligi", "model1_only": False},
    {"id": 624, "label": "Yuksek yogunluk / Kurye yok", "model1_only": True},
    {"id": 626, "label": "Alan disi", "model1_only": True},
]


class ReviewAnswerStatus(str, Enum):
    NONE = "none"                      # restoran cevap vermedi
    WAITING = "waiting"                # platform onayi bekleniyor
    APPROVED = "approved"              # yayinda
    REJECTED = "rejected"              # platform reddetti (rejected_reason)


class Review(BaseModel):
    """Musteri degerlendirmesi. Siparis basina bir tane.

    Trendyol: musteri siparisten sonra 7 gun icinde puan/yorum verebiliyor,
    sonradan degistiremiyor. Restoran cevabi platform onayindan geciyor.
    """

    connection_id: int | None = None
    platform: Platform
    review_id: str
    store_id: str | None = None
    # Siparis no (Trendyol orderParentId = paketteki orderNumber).
    order_number: str | None = None
    order_created_at: datetime | None = None
    created_at: datetime | None = None
    flavor_score: float | None = None
    service_score: float | None = None
    delivery_score: float | None = None
    average: float | None = None
    delivery_type: str | None = None
    products: list[str] = Field(default_factory=list)
    comment: str | None = None
    answer_text: str | None = None
    answer_status: ReviewAnswerStatus = ReviewAnswerStatus.NONE
    answer_reject_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Store(BaseModel):
    """Platformdaki bir sube/restoran."""

    connection_id: int | None = None
    platform: Platform
    id: str
    name: str
    status: str | None = None          # OPEN / CLOSED
    delivery_type: str | None = None   # GO (platform kuryesi) / STORE (kendi kuryesi)
    address: str | None = None
    phone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    preparation_minutes: int | None = None


class MenuIngredient(BaseModel):
    """Malzeme kutuphanesindeki bir kalem ("Sogan", "Tursu").

    Urunun `ingredients` listesi buraya bakiyor; musteri bunlari
    urunden CIKARABILIYOR (siparisteki removedIngredients).
    """

    id: str
    name: str
    price: float = 0.0
    active: bool = True


class MenuOption(BaseModel):
    """Opsiyon grubundaki bir secenek. Aslinda menude bir urun."""

    id: str
    name: str
    price: float = 0.0


class MenuModifierGroup(BaseModel):
    """Opsiyon grubu ("Icecek secimi", en az 1 en fazla 1)."""

    id: str
    name: str
    min: int = 0
    max: int = 0
    options: list[MenuOption] = Field(default_factory=list)


class MenuProduct(BaseModel):
    """Menudeki bir urun."""

    id: str
    name: str
    description: str | None = None
    selling_price: float = 0.0
    # Platform tarafindan hesaplanir; biz degistirmiyoruz, sadece gosteriyoruz.
    original_price: float = 0.0
    active: bool = True
    position: int | None = None
    # Platformun urun yapisi; kimlikler Menu.ingredients /
    # Menu.modifier_groups / diger urunlere (ekstralar) bakiyor. Yeni urun
    # taslagi bu yapiyi birebir kopyaliyor (bkz. urun taslaklari).
    ingredients: list[str] = Field(default_factory=list)
    extra_ingredients: list[str] = Field(default_factory=list)
    modifier_groups: list[str] = Field(default_factory=list)


class MenuSection(BaseModel):
    """Menu kategorisi ve altindaki urunler."""

    id: str
    name: str
    position: int | None = None
    active: bool = True
    products: list[MenuProduct] = Field(default_factory=list)


class Menu(BaseModel):
    connection_id: int | None = None
    store_id: str
    sections: list[MenuSection] = Field(default_factory=list)
    # Hicbir kategoriye baglanmamis urunler gozden kacmasin.
    orphan_products: list[MenuProduct] = Field(default_factory=list)
    # Kutuphaneler: urunler bunlara kimlikle bagli.
    ingredients: list[MenuIngredient] = Field(default_factory=list)
    modifier_groups: list[MenuModifierGroup] = Field(default_factory=list)


class PriceChange(BaseModel):
    product_id: str
    selling_price: float = Field(gt=0)


class BatchItemResult(BaseModel):
    product_id: str | None = None
    status: str
    failure_reasons: list[str] = Field(default_factory=list)


class BatchResult(BaseModel):
    """Fiyat guncelleme kuyruga alindigi icin sonucu ayrica sorulur."""

    batch_request_id: str
    status: str                 # COMPLETED / IN_PROGRESS ...
    completed: bool = False
    item_count: int = 0
    failed_item_count: int = 0
    items: list[BatchItemResult] = Field(default_factory=list)


class Credentials(BaseModel):
    """Kullanicinin 'baglanti ekle' ekraninda girdigi bilgiler.

    Hangi alanlarin dolacagi platforma gore degisir; adapter dogrular.
    """

    # API_KEY (Trendyol Go)
    supplier_id: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    # Trendyol her istekte "islemi yapan kisi" e-postasini
    # (x-executor-user) zorunlu tutuyor. Ayar dosyasi yerine baglantinin
    # kendi alani: her hesap kendi e-postasiyla calisir.
    executor_user: str | None = None

    # OAUTH2 (Yemeksepeti)
    client_id: str | None = None
    client_secret: str | None = None

    # SESSION_LOGIN (panel girisi)
    email: str | None = None
    password: str | None = None

    sandbox: bool = False


class ConnectionInfo(BaseModel):
    id: int
    platform: Platform
    label: str
    sandbox: bool
    created_at: datetime
    last_verified_at: datetime | None = None


class UserInfo(BaseModel):
    """Uygulamanin kendi kullanicisi (platform hesabi degil)."""

    id: int
    username: str
    display_name: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None
