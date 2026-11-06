"""App-wide Paper | Live (account_mode), and the config classification,
pinned.

One switch, the user's alone: in Paper the terminal, the phone, standing
rules and every agent trade the paper book, and the engines send nothing —
with their own switches left exactly as the user set them. Each test guards
one way that goes wrong: an upgrade that silently stops a live user's engines
(or starts a new user live), two paper flags that disagree, an agent that
flips the account, or a config key nobody decided an agent may write.
"""
from __future__ import annotations

import json

import pytest

import config
import db
import mcp_server
import mcp_tools
import terminal_paper as tp
from config import DEFAULT_CONFIG, merge_with_defaults
from test_mcp import _BUY, _call, _fake_service, _forecast, _mock_book, _run


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "acct.db")
    db.init_db()
    mcp_server._sessions.clear()
    mcp_server._hits.clear()
    mcp_server._agent_hits.clear()
    return tmp_path


# ── Migration and derivation ────────────────────────────────────────────────


def test_a_fresh_config_is_paper():
    c = merge_with_defaults({})
    assert c["account_mode"] == "paper" and c["terminal_paper_mode"] is True
    assert DEFAULT_CONFIG["account_mode"] == "paper"


def test_an_existing_config_keeps_what_it_had():
    # Had paper mode on -> paper.
    assert merge_with_defaults({"terminalPaperMode": True})["account_mode"] == "paper"
    # Had paper mode off -> live: its engines traded yesterday.
    assert merge_with_defaults({"terminalPaperMode": False})["account_mode"] == "live"
    # A stored config that never had the paper key at all is still existing.
    assert merge_with_defaults({"enableTrading": True})["account_mode"] == "live"


def test_a_given_account_mode_is_never_rewritten():
    c = merge_with_defaults({"accountMode": "paper", "terminalPaperMode": False})
    assert c["account_mode"] == "paper" and c["terminal_paper_mode"] is True
    c = merge_with_defaults({"accountMode": "live", "terminalPaperMode": True})
    assert c["account_mode"] == "live" and c["terminal_paper_mode"] is False


def test_an_unreadable_account_mode_is_paper():
    for bad in ("LIVEE", 1, True, None, "", "real"):
        assert merge_with_defaults({"account_mode": bad})["account_mode"] == "paper", bad
    assert merge_with_defaults({"account_mode": "LIVE"})["account_mode"] == "live"


def test_terminal_paper_mode_is_derived_and_cannot_disagree():
    c = merge_with_defaults({"account_mode": "live", "terminal_paper_mode": True})
    assert c["terminal_paper_mode"] is False
    # A re-merge (what every in-process patch does) keeps them in step.
    assert merge_with_defaults({**c, "terminal_paper_mode": True})["terminal_paper_mode"] is False


def test_account_live_only_narrows():
    assert config.account_live({"account_mode": "live", "terminal_paper_mode": False})
    assert not config.account_live({"account_mode": "paper"})
    assert not config.account_live({"account_mode": "live", "terminal_paper_mode": True})
    assert not config.account_live({"account_mode": "weird"})
    # A raw dict that never went through merge_with_defaults (tests, CLI).
    assert config.account_live({})
    assert not config.account_live({"terminal_paper_mode": True})


def test_before_the_first_setconfig_the_terminal_reads_paper(monkeypatch):
    """The backend starts on the raw DEFAULT_CONFIG and only derives
    terminal_paper_mode when Electron's setConfig lands. If every push
    failed, the terminal gate and the standing tick read that underived key
    as Live while the default account (and every engine) is Paper."""
    import sys
    import types

    import terminal_trade as tt
    assert DEFAULT_CONFIG["terminal_paper_mode"] is True, "disagrees with account_mode"
    fake = types.SimpleNamespace(STATE=types.SimpleNamespace(
        cfg={**DEFAULT_CONFIG, "terminal_paper_mode": False}))
    monkeypatch.setitem(sys.modules, "service", fake)
    assert tt._paper_mode_on() is True
    fake.STATE.cfg = merge_with_defaults({"account_mode": "live"})
    assert tt._paper_mode_on() is False


