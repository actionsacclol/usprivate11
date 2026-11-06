"""What the audit of the app against docs.polymarket.us pinned (2026-11-05).

The docs deprecate `outcomes`, `outcomePrices`, `archived`, `marketType` and friends, and
said on 2026-06-26 they would go three days later. They are still sent, but nothing here
may depend on them: `marketSides` replaces them. The rows are shaped like live answers.
"""
from __future__ import annotations

import pytest

import backtest
import us_api as us

DEPRECATED = ("outcomes", "outcomePrices", "marketType", "sportsMarketTypeV2", "archived",
              "manualActivation", "gameStartTime")


def _market(**over) -> dict:
    row = {
        "slug": "aec-nfl-lac-ten-2026-11-08", "question": "Chargers vs. Titans", "category": "sports",
        "status": "MARKET_STATUS_OPEN", "active": True, "closed": False, "archived": False,
        "orderPriceMinTickSize": 0.01, "minimumTradeQty": 1, "feeCoefficient": 0.0695,
        "endDate": "2026-11-09T00:00:00Z", "manualActivation": False, "gameStartTime": "",
        "marketType": "", "sportsMarketTypeV2": "",
        "outcomes": '["Chargers","Titans"]', "outcomePrices": '["0.62","0.38"]',
        "marketSides": [
            {"long": True, "description": "Chargers", "price": "0.62"},
            {"long": False, "description": "Titans", "price": "0.38"},
        ],
    }
    row.update(over)
    return row


def _without_deprecated(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in DEPRECATED}


@pytest.mark.parametrize("row", [
    _market(),
    _market(bestBidQuote={"value": "0.61", "currency": "USD"}, bestAskQuote={"value": "0.63", "currency": "USD"}),
    _market(status="MARKET_STATUS_RESOLVED", closed=True, outcomePrices='["1","0"]', marketSides=[
        {"long": True, "description": "Chargers", "price": "1"},
        {"long": False, "description": "Titans", "price": "0"}]),
    _market(status="MARKET_STATUS_RESOLVED", closed=True, outcomePrices='["0","1"]', marketSides=[
        {"long": True, "description": "Chargers", "price": "0"},
        {"long": False, "description": "Titans", "price": "1"}]),
    _market(status="MARKET_STATUS_RESOLVED", closed=True, outcomePrices='["0.5","0.5"]', marketSides=[
        {"long": True, "description": "Chargers", "price": "0.5"},
        {"long": False, "description": "Titans", "price": "0.5"}]),
    _market(outcomePrices='["0.02"]', marketSides=[
        {"long": True, "description": "Chargers", "price": "0.02"},
        {"long": False, "description": "Titans", "price": None}]),
    _market(status="MARKET_STATUS_RESOLVING", closed=True),
])
def test_a_market_reads_the_same_once_the_deprecated_fields_are_gone(row):
    assert us.normalize_market(_without_deprecated(row)) == us.normalize_market(row)


def test_outcome_prices_prefers_the_sides_and_falls_back():
    assert us.outcome_prices(_market(outcomePrices='["0.1","0.9"]')) == ["0.62", "0.38"]
    assert us.outcome_prices({"outcomePrices": '["0.1","0.9"]'}) == ["0.1", "0.9"]
    assert us.outcome_prices({}) == []


def test_one_priced_side_fills_in_the_other():
    """Both orders live: `["0.02"]` is YES on one row and `["0.01"]` is NO on another."""
    yes_only = _market(outcomePrices='["0.02"]', marketSides=[
        {"long": True, "description": "A", "price": "0.0200"}, {"long": False, "description": "B", "price": None}])
    market, _ = us.normalize_market(yes_only)
    assert market["no_price_dollars"] == pytest.approx(0.98)
    no_only = _market(outcomePrices='["0.01"]', marketSides=[
        {"long": True, "description": "A", "price": None}, {"long": False, "description": "B", "price": "0.01"}])
    market, _ = us.normalize_market(no_only)
    assert market["no_price_dollars"] == pytest.approx(0.01)
    assert market["last_price_dollars"] == pytest.approx(0.99)


def test_a_halted_market_is_not_open():
    market, _ = us.normalize_market(_market(status="MARKET_STATUS_HALTED"))
    assert market["status"] == "inactive"


@pytest.mark.parametrize("state,kept", [
    ("TRADE_STATE_CLEARED", True), ("TRADE_STATE_NEW", True), ("", True),
    ("TRADE_STATE_BUSTED", False), ("TRADE_STATE_REJECTED", False),
])
def test_a_voided_trade_is_not_a_fill(state, kept):
    row = us.activity_row({"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": "t1", "marketSlug": "m", "state": state, "qtyDecimal": "2",
        "price": {"value": "0.5", "currency": "USD"}, "createTime": "2026-11-05T00:00:00Z"}})
    assert (row is not None) is kept


@pytest.mark.parametrize("category", ["sports", "politics", "finance", "geopolitics", "culture", "", "nonsense"])
def test_every_category_pays_the_us_rate(category):
    assert backtest.taker_fee_rate(category) == 0.0695
    assert backtest.venue_fee_per_contract(0.5, category=category) == pytest.approx(0.017375)


def test_the_fee_page_worked_example():
    """docs fees.md, example 2: buying 1,000 at 65c costs $15.81."""
    assert round(backtest.venue_fee_per_contract(0.65, category="sports") * 1000, 2) == 15.81


def test_rebate_tiers_are_the_us_ones():
    assert backtest.TAKER_REBATE_TIERS == (0.0, 3.0, 5.0, 10.0, 15.0, 30.0)
    backtest.set_taker_rebate(50)
    try:
        assert backtest.TAKER_REBATE == 0.30
    finally:
        backtest.set_taker_rebate(0)


def test_a_market_stamped_with_its_own_rate_keeps_it():
    """Table Tennis is 0.10, which no category can say."""
    market, _ = us.normalize_market(_market(feeCoefficient=0.10))
    assert market["fee_schedule"]["rate"] == 0.10
