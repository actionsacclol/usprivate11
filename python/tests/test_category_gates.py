"""Category filtering: the UI's keys must reach the trader's gates.

Reported 2026-07-25: toggling Categories in Settings appeared to have no
effect, with the backend logging `whale: category 'sports' not in whale set`.

The report concluded the UI wrote a key the trader ignored. That was wrong —
the trader DOES read `allowed_categories`. The real fault was that the
PER-SOURCE lists (`allowed_whale_categories` / `allowed_momentum_categories`)
were written only by strategy presets, enforced by the trader, and had no UI
control at all — so a preset could narrow an engine invisibly and the visible
control looked broken.

These tests pin the whole chain: the camelCase keys the renderer writes must
survive merge_with_defaults into the snake_case keys should_trade() reads,
and each gate must behave as documented.
"""
from __future__ import annotations

import pytest

import config as cfgmod
import trader


def _signal(category: str) -> dict:
    return {
        "category": category, "ticker": "0xt", "confidence": 99.0,
        "edge_pts": 99.0, "price": 0.5, "close_time": "",
        # must be in the default allowed_momentum_signal_types, or the
        # momentum path bails before it ever reaches the category gates
        "signal_type": "trade_cluster", "dollar_value": 10_000.0,
    }


def _cfg(**camel) -> dict:
    return cfgmod.merge_with_defaults(dict(camel))


# ───────── the renderer's keys reach the engine ──────────────────────────


def test_camel_keys_from_the_ui_map_to_the_snake_keys_the_trader_reads():
    merged = _cfg(
        allowedCategories=["sports", "crypto"],
        allowedWhaleCategories=["crypto"],
        allowedMomentumCategories=["sports"],
    )
    assert merged["allowed_categories"] == ["sports", "crypto"]
    assert merged["allowed_whale_categories"] == ["crypto"]
    assert merged["allowed_momentum_categories"] == ["sports"]


# ───────── the global filter (what the main picker writes) ───────────────


def test_global_filter_is_enforced():
    cfg = _cfg(allowedCategories=["crypto"])
    ok, why = trader.should_trade(_signal("sports"), "whale", cfg)
    assert ok is False
    assert "not in allowed set" in why


def test_global_filter_allows_a_listed_category():
    cfg = _cfg(allowedCategories=["sports"])
    ok, _why = trader.should_trade(_signal("sports"), "whale", cfg)
    assert ok is True


def test_global_none_means_no_filter():
    cfg = _cfg(allowedCategories=None)
    ok, _why = trader.should_trade(_signal("politics"), "whale", cfg)
    assert ok is True


def test_global_empty_list_means_trade_nothing():
    """The trap the UI now warns about explicitly."""
    cfg = _cfg(allowedCategories=[])
    ok, why = trader.should_trade(_signal("crypto"), "whale", cfg)
    assert ok is False
    assert "no categories enabled" in why


# ───────── the per-source filters (previously invisible) ─────────────────


def test_per_source_list_narrows_within_the_global_one():
    """The reported symptom: 'sports' allowed globally, still blocked for
    whales because a preset had set the whale list to crypto only."""
    cfg = _cfg(
        allowedCategories=["sports", "crypto"],
        allowedWhaleCategories=["crypto"],
    )
    ok, why = trader.should_trade(_signal("sports"), "whale", cfg)
    assert ok is False
    assert "not in whale set" in why, why
    # …while the category the per-source list does allow still passes.
    ok2, _ = trader.should_trade(_signal("crypto"), "whale", cfg)
    assert ok2 is True


def test_per_source_none_adds_no_restriction():
    """What the new 'Any' button writes — it must unblock the case above."""
    cfg = _cfg(
        allowedCategories=["sports", "crypto"],
        allowedWhaleCategories=None,
    )
    ok, _why = trader.should_trade(_signal("sports"), "whale", cfg)
    assert ok is True


def test_per_source_empty_list_halts_that_engine():
    cfg = _cfg(allowedWhaleCategories=[])
    ok, why = trader.should_trade(_signal("crypto"), "whale", cfg)
    assert ok is False
    assert "no whale categories enabled" in why


def test_convergence_shares_the_whale_list():
    cfg = _cfg(tradeConvergence=True, allowedWhaleCategories=["crypto"])
    ok, why = trader.should_trade(_signal("sports"), "convergence", cfg)
    assert ok is False
    assert "not in convergence set" in why or "not in whale set" in why


def test_momentum_uses_its_own_list_not_the_whale_one():
    """A whale-only restriction must not silently gag momentum."""
    cfg = _cfg(
        allowedCategories=["sports"],
        allowedWhaleCategories=["crypto"],     # would block sports for whales
        allowedMomentumCategories=None,        # …but momentum is unrestricted
    )
    ok, _why = trader.should_trade(_signal("sports"), "momentum", cfg)
    assert ok is True


@pytest.mark.parametrize("source", ["whale", "momentum"])
def test_global_and_per_source_compose_with_AND(source):
    """Passing one but not the other must still block."""
    key = ("allowedWhaleCategories" if source == "whale"
           else "allowedMomentumCategories")
    cfg = _cfg(**{"allowedCategories": ["crypto"], key: ["sports"]})
    # sports passes the per-source list but fails the global one
    ok, _ = trader.should_trade(_signal("sports"), source, cfg)
    assert ok is False
    # crypto passes the global list but fails the per-source one
    ok2, _ = trader.should_trade(_signal("crypto"), source, cfg)
    assert ok2 is False
