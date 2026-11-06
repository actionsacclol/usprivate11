"""Unit tests for the pure decision logic in trader.py.

Covers position sizing, signal-cost/edge interpretation, the should_trade
gate matrix, limit-cross pricing, Polymarket order/position parsing, the
order->db status machine, and binary settlement payout. These are the
functions whose past regressions are documented in trader.py's module
docstring and inline comments — pin them down.
"""
from __future__ import annotations

import asyncio

import pytest

import trader
from config import merge_with_defaults


@pytest.fixture
def cfg() -> dict:
    """A fully-populated config (all DEFAULT_CONFIG keys present)."""
    return merge_with_defaults({})


# ───────── sizing ──────────────────────────────────────────────────────


def test_size_below_base_edge_uses_min_fraction(cfg):
    # edge 3 <= base_edge 5 -> min_size_fraction 0.02 -> $1000*0.02 = $20
    assert trader._compute_position_usd(1000.0, 3.0, cfg) == 20.0


def test_size_above_max_edge_capped_by_hard_max(cfg):
    # edge 25 >= max_edge 20 -> max_fraction 0.06 -> $60, capped to $50
    assert trader._compute_position_usd(1000.0, 25.0, cfg) == 50.0


def test_size_interpolates_between_edges(cfg):
    # edge 12.5 is the midpoint of [5,20] -> frac 0.04 -> $40
    assert trader._compute_position_usd(1000.0, 12.5, cfg) == pytest.approx(40.0)


# ───────── signal cost / edge interpretation ───────────────────────────


def test_signal_cost_whale_uses_taker_side_price():
    direction, cents = trader._signal_cost_cents(
        {"taker_side": "no", "price": 0.62}, "whale"
    )
    assert (direction, cents) == ("no", 62)


def test_signal_cost_momentum_no_side_inverts_price():
    # momentum stores YES price; a NO trade costs 100 - yes
    direction, cents = trader._signal_cost_cents(
        {"direction": "no", "price": 0.30}, "momentum"
    )
    assert (direction, cents) == ("no", 70)


def test_compute_edge_momentum_no_side():
    # implied for NO at yes=0.30 is 70; conf 80 -> edge 10
    edge = trader._compute_edge(
        {"direction": "no", "price": 0.30, "confidence": 80.0}, "momentum"
    )
    assert edge == pytest.approx(10.0)


# ───────── should_trade gate matrix ────────────────────────────────────


def test_should_trade_passes_clean_whale(cfg):
    sig = {"ticker": "X", "price": 0.60, "confidence": 70.0,
           "taker_side": "yes", "category": "sports"}
    ok, reason = trader.should_trade(sig, "whale", cfg)
    assert ok is True and reason == "ok"


def test_should_trade_blocks_when_source_disabled(cfg):
    cfg["trade_whales"] = False
    ok, reason = trader.should_trade({"price": 0.6, "confidence": 70.0,
                                      "taker_side": "yes"}, "whale", cfg)
    assert ok is False and reason == "whales disabled"


def test_should_trade_blocks_low_confidence(cfg):
    ok, reason = trader.should_trade(
        {"ticker": "X", "price": 0.60, "confidence": 50.0,
         "taker_side": "yes"}, "whale", cfg)
    assert ok is False and reason.startswith("conf ")


def test_should_trade_blocks_entry_above_max(cfg):
    # conf/edge pass but cost 90c > max_entry 85c
    ok, reason = trader.should_trade(
        {"ticker": "X", "price": 0.90, "confidence": 99.0,
         "taker_side": "yes"}, "whale", cfg)
    assert ok is False and reason == "entry 90c > 85c"


