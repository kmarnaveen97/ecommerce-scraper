import pytest

from ecommerce_scraper.extractors import ProductEvidenceError, extract_product
from ecommerce_scraper.models import ExtractionSource


def test_extracts_product_and_breadcrumb_json_ld() -> None:
    html = """
    <html><head>
      <link rel="canonical" href="https://shop.example/products/ring">
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@graph": [
          {
            "@type": "BreadcrumbList",
            "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Home"},
              {"@type": "ListItem", "position": 2, "name": "Rings"}
            ]
          },
          {
            "@type": "Product",
            "name": "Silver Ring",
            "sku": "R-1",
            "brand": {"@type": "Brand", "name": "Emori"},
            "image": ["/images/ring.jpg"],
            "offers": {
              "@type": "Offer",
              "price": "1499.00",
              "priceCurrency": "INR",
              "availability": "https://schema.org/InStock"
            }
          }
        ]
      }
      </script>
    </head><body><h1>Silver Ring</h1></body></html>
    """

    product = extract_product(html, "https://shop.example/products/ring")

    assert product.name == "Silver Ring"
    assert product.category_path == ["Rings"]
    assert product.price == "1499.00"
    assert product.currency == "INR"
    assert product.availability == "InStock"
    assert product.brand == "Emori"
    assert product.images == ["https://shop.example/images/ring.jpg"]
    assert product.sources == [ExtractionSource.JSON_LD]


def test_rejects_page_without_minimum_product_evidence() -> None:
    html = "<html><head><title>Summer Collection</title></head><body><h1>Shoes</h1></body></html>"

    with pytest.raises(ProductEvidenceError, match="minimum product evidence"):
        extract_product(html, "https://shop.example/catalog/shoes")


def test_rejects_asset_url_even_if_body_looks_like_a_product() -> None:
    html = "<html><body><h1>Not a page</h1><div class='price'>99</div></body></html>"

    with pytest.raises(ProductEvidenceError, match="Asset URL"):
        extract_product(html, "https://shop.example/media/catalog/product/shoe.jpg")
