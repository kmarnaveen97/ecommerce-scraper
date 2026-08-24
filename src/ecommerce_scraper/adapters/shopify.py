from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ecommerce_scraper.adapters.base import Adapter
from ecommerce_scraper.adapters.generic import GenericAdapter
from ecommerce_scraper.discovery import SitemapInventory, discover_sitemaps, flatten_categories
from ecommerce_scraper.http_client import SafeHttpClient, TargetAccessError
from ecommerce_scraper.models import (
    CategoryNode,
    CategoryResult,
    CategoryVisibility,
    ExtractionSource,
    ExtractionStrategy,
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


def _collection_path(url: str) -> str:
    return urlsplit(canonicalize_url(url)).path.rstrip("/")


def _collection_name(url: str) -> str:
    slug = _collection_path(url).rsplit("/", 1)[-1]
    return slug.replace("-", " ").replace("_", " ").title() or "Collection"


def _collection_payload(payload: object) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("collections"), list):
        return None
    return [item for item in payload["collections"] if isinstance(item, dict)]


def _product_payload(payload: object) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        return None
    return [item for item in payload["products"] if isinstance(item, dict)]


@dataclass(frozen=True)
class ShopifyProbe:
    collections_json: bool = False
    products_json: bool = False
    initial_collections: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def json_available(self) -> bool:
        return self.collections_json and self.products_json


async def probe_shopify(client: SafeHttpClient, base_url: str) -> ShopifyProbe:
    """Check public Shopify catalogue capabilities without assuming the homepage theme."""
    warnings: list[str] = []
    collections_url = urljoin(base_url, "/collections.json?page=1&limit=250")
    try:
        response = await client.get(collections_url)
        if not response.is_success:
            return ShopifyProbe(
                warnings=(f"Shopify collections capability returned HTTP {response.status_code}",)
            )
        collections = _collection_payload(response.json())
    except TargetAccessError as exc:
        return ShopifyProbe(warnings=(f"Shopify JSON capability is unavailable: {exc}",))
    except Exception as exc:
        return ShopifyProbe(warnings=(f"Shopify collections JSON is unavailable: {exc}",))

    if collections is None:
        return ShopifyProbe(warnings=("Shopify collections URL did not return Shopify JSON",))

    handle = next(
        (str(item.get("handle") or "").strip() for item in collections if item.get("handle")),
        "",
    )
    if not handle:
        return ShopifyProbe(
            collections_json=True,
            products_json=True,
            initial_collections=tuple(collections),
            warnings=("Shopify collections JSON is available but currently contains no collections",),
        )

    products_url = urljoin(base_url, f"/collections/{handle}/products.json?page=1&limit=1")
    try:
        response = await client.get(products_url)
        if not response.is_success:
            warnings.append(f"Shopify collection-products capability returned HTTP {response.status_code}")
            products_available = False
        else:
            products_available = _product_payload(response.json()) is not None
            if not products_available:
                warnings.append("Shopify collection-products URL did not return Shopify JSON")
    except TargetAccessError as exc:
        products_available = False
        warnings.append(f"Shopify collection-products capability is unavailable: {exc}")
    except Exception as exc:
        products_available = False
        warnings.append(f"Shopify collection-products JSON is unavailable: {exc}")

    return ShopifyProbe(
        collections_json=True,
        products_json=products_available,
        initial_collections=tuple(collections),
        warnings=tuple(warnings),
    )