def _iso_in_days(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_should_trade_resolution_days_gate(cfg):
    base = {"ticker": "X", "price": 0.60, "confidence": 70.0,
            "taker_side": "yes", "category": "sports"}

    # Default (0) = off: a far-out market still passes.
    assert trader.should_trade({**base, "close_time": _iso_in_days(120)}, "whale", cfg)[0] is True

    cfg["max_resolution_days"] = 30
    ok, reason = trader.should_trade({**base, "close_time": _iso_in_days(120)}, "whale", cfg)
    assert ok is False and "resolves" in reason                       # ~120d > 30d -> blocked
    assert trader.should_trade({**base, "close_time": _iso_in_days(10)}, "whale", cfg)[0] is True   # 10d < 30d
    # Fail-open when the close time is missing/unparseable — don't wrongly block.
    assert trader.should_trade({**base, "close_time": ""}, "whale", cfg)[0] is True
    assert trader.should_trade({**base, "close_time": "garbage"}, "whale", cfg)[0] is True
    assert trader.should_trade(base, "whale", cfg)[0] is True          # no close_time key at all


def test_resolution_gate_applies_in_rules_mode(cfg):
    # Structural gate: it must fire even when custom rules are active (rules mode
    # otherwise short-circuits the quality checks).
    cfg["use_rules"] = True
    cfg["rules"] = []
    cfg["max_resolution_days"] = 30
    base = {"ticker": "X", "price": 0.60, "confidence": 70.0,
            "taker_side": "yes", "category": "sports"}
    ok, reason = trader.should_trade({**base, "close_time": _iso_in_days(120)}, "whale", cfg)
    assert ok is False and "resolves" in reason


def test_can_open_new_entries_gates_on_wallet_lock(monkeypatch):
    import instance_lock
    # No wallet configured -> fail-open (a lock/address problem never blocks trading).
    monkeypatch.setattr(trader, "trading_address", lambda env=None: "")
    assert trader.can_open_new_entries("mainnet")[0] is True

    # Wallet present and we hold the lock -> allowed to open.
    monkeypatch.setattr(trader, "trading_address", lambda env=None: "0xWALLET")
    monkeypatch.setattr(instance_lock, "claim", lambda addr, **k: True)
    assert trader.can_open_new_entries("mainnet")[0] is True

    # A live sibling instance holds the wallet -> back off opening, with a reason.
    monkeypatch.setattr(instance_lock, "claim", lambda addr, **k: False)
    monkeypatch.setattr(instance_lock, "foreign_holder", lambda addr, **k: "9999")
    ok, reason = trader.can_open_new_entries("mainnet")
    assert ok is False and "already being traded" in reason


def test_days_until_close_parses_and_fails_soft():
    assert trader._days_until_close("") is None
    assert trader._days_until_close("nonsense") is None
    d = trader._days_until_close(_iso_in_days(10))
    assert d is not None and 9.8 < d < 10.1


def test_should_trade_category_filter_excludes(cfg):
    cfg["allowed_categories"] = ["politics"]
    ok, reason = trader.should_trade(
        {"ticker": "X", "price": 0.60, "confidence": 70.0,
         "taker_side": "yes", "category": "sports"}, "whale", cfg)
    assert ok is False and "not in allowed set" in reason


def test_should_trade_empty_category_list_blocks_everything(cfg):
    cfg["allowed_categories"] = []
    ok, reason = trader.should_trade(
        {"ticker": "X", "price": 0.60, "confidence": 70.0,
         "taker_side": "yes", "category": "sports"}, "whale", cfg)
    assert ok is False and reason == "no categories enabled"


def test_should_trade_per_source_whale_category_blocks(cfg):
    # Whales restricted to crypto; a sports whale is blocked even though the
    # global allowed_categories is None (no global filter).
    cfg["allowed_whale_categories"] = ["crypto"]
    ok, reason = trader.should_trade(
        {"ticker": "X", "price": 0.60, "confidence": 70.0,
         "taker_side": "yes", "category": "sports"}, "whale", cfg)
    assert ok is False and "whale set" in reason


def test_should_trade_per_source_lets_each_source_keep_its_categories(cfg):
    # The Edge Stack shape: whales only crypto, momentum only sports. A
    # crypto whale passes; the SAME crypto category would block momentum.
    cfg["allowed_whale_categories"] = ["crypto"]
    cfg["allowed_momentum_categories"] = ["sports"]
    ok, _ = trader.should_trade(
        {"ticker": "X", "price": 0.60, "confidence": 70.0,
         "taker_side": "yes", "category": "crypto"}, "whale", cfg)
    assert ok is True
    ok2, reason2 = trader.should_trade(
        {"ticker": "X", "price": 0.30, "confidence": 80.0, "direction": "no",
         "signal_type": "trade_cluster", "category": "crypto"}, "momentum", cfg)
    assert ok2 is False and "momentum set" in reason2


def test_should_trade_momentum_signal_type_not_allowed(cfg):
    sig = {"ticker": "X", "price": 0.30, "confidence": 80.0,
           "direction": "no", "signal_type": "price_move"}
    ok, reason = trader.should_trade(sig, "momentum", cfg)
    assert ok is False and "not allowed" in reason


# ───────── custom rule builder (use_rules) ─────────────────────────────


def test_signal_rule_values_are_direction_adjusted():
    # momentum "no" side: cost is 100-price, edge uses the no-side implied
    sig = {"price": 0.30, "confidence": 80.0, "direction": "no"}
    v = trader._signal_rule_values(sig, "momentum")
    assert v["confidence"] == 80.0
    assert v["costCents"] == 70          # pays the 70c "no" contract
    assert v["edge"] == pytest.approx(10.0)  # 80 - 70 implied


def test_use_rules_replaces_builtin_thresholds(cfg):
    cfg["use_rules"] = True
    cfg["rules"] = [{"field": "confidence", "op": ">=", "value": 60}]
    # cost 90c would fail the built-in max_entry (85c) gate, but rules replace it
    sig = {"ticker": "X", "price": 0.90, "confidence": 70.0,
           "taker_side": "yes", "category": "sports"}
    ok, reason = trader.should_trade(sig, "whale", cfg)
    assert ok is True and reason == "rules pass"


def test_use_rules_failing_condition_blocks(cfg):
    cfg["use_rules"] = True
    cfg["rules"] = [{"field": "edge", "op": ">=", "value": 30}]
    sig = {"ticker": "X", "price": 0.60, "confidence": 70.0, "taker_side": "yes"}
    ok, reason = trader.should_trade(sig, "whale", cfg)
    assert ok is False and reason.startswith("edge ")


def test_use_rules_empty_set_never_trades(cfg):
    cfg["use_rules"] = True
    cfg["rules"] = []
    sig = {"ticker": "X", "price": 0.60, "confidence": 99.0, "taker_side": "yes"}
    ok, reason = trader.should_trade(sig, "whale", cfg)
    assert ok is False and reason == "no entry rules set"


def test_use_rules_still_honors_structural_gates(cfg):
    # source toggle + momentum signal-type still apply even with rules on
    cfg["use_rules"] = True
    cfg["rules"] = [{"field": "confidence", "op": ">=", "value": 1}]
    cfg["trade_whales"] = False
    sig = {"ticker": "X", "price": 0.60, "confidence": 70.0, "taker_side": "yes"}
    assert trader.should_trade(sig, "whale", cfg) == (False, "whales disabled")
    msig = {"ticker": "X", "price": 0.30, "confidence": 80.0,
            "direction": "no", "signal_type": "price_move"}
    ok, reason = trader.should_trade(msig, "momentum", cfg)
    assert ok is False and "not allowed" in reason


# ───────── limit pricing (CLOB quote) ──────────────────────────────────


def test_limit_cross_crosses_ask_plus_margin(cfg, monkeypatch):
    async def _q(_t, _side):
        return {"bid_cents": 58, "ask_cents": 62}
    monkeypatch.setattr(trader, "get_quote", _q)
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 60, cfg))
    # crosses the 62c ask + the 2c cross margin = 64c (under the 85c max-entry)
    assert px == 64


