from ..models import Credentials, Platform
from .base import PlatformAdapter
from .tgo import TgoAdapter

_ADAPTERS: dict[Platform, type[PlatformAdapter]] = {
    Platform.TGO: TgoAdapter,
    # Platform.YEMEKSEPETI: YemeksepetiAdapter,   # OAuth2, siradaki adim
    # Platform.MIGROS: MigrosAdapter,             # resmi API basvurusu bekliyor
}


def build_adapter(platform: Platform, credentials: Credentials) -> PlatformAdapter:
    try:
        cls = _ADAPTERS[platform]
    except KeyError:
        raise NotImplementedError(f"{platform} adapter'i henuz yazilmadi.") from None
    return cls(credentials)


def supported_platforms() -> list[Platform]:
    return list(_ADAPTERS)
