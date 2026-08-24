from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ecommerce_scraper.models import AccessEventKind

_CLOUDFLARE_BODY = re.compile(
    r"(?:/cdn-cgi/challenge-platform|cf-chl-|checking your browser|"
    r"performing security verification|just a moment\.\.\.)",
    re.IGNORECASE,
)
_DATADOME_BODY = re.compile(r"(?:captcha-delivery\.com|datadome|dd_captcha)", re.IGNORECASE)
_PERIMETERX_BODY = re.compile(r"(?:px-captcha|_px\w+|perimeterx)", re.IGNORECASE)
_IMPERVA_BODY = re.compile(r"(?:incapsula|incap_ses|visid_incap)", re.IGNORECASE)
_AKAMAI_BODY = re.compile(r"(?:akamai bot manager|akamai[- ]?ghost|reference\s*#\d+)", re.IGNORECASE)
_GENERIC_CHALLENGE = re.compile(
    r"(?:<title>\s*(?:captcha|access denied|verify (?:you are )?human)|"
    r"captcha.{0,80}(?:verify|human|security)|"
    r"(?:verify (?:you are )?human|unusual traffic).{0,80}(?:continue|security|request))",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class TargetResponseDetection:
    kind: AccessEventKind
    provider: str | None
    confidence: float


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def _provider(headers: Mapping[str, str], body: str) -> str | None:
    server = headers.get("server", "").lower()
    cookies = headers.get("set-cookie", "").lower()
    if "cf-ray" in headers or "cloudflare" in server or _CLOUDFLARE_BODY.search(body):
        return "cloudflare"
    if "x-datadome" in headers or "datadome" in cookies or _DATADOME_BODY.search(body):
        return "datadome"
    if "x-px" in headers or "_px" in cookies or _PERIMETERX_BODY.search(body):
        return "perimeterx"
    if "incap_ses" in cookies or "visid_incap" in cookies or _IMPERVA_BODY.search(body):
        return "imperva"
    if "akamai-grn" in headers or server == "akamaighost" or _AKAMAI_BODY.search(body):
        return "akamai"
    return None


def detect_target_response(
    status_code: int, headers: Mapping[str, str], content: bytes
) -> TargetResponseDetection | None:
    normalized = _normalized_headers(headers)
    body = content[:250_000].decode("utf-8", errors="ignore")
    provider = _provider(normalized, body)

    if status_code == 429:
        return TargetResponseDetection(AccessEventKind.RATE_LIMITED, provider, 1.0)

    explicit_challenge = (
        normalized.get("cf-mitigated", "").lower() == "challenge"
        or "x-datadome" in normalized
        or bool(_CLOUDFLARE_BODY.search(body))
        or bool(_DATADOME_BODY.search(body))
        or bool(_PERIMETERX_BODY.search(body))
        or bool(_IMPERVA_BODY.search(body))
        or bool(_AKAMAI_BODY.search(body))
        or bool(_GENERIC_CHALLENGE.search(body))
    )
    location = normalized.get("location", "")
    if "/cdn-cgi/challenge-platform" in location or "captcha" in location.lower():
        explicit_challenge = True

    if explicit_challenge:
        return TargetResponseDetection(AccessEventKind.BOT_CHALLENGE, provider, 0.98)
    if status_code == 403 and provider:
        return TargetResponseDetection(AccessEventKind.BOT_CHALLENGE, provider, 0.9)
    if status_code == 403:
        return TargetResponseDetection(AccessEventKind.ACCESS_DENIED, None, 0.85)
    return None


def parse_retry_after(
    headers: Mapping[str, str], *, now: datetime | None = None
) -> float | None:
    normalized = _normalized_headers(headers)
    retry_after = normalized.get("retry-after", "").strip()
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                current = now or datetime.now(UTC)
                return max(0.0, (parsed - current).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass

    remaining = normalized.get("ratelimit-remaining") or normalized.get("x-ratelimit-remaining")
    reset = normalized.get("ratelimit-reset") or normalized.get("x-ratelimit-reset")
    if remaining == "0" and reset:
        try:
            value = float(reset)
        except ValueError:
            return None
        current_timestamp = (now or datetime.now(UTC)).timestamp()
        return max(0.0, value - current_timestamp if value > current_timestamp else value)
    return None


def rate_limit_exhausted(headers: Mapping[str, str]) -> bool:
    normalized = _normalized_headers(headers)
    remaining = normalized.get("ratelimit-remaining") or normalized.get("x-ratelimit-remaining")
    return remaining == "0"
