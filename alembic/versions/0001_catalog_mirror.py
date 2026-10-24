"""catalog mirror tables (products, images, variants, metafields, sync state)

Revision ID: 0001_catalog_mirror
Revises:
Create Date: 2026-10-24
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_catalog_mirror"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products_mirror",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gid", sa.String(length=64), nullable=False),
        sa.Column("numeric_id", sa.String(length=32), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("handle", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("vendor", sa.String(length=255), nullable=True),
        sa.Column("product_type", sa.String(length=255), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("seo_title", sa.String(length=512), nullable=True),
        sa.Column("seo_description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("price_min", sa.Float(), nullable=True),
        sa.Column("price_max", sa.Float(), nullable=True),
        sa.Column("faq_json", sa.Text(), nullable=True),
        sa.Column("reviews_json", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("remote_updated_at", sa.String(length=64), nullable=True),
        sa.Column("synced_at", sa.String(length=64), nullable=True),
        sa.Column("deleted_remotely", sa.Boolean(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gid"),
    )
    op.create_index("ix_products_mirror_gid", "products_mirror", ["gid"])
    op.create_index("ix_products_mirror_numeric_id", "products_mirror", ["numeric_id"])
    op.create_index("ix_products_mirror_handle", "products_mirror", ["handle"])

    op.create_table(
        "product_images_mirror",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("src", sa.Text(), nullable=True),
        sa.Column("alt", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("is_video", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products_mirror.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_images_mirror_product_id", "product_images_mirror", ["product_id"])

    op.create_table(
        "product_variants_mirror",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gid", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("sku", sa.String(length=255), nullable=True),
        sa.Column("barcode", sa.String(length=64), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products_mirror.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gid"),
    )
    op.create_index("ix_product_variants_mirror_gid", "product_variants_mirror", ["gid"])
    op.create_index("ix_product_variants_mirror_product_id", "product_variants_mirror", ["product_id"])

    op.create_table(
        "product_metafields_mirror",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=True),
        sa.Column("key", sa.String(length=64), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products_mirror.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_metafields_mirror_product_id", "product_metafields_mirror", ["product_id"])

    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    op.drop_table("product_metafields_mirror")
    op.drop_table("product_variants_mirror")
    op.drop_table("product_images_mirror")
    op.drop_table("products_mirror")
