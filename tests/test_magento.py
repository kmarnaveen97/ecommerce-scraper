from __future__ import annotations

import httpx

from ecommerce_scraper.adapters.magento import MagentoGraphQLAdapter, probe_magento
from ecommerce_scraper.config import Settings
from ecommerce_scraper.http_client import SafeHttpClient
from ecommerce_scraper.models import (
    CategoryVisibility,
    ExtractionStrategy,
    Platform,
    ScrapeRequest,
)
from ecommerce_scraper.platforms import detect_platform


def _settings() -> Settings:
    return Settings(
        robots_obey=False,
        target_requests_per_second=0,
        target_jitter_seconds=0,
        target_backoff_base_seconds=0,
        target_backoff_cap_seconds=0,
    )


def _handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("query", "")
    if "StoreCapability" in query:
        return httpx.Response(
            200,
            json={
                "data": {
                    "storeConfig": {
                        "base_url": "https://shop.example/",
                        "store_name": "Example Magento Store",
                        "root_category_id": 2,
                    }
                }
            },
            request=request,
        )
    if "CategoryTree" in query:
        return httpx.Response(
            200,
            json={
                "data": {
                    "categoryList": [
                        {
                            "id": 2,
                            "uid": "Mg==",
                            "name": "Default Category",
                            "url_path": "",
                            "url_suffix": "",
                            "include_in_menu": 0,
                            "product_count": 0,
                            "children": [
                                {
                                    "id": 3,
                                    "uid": "Mw==",
                                    "name": "Men",
                                    "url_path": "men",
                                    "url_suffix": ".html",
                                    "include_in_menu": 1,
                                    "product_count": 2,
                                    "children": [],
                                }
                            ],
                        }
                    ]
                }
            },
            request=request,
        )
    if "ProductDetails" in query:
        return httpx.Response(
            200,
            json={
                "data": {
                    "products": {
                        "items": [
                            {
                                "uid": "shoe-one",
                                "sku": "SHOE-1",
                                "name": "Running Shoe",
                                "url_key": "running-shoe",
                                "url_suffix": ".html",
                                "description": {"html": "<p>A cushioned running shoe.</p>"},
                                "short_description": {"html": "<p>Running shoe.</p>"},
                                "stock_status": "IN_STOCK",
                                "image": {
                                    "url": "https://shop.example/media/catalog/product/shoe.jpg",
                                    "label": "Running Shoe",
                                },
                                "media_gallery": [],
                                "price_range": {
                                    "minimum_price": {
                                        "regular_price": {"value": 13999, "currency": "INR"},
                                        "final_price": {"value": 11199, "currency": "INR"},
                                        "discount": {"amount_off": 2800, "percent_off": 20},
                                    }
                                },
                                "configurable_options": [
                                    {
                                        "label": "Size",
                                        "attribute_code": "size",
                                        "values": [
                                            {"uid": "size-8", "label": "8", "value_index": 8}
                                        ],
                                    }
                                ],
                                "variants": [
                                    {
                                        "attributes": [
                                            {
                                                "code": "size",
                                                "value_index": 8,
                                                "label": "8",
                                                "uid": "size-8",
                                            }
                                        ],
                                        "product": {
                                            "uid": "shoe-one-8",
                                            "sku": "SHOE-1-8",
                                            "name": "Running Shoe, Size 8",
                                            "stock_status": "IN_STOCK",
                                            "price_range": {
                                                "minimum_price": {
                                                    "regular_price": {
                                                        "value": 13999,
                                                        "currency": "INR",
                                                    },
                                                    "final_price": {
                                                        "value": 11199,
                                                        "currency": "INR",
                                                    },
                                                }
                                            },
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
            request=request,
        )
    if "CategoryProducts" in query:
        return httpx.Response(
            200,
            json={
                "data": {
                    "products": {
                        "total_count": 2,
                        "page_info": {"total_pages": 1},
                        "items": [
                            {
                                "uid": "shoe-one",
                                "sku": "SHOE-1",
                                "name": "Running Shoe",
                                "url_key": "running-shoe",
                                "url_suffix": ".html",
                            },
                        ],
                    }
                }
            },
            request=request,
        )
    return httpx.Response(404, request=request)


async def test_magento_graphql_discovers_categories_and_normalizes_products() -> None:
    async with SafeHttpClient(
        _settings(), transport=httpx.MockTransport(_handler), resolve_dns=False
    ) as client:
        probe = await probe_magento(client, "https://shop.example")
        adapter = MagentoGraphQLAdapter(client, probe)
        results, warnings = await adapter.scrape(
            "https://shop.example",
            ScrapeRequest(
                url="https://shop.example",
                products_per_category=1,
                max_categories=1,
            ),
            [],
        )

    assert probe.graphql_available is True
    assert warnings == []
    assert adapter.strategy == ExtractionStrategy.MAGENTO_GRAPHQL
    assert adapter.category_tree[0].name == "Men"
    assert results[0].visibility == CategoryVisibility.PLATFORM_API
    assert results[0].discovered_product_count == 2
    product = results[0].products[0]
    assert product.name == "Running Shoe"
    assert product.url == "https://shop.example/running-shoe.html"
    assert product.price == "11199"
    assert product.compare_at_price == "13999"
    assert product.currency == "INR"
    assert product.category_path == ["Men"]
    assert product.variants[0].options == {"size": "8"}


def test_detects_headless_magento_venia_shell() -> None:
    request = httpx.Request("GET", "https://shop.example")
    response = httpx.Response(
        200,
        text='<script src="/RootCmp_CMS_PAGE__default.js"></script><div id="venia"></div>',
        request=request,
    )

    detection = detect_platform(response)

    assert detection.platform == Platform.MAGENTO
    assert "rootcmp_" in detection.signals
    assert "venia" in detection.signals