def test_limit_cross_caps_at_max_entry(cfg, monkeypatch):
    cfg["max_entry_price_cents"] = 65
    async def _q(_t, _side):
        return {"bid_cents": 60, "ask_cents": 64}
    monkeypatch.setattr(trader, "get_quote", _q)
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 62, cfg))
    assert px == 65  # 64 + 2 margin would be 66, capped to the 65c max-entry


def test_limit_cross_returns_ask_above_cap_so_caller_skips(cfg, monkeypatch):
    cfg["max_entry_price_cents"] = 60
    async def _q(_t, _side):
        return {"bid_cents": 68, "ask_cents": 72}
    monkeypatch.setattr(trader, "get_quote", _q)
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 58, cfg))
    assert px == 72  # ask above cap is returned bare; execute_signal's band re-check skips it


def test_limit_mid_uses_midpoint(cfg, monkeypatch):
    cfg["order_style"] = "limit_mid"
    async def _q(_t, _side):
        return {"bid_cents": 58, "ask_cents": 62}
    monkeypatch.setattr(trader, "get_quote", _q)
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 60, cfg))
    assert px == 60  # (58 + 62) // 2


def test_pricing_falls_back_when_no_quote(cfg, monkeypatch):
    async def _q(_t, _side):
        return {"bid_cents": None, "ask_cents": None}
    monkeypatch.setattr(trader, "get_quote", _q)
    # fallback = signal 60c + cross_spread_fallback_offset (2) = 62c
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 60, cfg))
    assert px == 62


