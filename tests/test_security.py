import pytest

from ecommerce_scraper.security import (
    UnsafeUrlError,
    canonicalize_url,
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
