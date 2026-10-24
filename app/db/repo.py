"""Repository helpers: canonical product dicts <-> mirror rows.

Canonical shape (scoring/fixer-compatible):
  id (numeric str), gid, title, handle, product_type, vendor, status, tags,
  body_html, seo_title, seo_description, category, price_min/max,
  images [{src, alt, position, is_video}], variants [{...}],
  metafields {"ns.key": value}, faq [{q,a}], reviews {count, avg, source},
  updated_at, content_hash
"""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Product,
    ProductImage,
    ProductMetafield,
    ProductVariant,
    SyncState,
    now_iso,
    split_metafield_key,
)
from .session import get_engine


def content_hash(canonical: dict) -> str:
    """Stable hash of the product's *content* (not sync bookkeeping).

    synced_at/raw_json change on every write, so they must not participate or
    every re-sync would look like an update.
    """
    payload = {
        k: v
        for k, v in canonical.items()
        if k not in ("content_hash", "synced_at", "raw_json")
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def merge_metafields(flat: dict[str, str]) -> dict[str, str]:
    """Normalize "ns.key" collisions for scoring (guard against weird ns names)."""
    return {k if "." in k else f"custom.{k}": v for k, v in flat.items()}


def upsert_product(session: Session, canonical: dict) -> Product:
    """Insert or refresh one product row + children from a canonical dict."""
    gid = canonical.get("gid") or f"gid://shopify/Product/{canonical['id']}"
    numeric = str(canonical["id"])
    row = session.scalar(select(Product).where(Product.gid == gid))
    if row is None:
        row = Product(gid=gid, numeric_id=numeric)
        session.add(row)

    row.numeric_id = numeric
    row.title = canonical.get("title", "") or ""
    row.handle = canonical.get("handle", "") or ""
    row.status = canonical.get("status", "") or "active"
    row.vendor = canonical.get("vendor", "") or ""
    row.product_type = canonical.get("product_type", "") or ""
    row.tags = ",".join(canonical.get("tags") or []) if isinstance(canonical.get("tags"), list) else (canonical.get("tags") or "")
    row.body_html = canonical.get("body_html", "") or ""
    row.seo_title = canonical.get("seo_title", "") or ""
    row.seo_description = canonical.get("seo_description", "") or ""
    row.category = canonical.get("category", "") or ""
    row.price_min = canonical.get("price_min")
    row.price_max = canonical.get("price_max")
    row.faq_json = json.dumps(canonical.get("faq") or [], ensure_ascii=False)
    row.reviews_json = json.dumps(canonical.get("reviews") or {}, ensure_ascii=False)
    row.remote_updated_at = canonical.get("updated_at", "") or row.remote_updated_at
    row.synced_at = now_iso()
    row.deleted_remotely = False
    if "raw_json" in canonical:
        row.raw_json = canonical["raw_json"] or ""

    wanted_images = canonical.get("images") or []
    _replace_children(session, row, wanted_images, canonical.get("variants") or [], canonical.get("metafields") or {})
    row.content_hash = content_hash(row_to_canonical(row))
    session.flush()
    return row


def _replace_children(session: Session, row: Product, images, variants, metafields) -> None:
    row.images.clear()
    for pos, img in enumerate(images or [], start=1):
        src = str(img.get("src") or img.get("url") or "")
        row.images.append(
            ProductImage(
                src=src,
                alt=str(img.get("alt") or ""),
                position=int(img.get("position") or pos),
                is_video=bool(img.get("is_video")) or src.lower().endswith((".mp4", ".mov", ".webm")),
            )
        )

    row.variants.clear()
    for pos, v in enumerate(variants or [], start=1):
        vgid = str(v.get("gid") or f"gid://shopify/ProductVariant/{v.get('id', pos)}")
        existing = session.scalar(select(ProductVariant).where(ProductVariant.gid == vgid))
        if existing and existing.product_id in (None, row.id):
            row.variants.append(
                ProductVariant(
                    gid=vgid,
                    product_id=row.id,
                    title=str(v.get("title") or ""),
                    sku=str(v.get("sku") or ""),
                    barcode=str(v.get("barcode") or ""),
                    price=float(v.get("price") or 0),
                    available=v.get("available"),
                    position=int(v.get("position") or pos),
                )
            )
        else:
            row.variants.append(
                ProductVariant(
                    gid=vgid,
                    product_id=row.id,
                    title=str(v.get("title") or ""),
                    sku=str(v.get("sku") or ""),
                    barcode=str(v.get("barcode") or ""),
                    price=float(v.get("price") or 0),
                    available=v.get("available"),
                    position=int(v.get("position") or pos),
                )
            )

    row.metafields.clear()
    for key, value in (metafields or {}).items():
        ns, k = split_metafield_key(key)
        row.metafields.append(ProductMetafield(product_id=row.id, namespace=ns, key=k, type="string", value=str(value)))


def row_to_canonical(row: Product, with_children: bool = True) -> dict:
    reviews: dict = {}
    try:
        reviews = json.loads(row.reviews_json) if row.reviews_json else {}
    except ValueError:
        reviews = {}
    faq: list = []
    try:
        faq = json.loads(row.faq_json) if row.faq_json else []
    except ValueError:
        faq = []

    canonical = {
        "id": row.numeric_id,
        "gid": row.gid,
        "title": row.title,
        "handle": row.handle,
        "product_type": row.product_type,
        "vendor": row.vendor,
        "status": row.status,
        "tags": [t for t in row.tags.split(",") if t] if row.tags else [],
        "body_html": row.body_html,
        "seo_title": row.seo_title,
        "seo_description": row.seo_description,
        "category": row.category,
        "price_min": row.price_min,
        "price_max": row.price_max,
        "faq": faq,
        "reviews": reviews,
        "updated_at": row.remote_updated_at,
        "synced_at": row.synced_at,
        "content_hash": "",
    }
    if with_children:
        canonical["images"] = [
            {"src": i.src, "alt": i.alt, "position": i.position, "is_video": i.is_video}
            for i in sorted(row.images, key=lambda i: i.position)
        ]
        canonical["variants"] = [
            {
                "gid": v.gid,
                "title": v.title,
                "sku": v.sku,
                "barcode": v.barcode,
                "price": v.price,
                "available": v.available,
                "position": v.position,
            }
            for v in sorted(row.variants, key=lambda v: v.position)
        ]
        canonical["metafields"] = {m.nested_key: m.value for m in row.metafields}
    return canonical


def get_product(numeric_id_or_gid: str) -> dict | None:
    """Canonical product dict from the mirror, or None."""
    with Session(get_engine()) as session:
        key = str(numeric_id_or_gid)
        row = session.scalar(select(Product).where(Product.numeric_id == key))
        if row is None:
            row = session.scalar(select(Product).where(Product.gid == key))
        if row is None:
            row = session.scalar(select(Product).where(Product.handle == key))
        return row_to_canonical(row) if row else None


def list_products(include_deleted: bool = False) -> list[dict]:
    with Session(get_engine()) as session:
        stmt = select(Product).order_by(Product.id)
        if not include_deleted:
            stmt = stmt.where(Product.deleted_remotely == False)  # noqa: E712
        rows = session.scalars(stmt).all()
        return [row_to_canonical(r) for r in rows]


def mark_deleted(gid: str) -> bool:
    with Session(get_engine()) as session:
        row = session.scalar(select(Product).where(Product.gid == gid))
        if not row:
            return False
        row.deleted_remotely = True
        session.commit()
        return True


def get_state(key: str) -> str | None:
    with Session(get_engine()) as session:
        row = session.get(SyncState, key)
        return row.value if row else None


def set_state(key: str, value: str) -> None:
    with Session(get_engine()) as session:
        row = session.get(SyncState, key)
        if row:
            row.value = value
        else:
            session.add(SyncState(key=key, value=value))
        session.commit()


def create_all() -> None:
    from .models import Base

    Base.metadata.create_all(get_engine())
