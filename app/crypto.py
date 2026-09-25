"""Kimlik bilgilerini diskte acik metin tutmamak icin ince bir sarmalayici.

Sifreleme anahtari kullanicinin derdi degil: yoksa ilk calistirmada
uretilir ve AppData altinda saklanir. Onceden .env icindeki SECRET_KEY
zorunluydu; paketlenmis exe .env'i bulamayinca kayit aninda cokuyordu
(HTTP 500). Artik .env olmadan da calisiyor.

SECRET_KEY ayari yine de desteklenir; verilmisse o kullanilir.
"""

import json
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .paths import data_dir

KEY_FILENAME = "secret.key"


def _key_path():
    return data_dir() / KEY_FILENAME


def _load_or_create_key() -> bytes:
    """Anahtari dosyadan oku; yoksa uret ve kaydet.

    Anahtar dosyasi silinirse eski kayitlar cozulemez -- bu durumda
    baglantilari yeniden eklemek gerekir (bkz. decrypt).
    """
    path = _key_path()
    if path.exists():
        key = path.read_bytes().strip()
        if key:
            return key

    key = Fernet.generate_key()
    path.write_bytes(key)
    # Windows'ta chmod sinirli etkili ama zarari yok; POSIX'te sadece
    # sahibi okuyabilsin.
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key


_fernet_onbellek: Fernet | None = None


def _fernet() -> Fernet:
    """Fernet nesnesi; bir kere kurulup saklaniyor.

    Her cozme isleminde anahtar dosyasini diskten okumak gereksiz: bu
    yol siparis yoklamasinda calisiyor ve anahtar uygulama calisirken
    degismiyor.
    """
    global _fernet_onbellek
    if _fernet_onbellek is not None:
        return _fernet_onbellek

    configured = (settings.secret_key or "").strip()
    key = configured.encode() if configured else _load_or_create_key()
    try:
        _fernet_onbellek = Fernet(key)
        return _fernet_onbellek
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Sifreleme anahtari gecersiz ({_key_path()}). Dosyayi silip "
            "uygulamayi yeniden baslatirsaniz yeni anahtar uretilir; "
            "kayitli baglantilari tekrar eklemeniz gerekir."
        ) from exc


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as exc:
        raise RuntimeError(
            "Kayitli baglanti cozulemedi: sifreleme anahtari degismis "
            "olabilir. Baglantiyi silip yeniden ekleyin."
        ) from exc
