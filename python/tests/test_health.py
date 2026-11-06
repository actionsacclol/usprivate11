"""Connection health (healthCheck), pinned.

Two promises: every non-green row says what to DO, and opening the panel
touches no network — a signed read or a provider call happens only on the
user's "Run checks" click (deep=True).
"""
from __future__ import annotations

import asyncio

import pytest

import health


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Pure row builders ───────────────────────────────────────────────────────


def _wallet(**kw):
    base = {"hasWalletKey": True, "hasApiCreds": True, "addressPreview": "0xabcd…1234",
            "metaError": "", "keyStoredUnencrypted": False}
    base.update(kw)
    return base


def test_paper_without_a_wallet_is_not_a_fault():
    r = health.check_wallet(_wallet(hasWalletKey=False), False, account_live=False)
    assert r["status"] == health.OK and r["network"] is False


def test_live_without_a_wallet_points_at_settings():
    r = health.check_wallet(_wallet(hasWalletKey=False), False, account_live=True)
    assert r["status"] == health.FAIL and r["action"]["kind"] == "nav"


def test_wallet_local_state_is_not_dressed_up_as_a_test():
    r = health.check_wallet(_wallet(), True, account_live=True)
    assert r["status"] == health.OK and r["tested"] is False and r["network"] is True
    r = health.check_wallet(_wallet(), False, account_live=True)
    assert r["status"] == health.WARN


def test_wallet_probe_results():
    ok = health.check_wallet(_wallet(), True, account_live=True, probed=True, probe_ok=True)
    assert ok["status"] == health.OK and ok["tested"]
    bad = health.check_wallet(_wallet(), True, account_live=True, probed=True,
                              probe_ok=False, probe_error=RuntimeError("401 Unauthorized"))
    assert bad["status"] == health.FAIL and "401" in bad["detail"]


@pytest.mark.parametrize("err,needle", [
    (RuntimeError("401 Unauthorized"), "api key"),
    (RuntimeError("403 Forbidden: geoblock"), "allowed to trade"),
    (RuntimeError("timestamp out of range"), "clock"),
    (asyncio.TimeoutError(), "internet"),
])
def test_wallet_failures_get_a_specific_fix(err, needle):
    _detail, fix = health.wallet_error_fix(err)
    assert needle in fix.lower()


def test_an_unencrypted_key_is_a_warning():
    r = health.check_wallet(_wallet(keyStoredUnencrypted=True), True, account_live=True)
    assert r["status"] == health.WARN and "UNENCRYPTED" in r["detail"]


def test_ai_rows():
    r = health.check_ai("anthropic", active=True, has_key=False, can_probe=False)
    assert r["status"] == health.OFF
    r = health.check_ai("anthropic", active=True, has_key=False, can_probe=False, needed=True)
    assert r["status"] == health.WARN
    r = health.check_ai("openai", active=True, has_key=True, can_probe=True)
    assert r["status"] == health.OK and r["network"] and not r["tested"]
    r = health.check_ai("openai", active=True, has_key=True, can_probe=True, probed=True,
                        probe={"ok": False, "reason": "model", "message": "no such model"})
    assert r["status"] == health.FAIL and "model" in r["fix"].lower()
    r = health.check_ai("ollama", active=True, has_key=True, can_probe=True, local=True,
                        probed=True, probe={"ok": False})
    assert "key" not in r["fix"].lower()
    r = health.check_ai("gemini", active=False, has_key=True, can_probe=True, idle=True)
    assert "not tested" in r["detail"].lower()


def test_mcp_rows():
    assert health.check_mcp({"enabled": False})["status"] == health.OFF
    r = health.check_mcp({"enabled": True, "running": False, "port": 47635})
    assert r["status"] == health.FAIL and "47635" in r["action"]["text"]
    r = health.check_mcp({"enabled": True, "running": True, "port": 1, "hasToken": True})
    assert r["status"] == health.WARN
    r = health.check_mcp({"enabled": True, "running": True, "port": 1, "hasToken": True,
                          "lastSeen": {"client": "cursor", "at": 1}, "agents": [
                              {"hasToken": True, "lastSeenAt": 1}]})
    assert r["status"] == health.OK and "cursor" in r["detail"]


def test_http_rows():
    assert health.check_http({"enabled": False})["status"] == health.OFF
    assert health.check_http({"enabled": True, "httpEnabled": False})["status"] == health.OFF
    st = {"enabled": True, "httpEnabled": True, "running": True, "port": 47635}
    assert health.check_http(st)["tested"] is False
    assert health.check_http(st, probed=True, probe={"ok": True, "tools": 40})["status"] == health.OK
    assert health.check_http(st, probed=True, probe={"ok": False, "status": 401})["status"] == health.FAIL


def test_port_command_names_the_port_and_nothing_else():
    cmd = health.port_owner_command(47635)
    assert "47635" in cmd and "&&" not in cmd and ";" not in cmd


def test_autopilot_rows():
    assert health.check_autopilot(None)["status"] == health.OFF
    assert health.check_autopilot({"enabled": False})["status"] == health.OFF
    r = health.check_autopilot({"enabled": True, "blockedReason": "no API key",
                                "limits": {"maxRunsPerDay": 3}, "today": {"runs": 1}})
    assert r["status"] == health.WARN and r["action"]
    r = health.check_autopilot({"enabled": True, "nextRunAt": "12:00",
                                "limits": {"maxRunsPerDay": 3, "dailyTokenBudget": 1000},
                                "today": {"runs": 1, "tokens": 10}})
    assert r["status"] == health.OK and "2 runs" in r["detail"]


