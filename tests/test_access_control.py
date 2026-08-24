from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from ecommerce_scraper.access_control import (
    detect_target_response,
    parse_retry_after,
    rate_limit_exhausted,
)
from ecommerce_scraper.models import AccessEventKind


def test_detects_cloudflare_challenge() -> None:
    detection = detect_target_response(
        403,
        {"server": "cloudflare", "cf-ray": "abc123"},
        b"<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>",
    )

    assert detection is not None
    assert detection.kind == AccessEventKind.BOT_CHALLENGE
    assert detection.provider == "cloudflare"


@pytest.mark.parametrize(
    ("headers", "body", "provider"),
    [
        ({"x-datadome": "protected"}, b"captcha-delivery.com", "datadome"),
        ({"set-cookie": "_px3=token"}, b"<div id='px-captcha'></div>", "perimeterx"),
        ({"set-cookie": "incap_ses_123=value"}, b"Incapsula incident", "imperva"),
    ],
)
def test_detects_challenge_pages_returned_as_success(
    headers: dict[str, str], body: bytes, provider: str
) -> None:
    detection = detect_target_response(200, headers, body)

    assert detection is not None
    assert detection.kind == AccessEventKind.BOT_CHALLENGE
    assert detection.provider == provider


def test_normal_cloudflare_response_is_not_a_challenge() -> None:
    assert detect_target_response(
        200,
        {"server": "cloudflare", "cf-ray": "abc123"},
        b"<html><title>Product</title><h1>Blue shirt</h1></html>",
    ) is None


def test_parse_retry_after_seconds_and_http_date() -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    retry_at = now + timedelta(seconds=45)

    assert parse_retry_after({"Retry-After": "12"}, now=now) == 12
    assert parse_retry_after({"Retry-After": format_datetime(retry_at)}, now=now) == 45


def test_parse_rate_limit_reset_epoch() -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    assert parse_retry_after(
        {
            "RateLimit-Remaining": "0",
            "RateLimit-Reset": str(int(now.timestamp()) + 30),
        },
        now=now,
    ) == 30
    assert rate_limit_exhausted({"X-RateLimit-Remaining": "0"}) is True
    assert rate_limit_exhausted({"RateLimit-Remaining": "4"}) is False
