"""Scripts must not depend on the other engines being switched on.

Every case here was a real trap: the Scripts page said a script was ENABLED
while something owned by a DIFFERENT page silently stopped it from ever
trading, and nothing in the UI connected the two.

  • crypto15m_assets — the 15m Crypto tab's per-coin checkboxes. Applying the
    5m preset there pins ['BTC'], which used to narrow every script in the
    app, live AND in backtest.
  • main_record_signals / enable_trading — the main engine's collection gate.
    With both off, scan_whales/scan_momentum never ran, so decide_signal read
    an empty table forever.
  • scripts_live_enabled — gated the whole engine, so enabling a shadow
    script did visibly nothing until the user found a second switch.
"""
from __future__ import annotations

import pytest

import script_backtest
import script_engine


# ── per-script asset scope ───────────────────────────────────────────────


def test_no_scope_means_every_coin():
    for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"):
        assert script_engine.script_allows_asset(None, asset)
        assert script_engine.script_allows_asset([], asset)


def test_a_scope_restricts_to_its_own_coins():
    scope = script_backtest.parse_asset_scope(["BTC", "eth"])
    assert scope == ["BTC", "ETH"]
    assert script_engine.script_allows_asset(scope, "BTC")
    assert script_engine.script_allows_asset(scope, "eth")     # case-insensitive
    assert not script_engine.script_allows_asset(scope, "SOL")


@pytest.mark.parametrize("raw,expect", [
    (None, None),
    ([], None),
    ("", None),
    ('["BTC","ETH"]', ["BTC", "ETH"]),      # JSON, as stored in the DB column
    ("BTC, ETH", ["BTC", "ETH"]),           # forgiving comma form
    (["btc", "BTC", " btc "], ["BTC"]),     # deduped + normalized
    (42, None),                             # garbage degrades to "all"
])
def test_asset_scope_parsing(raw, expect):
    assert script_backtest.parse_asset_scope(raw) == expect


def test_scope_ignores_the_crypto_engines_asset_setting():
    """The whole point: a script's universe is its own property.

    crypto15m.asset_enabled(cfg, ...) reads cfg['crypto15m_assets']; the
    script path must not consult it, so a BTC-only crypto engine still leaves
    an unscoped script seeing every coin."""
    import crypto15m
    cfg = {"crypto15m_assets": ["BTC"]}
    assert not crypto15m.asset_enabled(cfg, "SOL")     # crypto engine: no
    assert script_engine.script_allows_asset(None, "SOL")   # script: yes


def test_neither_engine_module_reads_crypto15m_assets_for_scripts():
    """A regression guard with teeth: if someone reintroduces the coupling,
    this fails even though the behavior tests above might not cover the new
    call site."""
    import inspect
    for mod in (script_engine, script_backtest):
        src = inspect.getsource(mod)
        # Mentions in comments/docstrings are how we explain the trap; what
        # must not come back is a live call gating a script on that key.
        assert "crypto15m.asset_enabled(" not in src, mod.__name__


# ── the whale/momentum feed for decide_signal scripts ────────────────────


def test_signal_feed_is_not_requested_when_no_script_wants_it(monkeypatch):
    monkeypatch.setattr(script_engine, "_enabled_hooks", set(), raising=False)
    assert script_engine.needs_signal_feed() is False


def test_signal_feed_is_requested_for_a_decide_signal_script(monkeypatch):
    import time
    monkeypatch.setattr(script_engine, "_enabled_hooks", {"decide_signal"},
                        raising=False)
    monkeypatch.setattr(script_engine, "_enabled_hooks_at", time.time(),
                        raising=False)
    assert script_engine.needs_signal_feed() is True


def test_a_decide_only_script_does_not_hold_the_signal_feed_up(monkeypatch):
    import time
    monkeypatch.setattr(script_engine, "_enabled_hooks", {"decide", "manage"},
                        raising=False)
    monkeypatch.setattr(script_engine, "_enabled_hooks_at", time.time(),
                        raising=False)
    assert script_engine.needs_signal_feed() is False


def test_a_stale_hook_census_stops_asserting_itself(monkeypatch):
    """If the script engine stops ticking, it must stop keeping the collector
    alive — otherwise a disabled script pins main-engine work forever."""
    import time
    monkeypatch.setattr(script_engine, "_enabled_hooks", {"decide_signal"},
                        raising=False)
    monkeypatch.setattr(
        script_engine, "_enabled_hooks_at",
        time.time() - script_engine._HOOKS_TTL_S - 1, raising=False)
    assert script_engine.needs_signal_feed() is False


# ── the master switch gates ORDERS, not the engine ───────────────────────


def test_the_service_loop_no_longer_gates_the_tick_on_the_master_switch():
    """Shadow scripts must tick as soon as they're enabled. Gating the whole
    engine on scripts_live_enabled meant enabling a script did nothing
    visible until the user found a second switch on the same page."""
    import inspect
    import re
    import service
    src = inspect.getsource(service)
    tick = src[src.index("# ── User-script executor"):]
    tick = tick[:tick.index("# ── Copy-trading executor")]
    guard = tick[tick.index("if ("):tick.index("await script_engine.run_tick")]
    assert "scripts_live_enabled" not in guard, guard


def test_run_tick_parks_an_armed_script_when_the_master_switch_is_off():
    """It must be PARKED, not quietly diverted into the shadow ledger — that
    would corrupt the very track record the ledger exists to build."""
    import inspect
    src = inspect.getsource(script_engine.run_tick)
    assert 'if not live_ok:' in src
    assert '_gate(str(s["id"]), "master_off")' in src
    assert 'enabled = [s for s in enabled if s.get("dry_run")]' in src
