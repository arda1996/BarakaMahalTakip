"""Tum platform adapterlarinin uydugu sozlesme.

Amac: servisin geri kalani "hangi platform" sorusunu hic sormasin.
Kimlik dogrulama sekli platformdan platforma degisiyor:

  * API_KEY       -> Trendyol Go. Her istekte Basic auth. Ayri bir giris adimi yok.
  * OAUTH2        -> Yemeksepeti. Bir kere token alinir, suresi dolunca yenilenir.
  * SESSION_LOGIN -> Resmi API'si olmayan panel. Email + sifre ile giris yapilip
                     donen cerez/token saklanir.

Her ucu de `authenticate()` arkasinda duruyor; cagiran taraf farki gormuyor.
"""

from abc import ABC, abstractmethod
from datetime import datetime

import httpx

from ..models import (
    AuthKind,
    BatchResult,
    Credentials,
    Menu,
    Order,
    OrderStatus,
    Platform,
    PriceChange,
    Review,
    Store,
)


class AuthError(Exception):
    """Kimlik bilgileri yanlis ya da oturum dusmus."""


class PlatformError(Exception):
    """Platform beklenmedik bir cevap dondu."""


class ReviewsUnsupported(PlatformError):
    """Platform/sube degerlendirme servisini sunmuyor.

    Trendyol: Uber Eats'e gecen subelerde yorum servisleri 403 donuyor
    (dokuman: "bu endpoint'lere istek atilmamalidir"). Kalici bir durum;
    her yoklamada tekrar denenmemeli.
    """


class PlatformAdapter(ABC):
    platform: Platform
    auth_kind: AuthKind

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self._client: httpx.AsyncClient | None = None

    # ---------- yasam dongusu ----------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- kimlik ----------

    @abstractmethod
    async def authenticate(self) -> None:
        """Oturumu hazirla. API_KEY tipinde bu genelde bos gecer."""

    @abstractmethod
    async def verify(self) -> dict:
        """Kimlik bilgilerini gercek bir istekle dogrula.

        Basarisizsa AuthError firlatir. Basariliysa kullaniciya gosterilecek
        kisa bir ozet dondurur (or. restoran adi, sube sayisi).
        """

    # ---------- subeler ----------

    @abstractmethod
    async def fetch_stores(self) -> list[Store]:
        """Bu hesaba bagli restoranlar/subeler."""

    @abstractmethod
    async def set_store_status(self, store_id: str, open_: bool) -> None:
        """Subeyi satisa ac / kapat."""

    # ---------- menu ----------

    @abstractmethod
    async def fetch_menu(self, store_id: str) -> Menu:
        """Subenin menusu: kategoriler, urunler, fiyatlar, satis durumu."""

    @abstractmethod
    async def set_product_status(
        self, store_id: str, product_id: str, active: bool
    ) -> None:
        """Urunu satisa ac / kapat."""

    @abstractmethod
    async def set_section_status(
        self, store_id: str, section_id: str, active: bool
    ) -> None:
        """Kategoriyi satisa ac / kapat."""

    @abstractmethod
    async def update_prices(
        self, store_id: str, changes: list[PriceChange]
    ) -> str:
        """Fiyatlari guncelle. Islem kuyruga alinir; takip kimligi doner."""

    @abstractmethod
    async def batch_result(self, batch_request_id: str) -> BatchResult:
        """Kuyruga alinan toplu islemin sonucu."""

    # ---------- siparis ----------

    @abstractmethod
    async def fetch_orders(
        self,
        statuses: list[OrderStatus] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Order]:
        """statuses None ise TUM statuler; since/until son degisiklik zamanina gore."""

    async def fetch_order(self, order_id: str) -> Order | None:
        """Tek siparisi ceker.

        Zorunlu degil: destekleyen adapter tek paket sorgulayarak statu
        degisikligini ucuz yoldan dogrular, desteklemeyen icin liste
        cekilir.
        """
        return None

    @abstractmethod
    async def accept_order(self, order_id: str, preparation_minutes: int) -> None:
        ...

    @abstractmethod
    async def mark_ready(self, order_id: str) -> None:
        ...

    @abstractmethod
    async def mark_shipped(self, order_id: str) -> None:
        ...

    @abstractmethod
    async def mark_delivered(self, order_id: str) -> None:
        ...

    @abstractmethod
    async def cancel_order(
        self, order_id: str, item_ids: list[str], reason_id: int
    ) -> None:
        ...

    # ---------- degerlendirme ----------

    async def fetch_reviews(
        self,
        store_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        order_number: str | None = None,
    ) -> list[Review]:
        """Musteri degerlendirmeleri. Platform desteklemiyorsa ReviewsUnsupported."""
        raise ReviewsUnsupported(f"{self.platform.value} degerlendirme API'si sunmuyor.")

    async def answer_review(self, store_id: str, review_id: str, text: str) -> None:
        """Yoruma restoran cevabi gonderir (platform onayindan gecer)."""
        raise ReviewsUnsupported(f"{self.platform.value} yoruma cevap API'si sunmuyor.")
