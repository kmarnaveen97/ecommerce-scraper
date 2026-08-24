from __future__ import annotations

import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    pass


_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_")


def normalize_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("Only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Credentials in URLs are not allowed")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    port = parsed.port
    netloc = f"{host}:{port}" if port else host
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def canonicalize_url(value: str) -> str:
    normalized = normalize_url(value)
    parsed = urlsplit(normalized)
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, urlencode(sorted(query)), ""))


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_hostname_syntax(url: str) -> None:
    host = urlsplit(normalize_url(url)).hostname
    if host is None or host.lower() in _BLOCKED_HOSTS or host.lower().endswith(".localhost"):
        raise UnsafeUrlError("Local or metadata hosts are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    if not is_public_ip(host):
        raise UnsafeUrlError("Private, loopback, link-local and reserved IPs are not allowed")


def validate_resolved_host(url: str) -> None:
    normalized = normalize_url(url)
    validate_hostname_syntax(normalized)
    parsed = urlsplit(normalized)
    assert parsed.hostname is not None
    try:
        addresses = {
            str(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Hostname could not be resolved: {parsed.hostname}") from exc
    if not addresses or any(not is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("Hostname resolves to a non-public IP address")


def same_site(left: str, right: str) -> bool:
    left_host = (urlsplit(normalize_url(left)).hostname or "").lower()
    right_host = (urlsplit(normalize_url(right)).hostname or "").lower()
    return (
        left_host == right_host
        or left_host.endswith(f".{right_host}")
        or right_host.endswith(f".{left_host}")
    )
