"""The trading-readiness funding floor.

What a Polymarket order costs is PRICE-DEPENDENT:

    cost = max(orderMinSize shares, ceil($1 / price) shares) x price

so at <=20c the $1 notional binds ($1.00 minimum order) and at 99c the
5-share minimum binds (~$4.95). A flat "you need $5" is the 99c worst case
and wrongly tells a small but perfectly tradable wallet that it cannot trade
— the bug these tests pin (reported against a $1.64 balance, 2026-07-25).
"""
from __future__ import annotations

import asyncio

import pytest

import polymarket_api


MIN_SHARES = 5
MIN_NOTIONAL = 1.00


def order_cost_usd(price_cents: int, min_shares: int = MIN_SHARES) -> float:
    """The cheapest order the exchange will accept at this price."""
    need_for_notional = -(-100 // price_cents)          # ceil($1 / price)
    shares = max(min_shares, need_for_notional)
    return round(shares * price_cents / 100.0, 4)


@pytest.mark.parametrize("cents,expected", [
    (1, 1.00),     # 100 shares to clear $1
    (5, 1.00),     # 20 shares
    (10, 1.00),    # 10 shares
    (20, 1.00),    # exactly 5 shares — both constraints meet
    (33, 1.65),    # 5-share minimum now binds
    (50, 2.50),
    (90, 4.50),
    (99, 4.95),    # the true worst case
])
def test_minimum_order_cost_is_price_dependent(cents, expected):
    assert order_cost_usd(cents) == pytest.approx(expected)


def test_cheapest_possible_order_is_one_dollar_not_five():
    """The floor across ALL prices is $1, not $5 — the whole point."""
    cheapest = min(order_cost_usd(c) for c in range(1, 100))
    assert cheapest == pytest.approx(1.00)


def test_worst_case_is_just_under_five_dollars():
    dearest = max(order_cost_usd(c) for c in range(1, 100))
    assert dearest == pytest.approx(4.95)


# ───────── the readiness message ─────────────────────────────────────────