def test_market_style_pays_up_to_cap(cfg):
    # Market style pays up to the user's max-entry cap (NOT a bare 99c, which the
    # band re-check would then reject on every trade).
    cfg["order_style"] = "market"
    cfg["max_entry_price_cents"] = 85
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 60, cfg))
    assert px == 85
    cfg["max_entry_price_cents"] = 99
    px = asyncio.run(trader._compute_limit_price_cents("X", "yes", 60, cfg))
    assert px == 99


# ───────── Polymarket order / position parsing ─────────────────────────────


def test_parse_polymarket_order_matched():
    # CLOB OpenOrder: 5 of 5 matched @ 0.60 -> cost $3.00 (300c), avg 60c.
    parsed = trader._parse_polymarket_order({
        "status": "MATCHED", "size_matched": "5", "original_size": "5",
        "price": "0.60",
    })
    assert parsed["filled"] == 5
    assert parsed["cost_cents"] == 300
    assert parsed["avg_cents"] == 60.0
    assert parsed["status"] == "executed"
    assert parsed["remaining"] == 0
    assert parsed["place_count"] == 5


def test_parse_polymarket_order_resting_reports_remaining():
    # A live resting order: nothing matched yet, 1 still working.
    parsed = trader._parse_polymarket_order({
        "status": "ORDER_STATUS_LIVE", "size_matched": "0",
        "original_size": "1", "price": "0.45",
    })
    assert parsed["filled"] == 0
    assert parsed["remaining"] == 1
    assert parsed["place_count"] == 1
    assert parsed["status"] == "resting"


def test_parse_polymarket_order_canceled():
    parsed = trader._parse_polymarket_order({
        "status": "CANCELED", "size_matched": "0", "original_size": "3",
        "price": "0.50",
    })
    assert parsed["status"] == "canceled"
    assert parsed["filled"] == 0


@pytest.mark.parametrize("raw,want", [
    ("matched", "executed"), ("ORDER_STATUS_MATCHED", "executed"),
    ("unmatched", "canceled"), ("ORDER_STATUS_UNMATCHED", "canceled"),
    ("killed", "canceled"), ("CANCELED", "canceled"), ("cancelled", "canceled"),
    ("ORDER_STATUS_CANCELED_MARKET_RESOLVED", "canceled"), ("INVALID", "canceled"),
    ("LIVE", "resting"), ("ORDER_STATUS_LIVE", "resting"), ("delayed", "delayed"),
])
def test_parse_polymarket_order_status_is_compared_exactly(raw, want):
    parsed = trader._parse_polymarket_order({"status": raw, "size_matched": "0",
                                             "original_size": "5", "price": "0.5"})
    assert parsed["status"] == want


