from __future__ import annotations

from datetime import UTC, datetime

from ecommerce_scraper.adapters import (
    GenericAdapter,
    MagentoGraphQLAdapter,
    ShopifyAdapter,
    WooCommerceAdapter,
    probe_magento,
    probe_shopify,
)
from ecommerce_scraper.adapters.base import Adapter
from ecommerce_scraper.config import Settings
from ecommerce_scraper.discovery import discover_navigation_tree
from ecommerce_scraper.http_client import SafeHttpClient, TargetAccessError
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

        async with SafeHttpClient(
            self.settings,
            resolve_dns=self.settings.http_validate_dns,
        ) as client:
            robots_warning = await client.load_robots_policy(base_url)
            homepage = await client.get(base_url)
            homepage.raise_for_status()
            final_url = normalize_url(str(homepage.url))
            detection = detect_platform(homepage)
            navigation = discover_navigation_tree(homepage.text, final_url)
            warnings = [
                f"Platform detection confidence: {detection.confidence:.0%}",
            ]
            if robots_warning:
                warnings.append(robots_warning)

            effective_platform = detection.platform
            shopify_probe = None
            if detection.platform in {Platform.SHOPIFY, Platform.GENERIC}:
                shopify_probe = await probe_shopify(client, final_url)
                if shopify_probe.json_available and detection.platform != Platform.SHOPIFY:
                    effective_platform = Platform.SHOPIFY
                    warnings.append(
                        "Public Shopify catalogue APIs were detected behind a custom/headless theme"
                    )

            magento_probe = None
            if effective_platform != Platform.SHOPIFY and detection.platform in {
                Platform.MAGENTO,
                Platform.GENERIC,
            }:
                magento_probe = await probe_magento(client, final_url)
                if magento_probe.graphql_available:
                    effective_platform = Platform.MAGENTO
                    if detection.platform != Platform.MAGENTO:
                        warnings.append(
                            "Public Magento GraphQL was detected behind a custom/headless storefront"
                        )

            adapter: Adapter
            if effective_platform == Platform.SHOPIFY:
                adapter = ShopifyAdapter(client, shopify_probe)
            elif (
                effective_platform == Platform.MAGENTO
                and magento_probe
                and magento_probe.graphql_available
            ):
                adapter = MagentoGraphQLAdapter(client, magento_probe)
            elif detection.platform == Platform.WOOCOMMERCE:
                adapter = WooCommerceAdapter(client)
            else:
                adapter = GenericAdapter(client)
                if detection.platform == Platform.MAGENTO and magento_probe:
                    warnings.extend(magento_probe.warnings)
                    warnings.append(
                        "Public Magento GraphQL is unavailable; using generic discovery"
                    )
                elif detection.platform == Platform.BIGCOMMERCE:
                    warnings.append("No dedicated BigCommerce adapter; using generic discovery")

            try:
                categories, adapter_warnings = await adapter.scrape(final_url, request, navigation)
            except TargetAccessError:
                raise
            except Exception as exc:
                if isinstance(adapter, GenericAdapter):
                    raise
                warnings.append(
                    f"{effective_platform.value} adapter failed ({exc}); generic discovery was used"
                )
                adapter = GenericAdapter(client)
                categories, adapter_warnings = await adapter.scrape(final_url, request, navigation)
            warnings.extend(adapter_warnings)
            access_report = client.access_report()

        return ScrapeResult(
            site_url=final_url,
            platform=effective_platform,
            extraction_strategy=adapter.strategy,
            category_tree=adapter.category_tree or navigation,
            categories=categories,
            warnings=warnings,
            access_report=access_report,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
