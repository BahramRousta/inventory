import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.domain.enums import ProviderKind
from app.infrastructure.db.models import (
    InternalStockModel,
    InventoryProviderModel,
    ProductModel,
    StockSourceModel,
)
from app.infrastructure.db.session import get_session


DEMO_PRODUCTS = (
    ("ANKR-HUB-7C", "Anker USB-C Hub 7-in-1", 10),
    ("LOGI-MX-M3S", "Logitech MX Master 3S", 25),
    ("SONY-WH-1000XM5", "Sony WH-1000XM5 Headphones", 12),
    ("KINDLE-PW-11", "Kindle Paperwhite 11th Gen", 18),
)

DEMO_PROVIDERS = (
    ("InternalStock", ProviderKind.INTERNAL),
    ("DemoSupplierA", ProviderKind.EXTERNAL),
    ("DemoSupplierB", ProviderKind.EXTERNAL),
    ("DemoMarketplaceSeller", ProviderKind.EXTERNAL),
)

# A product may have several source-specific offers. The caller must send the
# chosen stock_source_id, never the provider_id, when creating a reservation.
DEMO_SOURCE_PLANS = (
    ("ANKR-HUB-7C", "InternalStock"),
    ("ANKR-HUB-7C", "DemoSupplierA"),
    ("ANKR-HUB-7C", "DemoSupplierB"),
    ("LOGI-MX-M3S", "InternalStock"),
    ("LOGI-MX-M3S", "DemoSupplierA"),
    ("LOGI-MX-M3S", "DemoMarketplaceSeller"),
    ("SONY-WH-1000XM5", "InternalStock"),
    ("SONY-WH-1000XM5", "DemoSupplierB"),
    ("SONY-WH-1000XM5", "DemoMarketplaceSeller"),
    ("KINDLE-PW-11", "DemoSupplierA"),
    ("KINDLE-PW-11", "DemoSupplierB"),
)


async def main() -> None:
    seeded_sources: list[tuple[str, str, str, str, str]] = []

    async with get_session() as session:
        async with session.begin():
            providers: dict[str, InventoryProviderModel] = {}
            for provider_name, provider_kind in DEMO_PROVIDERS:
                provider = await session.scalar(
                    select(InventoryProviderModel).where(
                        InventoryProviderModel.name == provider_name
                    )
                )
                if provider is None:
                    provider = InventoryProviderModel(
                        id=uuid4(),
                        name=provider_name,
                        kind=provider_kind,
                        enabled=True,
                    )
                    session.add(provider)
                    await session.flush()
                providers[provider_name] = provider

            products: dict[str, tuple[ProductModel, int]] = {}
            for sku, name, on_hand in DEMO_PRODUCTS:
                product = await session.scalar(
                    select(ProductModel).where(ProductModel.sku == sku)
                )
                if product is None:
                    product = ProductModel(id=uuid4(), sku=sku, name=name)
                    session.add(product)
                    await session.flush()
                products[sku] = (product, on_hand)

            for sku, provider_name in DEMO_SOURCE_PLANS:
                product, on_hand = products[sku]
                provider = providers[provider_name]
                source = await session.scalar(
                    select(StockSourceModel).where(
                        StockSourceModel.product_id == product.id,
                        StockSourceModel.provider_id == provider.id,
                    )
                )
                if source is None:
                    source = StockSourceModel(
                        id=uuid4(),
                        product_id=product.id,
                        provider_id=provider.id,
                        provider_sku=f"{sku}@{provider_name}",
                        enabled=True,
                    )
                    session.add(source)
                    await session.flush()

                stock = await session.get(InternalStockModel, source.id)
                if provider.kind == ProviderKind.INTERNAL and stock is None:
                    session.add(
                        InternalStockModel(
                            stock_source_id=source.id,
                            on_hand=on_hand,
                            held=0,
                        )
                    )

                seeded_sources.append(
                    (
                        sku,
                        str(product.id),
                        provider_name,
                        str(provider.id),
                        str(source.id),
                    )
                )

    print("Seeded sources; POST /reservations requires stock_source_id, not provider_id:")
    for sku, product_id, provider_name, provider_id, source_id in seeded_sources:
        print(
            f"sku={sku} product_id={product_id} provider={provider_name} "
            f"provider_id={provider_id} stock_source_id={source_id}"
        )


if __name__ == "__main__":
    asyncio.run(main())