@pytest.mark.parametrize("raw", ["MATCHING", "rematch_pending", "mismatched"])
def test_parse_polymarket_order_unknown_status_is_not_a_fill(raw):
    # Statuses are compared exactly: a word that merely CONTAINS "match" (as
    # "unmatched" does) is not the venue saying the order filled.
    parsed = trader._parse_polymarket_order({"status": raw, "size_matched": "0",
                                             "original_size": "5", "price": "0.5"})
    assert parsed["status"] != "executed"


def test_parse_polymarket_fill_live_fp_dollars_shape():
    # The EXACT shape /portfolio/fills returned live on a demo fill: size in
    # `count_fp`, price in `*_price_dollars`, legacy `count`/`*_price` absent.
    # The old reconciler keyed on `count` (None here) and counted 0 fills.
    fill = {
        "count_fp": "1.00", "count": None,
        "side": "no", "action": "buy",
        "yes_price_dollars": "0.0100", "no_price_dollars": "0.9900",
        "order_id": "abc",
    }
    parsed = trader._parse_polymarket_fill(fill, default_side="no")
    assert parsed["count"] == 1
    assert parsed["side"] == "no"
    assert parsed["price_cents"] == 99


def test_parse_polymarket_fill_uses_price_cents():
    # get_fills_for_order emits the bought token's price directly as cents,
    # and leaves `side` blank so the caller's known direction is used.
    fill = {"count_fp": "3", "price_cents": 60, "side": "", "action": "buy"}
    parsed = trader._parse_polymarket_fill(fill, default_side="yes")
    assert parsed["count"] == 3
    assert parsed["side"] == "yes"
    assert parsed["price_cents"] == 60


def test_parse_polymarket_fill_out_of_range_price_is_none():
    # A 0c or 100c price is nonsense for a binary contract -> None (skipped).
    fill = {"count_fp": "1.00", "side": "yes", "yes_price_dollars": "1.0000"}
    assert trader._parse_polymarket_fill(fill, "yes")["price_cents"] is None


def test_parse_polymarket_position_negative_qty_is_no_side():
    parsed = trader._parse_polymarket_position({
        "position_fp": -4, "market_exposure_dollars": 2.0,
    })
    assert parsed["filled"] == 4
    assert parsed["side"] == "no"
    assert parsed["cost_cents"] == 200


def test_db_status_executed_is_filled():
    parsed = {"status": "executed", "filled": 5}
    assert trader._db_status_from_order(parsed, target=5) == "filled"


def test_db_status_canceled_with_partial_fill():
    parsed = {"status": "canceled", "filled": 2}
    assert trader._db_status_from_order(parsed, target=5) == "partial"


def test_db_status_resting_unfilled_is_submitted():
    parsed = {"status": "resting", "filled": 0}
    assert trader._db_status_from_order(parsed, target=5) == "submitted"


# ───────── binary settlement payout ────────────────────────────────────


def test_yes_payout_result_yes_is_one():
    assert trader._market_yes_payout({"result": "yes"}) == 1.0


def test_yes_payout_result_no_is_zero():
    assert trader._market_yes_payout({"result": "no"}) == 0.0


def test_yes_payout_unsettled_market_is_none():
    assert trader._market_yes_payout({"status": "open", "result": ""}) is None


def test_yes_payout_uses_settlement_value_dollars():
    m = {"status": "settled", "result": "", "settlement_value_dollars": 0.5}
    assert trader._market_yes_payout(m) == 0.5


def test_yes_payout_settlement_value_is_dollars():
    # The cache writes settlement_value FROM settlement_value_dollars, so it's
    # dollars [0,1] — NOT cents. A 0.75 dollars value is a 75c YES payout.
    m = {"status": "finalized", "result": "", "settlement_value": 0.75}
    assert trader._market_yes_payout(m) == 0.75


def test_yes_payout_out_of_range_settlement_is_rejected():
    # A junk/legacy out-of-[0,1] value must NOT be guessed at (the old /100 path
    # turned a 1.0 winner into a 0.01 loss).
    m = {"status": "finalized", "result": "", "settlement_value": 75}
    assert trader._market_yes_payout(m) is None


def test_yes_payout_none_market_is_none():
    assert trader._market_yes_payout(None) is None
