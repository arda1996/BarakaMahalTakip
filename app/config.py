from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import data_dir, frozen, resource_dir


def _env_files() -> tuple[str, ...]:
    r""".env dosyasini guvenilir sekilde bul.

    HATA KAYNAGIYDI: env_file=".env" calisma dizinine goreliydi. Exe
    cikti\ klasorunden calisinca proje kokundeki .env hic bulunamiyor,
    ayarlar sessizce bos kaliyordu -- ve bos x-executor-user header'i
    Trendyol tarafindan reddediliyordu.

    Artik birden fazla yere bakiyoruz; sonraki dosya oncekini ezer.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",  # proje koku
        data_dir() / ".env",                              # AppData
        Path.cwd() / ".env",                              # calisma dizini
    ]
    if frozen():
        # Exe'nin yanindaki .env en yuksek onceligi alir.
        candidates.append(resource_dir() / ".env")
        import sys

        candidates.append(Path(sys.executable).resolve().parent / ".env")

    seen: list[str] = []
    for c in candidates:
        p = str(c)
        if c.exists() and p not in seen:
            seen.append(p)
    return tuple(seen)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_files(), extra="ignore")

    app_env: str = "dev"
    secret_key: str = ""
    db_path: Path = data_dir() / "connections.db"

    # Trendyol Go, her istekte bu ikisini header'da bekliyor.
    # tgo_executor_user bos birakilabilir: baglanti eklenirken
    # kullanicidan alinan e-posta onun yerine gecer.
    tgo_agent_name: str = "SelfIntegration"
    tgo_executor_user: str = ""


settings = Settings()
