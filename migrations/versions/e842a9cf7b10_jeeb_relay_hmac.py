"""bind Jeeb relay events to signed device requests

Revision ID: e842a9cf7b10
Revises: d31f6a8c2e44
Create Date: 2026-07-21 06:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e842a9cf7b10"
down_revision: str | Sequence[str] | None = "d31f6a8c2e44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jeeb_amount_reservations",
        sa.Column("reservation_key", sa.String(length=80), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("reservation_key"),
        sa.UniqueConstraint("payment_id"),
    )
    op.create_index(
        op.f("ix_jeeb_amount_reservations_expires_at"),
        "jeeb_amount_reservations",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "jeeb_relay_receipts",
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("source_device_id", sa.String(length=64), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("transaction_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("nonce"),
    )
    op.create_index(
        op.f("ix_jeeb_relay_receipts_source_device_id"),
        "jeeb_relay_receipts",
        ["source_device_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_jeeb_relay_receipts_transaction_id"),
        "jeeb_relay_receipts",
        ["transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_jeeb_relay_receipts_transaction_id"),
        table_name="jeeb_relay_receipts",
    )
    op.drop_index(
        op.f("ix_jeeb_relay_receipts_source_device_id"),
        table_name="jeeb_relay_receipts",
    )
    op.drop_table("jeeb_relay_receipts")
    op.drop_index(
        op.f("ix_jeeb_amount_reservations_expires_at"),
        table_name="jeeb_amount_reservations",
    )
    op.drop_table("jeeb_amount_reservations")
