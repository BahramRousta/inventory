"""assignment remediation persistence

Revision ID: b7d9a2c4e6f1
Revises: aa44ccf9f7e6
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7d9a2c4e6f1"
down_revision: Union[str, Sequence[str], None] = "aa44ccf9f7e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))

    for name in (
        "supports_check",
        "supports_hold",
        "supports_release",
        "supports_get_hold",
        "hold_is_final_allocation",
    ):
        op.add_column(
            "inventory_providers",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.add_column("inventory_providers", sa.Column("config_key", sa.String(length=160), nullable=True))
    op.add_column(
        "inventory_providers",
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
    )

    op.create_table(
        "payment_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=160), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum("SUCCESS", "FAILURE", name="paymentoutcome", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_payment_event_reservation",
        "payment_events",
        ["reservation_id"],
        unique=False,
    )

    op.create_table(
        "order_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("stock_source_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("provider_allocation_ref", sa.String(length=255), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_order_line_quantity_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_id"], ["inventory_providers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_source_id"], ["stock_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "stock_source_id", name="uq_order_line_source"),
    )


def downgrade() -> None:
    op.drop_table("order_lines")
    op.drop_index("ix_payment_event_reservation", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_column("inventory_providers", "credential_ref")
    op.drop_column("inventory_providers", "config_key")
    for name in (
        "hold_is_final_allocation",
        "supports_get_hold",
        "supports_release",
        "supports_hold",
        "supports_check",
    ):
        op.drop_column("inventory_providers", name)
    op.drop_column("reservations", "request_fingerprint")
