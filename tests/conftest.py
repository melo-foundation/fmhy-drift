"""Test-wide guard: no test may reach the real network (httpx.MockTransport only)."""

from __future__ import annotations

import socket

import pytest

_LOCAL = {"localhost", "127.0.0.1", "::1", None, ""}


class NetworkBlockedError(RuntimeError):
    pass


def _host_of(address: object) -> object:
    if isinstance(address, tuple) and address:
        return address[0]
    return None


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: object) -> object:
        if self.family in (socket.AF_INET, socket.AF_INET6) and _host_of(address) not in _LOCAL:
            raise NetworkBlockedError(f"test tried to connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> object:
        if self.family in (socket.AF_INET, socket.AF_INET6) and _host_of(address) not in _LOCAL:
            raise NetworkBlockedError(f"test tried to connect to {address!r}")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host not in _LOCAL:
            raise NetworkBlockedError(f"test tried to resolve {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
