"""Sanity tests for the strategy presets."""
from __future__ import annotations


from config import (
    CRYPTO15M_PRESETS,
    crypto15m_preset_config, merge_with_defaults,
)


def _by_id(presets):
    return {p["id"]: p for p in presets}


# ───────── main-engine presets ─────────────────────────────────────────


# ───────── 15-minute crypto presets ───────────────────────────────────


def test_crypto15m_presets_exist():
    ids = {p["id"] for p in CRYPTO15M_PRESETS}
    assert {"c15-favorite", "c15-contrarian"} <= ids


def test_contrarian_preset_sets_contrarian_mode():
    cfg = crypto15m_preset_config("c15-contrarian")
    assert cfg is not None
    assert cfg["crypto15m_direction_mode"] == "contrarian"
    assert cfg["crypto15m_exit_threshold"] == 0.0  # holds to settlement


def test_deep_favorite_preset_uses_95c_band():
    cfg = crypto15m_preset_config("c15-favorite")
    assert cfg["crypto15m_entry_threshold"] == 0.95
    assert cfg["crypto15m_entry_max"] == 0.98


def test_crypto15m_preset_descriptions_carry_the_remeasurement():
    for p in CRYPTO15M_PRESETS:
        assert "2026-09-01" in p["description"], p["id"]


def test_unknown_preset_is_none():
    assert crypto15m_preset_config("nope") is None
