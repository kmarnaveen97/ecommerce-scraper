import pytest

from ecommerce_scraper.security import (
    UnsafeUrlError,
    canonicalize_url,
    is_asset_url,
    normalize_url,
    validate_hostname_syntax,
)


def test_normalize_adds_https() -> None:
    assert normalize_url("example.com/shop") == "https://example.com/shop"


def test_canonicalize_drops_tracking_and_fragment() -> None:
    assert (
        canonicalize_url("https://Example.com/products/a/?utm_source=x&size=m#reviews")
        == "https://example.com/products/a?size=m"
    )


def test_magento_catalog_assets_are_not_product_pages() -> None:
    assert is_asset_url(
        "https://www.asics.co.in/media/catalog/product/1/0/1011b974_400_sr_rt_glb.jpg"
    )
    assert not is_asset_url("https://www.asics.co.in/novablast-5-1011b974-400.html")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://user:password@example.com",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_hostname_syntax(url)
