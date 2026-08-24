from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ecommerce_scraper.models import ExtractionSource, Product, ProductVariant
from ecommerce_scraper.security import canonicalize_url


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _type_matches(value: Any, expected: str) -> bool:
    return any(str(item).rsplit("/", 1)[-1].lower() == expected.lower() for item in _as_list(value))


def _json_ld_nodes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for entry in _as_list(parsed):
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph")
            if isinstance(graph, list):
                nodes.extend(item for item in graph if isinstance(item, dict))
            nodes.append(entry)
    return nodes


def _brand(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() if name else None
    return None


def _images(value: Any, base_url: str) -> list[str]:
    output: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = item.get("url") or item.get("contentUrl")
        if isinstance(item, str) and item.strip():
            output.append(urljoin(base_url, item.strip()))
    return list(dict.fromkeys(output))


def _availability(value: Any) -> str | None:
    if not value:
        return None
    return str(value).rsplit("/", 1)[-1]


def _offer_data(offers: Any) -> tuple[str | None, str | None, str | None, list[ProductVariant]]:
    entries = [item for item in _as_list(offers) if isinstance(item, dict)]
    if not entries:
        return None, None, None, []
    first = entries[0]
    price = first.get("price") or first.get("lowPrice")
    currency = first.get("priceCurrency")
    availability = _availability(first.get("availability"))
    variants = [
        ProductVariant(
            id=str(entry.get("sku") or entry.get("@id") or "") or None,
            title=str(entry.get("name") or "") or None,
            sku=str(entry.get("sku") or "") or None,
            price=str(entry.get("price") or entry.get("lowPrice") or "") or None,
            currency=str(entry.get("priceCurrency") or "") or None,
            available=_availability(entry.get("availability")) == "InStock"
            if entry.get("availability")
            else None,
        )
        for entry in entries
        if len(entries) > 1
    ]
    return (
        str(price) if price is not None else None,
        str(currency) if currency is not None else None,
        availability,
        variants,
    )


def _breadcrumb(nodes: Iterable[dict[str, Any]]) -> list[str]:
    for node in nodes:
        if not _type_matches(node.get("@type"), "BreadcrumbList"):
            continue
        elements = [item for item in _as_list(node.get("itemListElement")) if isinstance(item, dict)]
        elements.sort(key=lambda item: int(item.get("position", 10_000)))
        names: list[str] = []
        for element in elements:
            value = element.get("name")
            if not value and isinstance(element.get("item"), dict):
                value = element["item"].get("name")
            if value and str(value).strip().lower() not in {"home", "homepage"}:
                names.append(str(value).strip())
        if names:
            return names
    return []


def _meta(soup: BeautifulSoup, *selectors: str) -> str | None:
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag:
            value = (
                tag.get("content")
                or tag.get("href")
                or tag.get("src")
                or tag.get("value")
                or tag.get_text(" ", strip=True)
            )
            if value and str(value).strip():
                return str(value).strip()
    return None


def _attributes(soup: BeautifulSoup) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for row in soup.select("table tr")[:100]:
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            key = cells[0].get_text(" ", strip=True)
            value = cells[1].get_text(" ", strip=True)
            if key and value and len(key) <= 100 and len(value) <= 2_000:
                attributes[key] = value
    return attributes


def extract_product(html: str, page_url: str, fallback_path: list[str] | None = None) -> Product:
    soup = BeautifulSoup(html, "lxml")
    nodes = _json_ld_nodes(soup)
    product_node = next((node for node in nodes if _type_matches(node.get("@type"), "Product")), None)
    breadcrumb = _breadcrumb(nodes)

    if product_node:
        price, currency, availability, offer_variants = _offer_data(product_node.get("offers"))
        rating_node = product_node.get("aggregateRating")
        rating = rating_node if isinstance(rating_node, dict) else {}
        variants: list[ProductVariant] = offer_variants
        for item in _as_list(product_node.get("hasVariant")):
            if not isinstance(item, dict):
                continue
            variant_price, variant_currency, variant_availability, _ = _offer_data(item.get("offers"))
            variants.append(
                ProductVariant(
                    id=str(item.get("productID") or item.get("sku") or "") or None,
                    title=str(item.get("name") or "") or None,
                    sku=str(item.get("sku") or "") or None,
                    price=variant_price,
                    currency=variant_currency,
                    available=variant_availability == "InStock" if variant_availability else None,
                )
            )
        canonical = _meta(soup, "link[rel='canonical']") or product_node.get("url") or page_url
        name = str(
            product_node.get("name")
            or _meta(soup, "meta[property='og:title']", "h1")
            or "Unknown product"
        )
        populated = sum(
            bool(value)
            for value in (
                name,
                product_node.get("description"),
                product_node.get("brand"),
                product_node.get("sku"),
                price,
                product_node.get("image"),
            )
        )
        return Product(
            name=name.strip(),
            url=canonicalize_url(page_url),
            canonical_url=canonicalize_url(urljoin(page_url, str(canonical))),
            category_path=breadcrumb or (fallback_path or []),
            description=str(product_node.get("description") or "").strip() or None,
            brand=_brand(product_node.get("brand")),
            sku=str(product_node.get("sku") or "").strip() or None,
            mpn=str(product_node.get("mpn") or "").strip() or None,
            gtin=str(
                product_node.get("gtin")
                or product_node.get("gtin8")
                or product_node.get("gtin12")
                or product_node.get("gtin13")
                or product_node.get("gtin14")
                or ""
            ).strip()
            or None,
            price=price,
            currency=currency,
            availability=availability,
            images=_images(product_node.get("image"), page_url),
            rating=float(rating["ratingValue"]) if rating.get("ratingValue") is not None else None,
            review_count=int(float(rating["reviewCount"])) if rating.get("reviewCount") is not None else None,
            attributes=_attributes(soup),
            variants=variants,
            sources=[ExtractionSource.JSON_LD],
            confidence=min(0.65 + populated * 0.05, 0.95),
        )

    name = _meta(soup, "meta[property='og:title']", "h1", "title") or "Unknown product"
    canonical = _meta(soup, "link[rel='canonical']") or page_url
    price = _meta(
        soup,
        "meta[property='product:price:amount']",
        "[itemprop='price']",
        "[class*='price'] [class*='amount']",
        "[class*='price']",
    )
    currency = _meta(soup, "meta[property='product:price:currency']", "[itemprop='priceCurrency']")
    description = _meta(
        soup,
        "meta[property='og:description']",
        "meta[name='description']",
        "[itemprop='description']",
        "[class*='product-description']",
    )
    image = _meta(soup, "meta[property='og:image']", "[itemprop='image']")
    return Product(
        name=name,
        url=canonicalize_url(page_url),
        canonical_url=canonicalize_url(urljoin(page_url, canonical)),
        category_path=breadcrumb or (fallback_path or []),
        description=description,
        brand=_meta(soup, "[itemprop='brand']", "meta[property='product:brand']"),
        sku=_meta(soup, "[itemprop='sku']", "[class*='sku']"),
        price=price,
        currency=currency,
        availability=_availability(_meta(soup, "[itemprop='availability']")),
        images=[urljoin(page_url, image)] if image else [],
        attributes=_attributes(soup),
        sources=[ExtractionSource.DOM],
        confidence=0.45,
    )
