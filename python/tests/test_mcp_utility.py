"""AI agents over MCP — the wider toolset, pinned.

update_config, read-only SQL over the research DB, the agent's own paper
stops, live stops attributed to the agent, and the prompts. Each test guards
a way "the agent can do anything" turns into "the agent did something nobody
allowed".
"""
from __future__ import annotations

import asyncio

import pytest

import db
import mcp_server
import mcp_tools
import terminal_paper as tp
import terminal_trade as tt
from test_mcp import (_agent_buy, _call, _fake_service, _forecast, _mock_book,
                      _no_venue, _run, _text)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "mcpu.db")
    db.init_db()
    mcp_server._sessions.clear()
    return tmp_path


# ────────────────────────────────────────────────────────────────────────
# update_config
# ────────────────────────────────────────────────────────────────────────


def test_an_agent_cannot_touch_its_own_caps_or_permissions(fresh, monkeypatch):
    svc, events = _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    for key, val in (("mcp_max_order_usd", 1e6), ("mcpDailyLimitUsd", 1e6),
                     ("mcp_live_trading_enabled", True), ("remote_trading_enabled", True),
                     ("ai_provider", "openai"), ("network", "testnet")):
        res = _call("update_config", {"changes": {key: val}})
        assert res["isError"], key
    assert svc.STATE.cfg["mcp_max_order_usd"] == 25.0
    assert not [e for e in events if e[0] == "mcp:configPatch"]


def test_only_the_user_turns_paper_mode_off(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_live_trading_enabled=True,
                           terminal_paper_mode=True)
    res = _call("update_config", {"changes": {"terminal_paper_mode": False}})
    assert res["isError"] and svc.STATE.cfg["terminal_paper_mode"] is True


def test_safe_changes_need_no_live_permission_and_risky_ones_do(fresh, monkeypatch):
    svc, events = _fake_service(monkeypatch, mcp_live_trading_enabled=False,
                                crypto15m_enabled=True)
    assert not _call("update_config", {"changes": {"crypto15m_enabled": False}})["isError"]
    # The account's Paper | Live is the user's alone — even the "safe"
    # direction (terminal_paper_mode is derived from account_mode now).
    assert _call("update_config", {"changes": {"terminal_paper_mode": True}})["isError"]
    # False on a non-engine key can loosen a rail, so it is not free.
    assert _call("update_config", {"changes": {"flatten_on_daily_stop": False}})["isError"]
    res = _call("update_config", {"changes": {"crypto15m_order_size": 500}})
    assert res["isError"] and "OFF" in _text(res)
    # Patches go out camelCase — Electron's store is keyed that way.
    assert events[0] == ("mcp:configPatch", {"patch": {"crypto15mEnabled": False}})


def test_live_permission_reaches_any_engine_setting_typed_and_clamped(fresh, monkeypatch):
    svc, events = _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    res = _call("update_config", {"changes": {
        "crypto15m_order_size": 12, "crypto15m_direction_mode": "contrarian"}})
    assert not res["isError"], _text(res)
    import json
    out = json.loads(_text(res))
    assert out["effective"]["crypto15m_order_size"] == 12
    assert svc.STATE.cfg["crypto15m_order_size"] == 12
    assert events[-1][1]["patch"] == {"crypto15mOrderSize": 12,
                                      "crypto15mDirectionMode": "contrarian"}
    # Wrong type and unknown key are refused, not coerced.
    assert _call("update_config", {"changes": {"crypto15m_order_size": "lots"}})["isError"]
    assert _call("update_config", {"changes": {"make_money": True}})["isError"]


# ────────────────────────────────────────────────────────────────────────
# research_data: read-only SQL
# ────────────────────────────────────────────────────────────────────────


def test_sql_reads_work(fresh, monkeypatch):
    _fake_service(monkeypatch)
    res = _call("research_data", {"sql": "SELECT 1 AS one, 'x' AS two", "limit": 5})
    import json
    out = json.loads(_text(res))
    assert out["columns"] == ["one", "two"] and out["rows"] == [[1, "x"]]


