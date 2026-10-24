"""Catalog mirror models (SQLAlchemy 2.0 typed style).

Purpose: a local, hash-tracked copy of the store catalog so scoring, diagnosis
and suggestion drafting never hammer the Shopify API, and apply-time staleness
checks have persistent state. Types are Postgres- and SQLite-compatible.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Product(Base):
    """One row per Shopify product in the mirror."""

    __tablename__ = "products_mirror"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gid: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # gid://shopify/Product/123
    numeric_id: Mapped[str] = mapped_column(String(32), index=True)  # "123" for UI links
    title: Mapped[str] = mapped_column(Text, default="")
    handle: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    vendor: Mapped[str] = mapped_column(String(255), default="")
    product_type: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[str] = mapped_column(Text, default="")  # comma-joined
    body_html: Mapped[str] = mapped_column(Text, default="")  # descriptionHtml
    seo_title: Mapped[str] = mapped_column(String(512), default="")
    seo_description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(255), default="")  # taxonomy GID or name
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    faq_json: Mapped[str] = mapped_column(Text, default="")  # JSON list of {"q","a"} from geo_agent.faq_json
    reviews_json: Mapped[str] = mapped_column(Text, default="")  # {"count": int|None, "avg": float|None, "source": str}
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    remote_updated_at: Mapped[str] = mapped_column(String(64), default="")
    synced_at: Mapped[str] = mapped_column(String(64), default=now_iso)
    deleted_remotely: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[str] = mapped_column(Text, default="")  # last raw GraphQL node for debugging

    images: Mapped[list["ProductImage"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    metafields: Mapped[list["ProductMetafield"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductImage(Base):
    __tablename__ = "product_images_mirror"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products_mirror.id", ondelete="CASCADE"), index=True)
    src: Mapped[str] = mapped_column(Text, default="")
    alt: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_video: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped[Product] = relationship(back_populates="images")


class ProductVariant(Base):
    __tablename__ = "product_variants_mirror"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products_mirror.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    sku: Mapped[str] = mapped_column(String(255), default="")
    barcode: Mapped[str] = mapped_column(String(64), default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped[Product] = relationship(back_populates="variants")


class ProductMetafield(Base):
    __tablename__ = "product_metafields_mirror"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products_mirror.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(64), default="custom")
    key: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(64), default="string")
    value: Mapped[str] = mapped_column(Text, default="")

    product: Mapped[Product] = relationship(back_populates="metafields")

    @property
    def nested_key(self) -> str:
        # "custom" namespace stores bare keys (scoring + fixer consume short names)
        return self.key if self.namespace == "custom" else f"{self.namespace}.{self.key}"


# Namespaces whose names contain dots ("judge.me") defeat naive partitioning;
# they are matched as prefixes (longest first) before falling back.
DOTTED_NAMESPACES = ("judge.me",)


def split_metafield_key(key: str) -> tuple[str, str]:
    """Split a canonical metafield key into (namespace, short_key)."""
    for ns in DOTTED_NAMESPACES:
        if key.startswith(f"{ns}."):
            return ns, key[len(ns) + 1 :]
    ns, _, short = key.partition(".")
    return (ns, short) if short else ("custom", key)


class SyncState(Base):
    """Small KV for sync bookkeeping (cursors, last bulk URL, job ids)."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def to_jsonable(value: Any) -> Any:  # helper shared by repo serializers
    return value
