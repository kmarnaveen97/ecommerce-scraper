from __future__ import annotations

import httpx
import pytest

from ecommerce_scraper.config import Settings
from ecommerce_scraper.http_client import (
    RobotsDeniedError,
    SafeHttpClient,
    TargetBlockedError,
)
from ecommerce_scraper.models import AccessEventKind


def _settings(**overrides: object) -> Settings:
    return Settings(
        target_requests_per_second=0,
        target_jitter_seconds=0,
        target_backoff_base_seconds=0,
        target_backoff_cap_seconds=0,
        **overrides,
    )


async def test_retries_rate_limit_then_returns_success() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = httpx.MockTransport(handler)
    async with SafeHttpClient(
        _settings(robots_obey=False, target_max_retries=2),
        transport=transport,
        resolve_dns=False,
    ) as client:
        response = await client.get("https://shop.example/products/one")
        report = client.access_report()

    assert response.json() == {"ok": True}
    assert calls == 2
    assert report.total_requests == 2
    assert report.retries == 1
    assert report.rate_limited_responses == 1
    assert report.events[0].kind == AccessEventKind.RATE_LIMITED


async def test_challenge_stops_without_retry_and_opens_circuit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            headers={"server": "cloudflare", "cf-ray": "abc123"},
            content=b"<title>Just a moment...</title>",
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with SafeHttpClient(
        _settings(robots_obey=False, target_block_threshold=1),
        transport=transport,
        resolve_dns=False,
    ) as client:
        with pytest.raises(TargetBlockedError) as caught:
            await client.get("https://shop.example/products/one")
        report = caught.value.report

    assert calls == 1
    assert report.block_events == 1
    assert report.circuit_open is True
    assert report.events[0].provider == "cloudflare"


async def test_robots_denial_prevents_target_request_and_applies_crawl_delay() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private\nCrawl-delay: 2\n",
                request=request,
            )
        return httpx.Response(200, text="should not be fetched", request=request)

    transport = httpx.MockTransport(handler)
    async with SafeHttpClient(
        _settings(robots_obey=True), transport=transport, resolve_dns=False
    ) as client:
        await client.load_robots_policy("https://shop.example")
        with pytest.raises(RobotsDeniedError) as caught:
            await client.get("https://shop.example/private/catalog")
        report = caught.value.report

    assert requested_paths == ["/robots.txt"]
    assert report.total_requests == 1
    assert report.robots_denied == 1
    assert report.effective_min_interval_seconds == 2
    assert report.events[0].kind == AccessEventKind.ROBOTS_DENIED