@pytest.mark.parametrize("sql", [
    "DELETE FROM terminal_orders",
    "UPDATE app_kv SET value='x'",
    "INSERT INTO terminal_orders (condition_id) VALUES ('x')",
    "DROP TABLE terminal_orders",
    "ATTACH DATABASE 'C:/x.db' AS other",
    "PRAGMA journal_mode = DELETE",
    "CREATE TABLE evil (x)",
])
def test_sql_cannot_write_or_reach_other_files(fresh, monkeypatch, sql):
    _fake_service(monkeypatch)
    res = _call("research_data", {"sql": sql})
    assert res["isError"], sql


def test_sql_row_limit_is_enforced(fresh, monkeypatch):
    _fake_service(monkeypatch)
    import json
    res = _call("research_data", {
        "sql": "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n "
               "WHERE i < 50) SELECT i FROM n", "limit": 10})
    out = json.loads(_text(res))
    assert len(out["rows"]) == 10 and out["truncated"] is True


@pytest.mark.parametrize("sql", [
    "SELECT k, v FROM app_kv",
    "SELECT v FROM main.APP_KV WHERE k LIKE 'remote:%'",
    "WITH x AS (SELECT * FROM app_kv) SELECT group_concat(v) FROM x",
    "SELECT 1, (SELECT v FROM app_kv WHERE k LIKE '%paircode%')",
    "SELECT count(*) FROM app_kv WHERE v LIKE '%\"code\": \"%'",
])
def test_sql_cannot_read_the_apps_private_state(fresh, monkeypatch, sql):
    """app_kv is the app's own state, not recorded data — and the live
    remote-control pairing code is in it. An agent that read the code (and
    had any way out, like a web fetch) could pair a stranger's chat account
    as a controller of the app."""
    _fake_service(monkeypatch)
    import remote
    code = remote.new_pair_code("telegram")["code"]
    res = _call("research_data", {"sql": sql})
    assert res["isError"] and code not in _text(res), _text(res)
    assert "app_kv" in _text(res)


def test_the_inventory_does_not_offer_the_private_state(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch)

    async def _stats(_p):
        return {"streams": []}
    svc._h_collection_stats = _stats
    import json
    out = json.loads(_text(_call("research_data", {})))
    assert not [t for t in out["schema"] if t["table"] == "app_kv"]


def test_inventory_lists_the_schema(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch)

    async def _stats(_p):
        return {"streams": []}
    svc._h_collection_stats = _stats
    import json
    out = json.loads(_text(_call("research_data", {})))
    assert any(t["table"] == "terminal_orders" for t in out["schema"])


# ────────────────────────────────────────────────────────────────────────
# Standing instructions
# ────────────────────────────────────────────────────────────────────────


