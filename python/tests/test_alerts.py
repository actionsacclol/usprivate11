"""The corner alerts, pinned.

Three promises: every alert says what to do, nothing that isn't a fault raises one
(a fresh install on Paper is silent), and polling sends nothing signed or billed.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import alerts
import health
import mcp_server
import polymarket_api as api

# Past the startup grace, on Paper, with nothing wrong.
HEALTHY = {"uptimeSec": 300.0}


def build(**over):
    return alerts.build({**HEALTHY, **over})


def ids(rows):
    return [a["id"] for a in rows]


def by_id(rows, rid):
    return next(a for a in rows if a["id"] == rid)


def test_a_fresh_install_on_paper_has_no_alerts():
    assert build() == []
    # The same with the health rows such a machine really reports.
    rows = [
        health.check_wallet({"hasWalletKey": False}, False, account_live=False),
        health.check_ai("anthropic", active=True, has_key=False, can_probe=True),
        health.check_mcp({"enabled": False}),
        health.check_http({"enabled": False}),
        health.check_autopilot({"enabled": False}),
        health.check_remote("discord", enabled=False, has_token=False, paired=False, bot={}),
        health.check_feed({"connected": False}, needed=False),
        health.check_feed(None, needed=False, rid="spot", label="Crypto spot feed"),
    ]
    assert build(healthRows=rows) == []


def test_every_alert_says_what_to_do_and_its_action_is_usable():
    every = build(
        outages={"gateway": {"kind": "net", "sinceSec": 40}, "api": {"kind": "rate", "sinceSec": 40}},
        regionRefused=True, hasKey=True, authOk=False, accountLive=True,
        killReason="pressed", phonePaused=["Crypto engine"],
        restriction={"message": "Orders are paused for ~30s"},
        dailyRisk="daily stop-loss hit", accountLock="held", balanceStale=True,
        healthRows=[
            health.check_mcp({"enabled": True, "running": False, "port": 47635, "lastError": "in use"}),
            health.check_http({"enabled": True, "httpEnabled": True, "running": False}),
            health.check_autopilot({"enabled": True, "blockedReason": "daily budget used"}),
            health.check_remote("telegram", enabled=True, has_token=False, paired=False, bot={}),
            health.check_feed({"connected": False}, needed=True),
            health.check_ai("openai", active=True, has_key=False, can_probe=True, needed=True),
        ],
        endedClients=[{"client": "Claude Desktop", "at": "x"}],
        stoppedScripts=[{"id": "s1", "name": "Mine", "reason": "boom"}],
        crashingLoops=["crypto"],
    )
    assert len(every) >= 14
    pages = {"agents", "api", "logs", "positions", "remote", "settings", "strategies"}
    for a in every:
        assert a["title"] and a["fix"].strip(), a["id"]
        assert a["severity"] in (alerts.ERROR, alerts.WARN)
        act = a["action"]
        if act and act["kind"] == "nav":
            assert act["page"] in pages and act["label"]
    assert len(set(ids(every))) == len(every)
    severities = [a["severity"] for a in every]
    assert severities == sorted(severities), "errors first"


def test_no_alert_ever_suggests_a_vpn_as_the_fix_for_a_location_refusal():
    assert "vpn" not in by_id(build(regionRefused=True), "region")["fix"].lower()


@pytest.mark.parametrize("kind,title,severity", [
    ("net", "Can't reach Polymarket US", alerts.ERROR),
    ("server", "Polymarket US is having problems", alerts.ERROR),
    ("rate", "Polymarket US is limiting requests", alerts.WARN),
])
def test_api_outage_by_kind(kind, title, severity):
    a = by_id(build(outages={"gateway": {"kind": kind, "sinceSec": 25}}), "api:gateway")
    assert (a["title"], a["severity"]) == (title, severity)
    assert "25 seconds" in a["detail"]


def test_both_hosts_failing_the_same_way_is_one_alert():
    both = {"gateway": {"kind": "net", "sinceSec": 30}, "api": {"kind": "net", "sinceSec": 30}}
    rows = build(outages=both, hasKey=True, authOk=True)
    assert ids(rows) == ["api:gateway"]
    assert "market data and trading" in rows[0]["detail"]


def test_trading_api_outage_only_matters_with_a_key():
    only = {"api": {"kind": "server", "sinceSec": 30}}
    assert build(outages=only) == []
    assert ids(build(outages=only, hasKey=True, authOk=True)) == ["api:api"]


def test_offline_mode_is_a_choice_not_an_outage():
    assert build(offline=True, outages={"gateway": {"kind": "net", "sinceSec": 90}}) == []


def test_key_alerts():
    assert by_id(build(accountLive=True), "key")["severity"] == alerts.ERROR
    assert by_id(build(accountLive=True), "key")["action"]["page"] == "api"
    rejected = {"hasKey": True, "authOk": False, "keyRejected": True}
    assert by_id(build(**rejected), "key")["severity"] == alerts.WARN
    assert by_id(build(**rejected, accountLive=True), "key")["severity"] == alerts.ERROR
    assert "isn't working" in by_id(build(**rejected), "key")["title"]
    assert build(hasKey=True, authOk=True, accountLive=True) == []


def test_an_unconfirmed_key_is_not_called_rejected():
    a = by_id(build(hasKey=True, authOk=False), "key")
    assert "couldn't be checked" in a["title"]
    assert "didn't accept" not in a["detail"]


def test_a_location_refusal_does_not_blame_the_key():
    rows = build(hasKey=True, authOk=False, keyRejected=True, regionRefused=True)
    assert ids(rows) == ["region"]


def test_a_network_outage_does_not_blame_the_key():
    rows = build(hasKey=True, authOk=False, accountLive=True,
                 outages={"gateway": {"kind": "net", "sinceSec": 30}})
    assert ids(rows) == ["api:gateway"]


def test_slow_starters_get_a_grace_period_but_real_faults_do_not():
    feed = health.check_feed({"connected": False}, needed=True)
    state = {"hasKey": True, "authOk": False, "healthRows": [feed]}
    assert alerts.build({"uptimeSec": 5.0, **state}) == []
    assert set(ids(alerts.build({"uptimeSec": 60.0, **state}))) == {"key", "feed"}
    assert ids(alerts.build({"uptimeSec": 5.0, "killReason": "x"})) == ["kill-switch"]
    mcp = health.check_mcp({"enabled": True, "running": False, "port": 1})
    assert ids(alerts.build({"uptimeSec": 5.0, "healthRows": [mcp]})) == ["mcp"]


def test_the_kill_switch_says_so_instead_of_also_saying_paused():
    assert ids(build(killReason="pressed", paused=True)) == ["kill-switch"]
    a = by_id(build(paused=True), "paused")
    assert a["action"] == {"kind": "resume", "label": "Resume"}


def test_mcp_rows():
    # Connected to nothing yet is a setup step.
    waiting = health.check_mcp({"enabled": True, "running": True, "hasToken": True, "port": 1})
    assert waiting["status"] == health.WARN and build(healthRows=[waiting]) == []
    down = health.check_mcp({"enabled": True, "running": False, "port": 47635, "lastError": "in use"})
    a = by_id(build(healthRows=[down]), "mcp")
    assert a["severity"] == alerts.ERROR and "in use" in a["detail"]
    assert a["action"]["kind"] == "copy" and "47635" in a["action"]["text"]


def test_a_taken_port_is_said_in_plain_words():
    raw = ("Could not listen on 127.0.0.1:47635 — error while attempting to bind on address "
           "('127.0.0.1', 47635): [winerror 10048] only one usage of each socket address")
    down = health.check_mcp({"enabled": True, "running": False, "port": 47635, "lastError": raw})
    detail = by_id(build(healthRows=[down]), "mcp")["detail"]
    assert "winerror" not in detail.lower()
    assert "port 47635" in detail


def test_the_daily_limit_reads_as_a_sentence():
    a = by_id(build(dailyRisk="daily stop-loss hit (today pnl=$-5.00, limit=$-5.00)"), "daily-stop")
    assert a["detail"].startswith("Daily stop-loss hit") and a["detail"].endswith(".")


def test_a_row_with_no_fix_or_unknown_id_raises_nothing():
    assert build(healthRows=[health.row("mcp", "x", health.FAIL, "d")]) == []
    assert build(healthRows=[health.row("new-thing", "x", health.FAIL, "d", "f")]) == []


# ── the exchange tracker ───────────────────────────


@pytest.fixture
def fresh_api(monkeypatch):
    monkeypatch.setattr(api, "_api_state", {})
    monkeypatch.setattr(api, "_api_probe_task", None)
    monkeypatch.setattr(api, "_geoblocked_at", 0.0)
    return api


def _age(a, sec):
    a._api_state["gateway"]["firstFail"] -= sec


def test_tracker_needs_both_repeats_and_time(fresh_api):
    url = f"{api.GATEWAY}/v1/markets"
    api._note_api(url)
    api._note_api(url)
    _age(api, 60)
    assert api.api_outages() == {}, "two failures is a blip however long ago"
    api._note_api(url)
    assert api.api_outages()["gateway"]["kind"] == "net"
    api._api_state.clear()
    for _ in range(50):
        api._note_api(url, 500)
    assert api.api_outages() == {}, "a burst inside the window is not an outage yet"
    _age(api, 21)
    assert api.api_outages()["gateway"]["kind"] == "server"


def test_tracker_classifies_and_recovers(fresh_api):
    url = f"{api.GATEWAY}/v1/markets"
    for _ in range(3):
        api._note_api(url, 429)
    _age(api, 30)
    assert api.api_outages()["gateway"]["kind"] == "rate"
    # A 404 for one market is a working exchange.
    api._note_api(url, 404)
    assert api.api_outages() == {}


def test_tracker_ignores_other_sites_and_separates_hosts(fresh_api):
    api._note_api("https://api.coinbase.com/x")
    assert api._api_state == {}
    from us_api import API_BASE
    api._note_api(f"{API_BASE}/v1/orders", 500)
    assert list(api._api_state) == ["api"]


class _Resp:
    def __init__(self, status, text="{}", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def json(self):
        return {}


class _Client:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def get(self, url, params=None, **kw):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _stub_client(monkeypatch, result):
    c = _Client(result)

    async def _get_client():
        return c
    monkeypatch.setattr(api, "_get_client", _get_client)

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(api.asyncio, "sleep", _none)
    monkeypatch.setattr(api, "_recycle_client", _none)
    return c


def test_real_get_path_feeds_the_tracker(fresh_api, monkeypatch):
    _stub_client(monkeypatch, httpx.ConnectError("no route"))
    asyncio.run(api._get(f"{api.GATEWAY}/v1/markets", max_retries=3))
    assert api._api_state["gateway"]["fails"] == 3
    assert api._api_state["gateway"]["kind"] == "net"


def test_local_pool_saturation_is_not_an_outage(fresh_api, monkeypatch):
    _stub_client(monkeypatch, httpx.PoolTimeout("busy"))
    asyncio.run(api._get(f"{api.GATEWAY}/v1/markets", max_retries=3))
    assert "gateway" not in api._api_state


def test_a_region_refusal_on_a_public_read_is_noticed(fresh_api, monkeypatch):
    _stub_client(monkeypatch, _Resp(451, "not available in your region"))
    asyncio.run(api._get(f"{api.GATEWAY}/v1/markets", max_retries=1))
    assert api.geoblock_active()


def test_a_plain_403_is_not_called_a_region_refusal(fresh_api, monkeypatch):
    _stub_client(monkeypatch, _Resp(403, "forbidden"))
    asyncio.run(api._get(f"{api.GATEWAY}/v1/markets", max_retries=1))
    assert not api.geoblock_active()


def test_probe_is_rare_when_healthy_and_quicker_when_failing(fresh_api, monkeypatch):
    client = _stub_client(monkeypatch, _Resp(200))

    async def run():
        api.probe_api_if_idle()
        await api._api_probe_task
        api.probe_api_if_idle()     # just answered: no second request
        assert api._api_probe_task.done()
        calls = client.calls
        api._api_state["gateway"]["fails"] = 1
        api._api_state["gateway"]["lastSeen"] = time.monotonic() - 10
        api.probe_api_if_idle()     # failing: 10s is long enough
        await api._api_probe_task
        return calls
    assert asyncio.run(run()) == 1
    assert client.calls == 2


# ── a saved key is not a working key ────────────────────


@pytest.fixture
def saved_key(monkeypatch):
    monkeypatch.setattr(api.auth, "credentials_present", lambda *_a: True)
    monkeypatch.setattr(api.auth, "get_key_id", lambda: "key-id")
    monkeypatch.setattr(api, "_key_rejected", False)

    async def _no_clock():
        return None
    monkeypatch.setattr(api, "_sync_clock_offset", _no_clock)

    async def _market_ok(*_a, **_k):
        return {"markets": []}
    monkeypatch.setattr(api, "_get", _market_ok)


def _balance_answers(monkeypatch, answer):
    calls = []

    async def _req(method, path, **_k):
        calls.append((method, path))
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(api, "_authed_request", _req)
    return calls


def test_verifying_a_key_really_signs_a_request(saved_key, monkeypatch):
    calls = _balance_answers(monkeypatch, {})
    asyncio.run(api.ensure_api_creds())
    assert calls == [("GET", "/v1/account/balances")]
    assert not api.key_rejected()


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_fails_verification(saved_key, monkeypatch, status):
    _balance_answers(monkeypatch, api.PolymarketAPIError(status, "no"))
    with pytest.raises(api.PolymarketAPIError):
        asyncio.run(api.ensure_api_creds())
    assert api.key_rejected()


def test_a_server_error_fails_verification_without_condemning_the_key(saved_key, monkeypatch):
    _balance_answers(monkeypatch, api.PolymarketAPIError(500, "oops"))
    with pytest.raises(api.PolymarketAPIError):
        asyncio.run(api.ensure_api_creds())
    assert not api.key_rejected()


def test_establish_auth_is_false_for_a_refused_key_and_does_not_retry(saved_key, monkeypatch):
    import polymarket_auth
    import service
    monkeypatch.setattr(polymarket_auth, "prime_credentials", lambda **_k: True)
    calls = _balance_answers(monkeypatch, api.PolymarketAPIError(401, "no"))
    assert asyncio.run(service._establish_auth()) is False
    assert len(calls) == 1


def _test_rpc(monkeypatch, *, was_ok):
    import polymarket_auth
    import service
    monkeypatch.setattr(polymarket_auth, "credentials_present", lambda *_a: True)
    monkeypatch.setattr(polymarket_auth, "reset_credential_cache", lambda: None)
    monkeypatch.setattr(polymarket_auth, "prime_credentials", lambda **_k: True)
    monkeypatch.setattr(service.STATE, "auth_ok", was_ok)
    sent = []

    async def _emit(name, data):
        sent.append((name, data))
    monkeypatch.setattr(service, "emit_event", _emit)
    return service, sent


def test_the_test_button_marks_a_refused_key_disconnected(saved_key, monkeypatch):
    service, sent = _test_rpc(monkeypatch, was_ok=True)
    _balance_answers(monkeypatch, api.PolymarketAPIError(401, "no"))
    with pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(service._h_testCredentials({}))
    assert service.STATE.auth_ok is False
    assert ("backend:authChanged", {"authOk": False}) in sent


def test_the_test_button_on_a_dead_network_neither_proves_nor_condemns(saved_key, monkeypatch):
    service, sent = _test_rpc(monkeypatch, was_ok=True)
    _balance_answers(monkeypatch, ConnectionError("down"))
    with pytest.raises(RuntimeError, match="reach"):
        asyncio.run(service._h_testCredentials({}))
    assert service.STATE.auth_ok is True and sent == []
    service, _ = _test_rpc(monkeypatch, was_ok=False)
    with pytest.raises(RuntimeError):
        asyncio.run(service._h_testCredentials({}))
    assert service.STATE.auth_ok is False, "an unanswered request proves nothing"


def test_the_test_button_marks_an_accepted_key_connected(saved_key, monkeypatch):
    service, sent = _test_rpc(monkeypatch, was_ok=False)
    _balance_answers(monkeypatch, {"balances": [{"currency": "USD", "buyingPower": "5"}]})
    asyncio.run(service._h_testCredentials({}))
    assert service.STATE.auth_ok is True
    assert ("backend:authChanged", {"authOk": True}) in sent


# ── clients that stop ──────────────────────────────


@pytest.fixture
def no_sessions(monkeypatch):
    monkeypatch.setattr(mcp_server, "_sessions", {})
    monkeypatch.setattr(mcp_server, "_ended_clients", {})


def test_a_closed_client_is_reported_until_it_returns(no_sessions):
    sid = mcp_server._new_session({"name": "Claude Desktop"}, "2025-06-18")
    assert mcp_server.ended_clients() == []
    mcp_server.end_session(sid)
    assert [c["client"] for c in mcp_server.ended_clients()] == ["Claude Desktop"]
    mcp_server._new_session({"name": "Claude Desktop"}, "2025-06-18")
    assert mcp_server.ended_clients() == []


def test_one_of_two_windows_closing_is_not_a_stop(no_sessions):
    one = mcp_server._new_session({"name": "Cursor"}, "2025-06-18")
    two = mcp_server._new_session({"name": "Cursor"}, "2025-06-18")
    mcp_server.end_session(one)
    assert mcp_server.ended_clients() == []
    mcp_server.end_session(two)
    assert len(mcp_server.ended_clients()) == 1


def test_an_unknown_session_reports_nothing(no_sessions):
    mcp_server.end_session("nope")
    assert mcp_server.ended_clients() == []


def test_relay_says_goodbye_only_when_it_has_a_session(monkeypatch):
    import mcp_stdio
    sent = []

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b""

    def _open(req, timeout=0):
        sent.append((req.get_method(), req.get_header("Mcp-session-id")))
        return _R()
    monkeypatch.setattr(mcp_stdio.urllib.request, "urlopen", _open)
    monkeypatch.setattr(mcp_stdio, "_endpoint", lambda: ("http://127.0.0.1:1/mcp", "tok"))
    monkeypatch.setattr(mcp_stdio, "_session_id", None)
    mcp_stdio._end_session()
    assert sent == []
    monkeypatch.setattr(mcp_stdio, "_session_id", "abc")
    mcp_stdio._end_session()
    assert sent == [("DELETE", "abc")]


def test_relay_exit_survives_the_app_being_gone(monkeypatch):
    import mcp_stdio

    def _boom(*_a, **_k):
        raise ConnectionRefusedError("gone")
    monkeypatch.setattr(mcp_stdio.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(mcp_stdio, "_endpoint", lambda: ("http://127.0.0.1:1/mcp", "tok"))
    monkeypatch.setattr(mcp_stdio, "_session_id", "abc")
    mcp_stdio._end_session()


# ── the RPC ─────────────────────────────────


def test_polling_signs_nothing_and_calls_no_ai_provider(tmp_path, monkeypatch):
    import service
    monkeypatch.setattr(api, "probe_api_if_idle", lambda: None)

    def _forbidden(*_a, **_k):
        raise AssertionError("polling must not send a signed request")
    monkeypatch.setattr(api, "_authed_request", _forbidden)
    monkeypatch.setattr(service, "_h_testCredentials", _forbidden)
    import ai_analysis
    monkeypatch.setattr(ai_analysis, "check_provider", _forbidden, raising=False)

    out = asyncio.run(service._h_alerts({}))
    assert isinstance(out["alerts"], list)


def test_one_broken_source_does_not_hide_the_rest(monkeypatch):
    import service
    monkeypatch.setattr(api, "probe_api_if_idle", lambda: None)
    monkeypatch.setattr(api, "kill_switch_status", lambda: "pressed")

    async def _boom(*_a, **_k):
        raise RuntimeError("broken")
    monkeypatch.setattr(service, "_health_agents", _boom)
    monkeypatch.setattr(service, "_stopped_scripts", lambda: 1 / 0)
    monkeypatch.setattr(service, "_phone_paused", lambda _c: 1 / 0)
    out = asyncio.run(service._h_alerts({}))
    assert "kill-switch" in ids(out["alerts"])


def test_paused_state_comes_through_the_rpc(monkeypatch):
    import service
    monkeypatch.setattr(api, "probe_api_if_idle", lambda: None)
    monkeypatch.setattr(service.STATE, "paused", True)
    monkeypatch.setattr(api, "kill_switch_status", lambda: None)
    assert "paused" in ids(asyncio.run(service._h_alerts({}))["alerts"])


def test_rpc_is_registered():
    import service
    assert service._HANDLERS["alerts"] is service._h_alerts
