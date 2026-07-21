"""trusted Jeeb notification relay events

Revision ID: d31f6a8c2e44
Revises: 9b4d7a2e1f60
Create Date: 2026-07-21 02:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d31f6a8c2e44"
down_revision: str | Sequence[str] | None = "9b4d7a2e1f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jeeb_payment_intents",
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("payer_account", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("payment_id"),
    )
    op.create_table(
        "jeeb_transaction_events",
        sa.Column("transaction_id", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("sender_account", sa.String(length=40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_payment_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["matched_payment_id"], ["payments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.UniqueConstraint("matched_payment_id"),
    )


def downgrade() -> None:
    op.drop_table("jeeb_transaction_events")
    op.drop_table("jeeb_payment_intents")
