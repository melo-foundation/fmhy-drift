"""Unit tests for mandatory proxy validation, credential redaction, and egress verification."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fmhy_drift.proxy_guard import (
    ProxyBypassDetectedError,
    ProxyInvalidError,
    ProxyMissingError,
    redact_proxy_url,
    validate_proxy_url,
    verify_proxy_egress,
)


def test_validate_proxy_url_rejects_empty_and_bad_schemes() -> None:
    with pytest.raises(ProxyMissingError):
        validate_proxy_url(None)
    with pytest.raises(ProxyMissingError):
        validate_proxy_url("   ")
    with pytest.raises(ProxyInvalidError):
        validate_proxy_url("ftp://proxy.example.com:21")
    with pytest.raises(ProxyInvalidError):
        validate_proxy_url("socks5h://")

    assert (
        validate_proxy_url(" socks5h://user:secret@proxy.example.com:1080 ")
        == "socks5h://user:secret@proxy.example.com:1080"
    )


def test_redact_proxy_url_hides_credentials_and_ip_octets() -> None:
    redacted_ip = redact_proxy_url("socks5h://myuser:supersecret@198.51.100.42:1080")
    assert "myuser" not in redacted_ip
    assert "supersecret" not in redacted_ip
    assert "100.42" not in redacted_ip
    assert redacted_ip == "socks5h://***:***@198.51.x.x:1080"

    redacted_domain = redact_proxy_url("http://u:p@us-east.gate.provider.net:8080")
    assert "u:p" not in redacted_domain
    assert redacted_domain == "http://***:***@***.provider.net:8080"


def test_verify_proxy_egress_succeeds_when_distinct_from_host() -> None:
    proxy_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="203.0.113.99\n")
    )
    direct_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="192.0.2.10\n")
    )

    verification = asyncio.run(
        verify_proxy_egress(
            "socks5h://user:pass@203.0.113.99:1080",
            transport=proxy_transport,
            direct_transport=direct_transport,
        )
    )
    assert verification.verified_distinct_from_host is True
    assert "pass" not in verification.redacted_proxy
    assert len(verification.egress_ip_sha256_prefix) == 12


def test_verify_proxy_egress_aborts_if_proxy_leaks_runner_ip() -> None:
    same_ip_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="192.0.2.55\n")
    )

    with pytest.raises(ProxyBypassDetectedError, match="matches direct host/runner IP"):
        asyncio.run(
            verify_proxy_egress(
                "http://proxy.example.com:8080",
                transport=same_ip_transport,
                direct_transport=same_ip_transport,
            )
        )
