"""Pytest bootstrap for the Krypt PolyBot US backend.

The backend modules use top-level imports (`import db`, `import trader`,
…) and expect to run with the `python/` directory on `sys.path`. This
conftest lives in `python/` so pytest puts that directory on the path
for us, and it redirects all on-disk state (the SQLite DB, the rotating
log files, credential files) into a throwaway temp directory so the
suite never touches the user's real `<userData>` tree. It also keeps the
suite off the real network: only loopback connections are allowed.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path

# 1. Make `import db`, `import trader`, etc. resolve from python/.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 2. Point every file-writing module at an isolated scratch dir. db.py,
#    polymarket_auth.py, and service.py all key off KRYPT_POLYBOT_US_USERDATA,
#    so setting it before those modules import keeps the suite hermetic.
_SCRATCH = tempfile.mkdtemp(prefix="krypt-test-")
os.environ.setdefault("KRYPT_POLYBOT_US_USERDATA", _SCRATCH)
# The vault too. db.db_path() restores the latest backup from the vault when
# the DB file is missing, so without this the suite silently ran against a
# copy of the developer's REAL database (schema, fills, resolutions) on any
# machine that had one — and against an empty file on a clean machine (CI),
# where every test that used the default path failed with "no such table".
os.environ.setdefault("KRYPT_POLYBOT_US_VAULT", os.path.join(_SCRATCH, "vault"))

import pytest  # noqa: E402

# 3. No test reaches the real network. A test that did was slow, flaky offline
#    and on CI, and could talk to a live exchange. Every outbound path is
#    patched once, for the whole session (so a thread a test left running is
#    covered too): DNS lookups and connects to anything but loopback raise
#    NetworkBlocked, and the `_no_network` fixture fails the test that tried,
#    naming the host — even if the code swallowed the error. A deliberate live
#    check opts out with @pytest.mark.network.


class NetworkBlocked(OSError):
    """A test tried to reach a host other than this machine."""


_NET: dict = {"allowed": False, "reached": []}


def _is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    h = str(host).strip("[]").rstrip(".").lower()
    if h in ("", "localhost") or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return False
    # The unspecified address is a bind (or, on Linux, this machine).
    return ip.is_loopback or ip.is_unspecified


def _guard(host, port, what: str) -> None:
    if _NET["allowed"] or _is_local(host):
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    where = f"{host}:{port}" if port is not None else str(host)
    _NET["reached"].append(f"{what} {where}")
    raise NetworkBlocked(f"tests may not reach the network ({what} {where}); stub it, or "
                         "mark a deliberate live check @pytest.mark.network")


def _install_network_guard() -> None:
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, port, *a, **kw):
        _guard(host, port, "DNS lookup of")
        return real_getaddrinfo(host, port, *a, **kw)

    def create_connection(address, *a, **kw):
        _guard(address[0], address[1], "connect to")
        return real_create_connection(address, *a, **kw)

    def connect(self, address):
        if isinstance(address, tuple):
            _guard(address[0], address[1], "connect to")
        return real_connect(self, address)

    def connect_ex(self, address):
        if isinstance(address, tuple):
            _guard(address[0], address[1], "connect to")
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex

    # asyncio skips getaddrinfo for a numeric host, and the Windows proactor
    # loop connects with ConnectEx rather than socket.connect.
    import asyncio.base_events
    import asyncio.proactor_events
    import asyncio.selector_events
    real_loop_connect = asyncio.base_events.BaseEventLoop.create_connection

    async def loop_create_connection(self, protocol_factory, host=None, port=None, *a, **kw):
        if host is not None:
            _guard(host, port, "connect to")
        return await real_loop_connect(self, protocol_factory, host, port, *a, **kw)

    def guarded_sock_connect(real):
        async def sock_connect(self, sock, address):
            if isinstance(address, tuple):
                _guard(address[0], address[1], "connect to")
            return await real(self, sock, address)
        return sock_connect

    asyncio.base_events.BaseEventLoop.create_connection = loop_create_connection
    for cls in (asyncio.proactor_events.BaseProactorEventLoop,
                asyncio.selector_events.BaseSelectorEventLoop):
        cls.sock_connect = guarded_sock_connect(cls.sock_connect)


_install_network_guard()


@pytest.fixture(autouse=True)
def _no_network(request):
    """Fail any test that tried to reach a non-loopback host (see above)."""
    _NET["allowed"] = request.node.get_closest_marker("network") is not None
    _NET["reached"] = reached = []
    yield
    _NET["allowed"] = False
    if reached:
        pytest.fail("this test tried to reach the network: "
                    + "; ".join(dict.fromkeys(reached))
                    + ". Stub it, or mark a deliberate live check @pytest.mark.network.",
                    pytrace=False)


def pytest_sessionstart(session):
    """Give the default scratch DB its schema once, so a test that uses the
    default path (rather than its own tmp_path) never depends on test order
    or on a vault backup to find its tables."""
    import db
    db.init_db()
    db.close_thread_connection()


@pytest.fixture(autouse=True)
def _drop_cached_db_connection():
    """Per-thread connection reuse is keyed by path; drop the main thread's
    cached handle after every test so no test inherits another's open
    connection (or a stale view of a file another test replaced)."""
    yield
    try:
        import db
        db.close_thread_connection()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _hermetic_market_meta(monkeypatch):
    """`trader.execute_signal` now looks up a market's minimum order size via
    get_market_meta (a Gamma fetch on a cache miss). Default it to None for the
    whole suite so tests never touch the network; the min-size-bump tests
    override `trader.get_market_meta` themselves."""
    try:
        import trader

        async def _no_meta(_ticker):
            return None

        monkeypatch.setattr(trader, "get_market_meta", _no_meta, raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _hermetic_quote(request, monkeypatch):
    """get_quote hits the CLOB /price endpoint. Default it to an empty quote so
    the crypto15m live entry-gate (real-ask check + maker re-price) is a no-op
    in tests; tests that exercise it override get_quote themselves. Skip the
    polymarket_api module, which tests get_quote's real implementation."""
    if getattr(request.module, "__name__", "").endswith("test_polymarket_api"):
        return
    try:
        import polymarket_api

        async def _no_quote(_ticker, _side):
            return {"bid_cents": None, "ask_cents": None, "age_sec": 0.0}

        monkeypatch.setattr(polymarket_api, "get_quote", _no_quote, raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _hermetic_balance(request, monkeypatch):
    """get_balance hits the CLOB account endpoint. The crypto15m live tick now
    pulls the balance to size affordably, so default it to an empty account
    (balance 0) for the whole suite — tests that need a real balance override
    `trader.refresh_balance` or set `start_bankroll_usd`. Skip the
    polymarket_api module, which tests get_balance's real implementation."""
    if getattr(request.module, "__name__", "").endswith("test_polymarket_api"):
        return
    try:
        import polymarket_api
        import trader

        async def _no_balance():
            # RAISE, don't return 0: a successful $0 read is now a CONFIRMED
            # empty account and entries correctly skip (the real-zero fix).
            # What this fixture models is "no balance endpoint in tests" —
            # an UNKNOWN balance — which is the failure path, so sizing falls
            # back to start_bankroll_usd exactly as before.
            raise RuntimeError("hermetic test env: no balance endpoint")

        # trader imports get_balance by name, so patch BOTH bindings.
        monkeypatch.setattr(polymarket_api, "get_balance", _no_balance, raising=False)
        monkeypatch.setattr(trader, "get_balance", _no_balance, raising=False)
        # Per-test isolation for the read-ok flag the real-zero fix consults —
        # a cached successful read from one test must not leak into the next.
        monkeypatch.setattr(trader, "_balance_cache", {}, raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _hermetic_clock_sync(monkeypatch):
    """ensure_api_creds syncs the clock against the CLOB's /time before it
    derives credentials. With no wallet in tests the derive then fails
    locally, so that sync is the only network call on the path every
    unstubbed authed request takes. Make it a no-op."""
    try:
        import polymarket_api

        async def _no_sync():
            return None

        monkeypatch.setattr(polymarket_api, "_sync_clock_offset", _no_sync, raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_agent_rate_limits():
    """The agents' per-minute rate limiter (mcp_server._agent_hits / _hits) is
    process state: without a reset, the calls of every earlier test count
    against a later one, and a suite with enough agent tests trips the limit
    in whichever test happens to run past it."""
    import sys
    mod = sys.modules.get("mcp_server")
    if mod is not None:
        mod._agent_hits.clear()
        mod._hits.clear()
    yield