def test_remote_rows():
    assert health.check_remote("discord", enabled=False, has_token=False, paired=False,
                               bot={})["status"] == health.OFF
    assert health.check_remote("telegram", enabled=True, has_token=False, paired=False,
                               bot={})["status"] == health.FAIL
    assert health.check_remote("telegram", enabled=True, has_token=True, paired=False,
                               bot={})["status"] == health.WARN
    assert health.check_remote("discord", enabled=True, has_token=True, paired=True,
                               bot={"connected": True})["status"] == health.OK


def test_feed_rows():
    assert health.check_feed(None, needed=False)["status"] == health.OFF
    assert health.check_feed({"connected": True, "books": 3}, needed=True)["status"] == health.OK
    assert health.check_feed({"connected": True, "lastMsgAgeSec": 300},
                             needed=True)["status"] == health.WARN
    assert health.check_feed({"connected": False}, needed=False)["status"] == health.OFF
    assert health.check_feed({"connected": False}, needed=True)["status"] == health.WARN


def test_error_text_never_carries_a_secret():
    import logscrub
    saved = list(logscrub._values)
    logscrub.set_known_secrets(["kp_secret_token_value_123"])
    try:
        assert "kp_secret" not in health.error_text(RuntimeError("bad kp_secret_token_value_123"))
    finally:
        logscrub.set_known_secrets(saved)


# ── The RPC: local on open, network only on Run checks ─────────────────────


@pytest.fixture
def svc(monkeypatch):
    import polymarket_auth
    import service
    from config import merge_with_defaults
    monkeypatch.setattr(service.STATE, "cfg", merge_with_defaults(
        {"account_mode": "live", "mcp_enabled": True, "mcp_http_enabled": True}))
    monkeypatch.setattr(polymarket_auth, "credentials_status", lambda *a, **k: _wallet())
    calls = {"wallet": 0, "http": 0}

    async def _test(_p):
        calls["wallet"] += 1
        return {"ok": True}

    async def _probe(port, token, timeout=3.0):
        calls["http"] += 1
        return {"ok": True, "status": 200, "tools": 40}
    monkeypatch.setattr(service, "_h_testCredentials", _test)
    monkeypatch.setattr(health, "probe_http", _probe)
    import mcp_server
    monkeypatch.setattr(mcp_server, "is_running", lambda: True)
    monkeypatch.setattr(mcp_server, "load_token", lambda *_a: "kp_x")
    return service, calls


def test_opening_the_panel_touches_no_network(svc):
    service, calls = svc
    rep = _run(service._h_health_check({}))
    assert rep["deep"] is False and calls == {"wallet": 0, "http": 0}
    ids = {r["id"] for r in rep["rows"]}
    assert {"wallet", "mcp", "http", "autopilot", "remote:discord", "remote:telegram",
            "feed"} <= ids
    assert all(r["status"] in ("ok", "warn", "fail", "off") for r in rep["rows"])
    assert all(not r["tested"] for r in rep["rows"])


def test_run_checks_probes_each_network_connection_once(svc):
    service, calls = svc
    rep = _run(service._h_health_check({"deep": True}))
    assert rep["deep"] is True and calls == {"wallet": 1, "http": 1}
    rows = {r["id"]: r for r in rep["rows"]}
    assert rows["wallet"]["tested"] and rows["http"]["tested"]


def test_a_failing_wallet_test_is_a_row_not_a_crash(svc, monkeypatch):
    service, _ = svc

    async def _boom(_p):
        raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(service, "_h_testCredentials", _boom)
    rows = {r["id"]: r for r in _run(service._h_health_check({"deep": True}))["rows"]}
    assert rows["wallet"]["status"] == "fail"


def test_a_broken_check_does_not_blank_the_panel(svc, monkeypatch):
    service, _ = svc

    def _boom(_cfg):
        raise KeyError("x")
    monkeypatch.setattr(service, "_health_local", _boom)
    rep = _run(service._h_health_check({}))
    assert any(r["id"] == "local-services" and r["status"] == "fail" for r in rep["rows"])
    assert any(r["id"] == "wallet" for r in rep["rows"])


def test_the_ai_row_is_guarded_whatever_ai_providers_looks_like(svc, monkeypatch):
    import ai_analysis
    import ai_providers
    service, _ = svc
    # A build without a free key check: rows say "not tested", nothing is
    # contacted, nothing crashes.
    for mod in (ai_analysis, ai_providers):
        for name in ("check_provider", "check", "needs_key"):
            if hasattr(mod, name):
                monkeypatch.delattr(mod, name)
    rep = _run(service._h_health_check({"deep": True}))
    assert any(r["id"].startswith("ai:") for r in rep["rows"])
    assert not any(r["id"] == "ai-providers" and r["status"] == "fail" for r in rep["rows"])


def test_run_checks_contacts_only_the_selected_provider(svc, monkeypatch):
    import ai_analysis
    import ai_secrets
    service, _ = svc
    service.STATE.cfg["ai_provider"] = "openai"
    monkeypatch.setattr(ai_secrets, "has_key", lambda p: p in ("openai", "anthropic"))
    asked = []

    def _check(p, cfg=None):
        asked.append(p)
        return {"ok": True, "message": "fine"}
    monkeypatch.setattr(ai_analysis, "check_provider", _check)
    _run(service._h_health_check({}))
    assert asked == [], "opening the panel contacted a provider"
    rows = {r["id"]: r for r in _run(service._h_health_check({"deep": True}))["rows"]}
    assert asked == ["openai"]
    assert rows["ai:openai"]["tested"] and not rows["ai:anthropic"]["tested"]


def test_health_is_not_an_agent_door():
    import mcp_tools
    assert not any("health" in t["name"] for t in mcp_tools.list_tools())
