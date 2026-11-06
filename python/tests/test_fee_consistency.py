"""Every realised-fee function charges what the venue charges.

Audit 2026-10-11: there are ~seven fee implementations and nothing checked
them against each other. With a taker-rebate tier set, the main engine's
realised fees (trader._position_fee_usd / _sell_fee_usd, which copy and
script positions also use) were netted of the rebate while the crypto
engine's (crypto15m_trader._order_fee_usd) were not, so the same fill booked
two different fees. Polymarket charges the full fee at match time and pays
the rebate back separately, once a day, so realised accounting books the
full fee everywhere; only forward-looking estimates net the tier.

The bot's own categories (climate, entertainment, world) were also missing
from the fee table: the backtests charged them crypto's 0.07, the live
accounting 0.05, and the Terminal called the fee unknown.

Pure functions only: no network, no database.
"""
from __future__ import annotations

import pytest

import backtest
import crypto15m_trader
import script_engine
import terminal_trade
import trader

# Polymarket US's taker rate, copied from docs.polymarket.us/fees on 2026-11-05 —
# deliberately a literal, not backtest.CATEGORY_TAKER_FEE, so a typo in the table
# is caught rather than echoed.
VENUE_RATE = {
    cat: 0.0695 for cat in (
        "crypto", "sports", "finance", "politics", "mentions", "tech", "economics",
        "culture", "weather", "other", "geopolitics",
        # the bot's own category ids (categorize.py)
        "climate", "entertainment", "world",
    )
}
PRICES = [0.03, 0.25, 0.5, 0.8, 0.97]
CONTRACTS = 100


@pytest.fixture
def rebate_tier():
    """A taker rebate tier (15%) is set, the case where the engines split."""
    backtest.set_taker_rebate(15)
    try:
        yield
    finally:
        backtest.set_taker_rebate(0)


def _realised_fees_per_contract(cat: str, price: float) -> dict[str, float]:
    cents = price * 100.0
    pos = {"category": cat, "filled_contracts": CONTRACTS,
           "avg_fill_price_cents": cents, "cost_usd": price * CONTRACTS}
    out = {
        "trader._position_fee_usd": trader._position_fee_usd(pos) / CONTRACTS,
        "trader._sell_fee_usd": trader._sell_fee_usd(pos, CONTRACTS, cents) / CONTRACTS,
        "script_engine._shadow_fee": script_engine._shadow_fee(price, cat),
        "backtest.venue_fee_per_contract":
            backtest.venue_fee_per_contract(price, category=cat),
    }
    if cat == "crypto":
        out["crypto15m_trader._order_fee_usd"] = (
            crypto15m_trader._order_fee_usd({}, CONTRACTS, cents) / CONTRACTS)
    return out


@pytest.mark.parametrize("price", PRICES)
@pytest.mark.parametrize("cat", sorted(VENUE_RATE))
def test_every_realised_fee_function_charges_the_venue_fee(cat, price, rebate_tier):
    want = VENUE_RATE[cat] * price * (1.0 - price)
    got = _realised_fees_per_contract(cat, price)
    off = {k: v for k, v in got.items() if v is None or abs(v - want) > 1e-6}
    assert not off, f"{cat} @ {price}: want {want:.6f}, got {off}"


@pytest.mark.parametrize("cat", ["", "not-a-real-category"])
def test_an_unknown_category_has_one_shared_fallback(cat):
    """trader.py and the backtests rate an unknown category the same way."""
    other = VENUE_RATE["other"] * 0.25
    assert backtest.polymarket_fee_per_contract(0.5, category=cat) == pytest.approx(other)
    pos = {"category": cat, "filled_contracts": 1, "avg_fill_price_cents": 50.0}
    assert trader._position_fee_usd(pos) == pytest.approx(other)


def test_forward_estimates_still_net_the_rebate_tier(rebate_tier):
    """Edge gates and backtests are where the tier belongs."""
    full = backtest.venue_fee_per_contract(0.5, category="crypto")
    assert backtest.polymarket_fee_per_contract(0.5, category="crypto") == \
        pytest.approx(full * 0.85)


def test_the_terminal_reads_the_fee_off_the_market_and_never_guesses_one():
    """The market states its coefficient, so the terminal needs no category table,
    and a market that states none has an unknown fee rather than a guessed one."""
    sched = {"enabled": True, "rate": 0.0695, "exponent": 1.0, "rebateRate": 0.0}
    assert terminal_trade._fee_per_share(50.0, "crypto", sched) == pytest.approx(0.017375)
    assert terminal_trade._fee_per_share(50.0, "crypto", {"enabled": False}) == 0.0
    assert terminal_trade._fee_per_share(50.0, "crypto", None) is None


@pytest.mark.parametrize("price,contracts,want", [
    # 0.0695 x 100 x .5 x .5 = 1.7375, an ordinary round up to 1.74
    (50.0, 100, 1.74),
    # 0.1 x 1 x .5 x .5 = 0.025, a tie: banker's rounding goes to the even 0.02
    (50.0, 1, 0.02),
    # 0.1 x 3 x .5 x .5 = 0.075, a tie that goes up to the even 0.08
    (50.0, 3, 0.08),
])
def test_the_taker_fee_rounds_to_the_cent_the_way_the_exchange_does(price, contracts, want):
    rate = 0.0695 if contracts == 100 else 0.1
    sched = {"enabled": True, "rate": rate, "exponent": 1.0, "rebateRate": 0.0}
    assert terminal_trade.taker_fee_usd(price, contracts, sched) == want


def test_the_taker_fee_stays_unknown_when_the_rate_is():
    assert terminal_trade.taker_fee_usd(50.0, 10, None) is None
    assert terminal_trade.taker_fee_usd(50.0, 10, {"enabled": False}) == 0.0
