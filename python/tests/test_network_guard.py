"""The suite's network guard (conftest.py): a test may talk to this machine,
never to the internet. Each attempt here is made on purpose, so it is taken
back off the record before the guard's own check fails the test for it."""
from __future__ import annotations

import asyncio
import socket
import sys

import pytest

guard = sys.modules["conftest"]

_REMOTE = "203.0.113.7"            # TEST-NET-3: documentation only, never routed


@pytest.fixture
def attempts():
    reached = guard._NET["reached"]
    yield reached
    reached.clear()


def test_dns_lookups_of_real_hosts_are_blocked_and_named(attempts):
    with pytest.raises(guard.NetworkBlocked, match=r"example\.com:443"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(guard.NetworkBlocked, match="example.org"):
        socket.getaddrinfo(b"example.org", 443)       # httpx passes bytes
    assert attempts == ["DNS lookup of example.com:443", "DNS lookup of example.org:443"]


def test_connects_to_real_addresses_are_blocked(attempts):
    with pytest.raises(guard.NetworkBlocked, match=_REMOTE):
        socket.create_connection((_REMOTE, 443), timeout=1)
    s = socket.socket()
    try:
        with pytest.raises(guard.NetworkBlocked):
            s.connect((_REMOTE, 443))
        with pytest.raises(guard.NetworkBlocked):
            s.connect_ex((_REMOTE, 443))
    finally:
        s.close()

    async def _loop_connect():
        # A numeric host skips getaddrinfo, and the Windows proactor loop
        # connects without socket.connect: the loop path is guarded too.
        await asyncio.get_running_loop().create_connection(asyncio.Protocol, _REMOTE, 443)
    with pytest.raises(guard.NetworkBlocked):
        asyncio.run(_loop_connect())
    assert len(attempts) == 4


def test_loopback_stays_open(attempts):
    for host in ("localhost", "127.0.0.1", "::1", None):
        assert guard._is_local(host)
    assert not guard._is_local("polymarket.com") and not guard._is_local(_REMOTE)
    srv = socket.create_server(("127.0.0.1", 0))
    try:
        port = srv.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        socket.getaddrinfo("localhost", port)
    finally:
        srv.close()
    assert attempts == []


@pytest.mark.network
def test_a_live_check_opts_out_with_the_network_marker():
    assert guard._NET["allowed"] is True


def test_every_other_test_is_guarded():
    assert guard._NET["allowed"] is False
