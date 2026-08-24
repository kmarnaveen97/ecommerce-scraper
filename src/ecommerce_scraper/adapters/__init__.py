from ecommerce_scraper.adapters.generic import GenericAdapter
from ecommerce_scraper.adapters.magento import MagentoGraphQLAdapter, probe_magento
from ecommerce_scraper.adapters.shopify import (
    ShopifyAdapter,
    ShopifyJsonAdapter,
    ShopifySitemapAdapter,
    probe_shopify,
)
from ecommerce_scraper.adapters.woocommerce import WooCommerceAdapter

__all__ = [
    "GenericAdapter",
    "MagentoGraphQLAdapter",
    "ShopifyAdapter",
    "ShopifyJsonAdapter",
    "ShopifySitemapAdapter",
    "WooCommerceAdapter",
    "probe_shopify",
    "probe_magento",
]
