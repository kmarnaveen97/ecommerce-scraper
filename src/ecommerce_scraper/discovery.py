from __future__ import annotations

import gzip
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from lxml import etree

from ecommerce_scraper.http_client import RobotsDeniedError, SafeHttpClient, TargetAccessError
from ecommerce_scraper.models import CategoryNode
from ecommerce_scraper.security import UnsafeUrlError, canonicalize_url, is_asset_url, same_site

PRODUCT_URL_RE = re.compile(
    r"/(?:products?|product-detail|productdetails?|p|item|dp|pd)(?:/|-)[^/?#]+", re.IGNORECASE
)
CATEGORY_URL_RE = re.compile(
    r"/(?:collections?|categories?|category|product-category|catalog)(?:/|-)[^?#]+", re.IGNORECASE
)
PAGINATION_RE = re.compile(r"(?:[?&](?:page|p)=\d+|/page/\d+)", re.IGNORECASE)
_IGNORED_LABELS = {
    "home",
    "account",
    "my account",
    "cart",
    "bag",
    "wishlist",
    "login",
    "sign in",
    "search",
    "contact",
    "about",
    "blog",
    "track order",
}


@dataclass
class SitemapInventory:
    product_urls: list[str] = field(default_factory=list)
    category_urls: list[str] = field(default_factory=list)
    other_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _clean_label(value: str) -> str:
    return " ".join(value.split()).strip(" -–—|›»")


def _is_internal(base_url: str, target: str) -> bool:
    try:
        return same_site(base_url, target)
    except (UnsafeUrlError, ValueError):
        return False


def _direct_child(tag: Tag, name: str) -> Tag | None:
    child = tag.find(name, recursive=False)
    return child if isinstance(child, Tag) else None


def _parse_li(li: Tag, base_url: str, parent_path: list[str]) -> CategoryNode | None:
    anchor = _direct_child(li, "a")
    label = _clean_label(anchor.get_text(" ", strip=True) if anchor else "")
    href = anchor.get("href") if anchor else None
    if not label or label.lower() in _IGNORED_LABELS or len(label) > 100:
        return None
    absolute_url = urljoin(base_url, str(href)) if href else None
    if absolute_url and not _is_internal(base_url, absolute_url):
        absolute_url = None
    path = [*parent_path, label]
    node = CategoryNode(name=label, url=canonicalize_url(absolute_url) if absolute_url else None, path=path)
    nested = _direct_child(li, "ul")
    if nested:
        for child_li in nested.find_all("li", recursive=False):
            child = _parse_li(child_li, base_url, path)
            if child:
                node.children.append(child)
    return node


def discover_navigation_tree(html: str, base_url: str) -> list[CategoryNode]:
    soup = BeautifulSoup(html, "lxml")
    roots: list[CategoryNode] = []
    seen: set[tuple[str, ...]] = set()
    containers = soup.select("nav, [role='navigation'], header [class*='menu'], header [class*='nav']")
    for container in containers[:12]:
        for ul in container.find_all("ul", recursive=False) or container.find_all("ul", limit=2):
            for li in ul.find_all("li", recursive=False):
                node = _parse_li(li, base_url, [])
                if node and tuple(node.path) not in seen:
                    roots.append(node)
                    seen.add(tuple(node.path))
    if roots:
        return roots

    # Sparse fallback for sites without semantic navigation markup.
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, str(anchor.get("href")))
        if not _is_internal(base_url, href) or not CATEGORY_URL_RE.search(urlsplit(href).path):
            continue
        label = _clean_label(anchor.get_text(" ", strip=True))
        if not label or label.lower() in _IGNORED_LABELS:
            continue
        key = (label,)
        if key not in seen:
            roots.append(CategoryNode(name=label, url=canonicalize_url(href), path=[label]))
            seen.add(key)
    return roots


def flatten_categories(nodes: list[CategoryNode]) -> list[CategoryNode]:
    output: list[CategoryNode] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def visit(node: CategoryNode) -> None:
        key = (node.url or "", tuple(node.path))
        if key not in seen:
            output.append(node)
            seen.add(key)
        for child in node.children:
            visit(child)

    for root in nodes:
        visit(root)
    return output


