"""
Standalone logic verification for the Idempotency Gateway.
Uses only Python stdlib — no FastAPI/httpx required.
Mirrors the gateway logic so all tests can run in a network-free environment.

Run:
    python3 tests/test_logic.py
"""

import asyncio
import hashlib
import json
import time
import unittest

# ── Mirror of gateway logic ───────────────────────────────────────────────────
SUPPORTED_CURRENCIES = {"GHS", "USD", "EUR", "GBP", "NGN"}
KEY_TTL_SECONDS      = 86_400
PROCESSING_DELAY     = 0.0   # no delay in tests
INFLIGHT_TIMEOUT     = 10.0
MAX_KEY_LENGTH       = 255

idempotency_store: dict = {}
in_flight: dict         = {}


def _body_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate(amount, currency):
    if amount <= 0:
        return 422, {"detail": "amount must be > 0"}
    if currency.upper() not in SUPPORTED_CURRENCIES:
        return 422, {"detail": f"Unsupported currency '{currency}'"}
    return None, None


async def process_payment(
    idempotency_key, amount, currency,
    *, delay: float = 0.0
):
    """Returns (status_code, body, headers)."""
    if not idempotency_key:
        return 400, {"detail": "Missing required header: Idempotency-Key"}, {}
    if len(idempotency_key) > MAX_KEY_LENGTH:
        return 400, {"detail": "Idempotency-Key must not exceed 255 characters."}, {}

    err_code, err_body = _validate(amount, currency)
    if err_code:
        return err_code, err_body, {}

    currency     = currency.upper()
    payload      = {"amount": amount, "currency": currency}
    current_hash = _body_hash(payload)

    if idempotency_key in idempotency_store:
        stored = idempotency_store[idempotency_key]
        if stored["body_hash"] != current_hash:
            return 409, {"detail": "Idempotency key already used for a different request body."}, {}
        return stored["status_code"], stored["response"], {"X-Cache-Hit": "true"}

    if idempotency_key in in_flight:
        event = in_flight[idempotency_key]
        try:
            await asyncio.wait_for(event.wait(), timeout=INFLIGHT_TIMEOUT)
        except asyncio.TimeoutError:
            return 503, {"detail": "Upstream processing timed out."}, {}
        stored = idempotency_store.get(idempotency_key)
        if stored:
            return stored["status_code"], stored["response"], {"X-Cache-Hit": "true"}
        return 503, {"detail": "Processing result unavailable."}, {}

    event                    = asyncio.Event()
    in_flight[idempotency_key] = event
    try:
        await asyncio.sleep(delay)
        response_body = {
            "message":         f"Charged {amount:.10g} {currency}",
            "idempotency_key": idempotency_key,
            "amount":          amount,
            "currency":        currency,
            "status":          "success",
            "transaction_id":  f"txn_{current_hash[:12]}",
        }
        status_code = 201
        idempotency_store[idempotency_key] = {
            "body_hash":   current_hash,
            "status_code": status_code,
            "response":    response_body,
            "cached_at":   time.time(),
        }
        return status_code, response_body, {}
    finally:
        event.set()
        in_flight.pop(idempotency_key, None)


def run(coro):
    return asyncio.run(coro)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestUS1_HappyPath(unittest.TestCase):
    def setUp(self):
        idempotency_store.clear(); in_flight.clear()

    def test_returns_201_and_charged_message(self):
        code, body, _ = run(process_payment("k1", 100, "GHS"))
        self.assertEqual(code, 201)
        self.assertEqual(body["message"], "Charged 100 GHS")
        self.assertEqual(body["status"], "success")

    def test_transaction_id_present(self):
        _, body, _ = run(process_payment("k2", 50, "USD"))
        self.assertIn("transaction_id", body)
        self.assertTrue(body["transaction_id"].startswith("txn_"))

    def test_idempotency_key_echoed_in_response(self):
        _, body, _ = run(process_payment("my-key-99", 10, "GHS"))
        self.assertEqual(body["idempotency_key"], "my-key-99")

    def test_missing_key_returns_400(self):
        code, body, _ = run(process_payment(None, 100, "GHS"))
        self.assertEqual(code, 400)
        self.assertIn("Idempotency-Key", body["detail"])

    def test_empty_key_returns_400(self):
        code, _, _ = run(process_payment("", 100, "GHS"))
        self.assertEqual(code, 400)

    def test_key_too_long_returns_400(self):
        code, _, _ = run(process_payment("x" * 256, 10, "GHS"))
        self.assertEqual(code, 400)

    def test_negative_amount_returns_422(self):
        code, _, _ = run(process_payment("k3", -10, "GHS"))
        self.assertEqual(code, 422)

    def test_zero_amount_returns_422(self):
        code, _, _ = run(process_payment("k4", 0, "GHS"))
        self.assertEqual(code, 422)

    def test_unsupported_currency_returns_422(self):
        code, _, _ = run(process_payment("k5", 10, "XYZ"))
        self.assertEqual(code, 422)


