"""secure referral program

Revision ID: 9b4d7a2e1f60
Revises: c13a9c6a1b72
Create Date: 2026-07-21 01:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b4d7a2e1f60"
down_revision: str | Sequence[str] | None = "c13a9c6a1b72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "referral_program_configs",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("referrer_reward", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("invitee_reward", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("minimum_order_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_table(
        "referrals",
        sa.Column("invitee_id", sa.BigInteger(), nullable=False),
        sa.Column("referrer_id", sa.BigInteger(), nullable=False),
        sa.Column("qualified_order_id", sa.Uuid(), nullable=True),
        sa.Column("referrer_reward_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("invitee_reward_amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("rewarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("invitee_id <> referrer_id", name="ck_referral_not_self"),
        sa.ForeignKeyConstraint(
            ["invitee_id"], ["users.telegram_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["referrer_id"], ["users.telegram_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["qualified_order_id"], ["orders.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("invitee_id"),
        sa.UniqueConstraint("qualified_order_id"),
    )
    op.create_index(op.f("ix_referrals_referrer_id"), "referrals", ["referrer_id"])
    op.create_index(
        "ix_referrals_referrer_created",
        "referrals",
        ["referrer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_referrals_referrer_created", table_name="referrals")
    op.drop_index(op.f("ix_referrals_referrer_id"), table_name="referrals")
    op.drop_table("referrals")
    op.drop_table("referral_program_configs")
