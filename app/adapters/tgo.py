"""Trendyol Go (eski TGO Yemek / Trendyol Yemek) adapter'i.

Resmi Partner API kullanilir. Tarayici yok, panel girisi yok: her istek
Basic auth ile imzalanir. Kullanicidan alinan uc bilgi yeterlidir --
supplierId, API Key, API Secret Key. Bu ucu satici panelinde
"Hesap Bilgilerim -> Entegrasyon Bilgileri" sayfasinda duruyor.

Dikkat edilen kurallar:
  * User-Agent zorunlu. Olmayan istek 403 alir. Format: "{supplierId} - SelfIntegration"
  * x-agentname ve x-executor-user header'lari zorunlu.
  * Ayni endpoint'e 10 saniyede en fazla 50 istek, sonrasi 429.
"""

import asyncio
from datetime import datetime, timezone

import httpx

from ..config import settings
from ..models import (
    AuthKind,
    BatchItemResult,
    BatchResult,
    Credentials,
    Menu,
    MenuIngredient,
    MenuModifierGroup,
    MenuOption,
    MenuProduct,
    MenuSection,
    Order,
    OrderItem,
    OrderItemOption,
    OrderStatus,
    Platform,
    PriceChange,
    Review,
    ReviewAnswerStatus,
    Store,
)
from ..ratelimit import SlidingWindowLimiter
from .base import AuthError, PlatformAdapter, PlatformError, ReviewsUnsupported

PROD_BASE = "https://api.tgoapis.com"
STAGE_BASE = "https://stageapi.tgoapis.com"

# Platform statusu -> bizim ortak statumuz
STATUS_MAP = {
    "Created": OrderStatus.NEW,
    "Picking": OrderStatus.PREPARING,
    "Invoiced": OrderStatus.READY,
    "Shipped": OrderStatus.ON_THE_WAY,
    "Delivered": OrderStatus.DELIVERED,
    "Cancelled": OrderStatus.CANCELLED,
    "UnSupplied": OrderStatus.CANCELLED,
}
REVERSE_STATUS = {
    OrderStatus.NEW: "Created",
    OrderStatus.PREPARING: "Picking",
    OrderStatus.READY: "Invoiced",
    OrderStatus.ON_THE_WAY: "Shipped",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CANCELLED: "Cancelled",
}

# Limit adapter sinifinda degil modul seviyesinde: ayni supplier icin acilan
# butun ornekler ayni pencereyi paylassin.
_limiter = SlidingWindowLimiter(max_calls=45, per_seconds=10.0)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _from_ms(value) -> datetime | None:
    # Platform bu alani bazen metin, bazen nesne olarak donduruyor; sayi
    # olmayan her sey "tarih yok" demek.
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------
# Guvenli donusturucular
#
# HATA KAYNAGIYDI: kupon/promosyon "amount" alani duz sayi degil NESNE
# geliyor (dokuman: "Indirim tutarlari amount icerisindeki alanlarda
# gosterilmektedir"; hizli market ornegi: "amount": {"seller": 0.18}).
# float(dict) TypeError atiyor ve TEK bozuk siparis butun listeyi
# dusuruyordu. Ayni risk her sayisal/metinsel alan icin gecerli:
# pydantic v2 float alanina dict gelirse ValidationError veriyor.
#
# Bu yuzden platformdan gelen HICBIR deger dogrudan modele verilmiyor.
# ---------------------------------------------------------------

# Tutar nesnesinde once "toplami" ifade eden tek alan aranir; yoksa
# "kimin karsiladigi" paylari toplanir. Ters sira indirimi ikiye
# katlardi.
_TUTAR_TEKIL = (
    "totalAmount", "amount", "discountAmount", "totalPrice", "price",
    "value", "totalDiscount", "paidAmount",
)
_TUTAR_PAY = (
    "seller", "sellerCoveredAmount", "trendyolCoveredAmount",
    "tyCoveredAmount", "customerCoveredAmount", "storeCoveredAmount",
    "platformCoveredAmount",
)


def _sayi(deger, varsayilan: float = 0.0) -> float:
    """Ne gelirse gelsin bir sayi dondurur."""
    # bool, int'in alt sinifi: True sessizce 1.0 olmasin.
    if deger is None or isinstance(deger, bool):
        return varsayilan
    if isinstance(deger, (int, float)):
        return float(deger)
    if isinstance(deger, str):
        metin = deger.strip().replace("\u20ba", "").replace(" ", "")
        # Ondalik ayraci SON gorunen isarettir: "1.234,50" (TR) da
        # "1,234.50" (EN) da 1234.5 demek.
        son_virgul = metin.rfind(",")
        son_nokta = metin.rfind(".")
        if son_virgul > son_nokta:
            metin = metin.replace(".", "").replace(",", ".")
        else:
            metin = metin.replace(",", "")
        try:
            return float(metin)
        except ValueError:
            return varsayilan
    if isinstance(deger, dict):
        for anahtar in _TUTAR_TEKIL:
            ic = deger.get(anahtar)
            if ic is not None and not isinstance(ic, (dict, list)):
                return _sayi(ic, varsayilan)
        toplam, bulundu = 0.0, False
        for anahtar in _TUTAR_PAY:
            if anahtar in deger:
                toplam += _sayi(deger[anahtar])
                bulundu = True
        if bulundu:
            return toplam
        for anahtar in ("amount", "value", "total"):
            if isinstance(deger.get(anahtar), dict):
                return _sayi(deger[anahtar], varsayilan)
        return varsayilan
    if isinstance(deger, (list, tuple)):
        return sum(_sayi(x) for x in deger)
    return varsayilan


