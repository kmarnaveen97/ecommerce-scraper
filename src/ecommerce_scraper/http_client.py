from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from ecommerce_scraper.access_control import (
    detect_target_response,
    parse_retry_after,
    rate_limit_exhausted,
)
from ecommerce_scraper.config import Settings
from ecommerce_scraper.models import (
    AccessEventKind,
    TargetAccessEvent,
    TargetAccessReport,
)
from ecommerce_scraper.security import UnsafeUrlError, normalize_url, validate_resolved_host


class ResponseTooLargeError(RuntimeError):
    pass


class TargetAccessError(RuntimeError):
    def __init__(self, message: str, report: TargetAccessReport) -> None:
        super().__init__(message)
        self.report = report


class TargetBlockedError(TargetAccessError):
    pass


class TargetRateLimitedError(TargetAccessError):
    pass


class TargetCircuitOpenError(TargetAccessError):
    pass


class RobotsDeniedError(TargetAccessError):
    pass


@dataclass
class _DomainState:
    semaphore: asyncio.Semaphore
    spacing_lock: asyncio.Lock
    robots_lock: asyncio.Lock
    base_interval: float
    current_interval: float
    next_request_at: float = 0.0
    consecutive_blocks: int = 0
    circuit_open_until: float = 0.0
    robots_loaded: bool = False
    robots_parser: robotparser.RobotFileParser | None = None
    robots_text: str | None = None


