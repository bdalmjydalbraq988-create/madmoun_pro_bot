"""supplier catalog synchronization

Revision ID: c13a9c6a1b72
Revises: a66220d5e82a
Create Date: 2026-07-18 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c13a9c6a1b72"
down_revision: str | Sequence[str] | None = "a66220d5e82a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplier_sync_configs",
        sa.Column("provider_code", sa.String(length=40), nullable=False),
        sa.Column("markup_percent", sa.Numeric(precision=9, scale=4), nullable=False),
        sa.Column("minimum_profit", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("auto_activate", sa.Boolean(), nullable=False),
        sa.Column("deactivate_missing", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(length=20), nullable=False),
        sa.Column("last_sync_message", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_code"),
    )
    op.create_table(
        "supplier_catalog_items",
        sa.Column("provider_code", sa.String(length=40), nullable=False),
        sa.Column("provider_product_id", sa.String(length=100), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("delivery_type", sa.String(length=20), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("warranty_days", sa.Integer(), nullable=False),
        sa.Column("price_tiers_json", sa.JSON(), nullable=False),
        sa.Column("raw_payload_json", sa.JSON(), nullable=False),
        sa.Column("price_locked", sa.Boolean(), nullable=False),
        sa.Column("activation_locked", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("provider_code", "provider_product_id"),
        sa.UniqueConstraint("product_id", name="uq_supplier_catalog_product"),
    )
    op.create_index(
        "ix_supplier_catalog_seen",
        "supplier_catalog_items",
        ["provider_code", "last_seen_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_supplier_catalog_items_price_locked"),
        "supplier_catalog_items",
        ["price_locked"],
        unique=False,
    )
    op.create_index(
        op.f("ix_supplier_catalog_items_activation_locked"),
        "supplier_catalog_items",
        ["activation_locked"],
        unique=False,
    )
    op.create_index(
        op.f("ix_supplier_catalog_items_product_id"),
        "supplier_catalog_items",
        ["product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_supplier_catalog_items_product_id"),
        table_name="supplier_catalog_items",
    )
    op.drop_index(
        op.f("ix_supplier_catalog_items_activation_locked"),
        table_name="supplier_catalog_items",
    )
    op.drop_index(
        op.f("ix_supplier_catalog_items_price_locked"),
        table_name="supplier_catalog_items",
    )
    op.drop_index("ix_supplier_catalog_seen", table_name="supplier_catalog_items")
    op.drop_table("supplier_catalog_items")
    op.drop_table("supplier_sync_configs")
