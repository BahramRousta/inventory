import asyncio
from uuid import UUID, uuid4

from sqlalchemy import select

from app.domain.enums import ProviderKind
from app.infrastructure.db.models import (
    InternalStockModel,
    InventoryProviderModel,
    ProductModel,
    StockSourceModel,
)
from app.infrastructure.db.session import get_session


DEMO_EXTERNAL_PROVIDER_ID = UUID("11111111-1111-1111-1111-111111111111")
DEMO_QUERY_ONLY_PROVIDER_ID = UUID("22222222-2222-2222-2222-222222222222")

DEMO_PRODUCTS = (
    ("ANKR-HUB-7C", "Anker USB-C Hub 7-in-1", 10),
    ("LOGI-MX-M3S", "Logitech MX Master 3S", 25),
    ("SONY-WH-1000XM5", "Sony WH-1000XM5 Headphones", 12),
)

DEMO_SOURCE_PLANS = (
    ("ANKR-HUB-7C", "InternalStock"),
    ("ANKR-HUB-7C", "FakeExternalProvider"),
    ("ANKR-HUB-7C", "FakeQueryOnlyProvider"),
    ("LOGI-MX-M3S", "InternalStock"),
    ("LOGI-MX-M3S", "FakeExternalProvider"),
    ("LOGI-MX-M3S", "FakeQueryOnlyProvider"),
    ("SONY-WH-1000XM5", "InternalStock"),
    ("SONY-WH-1000XM5", "FakeExternalProvider"),
    ("SONY-WH-1000XM5", "FakeQueryOnlyProvider"),
)


async def main() -> None:
    seeded_sources: list[tuple[str, str, str, str, str]] = []

    async with get_session() as session:
        async with session.begin():
            internal = await session.scalar(
                select(InventoryProviderModel).where(InventoryProviderModel.name == "InternalStock")
            )
            if internal is None:
                internal = InventoryProviderModel(
                    id=uuid4(),
                    name="InternalStock",
                    kind=ProviderKind.INTERNAL,
                    enabled=True,
                )
                session.add(internal)
                await session.flush()
            internal.enabled = True

            external = await session.get(InventoryProviderModel, DEMO_EXTERNAL_PROVIDER_ID)
            if external is None:
                external = InventoryProviderModel(
                    id=DEMO_EXTERNAL_PROVIDER_ID,
                    name="FakeExternalProvider",
                    kind=ProviderKind.EXTERNAL,
                    enabled=True,
                )
                session.add(external)
                await session.flush()
            external.enabled = True

            query_only = await session.get(
                InventoryProviderModel, DEMO_QUERY_ONLY_PROVIDER_ID
            )
            if query_only is None:
                query_only = InventoryProviderModel(
                    id=DEMO_QUERY_ONLY_PROVIDER_ID,
                    name="FakeQueryOnlyProvider",
                    kind=ProviderKind.EXTERNAL,
                    enabled=True,
                )
                session.add(query_only)
                await session.flush()
            query_only.enabled = True

            providers = {
                "InternalStock": internal,
                "FakeExternalProvider": external,
                "FakeQueryOnlyProvider": query_only,
            }

            products: dict[str, tuple[ProductModel, int]] = {}
            for sku, name, on_hand in DEMO_PRODUCTS:
                product = await session.scalar(select(ProductModel).where(ProductModel.sku == sku))
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

                if provider.kind == ProviderKind.INTERNAL:
                    stock = await session.get(InternalStockModel, source.id)
                    if stock is None:
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

    print("EXTERNAL_PROVIDER_ID=11111111-1111-1111-1111-111111111111")
    print("QUERY_ONLY_PROVIDER_ID=22222222-2222-2222-2222-222222222222")
    print("Seeded source-specific inventory:")
    for sku, product_id, provider_name, provider_id, source_id in seeded_sources:
        print(
            f"sku={sku} product_id={product_id} provider={provider_name} "
            f"provider_id={provider_id} stock_source_id={source_id}"
        )


if __name__ == "__main__":
    asyncio.run(main())
