"""The ``products`` table (PRD §8).

Note on ``gtin``: in Shopify, GTIN/barcode is a *variant* field, not a product field.
The PRD models it at product level, so this column is kept (nullable, populated from the
primary variant) while every variant's barcode is preserved in ``variants_json`` — the
product-level column is a convenience, not the source of truth.
"""

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Product(Base, TimestampMixin):
    __tablename__ = "products"
    __table_args__ = (
        # Ingestion upserts on this pair, which is what makes re-ingest (and
        # retry-after-401) idempotent rather than duplicating the catalog.
        Index("uq_products_shop_shopify_id", "shop_id", "shopify_product_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )

    shopify_product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    variants_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    gtin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metafields_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    visibility_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Raw merchant fields used to classify the product (coffee / equipment / other) for the
    # per-class audit rubric. Stored raw — the class is derived deterministically at audit time by
    # ``services.catalog.classify_product``. ``product_type`` is Shopify's free-text productType;
    # ``category`` is the Standard-taxonomy fullName when set.
    product_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)

    shop: Mapped["Shop"] = relationship(back_populates="products")  # noqa: F821
