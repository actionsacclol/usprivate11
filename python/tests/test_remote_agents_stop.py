"""/pause, /agents off and the app's pause stop the AI agents and Autopilot.

2026-10-12 release audit: with approvals off, a user who sent /pause was told
"every engine paused" while the agents kept trading. Pausing now sets the
agents' trade mode OFF and Autopilot off, says so, and /resume does not turn
them back on — that is the AI Agents page's call.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

import db
import remote


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "remote_agents.db")
    db.init_db()
    remote._hits.clear()
    remote._pending.clear()
    remote._senders.clear()
    remote.add_paired("telegram", "42")
    return tmp_path


LIVE_AGENTS = [{"id": "default", "name": "Default", "mode": "live", "enabled": True},
               {"id": "sam", "name": "Sam", "mode": "live", "enabled": True}]


def _svc(monkeypatch, **cfg):
    from config import merge_with_defaults
    mod = types.ModuleType("service")
    events: list = []
    base = {"remote_enabled": True, "account_mode": "live",
            "mcp_trade_mode": "live", "mcp_agents": LIVE_AGENTS,
            "mcp_live_approval": False, "autopilot_enabled": True}
    base.update(cfg)
    mod.STATE = types.SimpleNamespace(cfg=merge_with_defaults(base), auth_ok=True)

    async def _emit(name, payload=None):
        events.append((name, payload))
    mod.emit_event = _emit
    monkeypatch.setitem(sys.modules, "service", mod)
    return mod, events


def _say(text):
    return _run(remote.handle("telegram", "42", text))


def test_pause_stops_the_agents_and_autopilot_and_says_so(fresh, monkeypatch):
    svc, events = _svc(monkeypatch, enable_trading=True)
    reply = _say("/pause")
    assert svc.STATE.cfg["mcp_trade_mode"] == "off"
    assert svc.STATE.cfg["autopilot_enabled"] is False
    assert svc.STATE.cfg["enable_trading"] is False
    assert "Every engine paused" in reply
    assert "AI agents: trading OFF" in reply and "Autopilot" in reply
    assert "Default" in reply and "Sam" in reply, reply
    # Electron persists the same patch, so the stop survives a restart.
    patches = [p["patch"] for n, p in events if n == "remote:configPatch"]
    assert {"mcpTradeMode": "off", "autopilotEnabled": False} in patches


def test_resume_does_not_turn_the_agents_back_on(fresh, monkeypatch):
    svc, _ = _svc(monkeypatch, enable_trading=True)
    _say("/pause")
    reply = _say("/resume")
    assert svc.STATE.cfg["enable_trading"] is True
    assert svc.STATE.cfg["mcp_trade_mode"] == "off"
    assert svc.STATE.cfg["autopilot_enabled"] is False
    assert "AI agents stay off" in reply


def test_pause_with_agents_already_off_says_that(fresh, monkeypatch):
    _svc(monkeypatch, mcp_trade_mode="off", autopilot_enabled=False)
    reply = _say("/pause")
    assert "AI agents were already off" in reply


def test_agents_status_names_the_live_ones_and_approvals(fresh, monkeypatch):
    _svc(monkeypatch)
    reply = _say("/agents")
    assert "AI agents: LIVE (Default, Sam; approvals OFF)" in reply
    assert "Autopilot on" in reply
    assert "/agents off" in reply
    # The approval queue is still listed under it.
    assert "waiting for approval" in reply


def test_agents_live_only_when_the_account_is_live(fresh, monkeypatch):
    _svc(monkeypatch, account_mode="paper")
    assert "AI agents: paper" in _say("/agents")


def test_agents_off_stops_them_without_touching_the_engines(fresh, monkeypatch):
    svc, _ = _svc(monkeypatch, enable_trading=True, crypto15m_enabled=True)
    reply = _say("/agents off")
    assert svc.STATE.cfg["mcp_trade_mode"] == "off"
    assert svc.STATE.cfg["autopilot_enabled"] is False
    assert svc.STATE.cfg["enable_trading"] is True
    assert svc.STATE.cfg["crypto15m_enabled"] is True
    assert "trading OFF" in reply and "were LIVE: Default, Sam" in reply
    assert "AI agents: off" in _say("/agents")


def test_agents_cannot_be_turned_on_from_chat(fresh, monkeypatch):
    svc, events = _svc(monkeypatch, mcp_trade_mode="off", autopilot_enabled=False)
    for cmd in ("/agents on", "/agents live", "/agents paper"):
        reply = _say(cmd)
        assert "only be turned back on in the app" in reply
    assert svc.STATE.cfg["mcp_trade_mode"] == "off"
    assert not [e for e in events if e[0] == "remote:configPatch"]
    assert "Usage" in _say("/agents banana")


def test_status_shows_the_agents(fresh, monkeypatch):
    _svc(monkeypatch)

    async def _bal():
        return "Cash: —"
    monkeypatch.setattr(remote, "_balance", _bal)
    reply = _say("/status")
    assert "AI agents: LIVE" in reply


def test_help_lists_agents_off(fresh, monkeypatch):
    _svc(monkeypatch)
    assert "/agents off" in _say("/help")


def test_an_unpaired_sender_cannot_stop_or_read_agents(fresh, monkeypatch):
    svc, events = _svc(monkeypatch)
    reply = _run(remote.handle("telegram", "999", "/agents off"))
    assert "not paired" in reply
    assert svc.STATE.cfg["mcp_trade_mode"] == "live"
    assert not events


def test_the_apps_pause_rpc_stops_the_agents_too(fresh, monkeypatch):
    import service
    from config import merge_with_defaults
    monkeypatch.setattr(service.STATE, "cfg", merge_with_defaults({
        "account_mode": "live", "mcp_trade_mode": "live", "mcp_agents": LIVE_AGENTS,
        "autopilot_enabled": True}))
    monkeypatch.setattr(service.STATE, "paused", False)
    events: list = []

    async def _emit(name, payload=None):
        events.append((name, payload))
    monkeypatch.setattr(service, "emit_event", _emit)
    out = _run(service._h_pause({"paused": True}))
    assert out["paused"] is True and out["agentsStopped"] is True
    assert "AI agents: trading OFF" in out["message"]
    assert service.STATE.cfg["mcp_trade_mode"] == "off"
    assert service.STATE.cfg["autopilot_enabled"] is False
    # Unpausing does not turn them back on.
    out = _run(service._h_pause({"paused": False}))
    assert out == {"paused": False}
    assert service.STATE.cfg["mcp_trade_mode"] == "off"
