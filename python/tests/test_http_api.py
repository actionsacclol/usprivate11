"""The plain HTTP API (/api/v1) on the MCP listener, pinned.

It is a second DOOR, not a second set of rules: every call goes through
mcp_tools.call, the one dispatcher MCP and Autopilot use. Each test guards one
way a second door goes wrong — reachable from a web page, open while switched
off, a token that is somebody else's, a rail that binds over MCP but not
here, or a status code a script would misread.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import db
import mcp_server
import mcp_tools
import terminal_paper as tp
from test_mcp import _BUY, _fake_service, _forecast, _mock_book, _run


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "http.db")
    db.init_db()
    mcp_server._sessions.clear()
    mcp_server._TOKENS.clear()
    mcp_server._hits.clear()
    mcp_server._agent_hits.clear()
    monkeypatch.setattr(mcp_server, "_want", True)
    return tmp_path


def _svc(monkeypatch, **cfg):
    base = {"mcp_http_enabled": True}
    base.update(cfg)
    return _fake_service(monkeypatch, **base)


def _api(method, path, token=None, body=None, **headers):
    h = {"host": "127.0.0.1:47635", "user-agent": "pytest-bot/1.0"}
    if token:
        h["authorization"] = f"Bearer {token}"
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    status, hdrs, out = _run(mcp_server._route(
        {"method": method, "path": path, "headers": h, "body": raw}))
    try:
        parsed = json.loads(out) if out else None
    except ValueError:
        parsed = out.decode("utf-8", "replace")
    return status, hdrs, parsed


# ── The door's own checks ───────────────────────────────────────────────────


def test_missing_or_wrong_token_is_401(fresh, monkeypatch):
    _svc(monkeypatch)
    mcp_server.ensure_token()
    assert _api("GET", "/api/v1/tools")[0] == 401
    status, hdrs, body = _api("GET", "/api/v1/tools", token="kp_wrong")
    assert status == 401 and body["ok"] is False and "WWW-Authenticate" in hdrs


def test_foreign_host_and_any_origin_are_refused(fresh, monkeypatch):
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()
    assert _api("GET", "/api/v1/tools", tok, host="evil.example:47635")[0] == 403
    # Polybot refuses ANY Origin, loopback ones included: a script never sends
    # one, a browser always does.
    assert _api("GET", "/api/v1/tools", tok, origin="http://127.0.0.1:47635")[0] == 403
    assert _api("GET", "/api/v1/tools", tok, origin="https://evil.example")[0] == 403


def test_the_api_has_its_own_switch_and_it_is_off_by_default(fresh, monkeypatch):
    _fake_service(monkeypatch)   # mcp_http_enabled not set
    tok = mcp_server.ensure_token()
    status, _, body = _api("GET", "/api/v1/tools", tok)
    assert status == 403 and "switched off" in body["error"]
    status, _, body = _api("POST", "/api/v1/tools/get_status", tok, body={})
    assert status == 403
    # Nothing ran: no audit row, no session counters.
    assert mcp_server.audit_log() == []
    # MCP itself is unaffected by the API switch.
    assert _run(mcp_server._route({"method": "POST", "path": "/mcp", "headers": {
        "host": "127.0.0.1:47635", "authorization": f"Bearer {tok}"},
        "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()}))[0] == 200


def test_agent_access_off_is_503_and_a_switched_off_agent_is_403(fresh, monkeypatch):
    _svc(monkeypatch, mcp_agents=[{"id": "default"}, {"id": "sam", "name": "Sam",
                                                      "enabled": False}])
    tok = mcp_server.ensure_token("sam")
    status, _, body = _api("GET", "/api/v1/tools", tok)
    assert status == 403 and "switched off" in body["error"]
    monkeypatch.setattr(mcp_server, "_want", False)
    assert _api("GET", "/api/v1/tools", mcp_server.ensure_token())[0] == 503


def test_methods_and_bodies_are_checked(fresh, monkeypatch):
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()
    status, hdrs, _ = _api("POST", "/api/v1/tools", tok)
    assert status == 405 and hdrs.get("Allow") == "GET"
    status, hdrs, _ = _api("GET", "/api/v1/tools/get_status", tok)
    assert status == 405 and hdrs.get("Allow") == "POST"
    assert _api("POST", "/api/v1/tools/get_status", tok, body=b"not json")[0] == 400
    assert _api("POST", "/api/v1/tools/get_status", tok, body=[1, 2])[0] == 400
    assert _api("GET", "/api/v1/nope", tok)[0] == 404


def test_tools_lists_exactly_what_mcp_lists(fresh, monkeypatch):
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, body = _api("GET", "/api/v1/tools", tok)
    assert status == 200
    assert [t["name"] for t in body["tools"]] == [t["name"] for t in mcp_tools.list_tools()]
    assert body["agent"]["id"] == "default" and body["tradeMode"] in ("off", "paper", "live")
    assert "conditionId" in body["instructions"]


def test_unknown_tool_is_404_and_never_runs(fresh, monkeypatch):
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, body = _api("POST", "/api/v1/tools/drain_wallet", tok, body={})
    assert status == 404 and body["ok"] is False
    assert _api("POST", "/api/v1/tools/Get-Status", tok, body={})[0] == 404


def test_openapi_is_generated_from_the_tool_schemas(fresh, monkeypatch):
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, spec = _api("GET", "/api/v1/openapi.json", tok)
    assert status == 200 and spec["openapi"].startswith("3.1")
    names = {t["name"] for t in mcp_tools.list_tools()}
    ops = {p.rsplit("/", 1)[-1] for p in spec["paths"] if p.startswith("/api/v1/tools/")}
    assert ops == names
    place = spec["paths"]["/api/v1/tools/place_order"]["post"]
    assert place["requestBody"]["content"]["application/json"]["schema"]["required"]
    assert {"401", "403", "404", "422", "429"} <= set(place["responses"])
    # Our own loopback address, never a host taken from the request.
    assert spec["servers"][0]["url"].startswith("http://127.0.0.1:")
    assert tok not in json.dumps(spec)


# ── The rails bind exactly as over MCP ─────────────────────────────────────


def test_a_read_returns_the_result_as_json(fresh, monkeypatch):
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, body = _api("POST", "/api/v1/tools/get_status", tok, body={})
    assert status == 200 and body["ok"] is True and body["tool"] == "get_status"
    assert body["result"]["you"]["agentId"] == "default"


def test_a_buy_without_a_forecast_is_422_with_the_mcp_words(fresh, monkeypatch):
    _svc(monkeypatch)
    _mock_book(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, body = _api("POST", "/api/v1/tools/place_order", tok, body=dict(_BUY))
    assert status == 422 and body["ok"] is False
    mcp_res = _run(mcp_tools.call("place_order", dict(_BUY),
                                  {"client": "mcp", "agent": "default"}))
    assert body["error"] == mcp_res["content"][0]["text"]


def test_a_paper_order_that_passes_fills_in_the_callers_own_book(fresh, monkeypatch):
    _svc(monkeypatch)
    _mock_book(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, body = _api("POST", "/api/v1/tools/place_order", tok,
                           body={**_BUY, "orderType": "FAK", "forecastId": _forecast()})
    assert status == 200, body
    assert body["result"]["mode"] == "paper"
    with tp.using_book("ai"):
        assert len(_run(tp.portfolio({}))["positions"]) == 1
    assert _run(tp.portfolio({}))["positions"] == [], "leaked into the user's book"
    # Audited under the HTTP client's label, like any MCP write.
    assert mcp_server.audit_log()[0]["client"].startswith("http:pytest-bot")


def test_trading_off_refuses_orders_as_over_mcp(fresh, monkeypatch):
    _svc(monkeypatch, mcp_trade_mode="off")
    _mock_book(monkeypatch)
    tok = mcp_server.ensure_token()
    status, _, body = _api("POST", "/api/v1/tools/place_order", tok,
                           body={**_BUY, "forecastId": _forecast()})
    assert status == 422 and "OFF" in body["error"]


def test_the_token_decides_the_agent(fresh, monkeypatch):
    _svc(monkeypatch, mcp_agents=[{"id": "default"}, {"id": "sam", "name": "Sam"}])
    tok = mcp_server.ensure_token("sam")
    status, _, body = _api("POST", "/api/v1/tools/get_status", tok, body={})
    assert status == 200 and body["result"]["you"]["agentId"] == "sam"


def test_rate_limit_is_429_with_retry_after(fresh, monkeypatch):
    _svc(monkeypatch)
    monkeypatch.setattr(mcp_server, "RATE_MAX", 2)
    tok = mcp_server.ensure_token()
    for _ in range(2):
        assert _api("POST", "/api/v1/tools/get_status", tok, body={})[0] == 200
    status, hdrs, body = _api("POST", "/api/v1/tools/get_status", tok, body={})
    assert status == 429 and hdrs["Retry-After"] == str(int(mcp_server.RATE_WINDOW))
    assert body["error"].startswith(mcp_server.RATE_LIMIT_TEXT)


def test_the_rate_limit_text_is_what_the_dispatcher_writes(fresh, monkeypatch):
    """Rewording the dispatcher's refusal must not turn a 429 into a 422."""
    _fake_service(monkeypatch)
    monkeypatch.setattr(mcp_server, "RATE_MAX", 0)
    res = _run(mcp_tools.call("get_status", {}, {"client": "x", "agent": "default"}))
    assert res["isError"] and res["content"][0]["text"].startswith(mcp_server.RATE_LIMIT_TEXT)


