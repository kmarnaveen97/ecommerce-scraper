from __future__ import annotations

import asyncio
from urllib.parse import urljoin

import httpx

from ecommerce_scraper.config import Settings
from ecommerce_scraper.security import UnsafeUrlError, normalize_url, validate_resolved_host


class ResponseTooLargeError(RuntimeError):
    pass


class SafeHttpClient:
    """HTTP client with redirect validation, response limits and concurrency control."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.5",
            },
        )

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def get(self, url: str) -> httpx.Response:
        current = normalize_url(url)
        async with self._semaphore:
            for _ in range(self.settings.http_max_redirects + 1):
                await asyncio.to_thread(validate_resolved_host, current)
                async with self._client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise httpx.HTTPStatusError(
                                "Redirect did not provide a location",
                                request=response.request,
                                response=response,
                            )
                        current = urljoin(current, location)
                        continue

                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.settings.http_max_response_bytes:
                        raise ResponseTooLargeError(f"Response is larger than configured limit: {current}")

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.http_max_response_bytes:
                            raise ResponseTooLargeError(f"Response exceeded configured limit: {current}")
                        chunks.append(chunk)

                    return httpx.Response(
                        status_code=response.status_code,
                        headers=response.headers,
                        content=b"".join(chunks),
                        request=response.request,
                        extensions=response.extensions,
                    )
            raise UnsafeUrlError("Too many redirects")

    async def get_json(self, url: str) -> tuple[object, httpx.Response]:
        response = await self.get(url)
        response.raise_for_status()
        return response.json(), response
