from __future__ import annotations

import json
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ecommerce_scraper.adapters.base import Adapter
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


class MagentoGraphQLError(RuntimeError):
    pass


@dataclass(frozen=True)
class MagentoProbe:
    graphql_available: bool = False
    store_name: str | None = None
    root_category_id: str = "2"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _MagentoCategory:
    category_id: str
    uid: str
    name: str
    url: str
    path: list[str]
    visible: bool
    product_count: int
    children: tuple[_MagentoCategory, ...] = ()


def _graphql_url(base_url: str, query: str) -> str:
    return f"{urljoin(base_url, '/graphql')}?{urlencode({'query': query})}"


def _error_message(payload: dict[str, Any]) -> str | None:
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    messages = [
        str(item.get("message") or "GraphQL error")
        for item in errors[:3]
        if isinstance(item, dict)
    ]
    return "; ".join(messages) or "Magento GraphQL returned an error"


async def _graphql(client: SafeHttpClient, base_url: str, query: str) -> dict[str, Any]:
    payload, _ = await client.get_json(_graphql_url(base_url, query))
    if not isinstance(payload, dict):
        raise MagentoGraphQLError("Magento GraphQL returned an unexpected payload")
    message = _error_message(payload)
    if message:
        raise MagentoGraphQLError(message)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MagentoGraphQLError("Magento GraphQL response did not contain data")
    return data


async def probe_magento(client: SafeHttpClient, base_url: str) -> MagentoProbe:
    query = "query StoreCapability { storeConfig { base_url store_name root_category_id } }"
    try:
        data = await _graphql(client, base_url, query)
    except TargetAccessError:
        raise
    except Exception as exc:
        return MagentoProbe(warnings=(f"Magento GraphQL capability is unavailable: {exc}",))
    config = data.get("storeConfig")
    if not isinstance(config, dict) or not config.get("base_url"):
        return MagentoProbe(warnings=("GraphQL endpoint is not a public Magento catalogue",))
    return MagentoProbe(
        graphql_available=True,
        store_name=str(config.get("store_name") or "").strip() or None,
        root_category_id=str(config.get("root_category_id") or "2"),
    )


def _category_fields(depth: int) -> str:
    fields = "id uid name url_path url_suffix include_in_menu product_count"
    for _ in range(depth):
        fields = f"id uid name url_path url_suffix include_in_menu product_count children {{ {fields} }}"
    return fields


_PRODUCT_FIELDS = """
uid
sku
name
url_key
url_suffix
description { html }
short_description { html }
stock_status
image { url label }
media_gallery { url label position disabled }
price_range {
  minimum_price {
    regular_price { value currency }
    final_price { value currency }
    discount { amount_off percent_off }
  }
}
... on ConfigurableProduct {
  configurable_options {
    label
    attribute_code
    values { uid label value_index }
  }
  variants {
    attributes { code value_index label uid }
    product {
      uid
      sku
      name
      stock_status
      price_range {
        minimum_price {
          regular_price { value currency }
          final_price { value currency }
        }
      }
    }
  }
}
"""


def _clean_html(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict) or not value.get("html"):
        return None, None
    html = str(value["html"])
    text = BeautifulSoup(unescape(html), "lxml").get_text(" ", strip=True)
    return text or None, html


