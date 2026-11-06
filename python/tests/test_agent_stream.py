"""The `mcp:toolCall` stream, pinned.

Every agent tool call — MCP, the HTTP API, Autopilot — is emitted from the one
dispatcher (mcp_tools.call) so the AI Agents page and the Hub can show what
the agents are doing. The event rides to the renderer, which never holds a
secret, so it is built from WHITELISTED pieces only. Each test guards one way
that goes wrong: an argument string forwarded raw, a token echoed by an agent
reaching the screen, a refusal that loses its reason, or a looping agent
flooding the renderer.
"""
from __future__ import annotations

import json
import time

import pytest

import db
import logscrub
import mcp_server
import mcp_tools
from test_mcp import _BUY, _fake_service, _forecast, _mock_book, _run

CID = "0x" + "ab" * 32


@pytest.fixture
def events(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "stream.db")
    db.init_db()
    mcp_server._sessions.clear()
    mcp_server._TOKENS.clear()
    mcp_server._hits.clear()
    mcp_server._agent_hits.clear()
    mcp_server.SEEN.clear()
    mcp_server.AGENT_SEEN.clear()
    monkeypatch.setattr(mcp_server, "_THROTTLE", mcp_server._Throttle())
    got: list = []

    async def _emit(name, payload=None):
        got.append((name, payload))
    mcp_server.set_event_callback(_emit)
    yield got
    mcp_server.set_event_callback(None)


def _calls(events):
    return [e for n, e in events if n == mcp_server.TOOLCALL_EVENT and e["kind"] == "call"]


def _call(name, args, **session):
    s = {"client": "claude-code 2.1", "agent": "default"}
    s.update(session)
    return _run(mcp_tools.call(name, args, s))


def test_an_mcp_client_connecting_and_calling_is_streamed(events, monkeypatch):
    _fake_service(monkeypatch)
    resp, sid = _run(mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "cursor", "version": "1.2"}}}, None))
    _run(mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "get_status", "arguments": {}}}, sid))
    kinds = [e["kind"] for n, e in events if n == mcp_server.TOOLCALL_EVENT]
    assert kinds == ["connect", "call"]
    ev = _calls(events)[0]
    assert ev["tool"] == "get_status" and ev["outcome"] == "ok"
    assert ev["agentId"] == "default" and ev["agentName"]
    assert ev["summary"].startswith("get_status")
    assert ev["v"] == 1 and isinstance(ev["at"], int) and ev["durationMs"] >= 0


def test_http_api_calls_are_streamed_through_the_same_dispatcher(events, monkeypatch):
    _fake_service(monkeypatch, mcp_http_enabled=True)
    _run(mcp_server.handle_api("POST", "/api/v1/tools/get_status", b"{}",
                               {"user-agent": "n8n/1.0"}, "default"))
    ev = _calls(events)[0]
    assert ev["transport"] == "http" and ev["client"] == "http:n8n/1.0"