def test_engine_on_is_switch_and_account():
    live = merge_with_defaults({"account_mode": "live", "enable_trading": True})
    paper = merge_with_defaults({"account_mode": "paper", "enable_trading": True})
    assert config.engine_on(live, "enable_trading")
    assert not config.engine_on(paper, "enable_trading")
    assert paper["enable_trading"] is True, "the user's switch was rewritten"
    assert not config.engine_on(merge_with_defaults({"account_mode": "live"}), "enable_trading")


def test_agent_trade_mode_narrows_on_the_account():
    import mcp_rails
    assert mcp_rails.trade_mode(merge_with_defaults(
        {"account_mode": "paper", "mcp_trade_mode": "live"})) == "paper"
    assert mcp_rails.trade_mode(merge_with_defaults(
        {"account_mode": "live", "mcp_trade_mode": "live"})) == "live"
    assert mcp_rails.trade_mode(merge_with_defaults(
        {"account_mode": "live", "mcp_trade_mode": "off"})) == "off"


# ── Agents can never flip it ───────────────────────────────────────────────


def test_an_agent_cannot_change_the_account_mode_either_way(fresh, monkeypatch):
    svc, events = _fake_service(monkeypatch, mcp_live_trading_enabled=True,
                                account_mode="paper")
    for key, val in (("account_mode", "live"), ("accountMode", "live"),
                     ("account_mode", "paper"), ("terminal_paper_mode", False),
                     ("terminal_paper_mode", True), ("terminalPaperBankrollUsd", 1e6)):
        res = _call("update_config", {"changes": {key: val}})
        assert res["isError"], key
    assert merge_with_defaults(svc.STATE.cfg)["account_mode"] == "paper"
    assert not [e for e in events if e[0] == "mcp:configPatch"]


def test_in_paper_an_agent_cannot_reach_a_real_order(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True, account_mode="paper",
                  mcp_trade_mode="live", mcp_live_approval=False,
                  mcp_agents=[{"id": "default", "mode": "live"}])
    _mock_book(monkeypatch)
    status = json.loads(_call("get_status", {})["content"][0]["text"])
    assert status["accountMode"] == "paper" and status["trading"]["yourMode"] == "paper"
    res = _call("flatten_wallet", {"confirm": True})
    assert res["isError"] and "PAPER" in res["content"][0]["text"]
    # place_order lands in the agent's paper book.
    res = _call("place_order", {**_BUY, "orderType": "FAK", "forecastId": _forecast()})
    assert not res["isError"]
    assert json.loads(res["content"][0]["text"])["mode"] == "paper"


# ── Config classification ──────────────────────────────────────────────────


def test_every_config_key_is_classified():
    """A new config knob must be placed deliberately. Unclassified keys are
    refused at runtime; this makes the omission a failing test instead."""
    unclassified = [k for k in DEFAULT_CONFIG if mcp_tools.classify(k) == "unknown"]
    assert not unclassified, unclassified
    overlap = ((mcp_tools.LIVE & mcp_tools.SETTINGS)
               | (mcp_tools.PROTECTED & (mcp_tools.LIVE | mcp_tools.SETTINGS)))
    assert not overlap, overlap
    stale = [k for k in mcp_tools.PROTECTED | mcp_tools.LIVE | mcp_tools.SETTINGS
             if k not in DEFAULT_CONFIG]
    assert not stale, f"classified keys that no longer exist: {stale}"


def test_the_lines_no_toggle_crosses():
    for k in ("account_mode", "terminal_paper_mode", "terminal_paper_bankroll_usd",
              "mcp_trade_mode", "mcp_max_order_usd", "mcp_allow_live_switches",
              "mcp_http_enabled", "remote_trading_enabled", "ai_provider",
              "autopilot_enabled", "network", "taker_rebate_pct",
              "crypto15m_record_signals", "main_record_signals"):
        assert mcp_tools.classify(k) == "protected", k
    for k in ("enable_trading", "crypto15m_enabled",
              "scripts_live_enabled", "stop_loss_on_day", "max_total_exposure_fraction",
              "flatten_on_daily_stop"):
        assert mcp_tools.classify(k) == "live", k
    assert mcp_tools.ENGINE_OFF_SAFE <= mcp_tools.LIVE


def test_an_unclassified_key_is_refused_at_runtime(fresh, monkeypatch):
    _fake_service(monkeypatch, mcp_live_trading_enabled=True)
    monkeypatch.setitem(DEFAULT_CONFIG, "brand_new_knob", 1.0)
    res = _call("update_config", {"changes": {"brand_new_knob": 2.0}})
    assert res["isError"] and "cannot be changed" in res["content"][0]["text"]


