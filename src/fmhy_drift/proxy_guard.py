"""Mandatory proxy validation and egress IP verification guard.

Ensures site liveness checks NEVER run directly from a GitHub Actions runner or local IP.
"""

from __future__ import annotations

import hashlib
import ipaddress
from urllib.parse import urlsplit, urlunsplit

import httpx

from fmhy_drift.models import ProxyVerification

ALLOWED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})

IP_ECHO_ENDPOINTS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
)


class ProxyGuardError(RuntimeError):
    """Base error raised when proxy enforcement fails."""


class ProxyMissingError(ProxyGuardError):
    """Raised when no proxy URL was provided."""


class ProxyInvalidError(ProxyGuardError):
    """Raised when the proxy URL is malformed or uses an unsupported scheme."""


class ProxyBypassDetectedError(ProxyGuardError):
    """Raised when proxy egress fails or resolves to the direct runner/host IP."""


def redact_proxy_url(proxy_url: str) -> str:
    """Redact credentials and mask host details so proxy secrets never leak into logs/reports."""
    try:
        parts = urlsplit(proxy_url.strip())
    except Exception:
        return "<redacted-proxy>"

    if not parts.scheme or not parts.hostname:
        return "<redacted-proxy>"

    host = parts.hostname
    # Partially mask literal IPv4/IPv6 addresses so private proxy IPs stay confidential
    try:
        ip_obj = ipaddress.ip_address(host)
        if ip_obj.version == 4:
            octets = host.split(".")
            masked_host = f"{octets[0]}.{octets[1]}.x.x"
        else:
            masked_host = "ipv6-proxy"
    except ValueError:
        # Domain name: keep registrable suffix or mask subdomain if multi-part
        labels = host.split(".")
        masked_host = f"***.{'.'.join(labels[-2:])}" if len(labels) > 2 else host

    netloc = masked_host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        netloc = f"***:***@{netloc}"

    return urlunsplit((parts.scheme.lower(), netloc, "", "", ""))


def validate_proxy_url(proxy_url: str | None) -> str:
    """Validate that a non-empty, well-formed proxy URL was configured."""
    if proxy_url is None or not proxy_url.strip():
        raise ProxyMissingError(
            "No proxy URL configured (set PROXY_URL or pass --proxy). "
            "Refusing to run FMHY site liveness checks directly from this machine/runner."
        )

    cleaned = proxy_url.strip()
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_PROXY_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_PROXY_SCHEMES))
        raise ProxyInvalidError(
            f"Unsupported proxy scheme {parts.scheme!r}. Expected one of: {allowed}"
        )
    if not parts.hostname:
        raise ProxyInvalidError("Proxy URL is missing a hostname.")

    return cleaned


async def _fetch_public_ip(
    client: httpx.AsyncClient,
    endpoints: tuple[str, ...] = IP_ECHO_ENDPOINTS,
) -> str | None:
    """Query public IP echo services and return the first valid IP address."""
    for endpoint in endpoints:
        try:
            resp = await client.get(
                endpoint,
                headers={"User-Agent": "curl/8.7.1", "Accept": "text/plain"},
            )
            if resp.status_code == 200:
                candidate = resp.text.strip()
                ipaddress.ip_address(candidate)
                return candidate
        except Exception:
            continue
    return None


async def verify_proxy_egress(
    proxy_url: str,
    *,
    timeout: float = 15.0,
    compare_direct_ip: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
    direct_transport: httpx.AsyncBaseTransport | None = None,
) -> ProxyVerification:
    """Verify the proxy works and does NOT leak the direct host/runner public IP."""
    validated = validate_proxy_url(proxy_url)
    redacted = redact_proxy_url(validated)

    proxy_client_kwargs: dict = {
        "timeout": httpx.Timeout(timeout),
        "follow_redirects": True,
        "trust_env": False,
    }
    if transport is not None:
        proxy_client_kwargs["transport"] = transport
    else:
        proxy_client_kwargs["proxy"] = validated

    async with httpx.AsyncClient(**proxy_client_kwargs) as proxy_client:
        proxy_ip = await _fetch_public_ip(proxy_client)

    if not proxy_ip:
        raise ProxyBypassDetectedError(
            f"Proxy preflight failed ({redacted}): could not reach any IP verification endpoint "
            "through the configured proxy."
        )

    verified_distinct = False
    if compare_direct_ip:
        direct_kwargs: dict = {
            "timeout": httpx.Timeout(min(timeout, 8.0)),
            "follow_redirects": True,
            "trust_env": False,
        }
        if direct_transport is not None:
            direct_kwargs["transport"] = direct_transport

        async with httpx.AsyncClient(**direct_kwargs) as direct_client:
            direct_ip = await _fetch_public_ip(direct_client)

        if direct_ip is not None:
            if ipaddress.ip_address(proxy_ip) == ipaddress.ip_address(direct_ip):
                raise ProxyBypassDetectedError(
                    f"Proxy preflight aborted ({redacted}): proxy egress IP matches direct "
                    "host/runner IP! Refusing to scan sites without real proxy isolation."
                )
            verified_distinct = True
        else:
            # Direct egress is blocked/firewalled while proxy egress succeeded -> isolated!
            verified_distinct = True

    ip_hash = hashlib.sha256(proxy_ip.encode("utf-8")).hexdigest()[:12]
    return ProxyVerification(
        redacted_proxy=redacted,
        egress_ip_sha256_prefix=ip_hash,
        verified_distinct_from_host=verified_distinct,
    )
