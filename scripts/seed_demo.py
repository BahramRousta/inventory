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
)


async def main() -> None:
    seeded_sources: list[tuple[str, str]] = []

    async with get_session() as session:
        async with session.begin():
            provider = await session.scalar(
                select(InventoryProviderModel).where(
                    InventoryProviderModel.name == "InternalStock"
                )
            )
            if provider is None:
                provider = InventoryProviderModel(
                    id=uuid4(),
                    name="InternalStock",
                    kind=ProviderKind.INTERNAL,
                    enabled=True,
                )
                session.add(provider)
                await session.flush()

            for sku, name, on_hand in DEMO_PRODUCTS:
                product = await session.scalar(
                    select(ProductModel).where(ProductModel.sku == sku)
                )
                if product is None:
                    product = ProductModel(id=uuid4(), sku=sku, name=name)
                    session.add(product)
                    await session.flush()

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
                        provider_sku=sku,
                        enabled=True,
                    )
                    session.add(source)
                    await session.flush()

                stock = await session.get(InternalStockModel, source.id)
                if stock is None:
                    session.add(
                        InternalStockModel(stock_source_id=source.id, on_hand=on_hand, held=0)
                    )

                seeded_sources.append((sku, str(source.id)))

    for sku, source_id in seeded_sources:
        print(f"sku={sku} stock_source_id={source_id}")


if __name__ == "__main__":
    asyncio.run(main())
