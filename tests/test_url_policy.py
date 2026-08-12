"""Tests for public-web navigation and redirect policy enforcement."""

import ipaddress
from typing import Any

import httpx
import pytest

from browser_mcp.security import ProxyAwareResolver, PublicUrlPolicy, UrlPolicyError
from browser_mcp.security import url_policy as url_policy_module


@pytest.mark.asyncio
async def test_policy_allows_only_global_dns_answers() -> None:
    """A public hostname is safe only when every returned address is globally routable."""

    async def public_resolver(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Return one documentation network address for the expected host and port."""
        assert hostname == "example.com"
        assert port == 443
        return (ipaddress.ip_address("93.184.216.34"),)

    assert await PublicUrlPolicy(public_resolver).validate("https://example.com/path") == (
        "https://example.com/path"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://10.0.0.8",
        "http://[::1]/",
        "file:///etc/passwd",
        "https://user:secret@example.com",
    ],
)
async def test_policy_rejects_local_schemes_addresses_and_credentials(url: str) -> None:
    """Literal local targets and credential-bearing URLs must fail before DNS or Chrome."""
    with pytest.raises(UrlPolicyError):
        await PublicUrlPolicy().validate(url)


@pytest.mark.asyncio
async def test_policy_rejects_mixed_public_and_private_dns_answers() -> None:
    """One private answer is enough to block DNS rebinding and split-horizon names."""

    async def mixed_resolver(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Return a deliberately unsafe mixed DNS response."""
        del hostname, port
        return (
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("192.168.1.20"),
        )

    with pytest.raises(UrlPolicyError, match="non-public"):
        await PublicUrlPolicy(mixed_resolver).validate("https://example.com")


@pytest.mark.asyncio
async def test_proxy_fake_ip_is_verified_by_public_dns() -> None:
    """RFC 2544 proxy answers may use DoH, while ordinary private answers may not bypass policy."""
    doh_calls: list[str] = []

    async def fake_ip_resolver(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Emulate Clash-style fake-IP DNS."""
        del hostname, port
        return (ipaddress.ip_address("198.18.6.30"),)

    async def public_doh_resolver(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Return the publicly verified address and record fallback use."""
        del port
        doh_calls.append(hostname)
        return (ipaddress.ip_address("93.184.216.34"),)

    resolver = ProxyAwareResolver(fake_ip_resolver, public_doh_resolver)

    await PublicUrlPolicy(resolver).validate("https://example.com")
    await PublicUrlPolicy(resolver).validate("https://example.com/second")

    assert doh_calls == ["example.com"]


@pytest.mark.asyncio
async def test_private_system_dns_answer_never_uses_public_fallback() -> None:
    """The proxy exception must remain limited to 198.18/15 and never bless RFC 1918 targets."""

    async def private_resolver(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Return a split-horizon private service address."""
        del hostname, port
        return (ipaddress.ip_address("192.168.1.20"),)

    async def forbidden_doh(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Fail if the narrow fake-IP fallback is accidentally widened."""
        del hostname, port
        raise AssertionError("DoH must not run for RFC 1918 answers")

    resolver = ProxyAwareResolver(private_resolver, forbidden_doh)

    with pytest.raises(UrlPolicyError, match="non-public"):
        await PublicUrlPolicy(resolver).validate("https://internal.example")


@pytest.mark.asyncio
async def test_public_doh_retries_transient_failures_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full transient DoH round should retry without weakening public verification."""
    calls = 0

    async def flaky_query(
        client: Any,
        endpoint: str,
        hostname: str,
        record_name: str,
        record_type: int,
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Fail the first four concurrent requests, then return one public address."""
        nonlocal calls
        del client, endpoint, hostname, record_name, record_type
        calls += 1
        if calls <= 4:
            raise httpx.ReadTimeout("transient DoH timeout")
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr(url_policy_module, "_query_doh", flaky_query)

    addresses = await url_policy_module._resolve_public_doh(  # pyright: ignore[reportPrivateUsage]
        "example.com", 443
    )

    assert addresses == (ipaddress.ip_address("93.184.216.34"),)
    assert calls >= 5
