"""add durable provider work claims to reservation lines

Revision ID: 3d4b8a9d2e6f
Revises: 0a5cea2ee1e6
Create Date: 2026-09-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3d4b8a9d2e6f"
down_revision: Union[str, Sequence[str], None] = "0a5cea2ee1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reservation_lines",
        sa.Column("provider_claim_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "reservation_lines",
        sa.Column("provider_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reservation_line_work_claim",
        "reservation_lines",
        ["status", "provider_lease_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reservation_line_work_claim", table_name="reservation_lines")
    op.drop_column("reservation_lines", "provider_lease_until")
    op.drop_column("reservation_lines", "provider_claim_token")
