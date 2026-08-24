from dataclasses import dataclass

import httpx

from ecommerce_scraper.models import Platform


@dataclass(frozen=True)
class PlatformDetection:
    platform: Platform
    confidence: float
    signals: tuple[str, ...]


def detect_platform(response: httpx.Response) -> PlatformDetection:
    html = response.text.lower()
    headers = " ".join(f"{key}:{value}" for key, value in response.headers.items()).lower()
    combined = f"{headers}\n{html[:1_000_000]}"

    signatures: list[tuple[Platform, tuple[str, ...]]] = [
        (
            Platform.SHOPIFY,
            (
                "cdn.shopify.com",
                "shopify.theme",
                "shopify-section",
                "x-shopid",
                "myshopify.com",
                "/cdn/shop/",
            ),
        ),
        (Platform.WOOCOMMERCE, ("woocommerce", "wp-content/plugins/woocommerce", "wc-block-")),
        (
            Platform.MAGENTO,
            (
                "magento_",
                "x-magento",
                "mage/cookies",
                "static/version",
                "magento-pwa",
                "rootcmp_",
                "venia",
                "peregrine",
            ),
        ),
        (Platform.BIGCOMMERCE, ("stencil-utils", "cdn11.bigcommerce.com", "bigcommerce")),
    ]
    scored: list[tuple[int, Platform, tuple[str, ...]]] = []
    for platform, tokens in signatures:
        matches = tuple(token for token in tokens if token in combined)
        scored.append((len(matches), platform, matches))
    score, platform, signals = max(scored, key=lambda item: item[0])
    if score == 0:
        return PlatformDetection(Platform.GENERIC, 0.35, ())
    return PlatformDetection(platform, min(0.55 + score * 0.12, 0.99), signals)
