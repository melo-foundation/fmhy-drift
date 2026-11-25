"""Hostname and registrable-domain helpers backed by the Public Suffix List."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList

_PSL_ALL = PublicSuffixList()
_PSL_ICANN = PublicSuffixList(only_icann=True)

NON_PUBLIC_DNS_TLDS = (".onion", ".i2p", ".bit", ".eth", ".localhost", ".local")


def url_host(url: str) -> str:
    """Lowercased hostname of `url` without a trailing dot or a leading `www.`."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_public_dns_host(host: str) -> bool:
    """False for empty names, IP literals, and `.onion` / `.i2p` / other non-ICANN names."""
    return bool(host) and "." in host and not is_ip_literal(host) and not host.endswith(NON_PUBLIC_DNS_TLDS)


@lru_cache(maxsize=65536)
def registrable_domain(host: str) -> str | None:
    """ICANN registrable domain (`a.b.co.uk` -> `b.co.uk`), the unit that RDAP registers and expires.

    Returns `None` for IPs, non-public names, and bare public suffixes.
    """
    if not is_public_dns_host(host):
        return None
    return _PSL_ICANN.privatesuffix(host)


@lru_cache(maxsize=65536)
def is_shared_platform_host(host: str) -> bool:
    """True when `host` sits under a PSL *private* suffix (`*.github.io`, `*.vercel.app`, `*.pages.dev`...).

    RDAP / ownership checks on these describe the platform, not the listed site, so callers skip them.
    """
    if not is_public_dns_host(host):
        return False
    all_ps = _PSL_ALL.publicsuffix(host)
    icann_ps = _PSL_ICANN.publicsuffix(host)
    return bool(all_ps and icann_ps and all_ps != icann_ps)


def site_key(host: str) -> str:
    """Unit for per-site signals: the PSL-private registrable name (`user.github.io`, `example.com`)."""
    if not is_public_dns_host(host):
        return host
    return _PSL_ALL.privatesuffix(host) or host
