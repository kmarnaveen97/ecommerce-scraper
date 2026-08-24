from ecommerce_scraper import discovery

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
