from __future__ import annotations

from html import unescape
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ecommerce_scraper.adapters.base import Adapter
from ecommerce_scraper.http_client import TargetAccessError
from ecommerce_scraper.models import (
    CategoryNode,
    CategoryResult,
    ExtractionSource,
    Product,
    ScrapeRequest,
)
from ecommerce_scraper.sampling import deterministic_sample
from ecommerce_scraper.security import canonicalize_url


def _clean_html(value: Any) -> str | None:
    if not value:
        return None
    return BeautifulSoup(unescape(str(value)), "lxml").get_text(" ", strip=True) or None


def _minor_price(value: Any, minor_unit: int) -> str | None:
    if value in {None, ""}:
        return None
    raw = str(value)
    try:
        number = int(raw)
    except ValueError:
        return raw
    if minor_unit <= 0:
        return str(number)
    return f"{number / (10**minor_unit):.{minor_unit}f}"


class WooCommerceAdapter(Adapter):
    async def _paged(self, base_url: str, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for page in range(1, 1_001):
            query = urlencode({**params, "page": page, "per_page": 100})
            payload, response = await self.client.get_json(urljoin(base_url, f"{path}?{query}"))
            if not isinstance(payload, list):
                raise ValueError("WooCommerce Store API returned an unexpected payload")
            batch = [item for item in payload if isinstance(item, dict)]
            output.extend(batch)
            total_pages = int(response.headers.get("x-wp-totalpages", "0") or 0)
            if len(batch) < 100 or (total_pages and page >= total_pages):
                break
        return output

    @staticmethod
    def _category_paths(categories: list[dict[str, Any]]) -> dict[int, list[str]]:
        by_id = {int(item["id"]): item for item in categories if item.get("id") is not None}
        paths: dict[int, list[str]] = {}
        for category_id, category in by_id.items():
            path: list[str] = []
            cursor: dict[str, Any] | None = category
            visited: set[int] = set()
            while cursor and int(cursor.get("id", 0)) not in visited:
                visited.add(int(cursor.get("id", 0)))
                path.append(str(cursor.get("name") or cursor.get("slug") or "Category"))
                parent = int(cursor.get("parent", 0) or 0)
                cursor = by_id.get(parent)
            paths[category_id] = list(reversed(path))
        return paths

    @staticmethod
    def _normalize_product(raw: dict[str, Any], path: list[str], base_url: str) -> Product:
        prices_value = raw.get("prices")
        prices: dict[str, Any] = prices_value if isinstance(prices_value, dict) else {}
        minor = int(prices.get("currency_minor_unit", 2) or 0)
        images = [
            str(item.get("src"))
            for item in raw.get("images", [])
            if isinstance(item, dict) and item.get("src")
        ]
        attributes = {
            str(item.get("name") or item.get("taxonomy") or "attribute"): item.get("terms", [])
            for item in raw.get("attributes", [])
            if isinstance(item, dict)
        }
        url = str(
            raw.get("permalink")
            or urljoin(base_url, f"/product/{raw.get('slug') or raw.get('id') or ''}")
        )
        return Product(
            name=str(raw.get("name") or "Unknown product"),
            url=canonicalize_url(url),
            canonical_url=canonicalize_url(url),
            category_path=path,
            description=_clean_html(raw.get("description") or raw.get("short_description")),
            description_html=str(raw.get("description") or "") or None,
            sku=str(raw.get("sku") or "") or None,
            price=_minor_price(prices.get("price"), minor),
            compare_at_price=_minor_price(prices.get("regular_price"), minor),
            currency=str(prices.get("currency_code") or "") or None,
            availability="InStock" if raw.get("is_in_stock") else "OutOfStock",
            images=list(dict.fromkeys(images)),
            attributes=attributes,
            sources=[ExtractionSource.PLATFORM_API],
            confidence=0.93,
        )

    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        del navigation
        categories = await self._paged(base_url, "/wp-json/wc/store/v1/products/categories", {})
        paths = self._category_paths(categories)
        results: list[CategoryResult] = []
        warnings: list[str] = []
        for category in categories[: request.max_categories]:
            category_id = int(category.get("id", 0) or 0)
            if not category_id:
                continue
            name = str(category.get("name") or category.get("slug") or f"Category {category_id}")
            try:
                products = await self._paged(
                    base_url, "/wp-json/wc/store/v1/products", {"category": category_id}
                )
            except TargetAccessError:
                raise
            except Exception as exc:
                warnings.append(f"Category {name!r} could not be read: {exc}")
                continue
            sampled = deterministic_sample(
                products,
                request.products_per_category,
                f"{request.seed or base_url}:woocommerce:{category_id}",
            )
            results.append(
                CategoryResult(
                    name=name,
                    url=canonicalize_url(urljoin(base_url, f"/product-category/{category.get('slug', '')}")),
                    path=paths.get(category_id, [name]),
                    discovered_product_count=len(products),
                    products=[
                        self._normalize_product(item, paths.get(category_id, [name]), base_url)
                        for item in sampled
                    ],
                )
            )
        if len(categories) > request.max_categories:
            warnings.append(
                f"Category processing stopped at the configured limit of {request.max_categories}"
            )
        return results, warnings
