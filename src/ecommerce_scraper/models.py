from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Platform(StrEnum):
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    MAGENTO = "magento"
    BIGCOMMERCE = "bigcommerce"
    GENERIC = "generic"


class ExtractionSource(StrEnum):
    PLATFORM_API = "platform_api"
    JSON_LD = "json_ld"
    MICRODATA = "microdata"
    DOM = "dom"


class ExtractionStrategy(StrEnum):
    SHOPIFY_JSON = "shopify_json"
    SHOPIFY_SITEMAP = "shopify_sitemap"
    WOOCOMMERCE_API = "woocommerce_api"
    GENERIC_HTML = "generic_html"


class CategoryVisibility(StrEnum):
    NAVIGATION = "navigation"
    SITEMAP = "sitemap"
    API_ONLY = "api_only"
    PLATFORM_API = "platform_api"
    SYNTHETIC = "synthetic"


class AccessEventKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    BOT_CHALLENGE = "bot_challenge"
    ACCESS_DENIED = "access_denied"
    TRANSIENT_ERROR = "transient_error"
    ROBOTS_DENIED = "robots_denied"


class TargetAccessEvent(BaseModel):
    kind: AccessEventKind
    url: str
    status_code: int | None = None
    provider: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    retry_after_seconds: float | None = Field(default=None, ge=0)
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TargetAccessReport(BaseModel):
    total_requests: int = 0
    retries: int = 0
    rate_limited_responses: int = 0
    block_events: int = 0
    robots_denied: int = 0
    effective_min_interval_seconds: float = 0.0
    circuit_open: bool = False
    events: list[TargetAccessEvent] = Field(default_factory=list)


class ProductVariant(BaseModel):
    id: str | None = None
    title: str | None = None
    sku: str | None = None
    price: str | None = None
    compare_at_price: str | None = None
    currency: str | None = None
    available: bool | None = None
    barcode: str | None = None
    options: dict[str, str] = Field(default_factory=dict)


class Product(BaseModel):
    name: str
    url: str
    canonical_url: str | None = None
    category_path: list[str] = Field(default_factory=list)
    description: str | None = None
    description_html: str | None = None
    brand: str | None = None
    sku: str | None = None
    mpn: str | None = None
    gtin: str | None = None
    price: str | None = None
    compare_at_price: str | None = None
    currency: str | None = None
    availability: str | None = None
    images: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    variants: list[ProductVariant] = Field(default_factory=list)
    sources: list[ExtractionSource] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)


class CategoryNode(BaseModel):
    name: str
    url: str | None = None
    path: list[str] = Field(default_factory=list)
    children: list[CategoryNode] = Field(default_factory=list)


class CategoryResult(BaseModel):
    name: str
    url: str | None = None
    path: list[str] = Field(default_factory=list)
    visibility: CategoryVisibility = CategoryVisibility.SYNTHETIC
    discovered_product_count: int = 0
    products: list[Product] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ScrapeRequest(BaseModel):
    url: str
    products_per_category: int = Field(default=10, ge=1, le=50)
    seed: str | None = None
    max_categories: int = Field(default=500, ge=1, le=2_000)
    max_sitemap_urls: int = Field(default=50_000, ge=100, le=500_000)
    max_pages_per_category: int = Field(default=25, ge=1, le=200)
    include_api_only_collections: bool = False

    @field_validator("url")
    @classmethod
    def url_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("URL must not be blank")
        return value.strip()


class ScrapeResult(BaseModel):
    site_url: str
    platform: Platform
    extraction_strategy: ExtractionStrategy = ExtractionStrategy.GENERIC_HTML
    category_tree: list[CategoryNode] = Field(default_factory=list)
    categories: list[CategoryResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    access_report: TargetAccessReport = Field(default_factory=TargetAccessReport)
    started_at: datetime
    completed_at: datetime


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(BaseModel):
    id: str
    state: JobState = JobState.QUEUED
    request: ScrapeRequest
    result: ScrapeResult | None = None
    error: str | None = None
    access_report: TargetAccessReport | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobAccepted(BaseModel):
    id: str
    state: JobState
    status_url: str