def test_tool_count_stays_within_forty():
    assert len(mcp_tools.list_tools()) <= 40


# ── The user's RPCs ────────────────────────────────────────────────────────


@pytest.fixture
def real_service(fresh, monkeypatch):
    import service
    monkeypatch.setattr(service.STATE, "cfg", merge_with_defaults(
        {"account_mode": "paper", "terminal_paper_bankroll_usd": 750.0}))
    return service


def test_paper_status_and_reset_are_the_accounts_manual_book(real_service, monkeypatch):
    service = real_service
    _mock_book(monkeypatch)
    _run(tp.place({"conditionId": "0xme", "side": "yes", "action": "buy",
                   "priceCents": 50, "size": 4, "orderType": "FAK"}, service.STATE.cfg))
    with tp.using_book("ai"):
        _run(tp.place({"conditionId": "0xagent", "side": "yes", "action": "buy",
                       "priceCents": 50, "size": 2, "orderType": "FAK"}, {}))
    st = _run(service._h_paper_status({}))
    assert st["accountMode"] == "paper" and st["bankrollUsd"] == 750.0
    assert st["openPositions"] == 1 and st["cashUsd"] < 750.0
    res = _run(service._h_paper_reset({}))
    assert res["ok"] and res["cleared"] == 1
    assert _run(service._h_paper_status({}))["openPositions"] == 0
    with tp.using_book("ai"):
        assert len(_run(tp.portfolio({}))["positions"]) == 1, "reset wiped an agent's book"


def test_close_agent_paper_sells_only_that_agents_book(real_service, monkeypatch):
    import mcp_agents
    service = real_service
    _mock_book(monkeypatch)
    with tp.using_book(mcp_agents.paper_book("sam")):
        _run(tp.place({"conditionId": "0x" + "1" * 64, "side": "yes", "action": "buy",
                       "priceCents": 50, "size": 5, "orderType": "FAK"}, {}))
    _run(tp.place({"conditionId": "0xme", "side": "yes", "action": "buy",
                   "priceCents": 50, "size": 3, "orderType": "FAK"}, service.STATE.cfg))
    res = _run(service._h_mcp_agent_close_paper({"agentId": "sam"}))
    assert res["ok"] and len(res["closed"]) == 1 and res["closed"][0]["size"] == 5
    with tp.using_book(mcp_agents.paper_book("sam")):
        assert _run(tp.portfolio({}))["positions"] == []
    assert len(_run(tp.portfolio({}))["positions"]) == 1, "it sold the user's position"
    again = _run(service._h_mcp_agent_close_paper({"agentId": "sam"}))
    assert again["ok"] and "Nothing to close" in again["message"]
    with pytest.raises(ValueError):
        _run(service._h_mcp_agent_close_paper({"agentId": "../x"}))


def test_close_agent_paper_is_not_an_agent_tool():
    names = {t["name"] for t in mcp_tools.list_tools()}
    assert not any("close" in n and "paper" in n for n in names)


def test_no_rpc_hands_the_renderer_a_bare_token():
    import service
    assert "mcpRevealToken" not in service._HANDLERS
    for name in ("mcpHttpSnippet", "mcpSeen", "mcpAgentClosePaper", "paperStatus",
                 "paperReset", "healthCheck"):
        assert name in service._HANDLERS, name


def test_mcp_status_carries_http_and_loss(real_service, monkeypatch):
    service = real_service
    monkeypatch.setattr(service.STATE, "cfg", merge_with_defaults(
        {"account_mode": "paper", "mcp_http_enabled": True, "mcp_trade_mode": "paper"}))
    st = _run(service._h_mcp_status({}))
    assert st["httpEnabled"] is True and "lossTodayUsd" in st
    assert st["lossTodayUsd"] == 0.0
    assert all("token" not in k.lower() or k in ("hasToken", "tokenMasked")
               for k in st)


def test_the_trading_status_panel_says_why_in_paper(real_service):
    service = real_service
    service.STATE.cfg["enable_trading"] = True
    out = _run(service._h_trading_status({}))
    gates = {g["id"]: g for g in out["main"]}
    assert gates["account"]["state"] == "blocked"
