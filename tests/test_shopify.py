from __future__ import annotations

import httpx

from ecommerce_scraper.adapters.shopify import ShopifyAdapter, probe_shopify
from ecommerce_scraper.config import Settings
from ecommerce_scraper.http_client import SafeHttpClient
from ecommerce_scraper.models import (
    CategoryNode,
    CategoryVisibility,
    ExtractionStrategy,
    ScrapeRequest,
)


def _settings() -> Settings:
    return Settings(
        robots_obey=False,
        target_requests_per_second=0,
        target_jitter_seconds=0,
        target_backoff_base_seconds=0,
        target_backoff_cap_seconds=0,
    )


def _product(handle: str, title: str) -> dict[str, object]:
    return {
        "handle": handle,
        "title": title,
        "vendor": "Example",
        "body_html": f"<p>{title} description</p>",
        "variants": [
            {
                "id": 1,
                "title": "Default",
                "sku": handle.upper(),
                "price": "99.00",
                "available": True,
            }
        ],
        "images": [{"src": f"https://cdn.example/{handle}.jpg"}],
        "options": [],
    }


async def test_shopify_json_filters_api_only_collections_and_labels_visibility() -> None:
    collections = [
        {"handle": "rings", "title": "Rings"},
        {"handle": "clearance", "title": "Clearance"},
        {"handle": "internal-sale-oct", "title": "Internal Sale"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/collections.json":
            return httpx.Response(200, json={"collections": collections}, request=request)
        if path == "/collections/rings/products.json":
            return httpx.Response(
                200,
                json={"products": [_product("ring-one", "Ring One")]},
                request=request,
            )
        if path == "/collections/clearance/products.json":
            return httpx.Response(
                200,
                json={"products": [_product("sale-ring", "Sale Ring")]},
                request=request,
            )
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    "<url><loc>https://shop.example/collections/rings</loc></url>"
                    "<url><loc>https://shop.example/collections/clearance</loc></url>"
                    "</urlset>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    navigation = [
        CategoryNode(
            name="Jewellery",
            path=["Jewellery"],
            children=[
                CategoryNode(
                    name="Rings",
                    url="https://shop.example/collections/rings",
                    path=["Jewellery", "Rings"],
                )
            ],
        )
    ]
    transport = httpx.MockTransport(handler)
    async with SafeHttpClient(_settings(), transport=transport, resolve_dns=False) as client:
        probe = await probe_shopify(client, "https://shop.example")
        adapter = ShopifyAdapter(client, probe)
        results, warnings = await adapter.scrape(
            "https://shop.example",
            ScrapeRequest(url="https://shop.example", products_per_category=1),
            navigation,
        )

    assert probe.json_available is True
    assert adapter.strategy == ExtractionStrategy.SHOPIFY_JSON
    assert [result.name for result in results] == ["Rings", "Clearance"]
    assert [result.visibility for result in results] == [
        CategoryVisibility.NAVIGATION,
        CategoryVisibility.SITEMAP,
    ]
    assert results[0].path == ["Jewellery", "Rings"]
    assert results[0].products[0].name == "Ring One"
    assert any("Skipped 1 API-only" in warning for warning in warnings)


async def test_shopify_uses_sitemap_and_json_ld_when_public_json_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/collections.json":
            return httpx.Response(200, text="<html>not JSON</html>", request=request)
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    "<url><loc>https://shop.example/collections/rings</loc></url>"
                    "<url><loc>https://shop.example/products/ring-one</loc></url>"
                    "</urlset>"
                ),
                request=request,
            )
        if path == "/collections/rings":
            return httpx.Response(
                200,
                text=(
                    "<html><div class='product-card'><a href='/products/ring-one'>Ring One</a></div></html>"
                ),
                request=request,
            )
        if path == "/products/ring-one":
            return httpx.Response(
                200,
                text="""
                <html><head><script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "Product",
                  "name": "Ring One",
                  "offers": {
                    "@type": "Offer",
                    "price": "99.00",
                    "priceCurrency": "INR",
                    "availability": "https://schema.org/InStock"
                  }
                }
                </script></head><body><h1>Ring One</h1></body></html>
                """,
                request=request,
            )
        return httpx.Response(404, request=request)

    navigation = [
        CategoryNode(
            name="Rings",
            url="https://shop.example/collections/rings",
            path=["Jewellery", "Rings"],
        )
    ]
    transport = httpx.MockTransport(handler)
    async with SafeHttpClient(_settings(), transport=transport, resolve_dns=False) as client:
        probe = await probe_shopify(client, "https://shop.example")
        adapter = ShopifyAdapter(client, probe)
        results, warnings = await adapter.scrape(
            "https://shop.example",
            ScrapeRequest(url="https://shop.example", products_per_category=1),
            navigation,
        )

    assert probe.json_available is False
    assert adapter.strategy == ExtractionStrategy.SHOPIFY_SITEMAP
    assert len(results) == 1
    assert results[0].visibility == CategoryVisibility.NAVIGATION
    assert results[0].products[0].name == "Ring One"
    assert results[0].products[0].price == "99.00"
    assert any("sitemap and structured product data" in warning for warning in warnings)
