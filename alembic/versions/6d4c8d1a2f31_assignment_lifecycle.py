"""assignment lifecycle requirements

Revision ID: 6d4c8d1a2f31
Revises: 0a5cea2ee1e6
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6d4c8d1a2f31"
down_revision: Union[str, Sequence[str], None] = "0a5cea2ee1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_providers",
        sa.Column(
            "capabilities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "inventory_providers",
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["reservations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reservation_id"),
    )


def downgrade() -> None:
    op.drop_table("orders")
    op.drop_column("inventory_providers", "credential_ref")
    op.drop_column("inventory_providers", "capabilities")
