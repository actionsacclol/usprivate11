"""Retry-idempotency guard for the signed request layer.

A POST /v1/orders is a non-idempotent mutation: if the response is lost to a timeout
or 5xx, the order may already have reached the exchange. Blindly re-firing it would
risk a DOUBLE FILL, so place_limit_order calls `_authed_request(..., idempotent=
False)` and that path must NOT retry on an ambiguous failure. Reads stay retried.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import polymarket_api as pa


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _stub_api(monkeypatch):
    async def _anoop(*a, **k):
        return None
    monkeypatch.setattr(pa, "_note_net_fail", _anoop)
    monkeypatch.setattr(pa, "_note_net_ok", lambda *a, **k: None)
    # A 503 opens the restart window, which would refuse orders for later tests.
    monkeypatch.setattr(pa, "_note_restart", lambda *a, **k: None)
    monkeypatch.setattr(pa.auth, "auth_headers", lambda *a, **k: {})
    # Skip the real backoff sleeps so retry tests are instant.
    monkeypatch.setattr(pa.asyncio, "sleep", _anoop)


class _TimeoutClient:
    def __init__(self):
        self.calls = 0

    async def request(self, *a, **k):
        self.calls += 1
        raise httpx.TimeoutException("simulated network timeout")


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self._payload = {"errorMsg": "server error"}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _StatusClient:
    def __init__(self, status):
        self.calls = 0
        self.status = status

    async def request(self, *a, **k):
        self.calls += 1
        return _Resp(self.status)


def _use_client(monkeypatch, client):
    async def _gc():
        return client
    monkeypatch.setattr(pa, "_get_client", _gc)


def test_post_order_not_retried_on_timeout(monkeypatch):
    client = _TimeoutClient()
    _use_client(monkeypatch, client)
    with pytest.raises(httpx.TimeoutException):
        _run(pa._authed_request("POST", "/v1/orders", json_body={"x": 1}, idempotent=False))
    assert client.calls == 1  # fired exactly once — no double-fill window


def test_post_order_not_retried_on_5xx(monkeypatch):
    client = _StatusClient(500)
    _use_client(monkeypatch, client)
    with pytest.raises(pa.PolymarketAPIError):
        _run(pa._authed_request("POST", "/v1/orders", json_body={"x": 1}, idempotent=False))
    assert client.calls == 1


def test_idempotent_get_still_retries_on_timeout(monkeypatch):
    client = _TimeoutClient()
    _use_client(monkeypatch, client)
    with pytest.raises(httpx.TimeoutException):
        _run(pa._authed_request("GET", "/v1/portfolio/positions"))
    assert client.calls == pa.MAX_RETRIES  # reads are safe to retry


def test_idempotent_get_still_retries_on_5xx(monkeypatch):
    client = _StatusClient(503)
    _use_client(monkeypatch, client)
    with pytest.raises(pa.PolymarketAPIError):
        _run(pa._authed_request("GET", "/v1/portfolio/positions"))
    assert client.calls == pa.MAX_RETRIES





def _client_returning(*responses):
    calls = []

    class _C:
        async def request(self, *a, **k):
            calls.append(1)
            return responses[min(len(calls), len(responses)) - 1]
    return _C(), calls


def _patch_client(monkeypatch, client):
    async def _get():
        return client
    monkeypatch.setattr(pa, "_get_client", _get)
    async def _creds():
        return None
    monkeypatch.setattr(pa, "ensure_api_creds", _creds)
    monkeypatch.setattr(pa.auth, "auth_headers", lambda *a, **k: {})
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(pa.asyncio, "sleep", _no_sleep)
