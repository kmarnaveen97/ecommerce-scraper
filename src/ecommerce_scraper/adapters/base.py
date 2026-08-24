from abc import ABC, abstractmethod

from ecommerce_scraper.http_client import SafeHttpClient
from ecommerce_scraper.models import (
    CategoryNode,
    CategoryResult,
    ExtractionStrategy,
    ScrapeRequest,
)


class Adapter(ABC):
    strategy = ExtractionStrategy.GENERIC_HTML

    def __init__(self, client: SafeHttpClient) -> None:
        self.client = client

    @abstractmethod
    async def scrape(
        self,
        base_url: str,
        request: ScrapeRequest,
        navigation: list[CategoryNode],
    ) -> tuple[list[CategoryResult], list[str]]:
        raise NotImplementedError
