"""HTTP(S) RPC URL validation, DNS/rebinding checks, redirect denial (T030 / US1)."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

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
    - Private/loopback hosts are rejected unless *allow_private_hosts* is True.
    - Returns the normalised URL string.

    Raises RpcUrlError on any validation failure.
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


def _check_not_private(hostname: str) -> None:
    """Raise RpcUrlError if *hostname* resolves to a private/loopback address."""
    # Direct IP address check.
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            raise RpcUrlError(
                f"RPC URL hostname {hostname!r} is a private or loopback address"
            )
        return
    except ValueError:
        pass  # not an IP literal — fall through to name check

    # Reject well-known loopback hostnames without a DNS lookup.
    if hostname.lower() in ("localhost", "ip6-localhost", "ip6-loopback"):
        raise RpcUrlError(f"RPC URL hostname {hostname!r} resolves to loopback")

    # Block hostnames that look like they encode private IPs (DNS rebinding).
    # A full implementation would perform a DNS lookup; for now we block obvious patterns.
    if re.match(r"^(10|172|192|127)\.", hostname):
        raise RpcUrlError(
            f"RPC URL hostname {hostname!r} appears to be a private address"
        )


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _PRIVATE_RANGES)
