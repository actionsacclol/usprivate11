"""Indicator candles must exclude the in-progress minute.

Hyperliquid's candleSnapshot runs to `now`, so its last row is the CURRENT
minute, still forming — its "close" is the price a few seconds in. Including
it made every momentum field measure the elapsed fraction of a minute rather
than a whole one.

Observed 2026-07-25: at 23:24:02 UTC velocity1mPct computed +0.0000 while the
completed minute was -0.0062. Across a day of live ticks |velocity1mPct| was
~3x smaller than the same statistic on completed candles (median 0.0075 vs
0.0241), so a 0.1% threshold fired on 0.15% of ticks instead of ~6.9% — and
LIVE rows were not comparable with backfilled historical ones.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import crypto15m
import indicators


class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    async def post(self, *_a, **_k):
        return _FakeResp(self._rows)


def _candles(closes, *, last_in_progress: bool):
    """Minute candles ending now; optionally the last one still forming."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # One second ago, not the minute boundary: a candle opened ON the boundary
    # closes when the minute rolls over, so a run that straddled it saw the
    # "in-progress" candle as closed (a ~1-in-60 flake).
    minute = now_ms - 1_000
    rows = []
    n = len(closes)
    for i, c in enumerate(closes):
        # oldest first; the LAST candle opens on the current minute
        t = minute - (n - 1 - i) * 60_000
        rows.append({"t": t, "T": t + 59_999, "c": str(c), "v": "1"})
    if not last_in_progress:
        # shift everything back a minute so even the last one has closed
        for r in rows:
            r["t"] -= 60_000
            r["T"] -= 60_000
    return rows


def _closes(rows):
    got, _vol = asyncio.run(
        crypto15m._fetch_closes("BTC", _FakeClient(rows), 10))
    return got


def test_in_progress_candle_is_dropped():
    rows = _candles([100.0, 101.0, 102.0], last_in_progress=True)
    assert _closes(rows) == [100.0, 101.0]


def test_completed_candles_are_all_kept():
    rows = _candles([100.0, 101.0, 102.0], last_in_progress=False)
    assert _closes(rows) == [100.0, 101.0, 102.0]


def test_velocity_reflects_a_FULL_minute_not_a_partial_one():
    """The reported failure, end to end."""
    # 64385 -> 64381 is a completed -0.0062% minute; the in-progress candle
    # re-prints 64381, which used to make the change read 0.0000%.
    rows = _candles([64385.0, 64381.0, 64381.0], last_in_progress=True)
    vals = _closes(rows)
    v = indicators.pct_change(vals, 1)
    assert v is not None
    assert v == pytest.approx(-0.0062, abs=5e-4), v
    # …and with the partial candle included it would have been flat.
    naive = indicators.pct_change([64385.0, 64381.0, 64381.0], 1)
    assert naive == pytest.approx(0.0)


def test_rows_without_a_close_time_are_not_discarded():
    """Fail OPEN on an unexpected payload shape — a missing `T` must not
    silently empty the series and blank every indicator."""
    rows = [{"t": 0, "c": "100", "v": "1"}, {"t": 60_000, "c": "101", "v": "1"}]
    assert _closes(rows) == [100.0, 101.0]


def test_all_candles_in_progress_yields_empty_not_a_crash():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = [{"t": now_ms, "T": now_ms + 59_999, "c": "100", "v": "1"}]
    assert _closes(rows) == []


def test_volumes_stay_aligned_with_closes():
    rows = _candles([100.0, 101.0, 102.0], last_in_progress=True)
    closes, volumes = asyncio.run(
        crypto15m._fetch_closes("BTC", _FakeClient(rows), 10))
    assert len(closes) == len(volumes) == 2