def _metin(deger) -> str | None:
    if deger is None or isinstance(deger, (dict, list, tuple, bool)):
        return None
    if isinstance(deger, str):
        return deger.strip() or None
    return str(deger)


def _tam_sayi(deger, varsayilan: int | None = None) -> int | None:
    if deger is None or isinstance(deger, bool):
        return varsayilan
    sayi = _sayi(deger, None)  # type: ignore[arg-type]
    return varsayilan if sayi is None else int(round(sayi))


def _liste(deger) -> list:
    """Anahtar null gelirse for dongusu patlamasin."""
    return deger if isinstance(deger, list) else []


def _harita(deger) -> dict:
    return deger if isinstance(deger, dict) else {}


def _koordinat(deger) -> float | None:
    """Enlem/boylam; platform bunlari METIN olarak gonderiyor.

    Model 2'de (platform kuryesi) musteri koordinati maskeleniyor ve
    "TGO Yemek" gibi bir metin geliyor -- o zaman koordinat yok demek.
    """
    if deger is None or isinstance(deger, bool):
        return None
    try:
        sayi = float(str(deger).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    # 0,0 Gine Korfezi: platform "bilinmiyor" yerine sifir gonderiyorsa
    # bunu gecerli koordinat saymamak lazim.
    if sayi == 0 or not (-90 <= sayi <= 180):
        return None
    return sayi



# Kapida odemede kuryenin ne tahsil edecegini gostermek gerekiyor;
# dokumanda kodlar listeli ama gövdedeki alan adi surumden surume
# degisebiliyor, o yuzden birkac olasi yeri deniyoruz.
_KAPIDA = {
    "CARD": "Kredi karti",
    "CASH": "Nakit",
    "SODEXO_CARD": "Pluxee kart",
    "SODEXO_CODE": "Pluxee kod",
    "MULTINET_CARD": "Multinet kart",
    "MULTINET_CODE": "Multinet kod",
    "EDENRED_CARD": "Edenred kart",
    "EDENRED_CODE": "Edenred kod",
    "SETCARD_CARD": "Setcard kart",
    "SETCARD_CODE": "Setcard kod",
    "METROPOL_CARD": "MetropolCard kart",
    "METROPOL_CODE": "MetropolCard kod",
    "TOKENFLEX_MOBILE": "Tokenflex mobil",
    "TOKENFLEX_CODE": "Tokenflex kod",
    "PAYEKART_CARD": "Paye kart",
    "PAYEKART_CODE": "Paye kod",
    "IWALLET_CODE": "iWallet kod",
}


def _kapida_odeme(payment: dict) -> str | None:
    if payment.get("paymentType") != "PAY_WITH_ON_DELIVERY":
        return None
    kaynak = payment.get("onDelivery")
    kod = None
    if isinstance(kaynak, dict):
        for alan in ("paymentType", "type", "paymentOption", "name"):
            if kaynak.get(alan):
                kod = str(kaynak[alan])
                break
    elif isinstance(kaynak, str):
        kod = kaynak
    if not kod:
        return "Kapida odeme"
    return _KAPIDA.get(kod.upper(), kod)


class TgoAdapter(PlatformAdapter):
    platform = Platform.TGO
    auth_kind = AuthKind.API_KEY

    def __init__(self, credentials: Credentials) -> None:
        super().__init__(credentials)
        missing = [
            f
            for f in ("supplier_id", "api_key", "api_secret")
            if not getattr(credentials, f)
        ]
        if missing:
            raise AuthError(f"Eksik alan: {', '.join(missing)}")
        self.supplier_id = str(credentials.supplier_id)
        self.base = STAGE_BASE if credentials.sandbox else PROD_BASE

        # x-executor-user zorunlu. Bos ya da ornek bir deger gonderirsek
        # platform istegi reddediyor ve sebebi anlasilmiyor; burada
        # baslamadan once net sekilde soyluyoruz.
        self.executor_user = (
            credentials.executor_user or settings.tgo_executor_user or ""
        ).strip()
        if "@" not in self.executor_user or self.executor_user.startswith("ornek@"):
            raise AuthError(
                "Gecerli bir e-posta gerekiyor. Trendyol her istekte "
                "\"islemi yapan kisi\" (x-executor-user) bilgisini zorunlu "
                "tutuyor; satici hesabinizin e-postasini yazin."
            )

    # ---------- kimlik ----------

    @property
    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.credentials.api_key, self.credentials.api_secret)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            # User-Agent yoksa platform 403 doner.
            "User-Agent": f"{self.supplier_id} - {settings.tgo_agent_name}",
            "x-agentname": settings.tgo_agent_name,
            "x-executor-user": self.executor_user,
            "Content-Type": "application/json",
        }

    async def authenticate(self) -> None:
        """Basic auth kullanildigi icin ayri bir giris adimi yok."""
        return None

    async def verify(self) -> dict:
        """Restoran listesini cekerek dogrula.

        Paket saymaktan iyi: kullanici hangi hesaba baglandigini isimle
        gorur, subeleri de ogrenmis oluruz.
        """
        data = await self._request(
            "GET", "/stores", params={"size": 50, "page": 0}, service="store"
        )
        stores = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "status": r.get("workingStatus"),
                "delivery_type": r.get("deliveryType"),
            }
            for r in data.get("restaurants", [])
        ]
        return {
            "supplier_id": self.supplier_id,
            "environment": "stage" if self.credentials.sandbox else "prod",
            "store_count": data.get("totalElements", len(stores)),
            "stores": stores,
        }

    # ---------- alt seviye ----------

    def _url(self, path: str, service: str = "order") -> str:
        """Platform servisleri farkli oneklerde duruyor.

        siparis  -> /integrator/order/meal/...
        restoran -> /integrator/store/meal/...
        yorum    -> /integrator/review/meal/...
        """
        return (
            f"{self.base}/integrator/{service}/meal"
            f"/suppliers/{self.supplier_id}{path}"
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
        service: str = "order",
    ) -> dict:
        # Limit endpoint bazli: {packageId} iceren yollari tek kovaya koyuyoruz.
        bucket = path.split("/")[1] if "/" in path.lstrip("/") else path
        await _limiter.acquire(f"{self.supplier_id}:{method}:{bucket}")

        resp = await self.client.request(
            method,
            self._url(path, service),
            params=params,
            json=json,
            headers=self._headers,
            auth=self._auth,
        )

        if resp.status_code == 401:
            if self.credentials.sandbox:
                raise AuthError(
                    "Test ortami reddetti. Test ortaminin hesap ve API "
                    "bilgileri canli ortamdan TAMAMEN FARKLIDIR; canli "
                    "anahtarlar burada calismaz. Canli ortami deneyin ya da "
                    "stagepartner.tgoyemek.com uzerinden test magazasi "
                    "bilgilerinizi kullanin."
                )
            detay = (resp.text or "").strip()[:300]
            raise AuthError(
                "supplierId / API Key / API Secret Key uclusunden biri hatali. "
                "Satici panelinde Hesap Bilgilerim > Entegrasyon Bilgileri."
                + (f" Platform cevabi: {detay}" if detay else "")
            )
        if resp.status_code == 403:
            raise PlatformError(
                "403. User-Agent header'i eksik olabilir ya da paket bu "
                "supplierId ile iliskili degil."
            )
        if resp.status_code == 409:
            raise PlatformError(
                "409. Ayni sube icin es zamanli bir degisiklik yapilmis. "
                "Tekrar denenmeli."
            )
        if resp.status_code == 429:
            raise PlatformError("429. Istek limiti asildi (10 sn / 50 istek).")
        if resp.status_code == 503 and self.credentials.sandbox:
            # Dokuman: "Test ortaminda alacaginiz 503 hatasi IP
            # yetkilendirmesi olmamasindan kaynaklidir."
            raise PlatformError(
                "503. Test ortami IP yetkilendirmesi ister. IP adresinizi "
                "0850 210 7555 uzerinden satici bildirimi olusturarak "
                "tanimlatmaniz gerekiyor. Canli ortamda IP yetkilendirmesi "
                "gerekmiyor."
            )
        if resp.status_code >= 400:
            raise PlatformError(f"{resp.status_code}: {resp.text[:300]}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ---------- subeler ----------

    async def fetch_stores(self) -> list[Store]:
        data = await self._request(
            "GET", "/stores", params={"size": 50, "page": 0}, service="store"
        )
        subeler: list[Store] = []
        for r in _liste(data.get("restaurants")):
            r = _harita(r)
            # Restoran koordinati mesafe hesabi icin gerekli; platform
            # mesafeyi hic vermiyor, biz hesapliyoruz.
            konum = _harita(r.get("location"))
            subeler.append(
                Store(
                    platform=Platform.TGO,
                    id=_metin(r.get("id")) or "",
                    name=_metin(r.get("name")) or "-",
                    status=_metin(r.get("workingStatus")),
                    delivery_type=_metin(r.get("deliveryType")),
                    address=_metin(r.get("address")),
                    phone=_metin(r.get("phoneNumber")),
                    preparation_minutes=_tam_sayi(
                        r.get("averageOrderPreparationTimeInMin")
                    ),
                    latitude=_koordinat(konum.get("latitude")),
                    longitude=_koordinat(konum.get("longitude")),
                )
            )
        return subeler

    async def set_store_status(self, store_id: str, open_: bool) -> None:
        """Subeyi satisa acar/kapatir.

        PUT /integrator/store/meal/suppliers/{supplierId}/stores/{storeId}/status
        govde: {"status": "OPEN"} veya {"status": "CLOSED"}
        """
        await self._request(
            "PUT",
            f"/stores/{store_id}/status",
            json={"status": "OPEN" if open_ else "CLOSED"},
            service="store",
        )

    # ---------- menu ----------

    async def fetch_menu(self, store_id: str) -> Menu:
        """Menuyu cekip kategori agacina donusturur.

        Platform urunleri ve kategorileri AYRI listelerde donduruyor;
        kategoriler yalnizca urun id'lerine referans veriyor. Burada
        birlestiriyoruz ki arayuz tek bir agac gorsun.
        """
        data = await self._request(
            "GET", f"/stores/{store_id}/products", service="product"
        )

        def kimlikler(liste) -> list[str]:
            """[2, 3] ya da [{"id": 4, "position": 1}] -> ["2", "3"] / ["4"]."""
            out = []
            for x in sorted(
                _liste(liste),
                key=lambda y: _tam_sayi(_harita(y).get("position"), 0) if isinstance(y, dict) else 0,
            ):
                k = _metin(_harita(x).get("id")) if isinstance(x, dict) else _metin(x)
                if k:
                    out.append(k)
            return out

        def urun(raw: dict, position: int | None = None) -> MenuProduct:
            raw = _harita(raw)
            return MenuProduct(
                id=_metin(raw.get("id")) or "",
                name=_metin(raw.get("name")) or "-",
                description=_metin(raw.get("description")),
                selling_price=_sayi(raw.get("sellingPrice")),
                original_price=_sayi(raw.get("originalPrice")),
                active=(raw.get("status") == "ACTIVE"),
                position=position,
                ingredients=kimlikler(raw.get("ingredients")),
                extra_ingredients=kimlikler(raw.get("extraIngredients")),
                modifier_groups=kimlikler(raw.get("modifierGroups")),
            )

        urunler = {
            str(p["id"]): p
            for p in _liste(data.get("products"))
            if isinstance(p, dict) and p.get("id") is not None
        }

        malzemeler = [
            MenuIngredient(
                id=_metin(m.get("id")) or "",
                name=_metin(m.get("name")) or "-",
                price=_sayi(m.get("price")),
                active=(m.get("status", "ACTIVE") == "ACTIVE"),
            )
            for m in (_harita(x) for x in _liste(data.get("ingredients")))
            if _metin(m.get("id"))
        ]

        # Opsiyon secenekleri menude urun olarak duruyor; grup yalnizca
        # kimlik ve (varsa) gruba ozel fiyat veriyor. Ad urunden geliyor.
        gruplar: list[MenuModifierGroup] = []
        for g in (_harita(x) for x in _liste(data.get("modifierGroups"))):
            if not _metin(g.get("id")):
                continue
            secenekler = []
            for mp in sorted(
                (_harita(x) for x in _liste(g.get("modifierProducts"))),
                key=lambda y: _tam_sayi(y.get("position"), 0),
            ):
                pid = _metin(mp.get("id"))
                if not pid:
                    continue
                kaynak = _harita(urunler.get(pid))
                fiyat = mp.get("price")
                secenekler.append(
                    MenuOption(
                        id=pid,
                        name=_metin(kaynak.get("name")) or f"#{pid}",
                        price=_sayi(fiyat) if fiyat is not None else _sayi(kaynak.get("sellingPrice")),
                    )
                )
            gruplar.append(
                MenuModifierGroup(
                    id=_metin(g.get("id")) or "",
                    name=_metin(g.get("name")) or "-",
                    min=_tam_sayi(g.get("min"), 0) or 0,
                    max=_tam_sayi(g.get("max"), 0) or 0,
                    options=secenekler,
                )
            )
        kullanilan: set[str] = set()

        sections: list[MenuSection] = []
        # "sections": null ya da position metin gelirse butun menu
        # dusmesin; siparisteki ders burada da gecerli.
        for sec in sorted(
            (_harita(x) for x in _liste(data.get("sections"))),
            key=lambda x: _tam_sayi(x.get("position"), 0),
        ):
            icerik: list[MenuProduct] = []
            for ref in sorted(
                (_harita(x) for x in _liste(sec.get("products"))),
                key=lambda x: _tam_sayi(x.get("position"), 0),
            ):
                pid = _metin(ref.get("id"))
                ham = urunler.get(pid) if pid else None
                if ham is None:
                    continue
                kullanilan.add(pid)
                icerik.append(urun(ham, _tam_sayi(ref.get("position"))))

            sections.append(
                MenuSection(
                    id=_metin(sec.get("id")) or "",
                    name=_metin(sec.get("name")) or "-",
                    position=_tam_sayi(sec.get("position")),
                    active=(sec.get("status") == "ACTIVE"),
                    products=icerik,
                )
            )

        # Kategorisiz urunler (opsiyon urunleri genelde boyle gelir)
        orphan = [urun(p) for pid, p in urunler.items() if pid not in kullanilan]

        return Menu(
            store_id=store_id,
            sections=sections,
            orphan_products=orphan,
            ingredients=malzemeler,
            modifier_groups=gruplar,
        )

    async def set_product_status(
        self, store_id: str, product_id: str, active: bool
    ) -> None:
        await self._status_with_retry(
            f"/stores/{store_id}/products/{product_id}/status", active
        )

    async def set_section_status(
        self, store_id: str, section_id: str, active: bool
    ) -> None:
        await self._status_with_retry(
            f"/stores/{store_id}/sections/{section_id}/status", active
        )

    async def _status_with_retry(self, path: str, active: bool) -> None:
        """409 (cakisma) durumunda artan araliklarla yeniden dener.

        Dokuman: "Aynı şube üzerinde eş zamanlı kategori veya ürün
        statüsü değişikliği yapılıyor. Kısa süre bekleyip tekrar
        deneyin." Bu cakisma bizim kendi isteklerimiz arasinda da,
        satici panelinden yapilan bir degisiklikle de olusabiliyor --
        tek deneme yetmiyordu, sessizce dusen istek anahtarin
        "takilmasinin" asil sebebiydi.

        Ayrica kalici 5xx'te de bir kez tekrar deniyoruz: dokuman bunu
        "anlik bir hata" olarak tarifliyor.
        """
        govde = {"status": "ACTIVE" if active else "PASSIVE"}
        araliklar = (0.5, 1.2, 2.5)  # son deneme sonrasi beklenmez
        son: PlatformError | None = None

        for i in range(len(araliklar) + 1):
            try:
                await self._request("PUT", path, json=govde, service="product")
                return
            except PlatformError as exc:
                metin = str(exc)
                # "503: ..." genel sunucu hatasi; "503. Test ortami IP..." ise
                # IP yetkilendirmesi -- onu tekrarlamanin anlami yok.
                tekrar = "409" in metin or metin.startswith(
                    ("500:", "502:", "503:")
                )
                if not tekrar or i >= len(araliklar):
                    raise
                son = exc
                await asyncio.sleep(araliklar[i])

        if son:  # pragma: no cover - dongu her durumda doner ya da firlatir
            raise son

    async def update_prices(self, store_id: str, changes: list[PriceChange]) -> str:
        """Fiyatlari kuyruga atar, takip kimligi doner.

        restaurantId'yi HER ZAMAN gonderiyoruz: dokumana gore bos
        birakilirsa fiyat TUM restoranlarda degisir. Cok subeli bir
        isletmede bu istenmeyen bir yan etki olurdu.
        """
        if not changes:
            raise PlatformError("Guncellenecek fiyat yok.")
        if len(changes) > 1000:
            raise PlatformError(
                f"Tek istekte en fazla 1000 urun guncellenebilir "
                f"({len(changes)} gonderildi)."
            )

        data = await self._request(
            "POST",
            "/products/price",
            json={
                "items": [
                    {
                        "restaurantId": int(store_id),
                        "productId": int(c.product_id),
                        "sellingPrice": c.selling_price,
                    }
                    for c in changes
                ]
            },
            service="product",
        )
        batch_id = data.get("batchRequestId")
        if not batch_id:
            raise PlatformError("Platform takip kimligi (batchRequestId) dondurmedi.")
        return str(batch_id)

    async def batch_result(self, batch_request_id: str) -> BatchResult:
        data = await self._request(
            "GET", f"/batch-requests/{batch_request_id}", service="product"
        )
        durum = str(data.get("status") or "")
        return BatchResult(
            batch_request_id=str(data.get("batchRequestId") or batch_request_id),
            status=durum,
            completed=(durum.upper() == "COMPLETED"),
            item_count=data.get("itemCount", 0) or 0,
            failed_item_count=data.get("failedItemCount", 0) or 0,
            items=[
                BatchItemResult(
                    product_id=(
                        str(i.get("requestItem", {}).get("productId"))
                        if i.get("requestItem", {}).get("productId") is not None
                        else None
                    ),
                    status=str(i.get("status") or ""),
                    failure_reasons=[str(r) for r in (i.get("failureReasons") or [])],
                )
                for i in data.get("items", [])
            ],
        )

    # ---------- siparis ----------

    async def fetch_orders(
        self,
        statuses: list[OrderStatus] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Order]:
        params: dict = {"size": 50, "page": 0}
        if statuses:
            kodlar = [REVERSE_STATUS[s] for s in statuses if s in REVERSE_STATUS]
            # Restoran kaynakli iptal "UnSupplied" statusunde duruyor;
            # yalnizca "Cancelled" sorulursa bizim iptallerimiz gelmiyordu.
            if OrderStatus.CANCELLED in statuses:
                kodlar.append("UnSupplied")
            params["packageStatuses"] = ",".join(kodlar)
        if since:
            params["packageModificationStartDate"] = _ms(since)
        if until:
            params["packageModificationEndDate"] = _ms(until)

        orders: list[Order] = []
        while True:
            data = await self._request("GET", "/packages", params=params)
            for raw in data.get("content", []):
                orders.append(self._to_order(raw))
            if params["page"] + 1 >= data.get("totalPages", 1):
                break
            params["page"] += 1
        return orders

    async def fetch_order(self, order_id: str) -> Order | None:
        """Tek paketi ceker.

        Statu degisikligini dogrulamak icin tum listeyi cekmek yerine
        bunu kullaniyoruz: hem hizli hem de limit dostu.
        """
        try:
            data = await self._request("GET", f"/packages/{order_id}")
        except PlatformError as exc:
            if "404" in str(exc):
                return None
            raise
        if not data:
            return None
        # Servis kimi zaman dogrudan paketi, kimi zaman liste doner.
        if "content" in data:
            icerik = data.get("content") or []
            if not icerik:
                return None
            data = icerik[0]
        return self._to_order(data)

    async def accept_order(self, order_id: str, preparation_minutes: int = 20) -> None:
        await self._request(
            "PUT",
            "/packages/picked",
            json={"packageId": order_id, "preparationTime": preparation_minutes},
        )

    async def mark_ready(self, order_id: str) -> None:
        await self._request(
            "PUT",
            "/packages/invoiced",
            json={"packageId": order_id, "actualDate": _ms(datetime.now(timezone.utc))},
        )

    async def mark_shipped(self, order_id: str) -> None:
        """Sadece kendi kuryesiyle calisan restoranlar (Model 1) icin."""
        await self._request(
            "PUT",
            f"/packages/{order_id}/manual-shipped",
            json={"actualDate": _ms(datetime.now(timezone.utc))},
        )

    async def mark_delivered(self, order_id: str) -> None:
        """Sadece kendi kuryesiyle calisan restoranlar (Model 1) icin."""
        await self._request(
            "PUT",
            f"/packages/{order_id}/manual-delivered",
            json={"actualDate": _ms(datetime.now(timezone.utc))},
        )

    async def cancel_order(
        self, order_id: str, item_ids: list[str], reason_id: int
    ) -> None:
        """Tum packageItemId'ler verilirse full, bir kismi verilirse kismi iptal."""
        await self._request(
            "PUT",
            "/packages/unsupplied",
            json={
                "packageId": order_id,
                "itemIdList": item_ids,
                "reasonId": reason_id,
            },
        )

    # ---------- degerlendirme ----------

    _YANIT_DURUMU = {
        "APPROVED": ReviewAnswerStatus.APPROVED,
        "WAITING_FOR_APPROVE": ReviewAnswerStatus.WAITING,
        "REJECTED": ReviewAnswerStatus.REJECTED,
    }

    async def _yorum_istegi(self, method: str, path: str, **kw) -> dict | list:
        try:
            return await self._request(method, path, service="review", **kw)
        except PlatformError as exc:
            # Dokuman: Uber Eats'e gecen subelerde bu servisler 403
            # ("endpoint.not-available.error") donuyor ve istek atilmamali.
            if str(exc).startswith("403"):
                raise ReviewsUnsupported(
                    "Bu sube icin Trendyol degerlendirme servisi kapali (Uber Eats "
                    "gecisi). Yorumlar satici panelinden yonetilmeli."
                ) from exc
            raise

    def _to_review(self, raw: dict, store_id: str) -> Review:
        raw = _harita(raw)
        puan = _harita(raw.get("rating"))
        yorum = _harita(raw.get("comment"))
        cevap = _harita(yorum.get("restaurantAnswer"))
        red = _harita(cevap.get("rejectedReason"))

        def skor(k):
            v = puan.get(k)
            return None if v is None else _sayi(v)

        urunler = []
        for p in _liste(raw.get("products")):
            p = _harita(p)
            ad = _metin(p.get("name"))
            if not ad:
                continue
            ekler = [_metin(_harita(m).get("name")) for m in _liste(p.get("modifierProducts"))]
            ekler = [e for e in ekler if e]
            urunler.append(f"{ad} ({', '.join(ekler)})" if ekler else ad)

        cevap_metni = _metin(cevap.get("text"))
        durum = self._YANIT_DURUMU.get(
            str(cevap.get("status") or "").upper(),
            ReviewAnswerStatus.WAITING if cevap_metni else ReviewAnswerStatus.NONE,
        )
        return Review(
            platform=Platform.TGO,
            review_id=_metin(raw.get("reviewId")) or "",
            store_id=_metin(raw.get("restaurantId")) or store_id,
            order_number=_metin(raw.get("orderParentId")),
            order_created_at=_from_ms(raw.get("orderCreatedDate")),
            created_at=_from_ms(raw.get("createdDate")),
            flavor_score=skor("flavorScore"),
            service_score=skor("serviceScore"),
            delivery_score=skor("deliveryScore"),
            average=skor("average"),
            delivery_type=_metin(raw.get("deliveryType")),
            products=urunler,
            comment=_metin(yorum.get("text")),
            answer_text=cevap_metni,
            answer_status=durum,
            answer_reject_reason=_metin(red.get("reason")),
            raw=raw,
        )

    async def fetch_reviews(
        self,
        store_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        order_number: str | None = None,
    ) -> list[Review]:
        """GET /integrator/review/meal/.../stores/{storeId}/reviews/filter (createdDate'e gore)."""
        params: dict = {"page": 0, "size": 50}
        if since:
            params["startDate"] = _ms(since)
        if until:
            params["endDate"] = _ms(until)
        if order_number:
            params["orderParentId"] = order_number
        sonuc: list[Review] = []
        while True:
            data = await self._yorum_istegi("GET", f"/stores/{store_id}/reviews/filter", params=params)
            # Dokumandaki ornek tek nesne; liste ya da sayfali ("content")
            # donebilir -- ucune de hazirlikli.
            if isinstance(data, list):
                kayitlar, toplam_sayfa = data, 1
            elif isinstance(data, dict) and "content" in data:
                kayitlar, toplam_sayfa = _liste(data.get("content")), _tam_sayi(data.get("totalPages"), 1) or 1
            elif isinstance(data, dict) and data.get("reviewId"):
                kayitlar, toplam_sayfa = [data], 1
            else:
                kayitlar, toplam_sayfa = [], 1
            for k in kayitlar:
                r = self._to_review(k, store_id)
                if r.review_id:
                    sonuc.append(r)
            if params["page"] + 1 >= toplam_sayfa or not kayitlar:
                return sonuc
            params["page"] += 1

    async def answer_review(self, store_id: str, review_id: str, text: str) -> None:
        """POST .../stores/{storeId}/reviews/{reviewId}/answer {"text": ...}"""
        await self._yorum_istegi(
            "POST", f"/stores/{store_id}/reviews/{review_id}/answer", json={"text": text}
        )

    # ---------- normalize ----------

    @staticmethod
    def _to_order(raw: dict) -> Order:
        raw = _harita(raw)
        addr = _harita(raw.get("address"))
        payment = _harita(raw.get("payment"))
        delivery_type = _metin(raw.get("deliveryType"))

        def _adlar(liste) -> list[str]:
            out = []
            for x in liste or []:
                ad = x.get("name") if isinstance(x, dict) else str(x)
                if ad:
                    out.append(ad)
            return out

        def _kimlikler(liste) -> list[str]:
            out = []
            for x in _liste(liste):
                x = _harita(x)
                k = _metin(x.get("productId")) or _metin(x.get("id"))
                if k:
                    out.append(k)
            return out

        items: list[OrderItem] = []
        indirim = 0.0
        for line in _liste(raw.get("lines")):
            line = _harita(line)
            options: list[OrderItemOption] = []
            extras: list[str] = []
            removed: list[str] = []
            extra_ids: list[str] = []
            removed_ids: list[str] = []

            def opsiyonlar(liste) -> None:
                # Opsiyonun da opsiyonu olabiliyor ("Menu" -> "Icecek" ->
                # "Buyuk boy"); her biri stoktan dusulecek ayri bir urun.
                for m in _liste(liste):
                    m = _harita(m)
                    options.append(
                        OrderItemOption(
                            name=_metin(m.get("name")) or "-",
                            price=_sayi(m.get("price")),
                            product_id=_metin(m.get("productId")) or _metin(m.get("id")),
                        )
                    )
                    # Opsiyonun kendi ic degisiklikleri de mutfagi ilgilendiriyor.
                    extras.extend(_adlar(m.get("extraIngredients")))
                    removed.extend(_adlar(m.get("removedIngredients")))
                    extra_ids.extend(_kimlikler(m.get("extraIngredients")))
                    removed_ids.extend(_kimlikler(m.get("removedIngredients")))
                    opsiyonlar(m.get("modifierProducts"))

            opsiyonlar(line.get("modifierProducts"))

            extras.extend(_adlar(line.get("extraIngredients")))
            removed.extend(_adlar(line.get("removedIngredients")))
            extra_ids.extend(_kimlikler(line.get("extraIngredients")))
            removed_ids.extend(_kimlikler(line.get("removedIngredients")))

            # Iptal servisi packageItemId istiyor; her adet ayri kimlik.
            satir_items = [_harita(i) for i in _liste(line.get("items"))]
            item_ids = [
                m for m in (_metin(i.get("packageItemId")) for i in satir_items) if m
            ]
            iptal = bool(satir_items) and all(i.get("isCancelled") for i in satir_items)

            for i in satir_items:
                # amount NESNE olabiliyor; _sayi icindeki tutari cikariyor.
                indirim += _sayi(_harita(i.get("coupon")).get("amount"))
                for pr in _liste(i.get("promotions")):
                    indirim += _sayi(_harita(pr).get("amount"))

            quantity = len(satir_items) or 1
            unit = _sayi(line.get("unitSellingPrice")) or _sayi(line.get("price"))
            items.append(
                OrderItem(
                    name=_metin(line.get("name")) or "-",
                    quantity=quantity,
                    unit_price=unit,
                    total_price=unit * quantity,
                    options=options,
                    extras=extras,
                    removed=removed,
                    product_id=_metin(line.get("productId")),
                    extra_ids=extra_ids,
                    removed_ids=removed_ids,
                    item_ids=item_ids,
                    cancelled=iptal,
                )
            )

        customer = _harita(raw.get("customer"))
        name = " ".join(
            p for p in (_metin(customer.get("firstName")), _metin(customer.get("lastName"))) if p
        )

        def _adres_parcasi(anahtar: str) -> str | None:
            deger = _metin(addr.get(anahtar))
            return None if deger in (None, "TGO Yemek", "Trendyol Yemek") else deger

        # apartmentNumber / floor / doorNumber alanlari maskelenebiliyor
        # ("TGO Yemek"). Gercek adres address1 icinde SERBEST METIN olarak
        # geliyor -- musteri tek bir alana yazdigi icin sabit bir kalipla
        # parse edilmemeli.
        address_text = ", ".join(
            p
            for p in (
                addr.get("address1"),
                addr.get("neighborhood"),
                addr.get("district"),
                addr.get("city"),
            )
            if p and p not in ("TGO Yemek", "Trendyol Yemek")
        ) or None

        return Order(
            platform=Platform.TGO,
            platform_order_id=_metin(raw.get("id")) or "",
            order_code=_metin(raw.get("orderCode")) or _metin(raw.get("orderNumber")),
            order_number=_metin(raw.get("orderNumber")),
            store_id=_metin(raw.get("storeId")),
            status=STATUS_MAP.get(raw.get("packageStatus", ""), OrderStatus.UNKNOWN),
            created_at=_from_ms(raw.get("packageCreationDate")),
            modified_at=_from_ms(raw.get("packageModificationDate")),
            customer_name=name or None,
            customer_phone=_metin(addr.get("phone")),
            customer_platform_id=_metin(customer.get("id")),
            address_neighborhood=_adres_parcasi("neighborhood"),
            address_district=_adres_parcasi("district"),
            address_city=_adres_parcasi("city"),
            call_center_phone=_metin(raw.get("callCenterPhone")),
            # Uber siparislerinde 8 hane, TGO siparislerinde 11 hane
            # (orderNumber). Ikisi de metin olarak gosteriliyor.
            pin_code=_metin(addr.get("pinCode")),
            latitude=_koordinat(addr.get("latitude")),
            longitude=_koordinat(addr.get("longitude")),
            address_text=address_text,
            is_pickup=bool(raw.get("storePickupSelected")),
            courier_by_platform=(delivery_type == "GO"),
            items=items,
            total_price=_sayi(raw.get("totalPrice")),
            delivery_price=(
                _sayi(raw.get("totalDeliveryPrice"))
                if raw.get("totalDeliveryPrice") is not None
                else None
            ),
            payment_type=_metin(payment.get("paymentType")),
            payment_on_delivery=_kapida_odeme(payment),
            is_on_delivery=(payment.get("paymentType") == "PAY_WITH_ON_DELIVERY"),
            discount_total=round(indirim, 2),
            note=_metin(raw.get("customerNote")),
            eta_text=_metin(raw.get("eta")),
            courier_eta_state=_metin(raw.get("pickupEtaState")),
            courier_eta_min=_from_ms(raw.get("estimatedPickupTimeMin")),
            courier_eta_max=_from_ms(raw.get("estimatedPickupTimeMax")),
            courier_nearby=bool(raw.get("isCourierNearby")),
            preparation_minutes=_tam_sayi(raw.get("preparationTime")) or None,
            cancel_reason=_metin(_harita(raw.get("cancelInfo")).get("reason"))
            or _metin(raw.get("cancelInfo")),
            is_test=bool(raw.get("testPackage")),
            app_name=_metin(_harita(raw.get("userInformation")).get("appName")),
            raw=raw,
        )