def test_agent_paper_stop_fires_into_its_own_book(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    buy = {"mode": "paper", "conditionId": "0xabc", "side": "yes", "action": "buy",
           "size": 10, "priceCents": 50, "orderType": "FAK",
           "forecastId": _forecast()}
    assert not _call("place_order", buy)["isError"]
    res = _call("manage_standing_instructions", {
        "mode": "paper", "action": "add", "kind": "stopLoss",
        "conditionId": "0xabc", "side": "yes", "triggerCents": 55, "size": 10})
    assert not res["isError"], _text(res)
    # Market probability is 50 in the mock, below the 55 stop → fires.
    assert _run(mcp_tools.paper_standing_tick()) == 1
    with tp.using_book("ai"):
        pf = _run(tp.portfolio({}))
    assert pf["positions"] == [], "the stop did not close the agent's paper position"
    rows = mcp_tools._ps_load()
    assert rows[0]["firedAt"] and "Paper sell" in rows[0]["result"]
    # Fired once only.
    assert _run(mcp_tools.paper_standing_tick()) == 0


def test_agent_paper_stops_are_per_agent(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_agents=[{"id": "default"}, {"id": "sam", "name": "Sam"}])
    sam = {"client": "c", "agent": "sam", "reads": 0, "writes": 0, "refused": 0}
    res = _run(mcp_tools.call("manage_standing_instructions", {
        "action": "add", "kind": "alertAbove", "conditionId": "0xabc",
        "side": "yes", "triggerCents": 70}, sam))
    assert not res["isError"], _text(res)
    import json
    mine = json.loads(_text(_call("manage_standing_instructions", {"action": "list"})))
    assert mine["instructions"] == [], "Default sees Sam's instruction"
    sid = mcp_tools._ps_load()[0]["id"]
    assert _call("manage_standing_instructions", {"action": "cancel", "id": sid})["isError"]


def test_a_live_stop_needs_shares_the_agent_opened(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_trade_mode="live",
                  mcp_agents=[{"id": "default", "mode": "live"}])
    _no_venue(monkeypatch)
    stop = {"action": "add", "kind": "stopLoss", "conditionId": "0xabc",
            "side": "yes", "triggerCents": 60, "size": 5}
    res = _call("manage_standing_instructions", stop)
    assert res["isError"] and "at most 0" in _text(res)


def test_a_live_stop_written_by_the_agent_fires_as_an_ai_order(fresh, monkeypatch):
    """It is the agent's order; Track and the order log must not say 'You'.
    And while it is armed it reserves the shares: the agent cannot also sell
    them by hand."""
    import mcp_rails
    _fake_service(monkeypatch, mcp_trade_mode="live",
                  mcp_agents=[{"id": "default", "mode": "live"}])
    _no_venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=5, filled_shares=5)
    res = _call("manage_standing_instructions", {
        "mode": "live", "action": "add", "kind": "stopLoss",
        "conditionId": "0xabc", "side": "yes", "triggerCents": 60, "size": 5})
    assert not res["isError"], _text(res)
    entry = tt.standing_list()[0]
    assert entry["origin"] == "ai" and entry["agentId"] == "default"
    assert mcp_rails.live_net("0xabc", "yes", "default") == 0.0

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": 40.0}

    async def _positions(strict=False):
        return [{"ticker": "0xabc", "position_fp": 5}]
    sent = []

    async def _place(req, *, origin="manual", standing_id=None):
        # As the real place() does: every send leaves an order-log row.
        sent.append(origin)
        tt._record_order({"condition_id": req["conditionId"], "side": req["side"],
                          "action": "sell", "order_type": "FAK",
                          "requested_size": req["size"], "requested_price_cents": 60,
                          "sent_size": req["size"], "sent_price_cents": 60,
                          "order_id": "0xstop", "status": "placed", "origin": origin,
                          "standing_id": standing_id})
        return {"ok": True, "message": "sold"}

    async def _preview(req):
        return {"costUsd": 2.0, "blockers": []}
    async def _quotes(rows):
        return rows
    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "clob_quotes", _quotes)
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt, "place", _place)
    monkeypatch.setattr(tt, "preview", _preview)
    _run(tt.standing_tick())
    assert sent == ["ai"]
    # Fired: the shares it sold stay spoken for (counted from the order it sent).
    assert mcp_rails.live_net("0xabc", "yes", "default") <= 0.0


# ────────────────────────────────────────────────────────────────────────
# Prompts, trader lookup, parlay gate
# ────────────────────────────────────────────────────────────────────────


def test_prompts_list_and_render(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}, None))
    names = {p["name"] for p in resp["result"]["prompts"]}
    assert {"analyze_market", "paper_trading_session"} <= names
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
         "params": {"name": "analyze_market", "arguments": {"market": "fed cut"}}}, None))
    assert "fed cut" in resp["result"]["messages"][0]["content"]["text"]
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
         "params": {"name": "analyze_market", "arguments": {}}}, None))
    assert resp["error"]["code"] == -32602


def test_arming_the_parlay_needs_live(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_live_trading_enabled=False)
    called = []

    async def _arm(p):
        called.append(p)
        return {"armed": True}
    svc._h_c15_parlay_arm = _arm
    assert _call("parlay_schedule", {"action": "arm"})["isError"]
    assert called == []
    assert not _call("parlay_schedule", {"action": "disarm"})["isError"]
