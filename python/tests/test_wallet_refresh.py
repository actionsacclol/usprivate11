"""The wallet panel's Refresh and the figures beside the balance."""
import asyncio

import polymarket_api as api
import service
import trader
import us_api as us


def test_the_extra_figures_come_from_the_balance_reply():
    reply = {"balances": [{
        "currency": "USD", "buyingPower": 40, "assetNotional": 10, "openOrders": 7.5,
        "pendingWithdrawals": [{"id": "a", "balance": 20}, {"id": "b", "balance": "5.5"}],
    }]}
    assert us.balance_detail(reply) == {
        "openOrdersUsd": 7.5, "pendingWithdrawalCount": 2, "pendingWithdrawalsUsd": 25.5}


def test_a_field_the_reply_lacks_is_unknown_not_zero():
    assert us.balance_detail({}) == {
        "openOrdersUsd": None, "pendingWithdrawalCount": 0, "pendingWithdrawalsUsd": None}
    assert us.balance_detail({"balances": [{"currency": "USD", "buyingPower": 1}]})["openOrdersUsd"] is None
    # A withdrawal with no readable amount still counts, and the sum isn't understated.
    partial = us.balance_detail({"balances": [{"currency": "USD", "pendingWithdrawals": [{"balance": 5}, {}]}]})
    assert partial["pendingWithdrawalCount"] == 2 and partial["pendingWithdrawalsUsd"] is None


def test_a_failed_read_keeps_the_time_of_the_last_good_one(monkeypatch):
    monkeypatch.setattr(trader, "_balance_cache", {})
    monkeypatch.setattr(trader, "credentials_present", lambda: True)

    async def good():
        return {"balance": 4000, "portfolio_value": 1000, "detail": {"openOrdersUsd": 7.5}}

    async def bad():
        raise api.PolymarketAPIError(500, "down")

    async def run():
        monkeypatch.setattr(trader, "get_balance", good)
        await trader.refresh_balance({}, force=True)
        first = trader.wallet_status()
        assert first["readOk"] is True and first["readAt"] and first["openOrdersUsd"] == 7.5
        monkeypatch.setattr(trader, "get_balance", bad)
        assert await trader.refresh_balance({}, force=True) == (4000, 1000)
        after = trader.wallet_status()
        assert after["readOk"] is False
        assert after["readAt"] == first["readAt"] and after["openOrdersUsd"] == 7.5

    asyncio.run(run())


def test_refresh_reads_now_even_inside_the_interval(monkeypatch):
    monkeypatch.setattr(trader, "_balance_cache", {})
    monkeypatch.setattr(service.polymarket_auth, "credentials_present", lambda *a: True)
    reads = []

    async def read():
        reads.append(1)
        return {"balance": 100 * len(reads), "portfolio_value": 0}

    async def snapshot():
        return {"cashUsd": trader._balance_cache[trader.get_env()]["cents"] / 100}

    monkeypatch.setattr(trader, "get_balance", read)
    monkeypatch.setattr(service, "_build_account_snapshot", snapshot)

    async def run():
        await trader.refresh_balance(service.STATE.cfg or {}, force=True)
        return await service._h_accountRefresh({})

    assert asyncio.run(run()) == {"cashUsd": 2.0}
    assert len(reads) == 2


def test_refresh_with_no_key_reads_nothing(monkeypatch):
    monkeypatch.setattr(service.polymarket_auth, "credentials_present", lambda *a: False)

    async def boom(*_a, **_k):
        raise AssertionError("no key, nothing to read")

    async def snapshot():
        return {"ok": True}

    monkeypatch.setattr(trader, "refresh_balance", boom)
    monkeypatch.setattr(service, "_build_account_snapshot", snapshot)
    assert asyncio.run(service._h_accountRefresh({})) == {"ok": True}
