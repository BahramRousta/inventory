"""add request fingerprint to reservations

Revision ID: a91f6d7c4e21
Revises: f4221c14265a
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a91f6d7c4e21"
down_revision: Union[str, Sequence[str], None] = "f4221c14265a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows predate fingerprint validation. The sentinel deliberately
    # cannot match a real SHA-256 fingerprint, so reusing an old idempotency key
    # fails closed instead of replaying an unverified request.
    op.add_column(
        "reservations",
        sa.Column(
            "request_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="0" * 64,
        ),
    )
    op.alter_column(
        "reservations",
        "request_fingerprint",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("reservations", "request_fingerprint")