def extract_product_links(html: str, page_url: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links: set[str] = set()
    selectors = (
        "[class*='product'] a[href]",
        "[data-product-id] a[href]",
        "a[href*='/product/']",
        "a[href*='/products/']",
        "a[href*='/p/']",
        "a[href*='/item/']",
        "a[href*='/dp/']",
    )
    for selector in selectors:
        for anchor in soup.select(selector):
            target = urljoin(page_url, str(anchor.get("href")))
            if (
                _is_internal(page_url, target)
                and not is_asset_url(target)
                and PRODUCT_URL_RE.search(urlsplit(target).path)
            ):
                links.add(canonicalize_url(target))
    return links


def extract_pagination_links(html: str, page_url: str) -> set[str]:
    soup = BeautifulSoup(html, "lxml")
    links: set[str] = set()
    for anchor in soup.select("a[rel='next'], [class*='pagination'] a[href], [class*='pager'] a[href]"):
        target = urljoin(page_url, str(anchor.get("href")))
        if _is_internal(page_url, target) and PAGINATION_RE.search(target):
            links.add(canonicalize_url(target))
    return links


def _sitemap_locations(content: bytes) -> tuple[list[str], list[str]]:
    if content.startswith(b"\x1f\x8b"):
        content = gzip.decompress(content)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    root = etree.fromstring(content, parser=parser)
    root_name = etree.QName(root.tag).localname.lower()
    locations: list[str] = []
    for entry in root:
        entry_name = etree.QName(entry.tag).localname.lower()
        if entry_name not in {"url", "sitemap"}:
            continue
        for child in entry:
            if etree.QName(child.tag).localname.lower() == "loc" and child.text:
                locations.append(str(child.text).strip())
                break
    if root_name == "sitemapindex":
        return [], locations
    return locations, []


def _sitemap_priority(url: str) -> tuple[int, str]:
    path = urlsplit(url).path.lower()
    if "collection" in path or "categor" in path:
        return (0, url)
    if "product" in path:
        return (2, url)
    return (1, url)


async def discover_sitemaps(
    client: SafeHttpClient, base_url: str, max_urls: int
) -> SitemapInventory:
    inventory = SitemapInventory()
    candidates: deque[str] = deque()
    visited_sitemaps: set[str] = set()
    robots_url = urljoin(base_url, "/robots.txt")
    robots_text = client.get_cached_robots_text(base_url)
    if robots_text is None:
        try:
            robots = await client.get(robots_url, respect_robots=False)
            robots_text = robots.text if robots.is_success else None
        except TargetAccessError:
            raise
        except Exception as exc:
            inventory.warnings.append(f"robots.txt could not be read: {exc}")
    if robots_text:
        for line in robots_text.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(urljoin(base_url, line.split(":", 1)[1].strip()))
    candidates.extend([urljoin(base_url, "/sitemap.xml"), urljoin(base_url, "/sitemap_index.xml")])

    all_urls: set[str] = set()
    while candidates and len(all_urls) < max_urls and len(visited_sitemaps) < 250:
        sitemap_url = canonicalize_url(candidates.popleft())
        if sitemap_url in visited_sitemaps or not _is_internal(base_url, sitemap_url):
            continue
        visited_sitemaps.add(sitemap_url)
        try:
            response = await client.get(sitemap_url)
            if not response.is_success:
                continue
            urls, child_sitemaps = _sitemap_locations(response.content)
        except RobotsDeniedError as exc:
            inventory.warnings.append(str(exc))
            continue
        except TargetAccessError:
            raise
        except Exception:
            continue
        # Category sitemaps are normally much smaller and are required to classify
        # storefront-visible collections. Process them before very large product
        # sitemaps consume the configured URL budget.
        candidates.extend(sorted(child_sitemaps, key=_sitemap_priority))
        for url in urls:
            if len(all_urls) >= max_urls:
                break
            if _is_internal(base_url, url):
                all_urls.add(canonicalize_url(url))

    for url in sorted(all_urls):
        path = urlsplit(url).path
        if is_asset_url(url):
            inventory.other_urls.append(url)
        elif PRODUCT_URL_RE.search(path):
            inventory.product_urls.append(url)
        elif CATEGORY_URL_RE.search(path):
            inventory.category_urls.append(url)
        else:
            inventory.other_urls.append(url)
    if len(all_urls) >= max_urls:
        inventory.warnings.append(f"Sitemap discovery stopped at the configured {max_urls:,}-URL limit")
    return inventory
