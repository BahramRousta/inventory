"""move provider capabilities and credentials out of database

Revision ID: c1e4f7a9b2d3
Revises: b7d9a2c4e6f1
Create Date: 2026-09-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c1e4f7a9b2d3"
down_revision: Union[str, Sequence[str], None] = "b7d9a2c4e6f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("inventory_providers", "credential_ref")
    op.drop_column("inventory_providers", "config_key")
    op.drop_column("inventory_providers", "hold_is_final_allocation")
    op.drop_column("inventory_providers", "supports_get_hold")
    op.drop_column("inventory_providers", "supports_release")
    op.drop_column("inventory_providers", "supports_hold")
    op.drop_column("inventory_providers", "supports_check")


def downgrade() -> None:
    op.add_column(
        "inventory_providers",
        sa.Column("supports_check", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "inventory_providers",
        sa.Column("supports_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "inventory_providers",
        sa.Column("supports_release", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "inventory_providers",
        sa.Column("supports_get_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "inventory_providers",
        sa.Column(
            "hold_is_final_allocation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "inventory_providers",
        sa.Column("config_key", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "inventory_providers",
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
    )