# ── Labels and the snippet ─────────────────────────────────────────────────


def test_client_name_is_a_cleaned_label():
    assert mcp_server.http_client_name("my-bot/1.0") == "http:my-bot/1.0"
    assert mcp_server.http_client_name(None) == "http:unknown"
    forged = mcp_server.http_client_name("bot\r\nX-Evil: 1\n[ERROR] fake" + "a" * 200)
    assert "\n" not in forged and "\r" not in forged and len(forged) <= 70


def test_the_snippet_carries_the_token_and_its_python_compiles():
    tok = "kp_" + "x" * 43
    text = mcp_server.http_snippet(47635, tok, "Sports Sam")
    assert text.count(tok) >= 4 and "http://127.0.0.1:47635/api/v1" in text
    py = text[text.index("import requests"):]
    compile(py, "snippet.py", "exec")
    # And it parses as Python in a fresh interpreter (no requests needed to parse).
    out = subprocess.run([sys.executable, "-c", "import ast,sys; ast.parse(sys.stdin.read())"],
                         input=py, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_a_real_socket_round_trip_through_the_api(fresh, monkeypatch):
    import asyncio
    import socket
    _svc(monkeypatch)
    tok = mcp_server.ensure_token()

    async def go():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        await mcp_server.start(port)
        try:
            import health
            return await health.probe_http(port, tok), await health.probe_http(port, "kp_bad")
        finally:
            await mcp_server.stop()
    good, bad = asyncio.new_event_loop().run_until_complete(go())
    assert good["ok"] and good["tools"] == len(mcp_tools.list_tools())
    assert not bad["ok"] and bad["status"] == 401
