"""The AI agents' PAPER stop/take-profit list: the tick merges, the cap refuses.

The tick reads the list, then awaits a market read per instruction (and a
paper sell for each stop that fires). The agent's add/cancel runs as a
concurrent task the whole time. The tick used to write back the list it read
at the top, which lost an add made mid-tick and resurrected a cancel; and the
list was saved as `rows[:200]` with adds on the front, so the 201st add
silently evicted the oldest entry — an armed stop included.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import mcp_server
import mcp_tools
import terminal
import terminal_trade
from mcp_tools import Refused

AGENT = {"id": "default", "name": "Default"}


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "ps.db")
    db.init_db()
    mcp_server._sessions.clear()

    async def _quotes(_ms):
        return None
    monkeypatch.setattr(terminal, "clob_quotes", _quotes)
    return tmp_path


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _add(**kw):
    a = {"action": "add", "kind": "alertAbove", "conditionId": "0xabc",
         "side": "yes", "triggerCents": 40}
    a.update(kw)
    return mcp_tools._paper_standing(a, AGENT)


def _market_at(monkeypatch, prob, during=None):
    """term.market returns `prob`; `during` runs once, inside the first read —
    i.e. while the tick is suspended on the network."""
    state = {"done": False}

    async def _market(cid, include_raw=False):
        if during and not state["done"]:
            state["done"] = True
            during()
        return {"conditionId": cid, "title": "T", "probability": prob}
    monkeypatch.setattr(terminal, "market", _market)


def _ids():
    return [r["id"] for r in mcp_tools._ps_load()]


def test_an_add_made_mid_tick_survives_the_ticks_write(fresh, monkeypatch):
    fires = _add(kind="alertAbove", triggerCents=40)["instruction"]
    added = {}
    _market_at(monkeypatch, 50.0,
               during=lambda: added.update(_add(kind="alertBelow", triggerCents=5)))
    assert _run(mcp_tools.paper_standing_tick()) == 1
    rows = {r["id"]: r for r in mcp_tools._ps_load()}
    new_id = added["instruction"]["id"]
    assert new_id in rows, "the add made during the tick was lost"
    assert rows[new_id]["armed"] is True and rows[new_id]["firedAt"] is None
    assert rows[fires["id"]]["firedAt"] and rows[fires["id"]]["armed"] is False
    assert "50" in rows[fires["id"]]["result"]


def test_a_cancel_made_mid_tick_is_not_resurrected(fresh, monkeypatch):
    fires = _add(kind="alertAbove", triggerCents=40)["instruction"]
    watching = _add(kind="alertBelow", triggerCents=5)["instruction"]
    _market_at(monkeypatch, 50.0, during=lambda: mcp_tools._paper_standing(
        {"action": "cancel", "id": watching["id"]}, AGENT))
    assert _run(mcp_tools.paper_standing_tick()) == 1
    assert watching["id"] not in _ids(), "a cancelled instruction came back"
    assert fires["id"] in _ids()


def test_a_stop_cancelled_mid_tick_does_not_sell(fresh, monkeypatch):
    stop = _add(kind="stopLoss", triggerCents=60, size=10)["instruction"]

    async def _no_sell(*_a, **_k):
        raise AssertionError("a cancelled stop must not sell")
    monkeypatch.setattr(terminal_trade, "sell_position_preview", _no_sell)
    monkeypatch.setattr(terminal_trade, "sell_position", _no_sell)
    _market_at(monkeypatch, 50.0, during=lambda: mcp_tools._paper_standing(
        {"action": "cancel", "id": stop["id"]}, AGENT))
    assert _run(mcp_tools.paper_standing_tick()) == 0
    assert _ids() == []


def test_the_tick_never_rearms_and_leaves_the_rest_alone(fresh, monkeypatch):
    a = _add(kind="alertAbove", triggerCents=40, note="mine")["instruction"]
    _market_at(monkeypatch, 50.0)
    assert _run(mcp_tools.paper_standing_tick()) == 1
    assert _run(mcp_tools.paper_standing_tick()) == 0
    row = mcp_tools._ps_load()[0]
    assert row["id"] == a["id"] and row["armed"] is False and row["note"] == "mine"


def _seed(armed: int, retired: int) -> None:
    rows = []
    for i in range(retired):
        rows.append({"id": f"r{i:03d}", "conditionId": "0xabc", "side": "yes",
                     "kind": "alertAbove", "triggerCents": 40, "armed": False,
                     "agentId": "default", "createdAt": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                     "firedAt": f"2026-01-02T00:{i // 60:02d}:{i % 60:02d}Z",
                     "result": "done"})
    for i in range(armed):
        rows.append({"id": f"a{i:03d}", "conditionId": "0xabc", "side": "yes",
                     "kind": "stopLoss", "triggerCents": 10, "size": 1.0,
                     "armed": True, "agentId": "default",
                     "createdAt": "2025-01-01T00:00:00Z", "firedAt": None,
                     "result": None})
    import json
    with db.get_db() as conn:
        db.kv_set(conn, mcp_tools._PAPER_STANDING_KV, json.dumps(rows))


def test_at_the_cap_an_add_is_refused_and_no_armed_stop_is_evicted(fresh):
    _seed(armed=mcp_tools._PAPER_STANDING_MAX, retired=0)
    with pytest.raises(Refused) as ex:
        _add()
    assert "full" in str(ex.value) and "cancel" in str(ex.value)
    ids = _ids()
    assert len(ids) == mcp_tools._PAPER_STANDING_MAX
    assert all(f"a{i:03d}" in ids for i in range(mcp_tools._PAPER_STANDING_MAX))


def test_over_the_cap_retired_rows_go_oldest_first_armed_ones_never(fresh):
    cap = mcp_tools._PAPER_STANDING_MAX
    _seed(armed=cap - 10, retired=20)          # cap + 10 rows, 190 armed
    new = _add()["instruction"]                # cap + 11 rows -> drop 11 retired
    ids = set(_ids())
    assert len(ids) == cap
    assert new["id"] in ids
    assert all(f"a{i:03d}" in ids for i in range(cap - 10)), "an armed stop was evicted"
    assert not any(f"r{i:03d}" in ids for i in range(11)), "the oldest retired rows stay"
    assert all(f"r{i:03d}" in ids for i in range(11, 20))


def test_retired_rows_do_not_count_against_the_cap(fresh):
    cap = mcp_tools._PAPER_STANDING_MAX
    _seed(armed=cap - 1, retired=50)
    assert _add()["ok"] is True
    with pytest.raises(Refused):
        _add()