def test_a_forecast_event_carries_its_number_edge_and_mid(events, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    res = _call("record_forecast", {"conditionId": CID, "fairValueCents": 70,
                                    "rationale": "SECRET-RATIONALE because the rules say so",
                                    "model": "claude-sonnet"})
    assert not res["isError"], res
    ev = _calls(events)[0]
    assert ev["conditionId"] == CID and ev["fairCents"] == 70
    assert ev["summary"].startswith("forecast 0x")
    assert "SECRET-RATIONALE" not in json.dumps(ev)
    assert ev["model"] == "claude-sonnet"


def test_a_refusal_is_streamed_with_the_rails_reason(events, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    _call("place_order", dict(_BUY))   # no forecast
    ev = _calls(events)[0]
    assert ev["outcome"] == "refused" and ev["reason"]
    assert not ev["reason"].lower().startswith("refused")
    assert "forecast" in ev["reason"].lower()


def test_a_paper_fill_is_labelled_paper(events, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    res = _call("place_order", {**_BUY, "conditionId": CID, "orderType": "FAK",
                                "forecastId": _forecast(cid=CID)})
    assert not res["isError"], res
    ev = _calls(events)[-1]
    assert ev["mode"] == "paper" and ev["side"] == "yes"
    assert "buy 10 YES" in ev["summary"] and "@50¢" in ev["summary"]


def test_an_unknown_tool_never_echoes_its_name(events, monkeypatch):
    _fake_service(monkeypatch)
    res = _call("kp_Q7x9Q7x9Q7x9Q7x9Q7x9Q7x9Q7x9Q7x9", {})
    assert res["isError"]
    ev = _calls(events)[0]
    assert ev["tool"] == "unknown" and "Q7x9" not in json.dumps(ev)


def test_token_and_key_material_fed_through_every_argument_is_absent(events, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    tok = mcp_server.ensure_token()
    logscrub.add_known_secret(tok)
    wallet = "0x" + "4c" * 32
    logscrub.add_known_secret(wallet)
    poison = {"conditionId": tok, "side": tok, "action": tok, "size": tok,
              "priceCents": tok, "query": tok, "rationale": tok, "model": tok,
              "fairValueCents": tok, "column": tok, "sql": tok, "code": tok,
              "action2": wallet}
    for name in ("search_markets", "record_forecast", "place_order", "preview_order",
                 "get_market", "discover_markets", "research_data", "close_position"):
        args = {k: v for k, v in poison.items()
                if k in mcp_tools._TOOLS[name].props}
        _call(name, args, client=f"cursor {tok}")
    _call("get_market", {"conditionId": wallet})
    blob = json.dumps(events)
    assert tok not in blob and tok.lower() not in blob and tok.upper() not in blob
    assert wallet not in blob and wallet[2:] not in blob
    assert all(e["client"] in ("unknown", "cursor") for e in _calls(events)[:-1])


def test_labels_and_condition_ids_are_whitelisted_shapes():
    assert mcp_server.safe_condition_id("cpc-btc-updown-15m-2026-11-05-0130z") == "cpc-btc-updown-15m-2026-11-05-0130z"
    assert mcp_server.safe_condition_id("Will BTC go up?") is None
    assert mcp_server.safe_label("claude-code 2.1") == "claude-code 2.1"
    assert mcp_server.safe_label("x" * 40) == ""          # credential-length run
    assert mcp_server.safe_label("Q7x9Q7x9Q7x9Q7x9") == ""  # mixed-case base64 shape
    assert mcp_server.safe_label("sk-ant-api03-abc") == ""
    assert "\n" not in mcp_server.safe_agent_name("Sam\nAPPROVED")


def test_the_summary_never_includes_free_text():
    for name, args in (("search_markets", {"query": "FREE TEXT"}),
                       ("research_data", {"sql": "SELECT 'FREE TEXT'"}),
                       ("save_script", {"code": "FREE TEXT", "name": "FREE TEXT"}),
                       ("manage_watchlist", {"note": "FREE TEXT"}),
                       ("discover_markets", {"column": "FREE TEXT"}),
                       ("perps", {"action": "FREE TEXT"})):
        out = mcp_server.summarize_call(name, args, {"markets": []}, "paper")
        assert "FREE TEXT" not in json.dumps(out), name


def test_out_of_range_numbers_are_dropped():
    out = mcp_server.summarize_call("place_order", {
        "conditionId": CID, "side": "yes", "action": "buy", "size": -5,
        "priceCents": 150}, None, "live")
    assert out["summary"].startswith("buy YES") and "150" not in out["summary"]
    out = mcp_server.summarize_call("record_forecast", {"fairValueCents": 1e9}, {}, None)
    assert out["fairCents"] is None


def test_the_throttle_drops_a_flood_and_counts_what_it_dropped():
    t = [0.0]
    th = mcp_server._Throttle(burst=3, rate=1.0, clock=lambda: t[0])
    assert [th.take() for _ in range(5)] == [True, True, True, False, False]
    assert th.suppressed == 2
    t[0] = 1.0
    assert th.take()


def test_a_looping_agent_is_throttled_and_the_next_event_says_how_many(events, monkeypatch):
    _fake_service(monkeypatch)
    t = [0.0]
    monkeypatch.setattr(mcp_server, "_THROTTLE",
                        mcp_server._Throttle(burst=2, rate=1.0, clock=lambda: t[0]))
    for _ in range(5):
        _call("get_my_agent", {})
    assert len(_calls(events)) == 2
    t[0] = 5.0
    _call("get_my_agent", {})
    assert _calls(events)[-1]["suppressed"] == 3


def test_seen_is_memory_only_and_counts_calls(events, monkeypatch):
    _fake_service(monkeypatch, mcp_agents=[{"id": "default"}, {"id": "sam", "name": "Sam"}])
    _call("get_my_agent", {})
    _call("get_my_agent", {}, agent="sam")
    s = mcp_server.seen()
    row = s["clients"][0]
    assert row["client"] == "claude-code 2.1" and row["calls"] == 2 and row["called"] is True
    assert row["at"] == row["lastAt"] and row["agentId"] in ("default", "sam")
    assert {a["agentId"] for a in s["agents"]} == {"default", "sam"}


def test_no_emit_hook_means_no_event_and_no_error(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "s2.db")
    db.init_db()
    mcp_server.set_event_callback(None)
    _fake_service(monkeypatch)
    assert not _call("get_my_agent", {})["isError"]


def test_a_broken_event_bus_never_breaks_a_call(events, monkeypatch):
    _fake_service(monkeypatch)

    async def _boom(*_a):
        raise RuntimeError("renderer gone")
    mcp_server.set_event_callback(_boom)
    assert not _call("get_my_agent", {})["isError"]


def test_build_call_event_is_fast():
    t0 = time.perf_counter()
    for _ in range(200):
        mcp_server.summarize_call("place_order", {**_BUY, "conditionId": CID}, {"ok": True}, "paper")
    assert time.perf_counter() - t0 < 1.0
