from __future__ import annotations

import asyncio
from collections import deque
from urllib.parse import urlsplit

from ecommerce_scraper.adapters.base import Adapter
from ecommerce_scraper.discovery import (
    SitemapInventory,
    discover_sitemaps,
    extract_pagination_links,
    extract_product_links,
    flatten_categories,
)
from ecommerce_scraper.extractors import extract_product
from ecommerce_scraper.http_client import RobotsDeniedError, TargetAccessError
from ecommerce_scraper.models import (
    CategoryNode,
    CategoryResult,
    CategoryVisibility,
    ExtractionStrategy,
    Product,
    ScrapeRequest,
)
from ecommerce_scraper.sampling import deterministic_sample
from ecommerce_scraper.security import canonicalize_url


class GenericAdapter(Adapter):
    strategy = ExtractionStrategy.GENERIC_HTML

    async def _category_products(
        self, category_url: str, max_pages: int
    ) -> tuple[list[str], list[str]]:
        queue: deque[str] = deque([category_url])
        visited: set[str] = set()
        product_urls: set[str] = set()
        warnings: list[str] = []
        while queue and len(visited) < max_pages:
            page_url = canonicalize_url(queue.popleft())
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                response = await self.client.get(page_url)
                response.raise_for_status()
            except RobotsDeniedError as exc:
                warnings.append(str(exc))
                continue
            except TargetAccessError:
                raise
            except Exception as exc:
                warnings.append(f"Category page could not be read ({page_url}): {exc}")
                continue
            product_urls.update(extract_product_links(response.text, page_url))
            for next_url in extract_pagination_links(response.text, page_url):
                if next_url not in visited:
                    queue.append(next_url)
        if queue:
            warnings.append(f"Pagination stopped after {max_pages} pages")
        return sorted(product_urls), warnings

    async def _extract_many(self, urls: list[str], path: list[str]) -> tuple[list[Product], list[str]]:
        warnings: list[str] = []

        async def one(url: str) -> Product | None:
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                return extract_product(response.text, str(response.url), path)
            except RobotsDeniedError as exc:
                warnings.append(str(exc))
                return None
            except TargetAccessError:
                raise
            except Exception as exc:
                warnings.append(f"Product could not be extracted ({url}): {exc}")
                return None

        products = await asyncio.gather(*(one(url) for url in urls))
        return [product for product in products if product is not None], warnings

    @staticmethod
    def _synthetic_categories(inventory: SitemapInventory) -> list[CategoryNode]:
        nodes: list[CategoryNode] = []
        for url in inventory.category_urls:
            slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            name = slug.replace("-", " ").replace("_", " ").title() or "Category"
            nodes.append(CategoryNode(name=name, url=url, path=[name]))
        return nodes

    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        inventory = await discover_sitemaps(self.client, base_url, request.max_sitemap_urls)
        warnings = list(inventory.warnings)
        categories = [node for node in flatten_categories(navigation) if node.url]
        if not categories:
            categories = self._synthetic_categories(inventory)
        categories = categories[: request.max_categories]

        results: list[CategoryResult] = []
        for category in categories:
            assert category.url is not None
            product_urls, category_warnings = await self._category_products(
                category.url, request.max_pages_per_category
            )
            # If the category page yielded nothing, cautiously use sitemap URLs sharing its final slug.
            if not product_urls:
                slug = urlsplit(category.url).path.rstrip("/").rsplit("/", 1)[-1].lower()
                product_urls = [url for url in inventory.product_urls if slug and slug in url.lower()]
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
                    visibility=CategoryVisibility.NAVIGATION,
                    discovered_product_count=len(product_urls),
                    products=products,
                    warnings=[*category_warnings, *product_warnings],
                )
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
            warnings.append("No discoverable categories or product URLs were found")
        return results, warnings