class ShopifyJsonAdapter(Adapter):
    strategy = ExtractionStrategy.SHOPIFY_JSON

    def __init__(
        self,
        client: SafeHttpClient,
        *,
        initial_collections: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(client)
        self.initial_collections = initial_collections

    async def _collections(self, base_url: str) -> list[dict[str, Any]]:
        collections = list(self.initial_collections)
        if self.initial_collections and len(self.initial_collections) < 250:
            return collections
        first_page = 2 if self.initial_collections else 1
        for page in range(first_page, 1_001):
            payload, _ = await self.client.get_json(
                urljoin(base_url, f"/collections.json?page={page}&limit=250")
            )
            batch = _collection_payload(payload)
            if batch is None:
                raise ValueError("Shopify collections endpoint returned an unexpected payload")
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
            batch = _product_payload(payload)
            if batch is None:
                raise ValueError(f"Shopify collection {handle!r} returned an unexpected payload")
            if not batch:
                break
            products.extend(batch)
            if len(batch) < 250:
                break
        return products

    @staticmethod
    def _navigation_paths(navigation: list[CategoryNode]) -> dict[str, list[str]]:
        return {
            _collection_path(node.url): node.path
            for node in flatten_categories(navigation)
            if node.url and _collection_path(node.url).startswith("/collections/")
        }

    @staticmethod
    def _visibility(
        collection_path: str,
        navigation_paths: dict[str, list[str]],
        sitemap_paths: set[str],
    ) -> CategoryVisibility:
        if collection_path in navigation_paths:
            return CategoryVisibility.NAVIGATION
        if collection_path in sitemap_paths:
            return CategoryVisibility.SITEMAP
        return CategoryVisibility.API_ONLY

    @staticmethod
    def _normalize_product(raw: dict[str, Any], base_url: str, category_path: list[str]) -> Product:
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
        inventory = await discover_sitemaps(self.client, base_url, request.max_sitemap_urls)
        warnings = list(inventory.warnings)
        navigation_paths = self._navigation_paths(navigation)
        sitemap_paths = {
            _collection_path(url)
            for url in inventory.category_urls
            if _collection_path(url).startswith("/collections/")
        }
        visibility_evidence = bool(navigation_paths or sitemap_paths)
        candidates: list[tuple[dict[str, Any], CategoryVisibility]] = []
        skipped_api_only = 0
        seen_handles: set[str] = set()

        for collection in collections:
            handle = str(collection.get("handle") or "").strip()
            if not handle or handle in seen_handles:
                continue
            seen_handles.add(handle)
            path = f"/collections/{handle}"
            visibility = self._visibility(path, navigation_paths, sitemap_paths)
            should_skip = (
                visibility == CategoryVisibility.API_ONLY
                and visibility_evidence
                and not request.include_api_only_collections
            )
            if should_skip:
                skipped_api_only += 1
                continue
            candidates.append((collection, visibility))

        if not visibility_evidence and collections:
            warnings.append(
                "Navigation and collection sitemap visibility could not be verified; "
                "public API collections were retained and labelled api_only"
            )
        if skipped_api_only:
            warnings.append(
                f"Skipped {skipped_api_only} API-only Shopify collections; set "
                "include_api_only_collections=true to include them"
            )

        results: list[CategoryResult] = []
        for collection, visibility in candidates[: request.max_categories]:
            handle = str(collection.get("handle") or "").strip()
            title = str(collection.get("title") or handle.replace("-", " ").title()).strip()
            collection_path = f"/collections/{handle}"
            category_path = navigation_paths.get(collection_path, [title])
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
            normalized = [self._normalize_product(item, base_url, category_path) for item in sampled]
            results.append(
                CategoryResult(
                    name=title,
                    url=canonicalize_url(urljoin(base_url, collection_path)),
                    path=category_path,
                    visibility=visibility,
                    discovered_product_count=len(products),
                    products=normalized,
                )
            )
        if len(candidates) > request.max_categories:
            warnings.append(
                f"Category processing stopped at the configured limit of {request.max_categories}"
            )
        return results, warnings


class ShopifySitemapAdapter(GenericAdapter):
    strategy = ExtractionStrategy.SHOPIFY_SITEMAP

    @staticmethod
    def _categories(
        navigation: list[CategoryNode], inventory: SitemapInventory
    ) -> list[tuple[CategoryNode, CategoryVisibility]]:
        categories: list[tuple[CategoryNode, CategoryVisibility]] = []
        seen: set[str] = set()
        for node in flatten_categories(navigation):
            if not node.url or not _collection_path(node.url).startswith("/collections/"):
                continue
            url = canonicalize_url(node.url)
            if url not in seen:
                seen.add(url)
                categories.append((node, CategoryVisibility.NAVIGATION))
        for url in inventory.category_urls:
            if not _collection_path(url).startswith("/collections/"):
                continue
            canonical = canonicalize_url(url)
            if canonical in seen:
                continue
            seen.add(canonical)
            name = _collection_name(url)
            categories.append(
                (
                    CategoryNode(name=name, url=canonical, path=[name]),
                    CategoryVisibility.SITEMAP,
                )
            )
        return categories

    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        inventory = await discover_sitemaps(self.client, base_url, request.max_sitemap_urls)
        warnings = list(inventory.warnings)
        candidates = self._categories(navigation, inventory)
        results: list[CategoryResult] = []

        for category, visibility in candidates[: request.max_categories]:
            assert category.url is not None
            product_urls, category_warnings = await self._category_products(
                category.url, request.max_pages_per_category
            )
            if not product_urls:
                category_warnings.append(
                    "No product links were present in the public collection HTML; "
                    "the page may require a store-specific JavaScript renderer"
                )
            sampled = deterministic_sample(
                product_urls,
                request.products_per_category,
                f"{request.seed or base_url}:{category.url}",
            )
            products, product_warnings = await self._extract_many(sampled, category.path)
            results.append(
                CategoryResult(
                    name=category.name,
                    url=category.url,
                    path=category.path,
                    visibility=visibility,
                    discovered_product_count=len(product_urls),
                    products=products,
                    warnings=[*category_warnings, *product_warnings],
                )
            )

        if len(candidates) > request.max_categories:
            warnings.append(
                f"Category processing stopped at the configured limit of {request.max_categories}"
            )
        if not results and inventory.product_urls:
            sampled = deterministic_sample(
                inventory.product_urls,
                request.products_per_category,
                f"{request.seed or base_url}:all-products",
            )
            products, product_warnings = await self._extract_many(sampled, ["All Products"])
            results.append(
                CategoryResult(
                    name="All Products",
                    path=["All Products"],
                    visibility=CategoryVisibility.SYNTHETIC,
                    discovered_product_count=len(inventory.product_urls),
                    products=products,
                    warnings=product_warnings,
                )
            )
        if not results:
            warnings.append("No public Shopify collection or product URLs were discovered")
        return results, warnings


class ShopifyAdapter(Adapter):
    """Capability router for public Shopify JSON and sitemap/HTML extraction."""

    strategy = ExtractionStrategy.SHOPIFY_SITEMAP

    def __init__(self, client: SafeHttpClient, probe: ShopifyProbe | None = None) -> None:
        super().__init__(client)
        self.probe = probe

    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        probe = self.probe or await probe_shopify(self.client, base_url)
        probe_warnings = list(probe.warnings)
        if probe.json_available:
            adapter: Adapter = ShopifyJsonAdapter(
                self.client,
                initial_collections=probe.initial_collections,
            )
            try:
                categories, warnings = await adapter.scrape(base_url, request, navigation)
            except TargetAccessError:
                raise
            except Exception as exc:
                probe_warnings.append(
                    f"Shopify JSON extraction failed ({exc}); sitemap/HTML fallback was used"
                )
            else:
                self.strategy = adapter.strategy
                return categories, [*probe_warnings, *warnings]

        adapter = ShopifySitemapAdapter(self.client)
        categories, warnings = await adapter.scrape(base_url, request, navigation)
        self.strategy = adapter.strategy
        if not probe.json_available:
            probe_warnings.append(
                "Public Shopify JSON was unavailable; sitemap and structured product data were used"
            )
        return categories, [*probe_warnings, *warnings]