def _price(raw: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    price_range = raw.get("price_range")
    if not isinstance(price_range, dict):
        return None, None, None
    minimum = price_range.get("minimum_price")
    if not isinstance(minimum, dict):
        return None, None, None
    regular = minimum.get("regular_price")
    final = minimum.get("final_price")
    regular_data = regular if isinstance(regular, dict) else {}
    final_data = final if isinstance(final, dict) else {}
    regular_value = regular_data.get("value")
    final_value = final_data.get("value")
    currency = final_data.get("currency") or regular_data.get("currency")
    final_text = str(final_value) if final_value is not None else None
    regular_text = str(regular_value) if regular_value is not None else None
    compare_at = regular_text if regular_text and regular_text != final_text else None
    return final_text or regular_text, compare_at, str(currency) if currency else None


def _variant(raw: dict[str, Any]) -> ProductVariant | None:
    product = raw.get("product")
    if not isinstance(product, dict):
        return None
    price, compare_at, currency = _price(product)
    attributes = [item for item in raw.get("attributes", []) if isinstance(item, dict)]
    options = {
        str(item.get("code") or "option"): str(item.get("label") or item.get("value_index") or "")
        for item in attributes
        if item.get("label") is not None or item.get("value_index") is not None
    }
    return ProductVariant(
        id=str(product.get("uid") or "") or None,
        title=" / ".join(value for value in options.values() if value) or None,
        sku=str(product.get("sku") or "") or None,
        price=price,
        compare_at_price=compare_at,
        currency=currency,
        available=str(product.get("stock_status") or "").upper() == "IN_STOCK",
        options=options,
    )


class MagentoGraphQLAdapter(Adapter):
    strategy = ExtractionStrategy.MAGENTO_GRAPHQL

    def __init__(self, client: SafeHttpClient, probe: MagentoProbe | None = None) -> None:
        super().__init__(client)
        self.probe = probe

    @staticmethod
    def _parse_category(
        raw: dict[str, Any], base_url: str, parent_path: list[str]
    ) -> _MagentoCategory | None:
        name = str(raw.get("name") or "").strip()
        category_id = str(raw.get("id") or "").strip()
        uid = str(raw.get("uid") or "").strip()
        if not name or not (uid or category_id):
            return None
        visible = raw.get("include_in_menu") not in {0, "0"}
        path = [*parent_path, name] if visible else list(parent_path)
        url_path = str(raw.get("url_path") or "").strip("/")
        suffix = str(raw.get("url_suffix") or "")
        category_url = urljoin(base_url, f"/{url_path}{suffix}" if url_path else "/")
        children = tuple(
            child
            for item in raw.get("children", [])
            if isinstance(item, dict)
            for child in [MagentoGraphQLAdapter._parse_category(item, base_url, path)]
            if child is not None
        )
        return _MagentoCategory(
            category_id=category_id,
            uid=uid,
            name=name,
            url=canonicalize_url(category_url),
            path=path,
            visible=visible,
            product_count=int(raw.get("product_count") or 0),
            children=children,
        )

    @staticmethod
    def _nodes(category: _MagentoCategory) -> list[CategoryNode]:
        visible_children = [
            node
            for child in category.children
            for node in MagentoGraphQLAdapter._nodes(child)
        ]
        if not category.visible:
            return visible_children
        return [
            CategoryNode(
                name=category.name,
                url=category.url,
                path=category.path,
                children=visible_children,
            )
        ]

    @staticmethod
    def _flatten(categories: list[_MagentoCategory]) -> list[_MagentoCategory]:
        output: list[_MagentoCategory] = []

        def visit(category: _MagentoCategory) -> None:
            if category.visible:
                output.append(category)
            for child in category.children:
                visit(child)

        for category in categories:
            visit(category)
        return output

    async def _categories(self, base_url: str, root_category_id: str) -> list[_MagentoCategory]:
        query = (
            "query CategoryTree { categoryList(filters: {ids: {eq: "
            f"{json.dumps(root_category_id)}"
            "}}) { "
            f"{_category_fields(6)}"
            " } }"
        )
        data = await _graphql(self.client, base_url, query)
        raw_roots = data.get("categoryList")
        if not isinstance(raw_roots, list):
            raise MagentoGraphQLError("Magento categoryList returned an unexpected payload")
        roots = [item for item in raw_roots if isinstance(item, dict)]
        if len(roots) == 1 and str(roots[0].get("name") or "").lower() == "default category":
            roots = [item for item in roots[0].get("children", []) if isinstance(item, dict)]
        return [
            category
            for item in roots
            for category in [self._parse_category(item, base_url, [])]
            if category is not None
        ]

    async def _basic_products(
        self,
        base_url: str,
        category: _MagentoCategory,
        max_pages: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        products: list[dict[str, Any]] = []
        total_count = 0
        total_pages = 1
        filter_attempts = [
            ("category_uid", category.uid),
            ("category_id", category.category_id),
        ]
        last_error: Exception | None = None
        for filter_name, filter_value in filter_attempts:
            if not filter_value:
                continue
            products = []
            try:
                for page in range(1, max_pages + 1):
                    query = f"""
                    query CategoryProducts {{
                      products(
                        filter: {{{filter_name}: {{eq: {json.dumps(filter_value)}}}}}
                        pageSize: 250
                        currentPage: {page}
                      ) {{
                        total_count
                        page_info {{ total_pages }}
                        items {{ uid sku name url_key url_suffix }}
                      }}
                    }}
                    """
                    data = await _graphql(self.client, base_url, query)
                    payload = data.get("products")
                    if not isinstance(payload, dict):
                        raise MagentoGraphQLError(
                            "Magento products query returned an unexpected payload"
                        )
                    batch = [
                        item for item in payload.get("items", []) if isinstance(item, dict)
                    ]
                    products.extend(batch)
                    total_count = int(payload.get("total_count") or len(products))
                    page_info = payload.get("page_info")
                    if isinstance(page_info, dict):
                        total_pages = int(page_info.get("total_pages") or 1)
                    if page >= total_pages or not batch:
                        break
                return products, total_count, total_pages > max_pages
            except MagentoGraphQLError as exc:
                last_error = exc
                continue
        raise MagentoGraphQLError(
            f"Magento category product filters failed: {last_error or 'no supported filter'}"
        )

    async def _details(
        self, base_url: str, sampled: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        skus = [str(item.get("sku") or "").strip() for item in sampled]
        skus = [sku for sku in skus if sku]
        if not skus:
            return []
        query = f"""
        query ProductDetails {{
          products(filter: {{sku: {{in: {json.dumps(skus)}}}}}, pageSize: {len(skus)}) {{
            items {{ {_PRODUCT_FIELDS} }}
          }}
        }}
        """
        data = await _graphql(self.client, base_url, query)
        payload = data.get("products")
        if not isinstance(payload, dict):
            raise MagentoGraphQLError("Magento product detail query returned an unexpected payload")
        details = [item for item in payload.get("items", []) if isinstance(item, dict)]
        by_sku = {str(item.get("sku") or ""): item for item in details}
        return [by_sku[sku] for sku in skus if sku in by_sku]

    @staticmethod
    def _normalize_product(
        raw: dict[str, Any], base_url: str, category_path: list[str]
    ) -> Product:
        name = str(raw.get("name") or "").strip()
        sku = str(raw.get("sku") or "").strip()
        if not name or not sku:
            raise MagentoGraphQLError("Magento product is missing its name or SKU")
        url_key = str(raw.get("url_key") or "").strip("/")
        url_suffix = str(raw.get("url_suffix") or "")
        if not url_key:
            raise MagentoGraphQLError(f"Magento product {sku!r} is missing its URL key")
        product_url = canonicalize_url(urljoin(base_url, f"/{url_key}{url_suffix}"))
        description, description_html = _clean_html(raw.get("description"))
        if not description:
            description, description_html = _clean_html(raw.get("short_description"))
        price, compare_at, currency = _price(raw)
        images: list[str] = []
        image = raw.get("image")
        if isinstance(image, dict) and image.get("url"):
            images.append(str(image["url"]))
        for item in raw.get("media_gallery", []):
            if isinstance(item, dict) and item.get("url") and not item.get("disabled"):
                images.append(str(item["url"]))
        variants = [
            variant
            for item in raw.get("variants", [])
            if isinstance(item, dict)
            for variant in [_variant(item)]
            if variant is not None
        ]
        configurable_options = [
            item for item in raw.get("configurable_options", []) if isinstance(item, dict)
        ]
        attributes = {
            str(item.get("label") or item.get("attribute_code") or "Option"): [
                str(value.get("label") or value.get("value_index") or "")
                for value in item.get("values", [])
                if isinstance(value, dict)
            ]
            for item in configurable_options
        }
        return Product(
            name=name,
            url=product_url,
            canonical_url=product_url,
            category_path=category_path,
            description=description,
            description_html=description_html,
            sku=sku,
            price=price,
            compare_at_price=compare_at,
            currency=currency,
            availability="InStock"
            if str(raw.get("stock_status") or "").upper() == "IN_STOCK"
            else "OutOfStock",
            images=list(dict.fromkeys(images)),
            attributes=attributes,
            variants=variants,
            sources=[ExtractionSource.PLATFORM_API],
            confidence=0.98,
        )

    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        del navigation
        probe = self.probe or await probe_magento(self.client, base_url)
        if not probe.graphql_available:
            raise MagentoGraphQLError("Public Magento GraphQL catalogue is unavailable")
        roots = await self._categories(base_url, probe.root_category_id)
        self.category_tree = [
            node for root in roots for node in self._nodes(root)
        ]
        all_categories = self._flatten(roots)
        candidates = [category for category in all_categories if category.product_count > 0]
        warnings = list(probe.warnings)
        empty_count = len(all_categories) - len(candidates)
        if empty_count:
            warnings.append(f"Skipped {empty_count} Magento categories with no products")

        results: list[CategoryResult] = []
        for category in candidates[: request.max_categories]:
            category_warnings: list[str] = []
            try:
                basic, total_count, truncated = await self._basic_products(
                    base_url, category, request.max_pages_per_category
                )
                if truncated:
                    category_warnings.append(
                        f"Product pagination stopped after {request.max_pages_per_category} pages"
                    )
                sampled = deterministic_sample(
                    basic,
                    request.products_per_category,
                    f"{request.seed or base_url}:magento:{category.uid or category.category_id}",
                )
                details = await self._details(base_url, sampled)
                products = [
                    self._normalize_product(item, base_url, category.path) for item in details
                ]
            except TargetAccessError:
                raise
            except Exception as exc:
                category_warnings.append(f"Magento category could not be read: {exc}")
                total_count = category.product_count
                products = []
            results.append(
                CategoryResult(
                    name=category.name,
                    url=category.url,
                    path=category.path,
                    visibility=CategoryVisibility.PLATFORM_API,
                    discovered_product_count=total_count,
                    products=products,
                    warnings=category_warnings,
                )
            )

        if len(candidates) > request.max_categories:
            warnings.append(
                f"Category processing stopped at the configured limit of {request.max_categories}"
            )
        if not results:
            warnings.append("Magento GraphQL returned no public categories containing products")
        return results, warnings
