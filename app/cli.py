"""Arayuz olmadan baglanti eklemek ve test etmek icin kucuk bir komut satiri.

Kullanim:
    python -m app.cli connect       # kimlik bilgilerini sorar, dogrular, kaydeder
    python -m app.cli list
    python -m app.cli orders 1
    python -m app.cli tani          # ham istek/cevap -- hata ayiklamak icin
"""

import asyncio
import getpass
import sys

from . import store
from .adapters.base import AuthError, PlatformError
from .adapters.registry import build_adapter
from .models import Credentials, OrderStatus, Platform


async def cmd_connect() -> None:
    store.init_db()
    print("Platform: 1) Trendyol Go")
    choice = input("Secim [1]: ").strip() or "1"
    if choice != "1":
        print("Su an yalnizca Trendyol Go destekleniyor.")
        return

    print(
        "\nBu bilgiler satici panelinde "
        "Hesap Bilgilerim -> Entegrasyon Bilgileri sayfasinda."
    )
    creds = Credentials(
        supplier_id=input("supplierId: ").strip(),
        api_key=input("API Key: ").strip(),
        api_secret=getpass.getpass("API Secret Key (gizli): ").strip(),
        sandbox=(input("Stage ortami mi? [h/E]: ").strip().lower() == "e"),
    )
    label = input("Bu baglantiya isim ver [Trendyol Go]: ").strip() or "Trendyol Go"

    adapter = build_adapter(Platform.TGO, creds)
    try:
        await adapter.authenticate()
        details = await adapter.verify()
    except AuthError as exc:
        print(f"\nGiris basarisiz: {exc}")
        return
    except PlatformError as exc:
        print(f"\nPlatform hatasi: {exc}")
        return
    finally:
        await adapter.aclose()

    info = store.add_connection(Platform.TGO, label, creds)
    store.mark_verified(info.id)
    print(f"\nBaglanti kuruldu. id={info.id} | {details}")


async def cmd_orders(connection_id: int) -> None:
    found = store.get_connection(connection_id)
    if not found:
        print("Baglanti bulunamadi.")
        return
    info, creds = found
    adapter = build_adapter(info.platform, creds)
    try:
        await adapter.authenticate()
        orders = await adapter.fetch_orders(
            statuses=[OrderStatus.NEW, OrderStatus.PREPARING]
        )
    finally:
        await adapter.aclose()

    if not orders:
        print("Acik siparis yok.")
    for o in orders:
        print(
            f"[{o.status.value:<10}] {o.order_code or o.platform_order_id[:8]} "
            f"| {o.total_price:>8.2f} TL | {len(o.items)} kalem "
            f"| {o.customer_name or '-'}"
        )


async def cmd_tani() -> None:
    """Ham istegi atip cevabi oldugu gibi gosterir.

    Arayuzdeki mesaj "dogrulanamadi" demekle yetiniyor; burada
    gonderilen header'lari (anahtarlar maskeli) ve platformun tam
    cevabini goruyoruz. Hata ayiklarken tahmin etmeyi birakmak icin.
    """
    import base64

    import httpx

    from .config import settings

    print("Trendyol Go baglanti tanisi\n")
    supplier = input("supplierId: ").strip()
    key = input("API Key: ").strip()
    secret = getpass.getpass("API Secret Key (gizli): ").strip()
    email = input("E-posta (x-executor-user): ").strip()
    stage = input("Test ortami mi? [h/E]: ").strip().lower() == "e"

    base = "https://stageapi.tgoapis.com" if stage else "https://api.tgoapis.com"
    url = f"{base}/integrator/store/meal/suppliers/{supplier}/stores"
    agent = settings.tgo_agent_name or "SelfIntegration"
    headers = {
        "User-Agent": f"{supplier} - {agent}",
        "x-agentname": agent,
        "x-executor-user": email,
        "Content-Type": "application/json",
    }

    def mask(v: str) -> str:
        return f"{v[:4]}...{v[-4:]} ({len(v)} karakter)" if len(v) > 8 else "(cok kisa!)"

    print("\n--- GONDERILEN ---")
    print("GET", url)
    for k, v in headers.items():
        print(f"  {k}: {v}")
    print(f"  Authorization: Basic base64({mask(key)}:{mask(secret)})")
    print(f"  (Basic bashlangici: {base64.b64encode(f'{key}:{secret}'.encode()).decode()[:12]}...)")

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            r = await c.get(
                url, headers=headers, auth=httpx.BasicAuth(key, secret),
                params={"page": 0, "size": 5},
            )
        except Exception as exc:
            print(f"\n--- AG HATASI ---\n{type(exc).__name__}: {exc}")
            return

    print("\n--- CEVAP ---")
    print("HTTP", r.status_code)
    for k in ("content-type", "www-authenticate", "x-request-id", "date"):
        if k in r.headers:
            print(f"  {k}: {r.headers[k]}")
    print("\nGovde:")
    print(r.text[:1500] or "(bos)")

    print("\n--- YORUM ---")
    if r.status_code == 200:
        print("Basarili. Bu bilgilerle uygulamadan da baglanabilirsiniz.")
    elif r.status_code == 401:
        print("Kimlik reddedildi. supplierId/key/secret uclusu ya da ortam yanlis.")
        if stage:
            print("Test ortami secildi: test anahtarlari canlidan FARKLIDIR.")
    elif r.status_code == 403:
        print("User-Agent eksik/yanlis olabilir ya da IP engelli olabilir.")
    elif r.status_code == 503 and stage:
        print("Test ortami IP yetkilendirmesi istiyor (0850 210 7555).")


def cmd_list() -> None:
    store.init_db()
    for c in store.list_connections():
        print(f"{c.id}: {c.label} ({c.platform.value}) sandbox={c.sandbox}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "connect":
        asyncio.run(cmd_connect())
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "orders" and len(args) > 1:
        asyncio.run(cmd_orders(int(args[1])))
    elif args[0] == "tani":
        asyncio.run(cmd_tani())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
