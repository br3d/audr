"""HTTP(S) RPC URL validation, DNS/rebinding checks, redirect denial (T030 / US1 / AUD-322)."""

from __future__ import annotations

import functools
import ipaddress
import socket
from urllib.parse import urlparse

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from audr.settings.integrations import get_integration

_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
]


class RpcUrlError(Exception):
    """Raised when an RPC URL fails validation."""


def validate_rpc_url(url: str, *, allow_private_hosts: bool = False) -> str:
    """Validate and normalise an Ethereum RPC URL.

    - Must use http:// or https://.
    - Must not redirect (redirect following is the caller's responsibility).
    - Private/loopback hosts are rejected unless *allow_private_hosts* is True,
      checked against the addresses the hostname actually resolves to — not
      just its literal spelling, so decimal/hex/octal IP encodings and
      IPv6-mapped IPv4 addresses can't slip past a hostname-only regex.
    - Returns the normalised URL string.

    Raises RpcUrlError on any validation failure. This performs a DNS
    resolution and therefore blocks; call via validate_rpc_url_async from
    async code.
    """
    if not url:
        raise RpcUrlError("RPC URL must not be empty")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RpcUrlError(f"RPC URL must use http or https, got {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise RpcUrlError("RPC URL has no hostname")

    if not allow_private_hosts:
        _check_not_private(hostname)

    # Strip trailing slash for consistency.
    return url.rstrip("/")


async def validate_rpc_url_async(url: str, *, allow_private_hosts: bool = False) -> str:
    """Async wrapper for validate_rpc_url — runs the blocking DNS lookup in a
    worker thread so it doesn't stall the event loop."""
    return await anyio.to_thread.run_sync(
        functools.partial(validate_rpc_url, url, allow_private_hosts=allow_private_hosts)
    )


def _check_not_private(hostname: str) -> None:
    """Raise RpcUrlError if *hostname* resolves to a private/loopback address.

    Resolves via socket.getaddrinfo — the same resolver httpx/asyncio use to
    open the connection — so this catches IP literals in any form the
    resolver accepts (dotted-decimal, bare decimal/hex/octal integers,
    IPv6-mapped IPv4) as well as DNS names that resolve to a private range.

    Numeric IP literals (in any of the forms above) resolve through libc's
    numeric-host fast path and never touch the network, so this check is
    reliable even when a host has no DNS egress. Genuine DNS names do need a
    live lookup; if that fails (offline host, transient DNS outage) we fail
    open rather than permanently blocking a legitimate public RPC endpoint —
    proportionate for a single-owner LAN deployment, not a hard guarantee
    against a determined attacker who controls DNS.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return

    for info in infos:
        ip_str = info[4][0].split("%", 1)[0]  # strip IPv6 zone id, if any
        addr = ipaddress.ip_address(ip_str)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if _is_private(addr):
            raise RpcUrlError(
                f"RPC URL hostname {hostname!r} resolves to a private or loopback"
                f" address ({ip_str})"
            )


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _PRIVATE_RANGES)


async def get_validated_rpc_url(session: AsyncSession) -> str | None:
    """Fetch the configured RPC URL and re-validate it against the private-host
    policy it was saved with, right before use.

    A URL is only checked once by default — at PUT /integrations/rpc time.
    Between then and a worker actually connecting, DNS for the configured
    hostname could be rebound to point at an internal address. Re-resolving
    here closes that window. Returns None if no RPC integration is configured.
    Raises RpcUrlError if the stored URL no longer passes validation.
    """
    integration = await get_integration(session, kind="rpc", decrypt_fields=True)
    if integration is None or not integration.url:
        return None
    return await validate_rpc_url_async(
        integration.url, allow_private_hosts=integration.allow_private_host
    )
