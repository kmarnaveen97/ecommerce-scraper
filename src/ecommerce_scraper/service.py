from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_scraper.adapters import GenericAdapter, ShopifyAdapter, WooCommerceAdapter
from ecommerce_scraper.adapters.base import Adapter
from ecommerce_scraper.config import Settings
from ecommerce_scraper.discovery import discover_navigation_tree
from ecommerce_scraper.http_client import SafeHttpClient
from ecommerce_scraper.models import Platform, ScrapeRequest, ScrapeResult
from ecommerce_scraper.platforms import detect_platform
from ecommerce_scraper.security import normalize_url, validate_hostname_syntax


class ScrapeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def scrape(self, request: ScrapeRequest) -> ScrapeResult:
        started_at = datetime.now(UTC)
        base_url = normalize_url(request.url)
        validate_hostname_syntax(base_url)

        async with SafeHttpClient(self.settings) as client:
            homepage = await client.get(base_url)
            homepage.raise_for_status()
            final_url = normalize_url(str(homepage.url))
            detection = detect_platform(homepage)
            navigation = discover_navigation_tree(homepage.text, final_url)
            warnings = [
                f"Platform detection confidence: {detection.confidence:.0%}",
            ]

            adapter: Adapter
            if detection.platform == Platform.SHOPIFY:
                adapter = ShopifyAdapter(client)
            elif detection.platform == Platform.WOOCOMMERCE:
                adapter = WooCommerceAdapter(client)
            else:
                adapter = GenericAdapter(client)
                if detection.platform not in {Platform.GENERIC, Platform.BIGCOMMERCE, Platform.MAGENTO}:
                    warnings.append(
                        f"No dedicated {detection.platform.value} adapter; using generic discovery"
                    )

            try:
                categories, adapter_warnings = await adapter.scrape(final_url, request, navigation)
            except Exception as exc:
                if isinstance(adapter, GenericAdapter):
                    raise
                warnings.append(
                    f"{detection.platform.value} API adapter failed ({exc}); generic discovery was used"
                )
                categories, adapter_warnings = await GenericAdapter(client).scrape(
                    final_url, request, navigation
                )
            warnings.extend(adapter_warnings)

        return ScrapeResult(
            site_url=final_url,
            platform=detection.platform,
            category_tree=navigation,
            categories=categories,
            warnings=warnings,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
