"""Unit tests for RPC URL SSRF validation (AUD-322).

Covers the bypasses called out in AUD-314/AUD-322: decimal/hex/octal IP
literal encodings and IPv6-mapped IPv4 addresses all resolve to the same
loopback/private address a plain dotted-quad regex would catch, so they must
be rejected too. DNS-name cases are exercised by monkeypatching
socket.getaddrinfo so the suite never depends on live network access.
"""

from __future__ import annotations

import socket

import pytest

from audr.providers.rpc_targets import (
    RpcUrlError,
    validate_rpc_url,
    validate_rpc_url_async,
)

pytestmark = pytest.mark.unit


class TestBasicUrlShape:
    def test_empty_url_rejected(self) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url("")

    def test_non_http_scheme_rejected(self) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url("ftp://example.com")

    def test_no_hostname_rejected(self) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url("http:///path")

    def test_trailing_slash_is_stripped(self) -> None:
        assert validate_rpc_url("http://8.8.8.8/") == "http://8.8.8.8"


class TestDirectIpLiterals:
    """These never touch the network — libc parses them locally."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://192.168.1.1/",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://169.254.1.1/",
            "http://[::1]/",
        ],
    )
    def test_rejects_direct_private_ip(self, url: str) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url(url)

    def test_allows_public_ip_literal(self) -> None:
        assert validate_rpc_url("http://8.8.8.8/") == "http://8.8.8.8"

    def test_allow_private_hosts_bypasses_check(self) -> None:
        assert (
            validate_rpc_url("http://127.0.0.1/", allow_private_hosts=True)
            == "http://127.0.0.1"
        )


class TestObfuscatedIpLiterals:
    """AUD-322: a hostname-only regex misses these encodings of 127.0.0.1."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://2130706433/",  # decimal
            "http://0x7f000001/",  # hex
            "http://0177.0.0.1/",  # octal first octet
            "http://127.1/",  # short form
        ],
    )
    def test_rejects_obfuscated_loopback(self, url: str) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url(url)

    def test_rejects_ipv6_mapped_ipv4_loopback(self) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url("http://[::ffff:127.0.0.1]/")

    def test_rejects_ipv6_mapped_ipv4_private_range(self) -> None:
        with pytest.raises(RpcUrlError):
            validate_rpc_url("http://[::ffff:10.0.0.5]/")


class TestDnsNames:
    """Mock the resolver so these never depend on live network access."""

    def test_dns_name_resolving_to_private_ip_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_getaddrinfo(host: str, *_a: object, **_kw: object) -> list:
            return [(socket.AF_INET, None, None, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
        with pytest.raises(RpcUrlError):
            validate_rpc_url("http://rebound.attacker.example/")

    def test_dns_name_resolving_to_public_ip_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_getaddrinfo(host: str, *_a: object, **_kw: object) -> list:
            return [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
        assert (
            validate_rpc_url("https://mainnet.example.com/")
            == "https://mainnet.example.com"
        )

    def test_unresolvable_dns_name_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient DNS/network outage must not permanently block a
        legitimate public hostname — numeric IP obfuscation is still always
        caught since it never depends on network (see TestObfuscatedIpLiterals)."""

        def _fake_getaddrinfo(host: str, *_a: object, **_kw: object) -> None:
            raise socket.gaierror("Temporary failure in name resolution")

        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
        assert (
            validate_rpc_url("https://mainnet.example.com/")
            == "https://mainnet.example.com"
        )


class TestAsyncWrapper:
    @pytest.mark.unit
    async def test_validate_rpc_url_async_matches_sync(self) -> None:
        assert (
            await validate_rpc_url_async("http://8.8.8.8/")
        ) == "http://8.8.8.8"

    @pytest.mark.unit
    async def test_validate_rpc_url_async_raises_for_private_host(self) -> None:
        with pytest.raises(RpcUrlError):
            await validate_rpc_url_async("http://127.0.0.1/")
