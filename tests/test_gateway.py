"""
Test suite for the Idempotency Gateway.
Covers all five user stories + edge cases.
"""

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from app import app, idempotency_store, in_flight

BASE_URL = "http://test"
ENDPOINT = "/process-payment"


@pytest.fixture(autouse=True)
def clear_stores():
    """Reset in-memory stores before every test."""
    idempotency_store.clear()
    in_flight.clear()
    yield
    idempotency_store.clear()
    in_flight.clear()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL)


# ──────────────────────────────────────────────────────────────────────────────
# User Story 1 – Happy Path (first transaction)
# ──────────────────────────────────────────────────────────────────────────────
class TestFirstTransaction:
    async def test_returns_201_and_success_message(self, client):
        async with client as c:
            resp = await c.post(
                ENDPOINT,
                json={"amount": 100, "currency": "GHS"},
                headers={"Idempotency-Key": "key-us1-a"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["message"] == "Charged 100 GHS"
        assert body["status"] == "success"
        assert "transaction_id" in body

    async def test_response_contains_idempotency_key(self, client):
        async with client as c:
            resp = await c.post(
                ENDPOINT,
                json={"amount": 50, "currency": "USD"},
                headers={"Idempotency-Key": "key-us1-b"},
            )
        assert resp.json()["idempotency_key"] == "key-us1-b"

    async def test_missing_idempotency_key_header_returns_400(self, client):
        async with client as c:
            resp = await c.post(ENDPOINT, json={"amount": 100, "currency": "GHS"})
        assert resp.status_code == 400
        assert "Idempotency-Key" in resp.json()["detail"]

    async def test_amount_must_be_positive(self, client):
        async with client as c:
            resp = await c.post(
                ENDPOINT,
                json={"amount": -10, "currency": "GHS"},
                headers={"Idempotency-Key": "key-neg"},
            )
        assert resp.status_code == 422  # Pydantic validation

    async def test_unsupported_currency_returns_422(self, client):
        async with client as c:
            resp = await c.post(
                ENDPOINT,
                json={"amount": 10, "currency": "XYZ"},
                headers={"Idempotency-Key": "key-cur"},
            )
        assert resp.status_code == 422


# ──────────────────────────────────────────────────────────────────────────────
# User Story 2 – Duplicate attempt (idempotency logic)
# ──────────────────────────────────────────────────────────────────────────────
class TestDuplicateRequest:
    async def test_duplicate_returns_same_response(self, client):
        headers = {"Idempotency-Key": "key-dup-1"}
        payload = {"amount": 200, "currency": "GHS"}

        async with client as c:
            first = await c.post(ENDPOINT, json=payload, headers=headers)
            second = await c.post(ENDPOINT, json=payload, headers=headers)

        assert first.status_code == second.status_code
        assert first.json() == second.json()

    async def test_duplicate_returns_x_cache_hit_true(self, client):
        headers = {"Idempotency-Key": "key-dup-2"}
        payload = {"amount": 75, "currency": "USD"}

        async with client as c:
            await c.post(ENDPOINT, json=payload, headers=headers)
            second = await c.post(ENDPOINT, json=payload, headers=headers)

        assert second.headers.get("x-cache-hit") == "true"

    async def test_first_request_has_no_cache_hit_header(self, client):
        async with client as c:
            first = await c.post(
                ENDPOINT,
                json={"amount": 30, "currency": "EUR"},
                headers={"Idempotency-Key": "key-dup-3"},
            )
        assert "x-cache-hit" not in first.headers

    async def test_many_retries_all_return_same_response(self, client):
        headers = {"Idempotency-Key": "key-dup-4"}
        payload = {"amount": 999, "currency": "GBP"}

        async with client as c:
            responses = [
                await c.post(ENDPOINT, json=payload, headers=headers)
                for _ in range(5)
            ]

        bodies = [r.json() for r in responses]
        assert all(b == bodies[0] for b in bodies)
        # All retries after the first should be cache hits
        for r in responses[1:]:
            assert r.headers.get("x-cache-hit") == "true"


# ──────────────────────────────────────────────────────────────────────────────
# User Story 3 – Different body, same key (conflict detection)
# ──────────────────────────────────────────────────────────────────────────────
class TestConflictDetection:
    async def test_same_key_different_amount_returns_409(self, client):
        headers = {"Idempotency-Key": "key-conflict-1"}

        async with client as c:
            await c.post(ENDPOINT, json={"amount": 100, "currency": "GHS"}, headers=headers)
            second = await c.post(ENDPOINT, json={"amount": 500, "currency": "GHS"}, headers=headers)

        assert second.status_code == 409
        assert "different request body" in second.json()["detail"]

    async def test_same_key_different_currency_returns_409(self, client):
        headers = {"Idempotency-Key": "key-conflict-2"}

        async with client as c:
            await c.post(ENDPOINT, json={"amount": 100, "currency": "GHS"}, headers=headers)
            second = await c.post(ENDPOINT, json={"amount": 100, "currency": "USD"}, headers=headers)

        assert second.status_code == 409

    async def test_same_key_same_body_is_not_conflict(self, client):
        headers = {"Idempotency-Key": "key-conflict-3"}
        payload = {"amount": 100, "currency": "GHS"}

        async with client as c:
            await c.post(ENDPOINT, json=payload, headers=headers)
            second = await c.post(ENDPOINT, json=payload, headers=headers)

        assert second.status_code == 201  # replayed, not conflict


# ──────────────────────────────────────────────────────────────────────────────
# Bonus – Race condition / in-flight guard
# ──────────────────────────────────────────────────────────────────────────────
class TestRaceCondition:
    async def test_concurrent_requests_same_key_only_process_once(self, client):
        headers = {"Idempotency-Key": "key-race-1"}
        payload = {"amount": 150, "currency": "NGN"}

        async with client as c:
            results = await asyncio.gather(
                c.post(ENDPOINT, json=payload, headers=headers),
                c.post(ENDPOINT, json=payload, headers=headers),
                c.post(ENDPOINT, json=payload, headers=headers),
            )

        # All should succeed
        assert all(r.status_code == 201 for r in results)
        # All bodies identical
        bodies = [r.json() for r in results]
        assert all(b["transaction_id"] == bodies[0]["transaction_id"] for b in bodies)

    async def test_concurrent_different_keys_all_succeed(self, client):
        payloads = [
            ({"Idempotency-Key": f"key-par-{i}"}, {"amount": i * 10, "currency": "GHS"})
            for i in range(1, 6)
        ]

        async with client as c:
            results = await asyncio.gather(
                *[c.post(ENDPOINT, json=p, headers=h) for h, p in payloads]
            )

        assert all(r.status_code == 201 for r in results)


# ──────────────────────────────────────────────────────────────────────────────
# Developer's Choice – Key TTL / expiry metadata
# ──────────────────────────────────────────────────────────────────────────────
class TestDeveloperChoiceFeatures:
    async def test_cached_entry_has_timestamp(self, client):
        key = "key-ttl-1"
        async with client as c:
            await c.post(
                ENDPOINT,
                json={"amount": 10, "currency": "USD"},
                headers={"Idempotency-Key": key},
            )
        assert "cached_at" in idempotency_store[key]

    async def test_idempotency_key_max_length_enforced(self, client):
        long_key = "x" * 256
        async with client as c:
            resp = await c.post(
                ENDPOINT,
                json={"amount": 10, "currency": "USD"},
                headers={"Idempotency-Key": long_key},
            )
        assert resp.status_code == 400

    async def test_health_endpoint(self, client):
        async with client as c:
            resp = await c.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "keys_cached" in data
        assert "status" in data
