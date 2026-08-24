import httpx

from ecommerce_scraper import discovery
from ecommerce_scraper.config import Settings
from ecommerce_scraper.http_client import SafeHttpClient

HTML = """
<html><body>
  <nav>
    <ul>
      <li><a href="/collections/jewellery">Jewellery</a>
        <ul>
          <li><a href="/collections/rings">Rings</a></li>
          <li><a href="/collections/earrings">Earrings</a></li>
        </ul>
      </li>
    </ul>
  </nav>
  <div class="product-card"><a href="/products/silver-ring?utm_source=nav">Silver Ring</a></div>
</body></html>
"""


def test_navigation_preserves_hierarchy() -> None:
    tree = discovery.discover_navigation_tree(HTML, "https://shop.example")
    flat = discovery.flatten_categories(tree)

    assert [node.path for node in flat] == [
        ["Jewellery"],
        ["Jewellery", "Rings"],
        ["Jewellery", "Earrings"],
    ]


def test_product_link_extraction_is_canonical() -> None:
    assert discovery.extract_product_links(HTML, "https://shop.example") == {
        "https://shop.example/products/silver-ring"
    }


async def test_collection_sitemap_is_processed_before_large_product_sitemap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    "<sitemap><loc>https://shop.example/sitemap_products_1.xml</loc></sitemap>"
                    "<sitemap><loc>https://shop.example/sitemap_collections_1.xml</loc></sitemap>"
                    "</sitemapindex>"
                ),
                request=request,
            )
        if request.url.path == "/sitemap_collections_1.xml":
            return httpx.Response(
                200,
                text=(
                    "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    "<url><loc>https://shop.example/collections/rings</loc></url>"
                    "</urlset>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    settings = Settings(
        robots_obey=False,
        target_requests_per_second=0,
        target_jitter_seconds=0,
    )
    async with SafeHttpClient(
        settings,
        transport=httpx.MockTransport(handler),
        resolve_dns=False,
    ) as client:
        inventory = await discovery.discover_sitemaps(client, "https://shop.example", 1)

    assert inventory.category_urls == ["https://shop.example/collections/rings"]