class TestUS2_DuplicateIdempotency(unittest.TestCase):
    def setUp(self):
        idempotency_store.clear(); in_flight.clear()

    def test_duplicate_returns_same_status_and_body(self):
        c1, b1, _ = run(process_payment("dup1", 200, "GHS"))
        c2, b2, _ = run(process_payment("dup1", 200, "GHS"))
        self.assertEqual(c1, c2)
        self.assertEqual(b1, b2)

    def test_duplicate_has_cache_hit_header(self):
        run(process_payment("dup2", 75, "USD"))
        _, _, headers = run(process_payment("dup2", 75, "USD"))
        self.assertEqual(headers.get("X-Cache-Hit"), "true")

    def test_first_request_has_no_cache_hit_header(self):
        _, _, headers = run(process_payment("dup3", 30, "EUR"))
        self.assertNotIn("X-Cache-Hit", headers)

    def test_five_retries_all_return_same_body(self):
        bodies = [run(process_payment("dup4", 999, "GBP"))[1] for _ in range(5)]
        self.assertTrue(all(b == bodies[0] for b in bodies))

    def test_duplicate_does_not_add_second_store_entry(self):
        run(process_payment("dup5", 10, "GHS"))
        run(process_payment("dup5", 10, "GHS"))
        self.assertEqual(len(idempotency_store), 1)


class TestUS3_ConflictDetection(unittest.TestCase):
    def setUp(self):
        idempotency_store.clear(); in_flight.clear()

    def test_same_key_different_amount_returns_409(self):
        run(process_payment("conf1", 100, "GHS"))
        code, body, _ = run(process_payment("conf1", 500, "GHS"))
        self.assertEqual(code, 409)
        self.assertIn("different request body", body["detail"])

    def test_same_key_different_currency_returns_409(self):
        run(process_payment("conf2", 100, "GHS"))
        code, _, _ = run(process_payment("conf2", 100, "USD"))
        self.assertEqual(code, 409)

    def test_same_key_same_body_not_a_conflict(self):
        run(process_payment("conf3", 100, "GHS"))
        code, _, _ = run(process_payment("conf3", 100, "GHS"))
        self.assertEqual(code, 201)


class TestBonus_RaceCondition(unittest.TestCase):
    def setUp(self):
        idempotency_store.clear(); in_flight.clear()

    def test_concurrent_same_key_processes_exactly_once(self):
        async def _run():
            return await asyncio.gather(*[
                process_payment("race1", 150, "NGN", delay=0.05)
                for _ in range(5)
            ])

        results  = asyncio.run(_run())
        codes    = [r[0] for r in results]
        txn_ids  = [r[1]["transaction_id"] for r in results]

        self.assertTrue(all(c == 201 for c in codes))
        self.assertEqual(len(set(txn_ids)), 1, "All must share one transaction_id")

    def test_concurrent_different_keys_all_succeed(self):
        async def _run():
            return await asyncio.gather(*[
                process_payment(f"par-{i}", i * 10, "GHS", delay=0.02)
                for i in range(1, 6)
            ])

        results = asyncio.run(_run())
        self.assertTrue(all(r[0] == 201 for r in results))
        # Each key should be independent
        txn_ids = [r[1]["transaction_id"] for r in results]
        self.assertEqual(len(set(txn_ids)), 5)


class TestDeveloperChoice(unittest.TestCase):
    def setUp(self):
        idempotency_store.clear(); in_flight.clear()

    def test_cached_entry_records_timestamp(self):
        run(process_payment("ttl1", 10, "USD"))
        self.assertIn("cached_at", idempotency_store["ttl1"])

    def test_body_hash_is_deterministic(self):
        h1 = _body_hash({"amount": 100, "currency": "GHS"})
        h2 = _body_hash({"currency": "GHS", "amount": 100})
        self.assertEqual(h1, h2)

    def test_different_bodies_produce_different_hashes(self):
        h1 = _body_hash({"amount": 100, "currency": "GHS"})
        h2 = _body_hash({"amount": 500, "currency": "GHS"})
        self.assertNotEqual(h1, h2)

    def test_currency_normalised_to_uppercase(self):
        _, body, _ = run(process_payment("cur1", 10, "ghs"))
        self.assertEqual(body["currency"], "GHS")


if __name__ == "__main__":
    print("=" * 65)
    print("  Idempotency Gateway — Full Logic Verification Suite")
    print("=" * 65)
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestUS1_HappyPath,
        TestUS2_DuplicateIdempotency,
        TestUS3_ConflictDetection,
        TestBonus_RaceCondition,
        TestDeveloperChoice,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    exit(0 if result.wasSuccessful() else 1)
