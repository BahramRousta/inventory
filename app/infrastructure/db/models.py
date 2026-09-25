from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.domain.enums import PaymentOutcome, ProviderKind, ReservationLineStatus, ReservationStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ProductModel(Base):
    __tablename__ = "products"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class InventoryProviderModel(Base):
    __tablename__ = "inventory_providers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    kind: Mapped[ProviderKind] = mapped_column(
        Enum(ProviderKind, native_enum=False, length=32), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StockSourceModel(Base):
    __tablename__ = "stock_sources"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_providers.id", ondelete="RESTRICT"), nullable=False
    )
    provider_sku: Mapped[str | None] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint(
            "product_id", "provider_id", name="uq_stock_source_product_provider"
        ),
    )


class InternalStockModel(Base):
    __tablename__ = "internal_stock"

    stock_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("stock_sources.id", ondelete="RESTRICT"), primary_key=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_internal_stock_on_hand_nonnegative"),
        CheckConstraint("held >= 0", name="ck_internal_stock_held_nonnegative"),
        CheckConstraint("held <= on_hand", name="ck_internal_stock_held_lte_on_hand"),
    )


class ReservationModel(Base):
    __tablename__ = "reservations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, native_enum=False, length=32), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    release_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_reservation_user_idempotency"
        ),
        Index("ix_reservation_status_expiry", "status", "expires_at"),
    )


class ReservationLineModel(Base):
    __tablename__ = "reservation_lines"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT"), nullable=False
    )
    stock_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("stock_sources.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ReservationLineStatus] = mapped_column(
        Enum(ReservationLineStatus, native_enum=False, length=32), nullable=False
    )
    external_hold_ref: Mapped[str | None] = mapped_column(String(255))
    provider_claim_token: Mapped[UUID | None] = mapped_column(Uuid)
    provider_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_reservation_line_quantity_positive"),
        UniqueConstraint(
            "reservation_id", "stock_source_id", name="uq_reservation_line_source"
        ),
        Index("ix_reservation_line_work_claim", "status", "provider_lease_until"),
    )


class PaymentEventModel(Base):
    __tablename__ = "payment_events"

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[PaymentOutcome] = mapped_column(
        Enum(PaymentOutcome, native_enum=False, length=16), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (Index("ix_payment_event_reservation", "reservation_id"),)


class OrderModel(Base):
    __tablename__ = "orders"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    user_id: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class OrderLineModel(Base):
    __tablename__ = "order_lines"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    stock_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("stock_sources.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_providers.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_allocation_ref: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_line_quantity_positive"),
        UniqueConstraint("order_id", "stock_source_id", name="uq_order_line_source"),
    )
