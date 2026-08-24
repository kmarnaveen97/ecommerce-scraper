from __future__ import annotations

from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ecommerce_scraper.adapters.base import Adapter
from ecommerce_scraper.discovery import flatten_categories
from ecommerce_scraper.http_client import TargetAccessError
from ecommerce_scraper.models import (
    CategoryNode,
    CategoryResult,
    ExtractionSource,
    Product,
    ProductVariant,
    ScrapeRequest,
)
from ecommerce_scraper.sampling import deterministic_sample
from ecommerce_scraper.security import canonicalize_url


def _text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = BeautifulSoup(unescape(value), "lxml").get_text(" ", strip=True)
    return cleaned or None


class ShopifyAdapter(Adapter):
    async def _collections(self, base_url: str) -> list[dict[str, Any]]:
        collections: list[dict[str, Any]] = []
        for page in range(1, 1_001):
            payload, _ = await self.client.get_json(
                urljoin(base_url, f"/collections.json?page={page}&limit=250")
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("collections"), list):
                raise ValueError("Shopify collections endpoint returned an unexpected payload")
            batch = [item for item in payload["collections"] if isinstance(item, dict)]
            if not batch:
                break
            collections.extend(batch)
            if len(batch) < 250:
                break
        return collections

    async def _products(self, base_url: str, handle: str) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        for page in range(1, 1_001):
            endpoint = f"/collections/{handle}/products.json?page={page}&limit=250"
            payload, _ = await self.client.get_json(urljoin(base_url, endpoint))
            if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
                raise ValueError(f"Shopify collection {handle!r} returned an unexpected payload")
            batch = [item for item in payload["products"] if isinstance(item, dict)]
            if not batch:
                break
            products.extend(batch)
            if len(batch) < 250:
                break
        return products

    @staticmethod
    def _navigation_paths(navigation: list[CategoryNode]) -> dict[str, list[str]]:
        return {
            urlsplit(node.url).path.rstrip("/"): node.path
            for node in flatten_categories(navigation)
            if node.url
        }

    @staticmethod
    def _normalize_product(
        raw: dict[str, Any], base_url: str, handle: str, category_path: list[str]
    ) -> Product:
        variants = [item for item in raw.get("variants", []) if isinstance(item, dict)]
        images = [
            str(item.get("src"))
            for item in raw.get("images", [])
            if isinstance(item, dict) and item.get("src")
        ]
        normalized_variants = [
            ProductVariant(
                id=str(item.get("id") or "") or None,
                title=str(item.get("title") or "") or None,
                sku=str(item.get("sku") or "") or None,
                price=str(item.get("price") or "") or None,
                compare_at_price=str(item.get("compare_at_price") or "") or None,
                available=bool(item.get("available")) if item.get("available") is not None else None,
                barcode=str(item.get("barcode") or "") or None,
                options={
                    f"option{index}": str(item.get(f"option{index}"))
                    for index in range(1, 4)
                    if item.get(f"option{index}") is not None
                },
            )
            for item in variants
        ]
        first = variants[0] if variants else {}
        product_url = urljoin(base_url, f"/products/{raw.get('handle', '')}")
        return Product(
            name=str(raw.get("title") or "Unknown product"),
            url=canonicalize_url(product_url),
            canonical_url=canonicalize_url(product_url),
            category_path=category_path,
            description=_text(raw.get("body_html")),
            description_html=str(raw.get("body_html") or "") or None,
            brand=str(raw.get("vendor") or "") or None,
            sku=str(first.get("sku") or "") or None,
            gtin=str(first.get("barcode") or "") or None,
            price=str(first.get("price") or "") or None,
            compare_at_price=str(first.get("compare_at_price") or "") or None,
            availability="InStock" if any(item.get("available") for item in variants) else "OutOfStock",
            images=list(dict.fromkeys(images)),
            attributes={
                option.get("name", f"option_{index}"): option.get("values", [])
                for index, option in enumerate(raw.get("options", []), start=1)
                if isinstance(option, dict)
            },
            variants=normalized_variants,
            sources=[ExtractionSource.PLATFORM_API],
            confidence=0.96,
        )

    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        collections = await self._collections(base_url)
        paths = self._navigation_paths(navigation)
        results: list[CategoryResult] = []
        warnings: list[str] = []
        for collection in collections[: request.max_categories]:
            handle = str(collection.get("handle") or "").strip()
            title = str(collection.get("title") or handle.replace("-", " ").title()).strip()
            if not handle:
                continue
            collection_path = f"/collections/{handle}"
            category_path = paths.get(collection_path, [title])
            try:
                products = await self._products(base_url, handle)
            except TargetAccessError:
                raise
            except Exception as exc:
                warnings.append(f"Collection {title!r} could not be read: {exc}")
                continue
            sampled = deterministic_sample(
                products,
                request.products_per_category,
                f"{request.seed or base_url}:{collection_path}",
            )
            normalized = [
                self._normalize_product(item, base_url, handle, category_path) for item in sampled
            ]
            results.append(
                CategoryResult(
                    name=title,
                    url=canonicalize_url(urljoin(base_url, collection_path)),
                    path=category_path,
                    discovered_product_count=len(products),
                    products=normalized,
                )
            )
        if len(collections) > request.max_categories:
            warnings.append(
                f"Category processing stopped at the configured limit of {request.max_categories}"
            )
        return results, warnings
