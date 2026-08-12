"""Public-web URL policy shared by initial fetches and browser redirect guards."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast
from urllib.parse import SplitResult, urlsplit

import httpx

DEFAULT_HTTP_PORTS: Final = {"http": 80, "https": 443}
FAKE_IP_NETWORK: Final = ipaddress.ip_network("198.18.0.0/15")
DOH_ENDPOINTS: Final = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
DOH_TIMEOUT_SECONDS: Final = 5.0
DOH_RETRY_ATTEMPTS: Final = 3
FAKE_IP_VERIFICATION_TTL_SECONDS: Final = 30.0
Resolver = Callable[
    [str, int], Awaitable[tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]
]


class UrlPolicyError(ValueError):
    """Raised before Chrome may navigate to a non-public or malformed URL."""


class PublicUrlPolicy:
    """Allow only HTTP(S) hosts whose literal and resolved addresses are globally routable."""

    def __init__(self, resolver: Resolver | None = None) -> None:
        """Create a policy with an injectable DNS resolver for deterministic tests."""
        self._resolver = resolver or ProxyAwareResolver()

    async def validate(self, raw_url: str) -> str:
        """Validate syntax, credentials, port, and every DNS answer before navigation."""
        parsed = self._parse(raw_url)
        hostname = parsed.hostname
        if hostname is None:
            raise UrlPolicyError("URL must include a hostname")
        normalized_host = hostname.rstrip(".").lower()
        if not normalized_host:
            raise UrlPolicyError("URL hostname must not be empty")
        if parsed.username is not None or parsed.password is not None:
            raise UrlPolicyError("URL credentials are not allowed")
        try:
            port = parsed.port or DEFAULT_HTTP_PORTS[parsed.scheme]
        except ValueError as error:
            raise UrlPolicyError("URL contains an invalid port") from error

        literal = _parse_ip(normalized_host)
        addresses = (
            (literal,) if literal is not None else await self._resolver(normalized_host, port)
        )
        if not addresses:
            raise UrlPolicyError(f"hostname did not resolve: {normalized_host}")
        blocked = [str(address) for address in addresses if not address.is_global]
        if blocked:
            raise UrlPolicyError(
                f"URL resolves to a non-public address: {normalized_host} -> {', '.join(blocked)}"
            )
        return parsed.geturl()

    @staticmethod
    def _parse(raw_url: str) -> SplitResult:
        """Parse a URL while converting urllib edge-case failures to policy errors."""
        try:
            parsed = urlsplit(raw_url)
            _ = parsed.hostname
        except ValueError as error:
            raise UrlPolicyError(f"invalid URL: {error}") from error
        if parsed.scheme.lower() not in DEFAULT_HTTP_PORTS:
            raise UrlPolicyError("only http and https URLs are allowed")
        if not parsed.netloc:
            raise UrlPolicyError("URL must be absolute")
        return parsed


def _parse_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return a literal IP address while leaving ordinary DNS names unresolved."""
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


class ProxyAwareResolver:
    """Resolve normally, with a narrow DoH fallback for proxy-generated fake-IP answers."""

    def __init__(
        self,
        system_resolver: Resolver | None = None,
        doh_resolver: Resolver | None = None,
    ) -> None:
        """Allow deterministic replacement of both resolver paths in tests."""
        self._system_resolver = system_resolver or _resolve_system_host
        self._doh_resolver = doh_resolver or _resolve_public_doh
        self._verified_fake_ip_cache: dict[
            str, tuple[float, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]
        ] = {}
        self._lock = asyncio.Lock()

    async def __call__(
        self, hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Use DoH only when every system answer is inside RFC 2544's fake-IP range."""
        addresses = await self._system_resolver(hostname, port)
        if addresses and all(address in FAKE_IP_NETWORK for address in addresses):
            async with self._lock:
                now = time.monotonic()
                cached = self._verified_fake_ip_cache.get(hostname)
                if cached is not None and now - cached[0] < FAKE_IP_VERIFICATION_TTL_SECONDS:
                    return cached[1]
                verified = await self._doh_resolver(hostname, port)
                if not verified:
                    raise UrlPolicyError(
                        f"public DNS returned no address for proxy fake-IP hostname: {hostname}"
                    )
                self._verified_fake_ip_cache[hostname] = (now, verified)
                return verified
        return addresses


async def _resolve_system_host(
    hostname: str, port: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Resolve all TCP addresses without blocking the asyncio event loop."""
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise UrlPolicyError(f"hostname resolution failed: {hostname}: {error}") from error
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for _family, _type, _protocol, _canonical_name, sockaddr in records:
        address = ipaddress.ip_address(str(sockaddr[0]))
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return tuple(addresses)


async def _resolve_public_doh(
    hostname: str, port: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Resolve through concurrent public DoH fallbacks with bounded transient retries."""
    del port
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=DOH_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        for attempt in range(DOH_RETRY_ATTEMPTS):
            tasks = [
                asyncio.create_task(
                    _query_doh(client, endpoint, hostname, record_name, record_type)
                )
                for endpoint in DOH_ENDPOINTS
                for record_name, record_type in (("A", 1), ("AAAA", 28))
            ]
            try:
                for completed in asyncio.as_completed(tasks):
                    try:
                        addresses = await completed
                    except (httpx.HTTPError, ValueError, TypeError) as error:
                        last_error = error
                        continue
                    if addresses:
                        return addresses
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if attempt + 1 < DOH_RETRY_ATTEMPTS:
                await asyncio.sleep(0.15 * (attempt + 1))
    detail = f": {last_error}" if last_error is not None else ""
    raise UrlPolicyError(f"public DNS verification failed for {hostname}{detail}")


async def _query_doh(
    client: httpx.AsyncClient,
    endpoint: str,
    hostname: str,
    record_name: str,
    record_type: int,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Query and strictly parse one JSON DoH record family."""
    response = await client.get(
        endpoint,
        params={"name": hostname, "type": record_name},
        headers={"accept": "application/dns-json"},
    )
    response.raise_for_status()
    payload = cast(dict[str, Any], response.json())
    status = payload.get("Status")
    if status not in {0, 3}:
        raise ValueError(f"DoH returned DNS status {status}")
    raw_answers = payload.get("Answer", [])
    if not isinstance(raw_answers, list):
        raise TypeError("DoH Answer must be a list")
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw_answer in cast(list[object], raw_answers):
        if not isinstance(raw_answer, dict):
            continue
        answer = cast(dict[str, object], raw_answer)
        if answer.get("type") != record_type:
            continue
        data = answer.get("data")
        if isinstance(data, str):
            addresses.append(ipaddress.ip_address(data))
    return tuple(addresses)
