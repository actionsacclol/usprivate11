"""What a network drop writes to the log.

Live test 2026-11-07 to 11-09: 460 WARNING/ERROR lines in about 30 hours, nearly
all from a few network drops. Each drop wrote one "GET failed" line per market
slug, a websockets traceback per feed, and nothing from the feeds themselves.
Log lines also carried local time with no zone, against a database in UTC.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import pytest

import polymarket_api as pa
import service
import spot_ws


def test_log_lines_are_stamped_in_utc_with_a_z():
    rec = logging.LogRecord("trader", logging.INFO, __file__, 1, "hello", None, None)
    rec.created, rec.msecs = 0.0, 0.0
    assert service._log_formatter().format(rec) == "[1970-01-01 00:00:00Z] INFO    trader: hello"


def _record(exc):
    try:
        raise exc
    except BaseException:  # noqa: BLE001
        info = sys.exc_info()
    return logging.LogRecord("websockets.client", logging.ERROR, __file__, 1,
                             "data transfer failed", None, info)


def test_a_dropped_link_traceback_is_filtered_and_a_real_error_is_not():
    f = service._DroppedLinkFilter()
    assert f.filter(_record(OSError(121, "The semaphore timeout period has expired"))) is False
    assert f.filter(_record(TypeError("a bug"))) is True
    plain = logging.LogRecord("websockets.client", logging.ERROR, __file__, 1, "x", None, None)
    assert f.filter(plain) is True


def test_one_get_failed_line_per_endpoint_not_per_slug(monkeypatch, caplog):
    monkeypatch.setattr(pa, "_get_fail_log_at", {})
    base = "https://gateway.polymarket.us/v1/events/slug/"
    with caplog.at_level(logging.WARNING, logger=pa.logger.name):
        pa._log_get_failed(base + "btc-updown-15m-2026-11-07-0600z", TimeoutError())
        pa._log_get_failed(base + "eth-updown-15m-2026-11-07-0600z", TimeoutError())
        pa._log_get_failed("https://gateway.polymarket.us/v1/asset-prices/history?asset=btc",
                           TimeoutError())
    lines = [r.getMessage() for r in caplog.records if "GET failed" in r.getMessage()]
    assert len(lines) == 2 and "btc-updown" in lines[0] and "asset-prices" in lines[1]


def test_spot_feed_backoff_starts_over_after_a_link_that_was_up(monkeypatch):
    """The count reset only when the socket closed cleanly, which a drop never
    does, so the logged waits climbed to about a minute and stayed there."""
    feed = spot_ws._Client()
    waits: list[float] = []
    calls = {"n": 0}

    async def _connect_once():
        calls["n"] += 1
        if calls["n"] > 6:
            feed._stop = True
            return
        feed.connected = True          # it connected, then the link dropped
        raise ConnectionError("dropped")

    async def _sleep(sec):
        waits.append(sec)

    monkeypatch.setattr(feed, "_connect_once", _connect_once)
    monkeypatch.setattr(spot_ws.asyncio, "sleep", _sleep)
    monkeypatch.setattr(spot_ws.random, "uniform", lambda a, b: 1.0)
    feed._stop = False
    asyncio.run(feed._run())
    assert waits == [2.0] * 6, waits


@pytest.mark.parametrize("was_up, level", [(True, logging.WARNING), (False, logging.DEBUG)])
def test_a_book_feed_drop_is_one_warning_and_a_failed_retry_is_quiet(
        monkeypatch, caplog, was_up, level):
    import clob_ws
    import websockets
    import ws_ssl
    feed = type(clob_ws.feed)()
    feed._tokens = {"some-market#yes": 1.0}
    monkeypatch.setattr(clob_ws.auth, "credentials_present", lambda *_a: True)
    monkeypatch.setattr(clob_ws.auth, "auth_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(ws_ssl, "client_context", lambda: None)

    class _Conn:
        async def __aenter__(self):
            if not was_up:
                raise OSError("no route")
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, _frame):
            return None

    async def _recv_loop(_ws):
        feed._stop = True
        raise ConnectionError("link dropped")

    async def _no_sleep(_sec):
        feed._stop = True

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(feed, "_recv_loop", _recv_loop)
    monkeypatch.setattr(clob_ws.asyncio, "sleep", _no_sleep)
    with caplog.at_level(logging.DEBUG, logger=clob_ws.logger.name):
        asyncio.run(feed._run())
    mine = [r for r in caplog.records if r.name == clob_ws.logger.name]
    assert [r.levelno for r in mine] == [level], [r.getMessage() for r in mine]