class SafeHttpClient:
    """SSRF-safe client with polite target throttling and block detection."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_dns: bool = True,
    ) -> None:
        self.settings = settings
        self._resolve_dns = resolve_dns
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))
        self._states: dict[str, _DomainState] = {}
        self._total_requests = 0
        self._retries = 0
        self._rate_limited_responses = 0
        self._block_events = 0
        self._robots_denied = 0
        self._events: list[TargetAccessEvent] = []
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/json,"
                    "application/xml;q=0.9,*/*;q=0.5"
                ),
            },
        )

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    @staticmethod
    def _domain_key(url: str) -> str:
        parts = urlsplit(url)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{port}"

    def _state_for(self, url: str) -> _DomainState:
        key = self._domain_key(url)
        state = self._states.get(key)
        if state is None:
            requests_per_second = max(0.0, self.settings.target_requests_per_second)
            interval = 1.0 / requests_per_second if requests_per_second else 0.0
            state = _DomainState(
                semaphore=asyncio.Semaphore(
                    max(1, self.settings.target_max_concurrency_per_host)
                ),
                spacing_lock=asyncio.Lock(),
                robots_lock=asyncio.Lock(),
                base_interval=interval,
                current_interval=interval,
            )
            self._states[key] = state
        return state

    def access_report(self) -> TargetAccessReport:
        now = asyncio.get_running_loop().time()
        intervals = [state.current_interval for state in self._states.values()]
        return TargetAccessReport(
            total_requests=self._total_requests,
            retries=self._retries,
            rate_limited_responses=self._rate_limited_responses,
            block_events=self._block_events,
            robots_denied=self._robots_denied,
            effective_min_interval_seconds=max(intervals, default=0.0),
            circuit_open=any(state.circuit_open_until > now for state in self._states.values()),
            events=list(self._events),
        )

    def _append_event(self, event: TargetAccessEvent) -> None:
        self._events.append(event)
        if len(self._events) > 100:
            del self._events[:-100]

    def _check_circuit(self, url: str, state: _DomainState) -> None:
        now = asyncio.get_running_loop().time()
        if state.circuit_open_until > now:
            remaining = state.circuit_open_until - now
            raise TargetCircuitOpenError(
                f"Target circuit is open for {remaining:.1f}s after repeated access blocks: {url}",
                self.access_report(),
            )
        if state.circuit_open_until:
            state.circuit_open_until = 0.0
            state.consecutive_blocks = 0

    def _check_robots(self, url: str, state: _DomainState) -> None:
        if not self.settings.robots_obey or state.robots_parser is None:
            return
        if state.robots_parser.can_fetch(self.settings.robots_user_agent, url):
            return
        self._robots_denied += 1
        self._append_event(TargetAccessEvent(kind=AccessEventKind.ROBOTS_DENIED, url=url))
        raise RobotsDeniedError(
            f"robots.txt does not permit {self.settings.robots_user_agent} to fetch {url}",
            self.access_report(),
        )

    async def _schedule_request(self, state: _DomainState) -> None:
        loop = asyncio.get_running_loop()
        async with state.spacing_lock:
            now = loop.time()
            jitter = random.uniform(0.0, max(0.0, self.settings.target_jitter_seconds))
            scheduled = max(now, state.next_request_at) + jitter
            state.next_request_at = scheduled + state.current_interval
        delay = scheduled - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _defer_domain(self, state: _DomainState, delay: float) -> None:
        async with state.spacing_lock:
            not_before = asyncio.get_running_loop().time() + max(0.0, delay)
            state.next_request_at = max(state.next_request_at, not_before)

    def _slow_down(
        self, state: _DomainState, *, retry_after: float | None, transient: bool = False
    ) -> None:
        multiplier = 1.5 if transient else 2.0
        candidate = max(state.base_interval, state.current_interval * multiplier)
        if retry_after is not None:
            candidate = max(candidate, retry_after)
        state.current_interval = min(
            max(0.0, self.settings.target_backoff_cap_seconds), candidate
        )

    @staticmethod
    def _recover(state: _DomainState) -> None:
        state.consecutive_blocks = 0
        state.current_interval = max(state.base_interval, state.current_interval * 0.9)

    def _record_block(
        self,
        url: str,
        state: _DomainState,
        *,
        kind: AccessEventKind,
        status_code: int,
        provider: str | None,
        confidence: float,
    ) -> None:
        self._block_events += 1
        state.consecutive_blocks += 1
        self._append_event(
            TargetAccessEvent(
                kind=kind,
                url=url,
                status_code=status_code,
                provider=provider,
                confidence=confidence,
            )
        )
        if state.consecutive_blocks >= max(1, self.settings.target_block_threshold):
            state.circuit_open_until = (
                asyncio.get_running_loop().time()
                + max(0.0, self.settings.target_circuit_break_seconds)
            )

    async def load_robots_policy(self, base_url: str) -> str | None:
        base_url = normalize_url(base_url)
        state = self._state_for(base_url)
        async with state.robots_lock:
            if state.robots_loaded:
                return None
            if not self.settings.robots_obey:
                state.robots_loaded = True
                return None
            robots_url = urljoin(base_url, "/robots.txt")
            try:
                response = await self.get(robots_url, respect_robots=False)
            except TargetAccessError:
                raise
            except Exception as exc:
                state.robots_loaded = True
                return f"robots.txt could not be read; configured base throttle remains active: {exc}"

            state.robots_loaded = True
            if not response.is_success:
                return None
            state.robots_text = response.text
            parser = robotparser.RobotFileParser(robots_url)
            parser.parse(response.text.splitlines())
            state.robots_parser = parser

            crawl_delay = parser.crawl_delay(self.settings.robots_user_agent)
            if crawl_delay is None:
                crawl_delay = parser.crawl_delay("*")
            request_rate = parser.request_rate(self.settings.robots_user_agent)
            if request_rate is None:
                request_rate = parser.request_rate("*")
            policy_interval = float(crawl_delay or 0)
            if request_rate and request_rate.requests > 0:
                policy_interval = max(
                    policy_interval,
                    float(request_rate.seconds) / float(request_rate.requests),
                )
            state.base_interval = max(state.base_interval, policy_interval)
            state.current_interval = max(state.current_interval, state.base_interval)
            return None

    def get_cached_robots_text(self, base_url: str) -> str | None:
        return self._state_for(normalize_url(base_url)).robots_text

    async def _request_once(
        self, url: str, *, respect_robots: bool
    ) -> tuple[httpx.Response, _DomainState]:
        state = self._state_for(url)
        if respect_robots and self.settings.robots_obey and not state.robots_loaded:
            await self.load_robots_policy(url)
        self._check_circuit(url, state)
        if respect_robots:
            self._check_robots(url, state)
        if self._resolve_dns:
            await asyncio.to_thread(validate_resolved_host, url)
        await self._schedule_request(state)

        async with self._semaphore:
            async with state.semaphore:
                self._check_circuit(url, state)
                self._total_requests += 1
                async with self._client.stream("GET", url) as response:
                    content_length = response.headers.get("content-length")
                    try:
                        declared_size = int(content_length) if content_length else None
                    except ValueError:
                        declared_size = None
                    if (
                        declared_size is not None
                        and declared_size > self.settings.http_max_response_bytes
                    ):
                        raise ResponseTooLargeError(
                            f"Response is larger than configured limit: {url}"
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.http_max_response_bytes:
                            raise ResponseTooLargeError(
                                f"Response exceeded configured limit: {url}"
                            )
                        chunks.append(chunk)

                    return (
                        httpx.Response(
                            status_code=response.status_code,
                            headers=response.headers,
                            content=b"".join(chunks),
                            request=response.request,
                            extensions=response.extensions,
                        ),
                        state,
                    )

    async def get(self, url: str, *, respect_robots: bool = True) -> httpx.Response:
        current = normalize_url(url)
        redirects = 0
        retries = 0
        while True:
            response, state = await self._request_once(current, respect_robots=respect_robots)
            detection = detect_target_response(
                response.status_code, response.headers, response.content
            )
            retry_after = parse_retry_after(response.headers)

            if detection and detection.kind == AccessEventKind.RATE_LIMITED:
                self._rate_limited_responses += 1
                self._append_event(
                    TargetAccessEvent(
                        kind=detection.kind,
                        url=current,
                        status_code=response.status_code,
                        provider=detection.provider,
                        confidence=detection.confidence,
                        retry_after_seconds=retry_after,
                    )
                )
                self._slow_down(state, retry_after=retry_after)
                wait_too_long = (
                    retry_after is not None
                    and retry_after > max(0.0, self.settings.target_max_retry_after_seconds)
                )
                if retries >= max(0, self.settings.target_max_retries) or wait_too_long:
                    detail = (
                        f"; target requested a {retry_after:.1f}s wait"
                        if retry_after is not None
                        else ""
                    )
                    raise TargetRateLimitedError(
                        f"Target rate limit persisted after {retries} retries{detail}: {current}",
                        self.access_report(),
                    )
                delay = retry_after
                if delay is None:
                    delay = min(
                        max(0.0, self.settings.target_backoff_cap_seconds),
                        max(0.0, self.settings.target_backoff_base_seconds) * (2**retries),
                    )
                retries += 1
                self._retries += 1
                await self._defer_domain(state, delay)
                continue

            if detection and detection.kind in {
                AccessEventKind.BOT_CHALLENGE,
                AccessEventKind.ACCESS_DENIED,
            }:
                self._record_block(
                    current,
                    state,
                    kind=detection.kind,
                    status_code=response.status_code,
                    provider=detection.provider,
                    confidence=detection.confidence,
                )
                provider = f" ({detection.provider})" if detection.provider else ""
                raise TargetBlockedError(
                    f"Target returned {detection.kind.value}{provider}; access-control bypass "
                    f"is not attempted: {current}",
                    self.access_report(),
                )

            if response.status_code in {502, 503, 504}:
                self._append_event(
                    TargetAccessEvent(
                        kind=AccessEventKind.TRANSIENT_ERROR,
                        url=current,
                        status_code=response.status_code,
                        retry_after_seconds=retry_after,
                    )
                )
                self._slow_down(state, retry_after=retry_after, transient=True)
                if retries < max(0, self.settings.target_max_retries):
                    delay = retry_after
                    if delay is None:
                        delay = min(
                            max(0.0, self.settings.target_backoff_cap_seconds),
                            max(0.0, self.settings.target_backoff_base_seconds) * (2**retries),
                        )
                    if delay <= max(0.0, self.settings.target_max_retry_after_seconds):
                        retries += 1
                        self._retries += 1
                        await self._defer_domain(state, delay)
                        continue

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise httpx.HTTPStatusError(
                        "Redirect did not provide a location",
                        request=response.request,
                        response=response,
                    )
                redirects += 1
                if redirects > self.settings.http_max_redirects:
                    raise UnsafeUrlError("Too many redirects")
                current = normalize_url(urljoin(current, location))
                retries = 0
                self._recover(state)
                continue

            if response.is_success and rate_limit_exhausted(response.headers) and retry_after is not None:
                self._rate_limited_responses += 1
                self._append_event(
                    TargetAccessEvent(
                        kind=AccessEventKind.RATE_LIMITED,
                        url=current,
                        status_code=response.status_code,
                        retry_after_seconds=retry_after,
                    )
                )
                self._slow_down(state, retry_after=retry_after)
                await self._defer_domain(state, retry_after)
            elif response.is_success:
                self._recover(state)
            return response

    async def get_json(self, url: str) -> tuple[object, httpx.Response]:
        response = await self.get(url)
        response.raise_for_status()
        return response.json(), response
