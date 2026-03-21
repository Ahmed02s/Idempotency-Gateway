# Idempotency Gateway — The "Pay-Once" Protocol

A production-ready Python/FastAPI service that guarantees every payment is processed **exactly once**, regardless of how many times a client retries.

Built for **FinSafe Transactions Ltd.**

---

## Architecture Diagram

### Request Decision Flowchart

```
POST /process-payment
  Header: Idempotency-Key: <uuid>
  Body:   {"amount": 100, "currency": "GHS"}
         │
         ▼
┌─────────────────────────────┐
│  Idempotency-Key present    │── NO ──► 400 Bad Request
│  and ≤ 255 chars?           │
└─────────────────────────────┘
         │ YES
         ▼
┌─────────────────────────────┐
│  Valid amount & currency?   │── NO ──► 422 Unprocessable Entity
└─────────────────────────────┘
         │ YES
         ▼
┌─────────────────────────────┐         ┌───────────────────────────┐
│  Key in idempotency_store?  │── YES ──►  Body hash matches?       │
└─────────────────────────────┘         └───────────────────────────┘
         │ NO                               │ NO          │ YES
         ▼                                  ▼             ▼
┌─────────────────────────────┐         409 Conflict   Replay cached response
│  Key in in_flight map?      │                        + X-Cache-Hit: true
└─────────────────────────────┘
         │ YES          │ NO
         ▼              ▼
   Await Event    Mark key in-flight
   (block until   (asyncio.Event)
    first done)        │
         │         Simulate 2s delay
         │              │
         │         Save result to store
         │              │
         │         Fire Event (unblock waiters)
         │              │
         └──────────────►
                        │
                   201 Created
```

### Sequence Diagram — Happy Path + Retry

```
Client          Gateway          idempotency_store      Processor
  │                │                     │                   │
  │──POST (key=K)──►                     │                   │
  │                │──lookup K───────────►                   │
  │                │◄──miss──────────────│                   │
  │                │──mark in-flight──────────────────────────
  │                │──process payment────────────────────────►
  │                │                     │         ~2s delay │
  │                │◄──payment result────────────────────────◄
  │                │──store result(K)────►                   │
  │                │──fire Event(K)──────────────────────────
  │◄──201 Created──│                     │                   │
  │                │                     │                   │
  │  (network timeout — client retries)  │                   │
  │                │                     │                   │
  │──POST (key=K)──►                     │                   │
  │                │──lookup K───────────►                   │
  │                │◄──hit (cached body)─│                   │
  │◄──201 + X-Cache-Hit: true            │                   │
```

### Sequence Diagram — Race Condition (Bonus)

```
Client A       Client B         Gateway           in_flight
  │               │                │                  │
  │──POST(key=K)──────────────────►│                  │
  │               │                │──Event(K)────────►
  │               │                │  (processing…)   │
  │               │──POST(key=K)───►                  │
  │               │                │──lookup in_flight►
  │               │                │◄──Event found────│
  │               │                │  await event…    │
  │  (~2s completes)               │                  │
  │◄──201 Created─────────────────◄│                  │
  │               │                │──fire Event(K)───►
  │               │◄──201 Created──│  (same body)     │
```

---

## Project Structure

```
idempotency-gateway/
├── app.py                 # FastAPI application — all gateway logic
├── main.py                # Entry point: python main.py
├── requirements.txt       # Python dependencies
├── pyproject.toml         # pytest configuration
├── Makefile               # Developer convenience targets
├── Dockerfile             # Container build (multi-stage)
├── docker-compose.yml     # One-command local spin-up
├── .gitignore
├── README.md
└── tests/
    ├── __init__.py
    ├── test_gateway.py    # Integration tests (httpx + pytest-asyncio)
    └── test_logic.py      # Stdlib-only logic tests (no server needed)
```

---

## Setup Instructions

### Prerequisites

- Python 3.11+
- pip
- (Optional) Docker & Docker Compose

### Option A — Local Python

```bash
# 1. Clone your fork
git clone https://github.com/<your-username>/idempotency-gateway.git
cd idempotency-gateway

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server  (any of these work)
python main.py
# or
make start
# or
uvicorn app:app --reload --port 8000
```

Server is live at **http://localhost:8000**
Swagger UI docs at **http://localhost:8000/docs**

### Option B — Docker Compose

```bash
docker compose up --build
```

### Running Tests

```bash
# Logic tests — no dependencies needed, runs anywhere
python3 tests/test_logic.py

# Full integration tests (requires installed packages)
pytest tests/test_gateway.py -v

# All tests via Makefile
make test-all
```

---

## API Documentation

### `POST /process-payment`

Process a payment. Guaranteed to charge exactly once per unique `Idempotency-Key`.

#### Headers

| Header              | Required | Description                                       |
|---------------------|----------|---------------------------------------------------|
| `Idempotency-Key`   | Yes      | Unique string (UUID recommended), max 255 chars   |
| `Content-Type`      | Yes      | `application/json`                                |
 
#### Request Body

```json
{
  "amount": 100,
  "currency": "GHS"
}
```

| Field      | Type   | Rules                                               |
|------------|--------|-----------------------------------------------------|
| `amount`   | float  | Required, must be > 0                               |
| `currency` | string | Required, one of: `GHS`, `USD`, `EUR`, `GBP`, `NGN`|

---

#### `201 Created` — First successful request

```json
{
  "message": "Charged 100 GHS",
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440000",
  "amount": 100.0,
  "currency": "GHS",
  "status": "success",
  "transaction_id": "txn_a1b2c3d4e5f6"
}
```

#### `201 Created` + `X-Cache-Hit: true` — Duplicate request

Identical body to the original response. No payment is processed again.

#### `409 Conflict` — Same key, different body

```json
{
  "detail": "Idempotency key already used for a different request body."
}
```

#### `400 Bad Request` — Missing or oversized header

```json
{ "detail": "Missing required header: Idempotency-Key" }
```

#### `422 Unprocessable Entity` — Invalid payload

```json
{
  "detail": [
    {
      "loc": ["body", "currency"],
      "msg": "Unsupported currency 'XYZ'. Supported: EUR, GBP, GHS, NGN, USD"
    }
  ]
}
```

---

### `GET /health`

```json
{
  "status": "ok",
  "keys_cached": 42,
  "keys_in_flight": 0,
  "key_ttl_seconds": 86400,
  "supported_currencies": ["EUR", "GBP", "GHS", "NGN", "USD"]
}
```

---

### Example cURL Calls

```bash
# First payment
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -d '{"amount": 100, "currency": "GHS"}'

# Retry — returns cached response + X-Cache-Hit: true
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -d '{"amount": 100, "currency": "GHS"}'

# Conflict — same key, different amount
curl -X POST http://localhost:8000/process-payment \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -d '{"amount": 500, "currency": "GHS"}'
# → 409 Conflict
```

---

## Environment Variables

All config is overridable without code changes:

| Variable                    | Default | Description                            |
|-----------------------------|---------|----------------------------------------|
| `KEY_TTL_SECONDS`           | 86400   | How long to keep cached keys (24 h)    |
| `CLEANUP_INTERVAL_SECONDS`  | 300     | How often the TTL cleanup runs (5 min) |
| `PROCESSING_DELAY_SECONDS`  | 2.0     | Simulated payment processor latency    |
| `INFLIGHT_TIMEOUT_SECONDS`  | 10.0    | Max wait time for in-flight requests   |
| `SUPPORTED_CURRENCIES`      | GHS,USD,EUR,GBP,NGN | Comma-separated list  |
| `LOG_LEVEL`                 | INFO    | Python logging level                   |

---

## Design Decisions

### FastAPI + asyncio
FastAPI's async route handlers make the `asyncio.Event` race-condition guard work without threads. Pydantic v2 provides free, detailed input validation.

### SHA-256 body hashing
Rather than storing raw payloads, we hash the canonical JSON (`sort_keys=True`) with SHA-256. This is order-independent, constant size, and collision-resistant for all practical purposes.

### `asyncio.Event` per in-flight key
When a payment is being processed, the key is registered in an `in_flight` dict with an associated `asyncio.Event`. Any concurrent duplicate awaits that event instead of starting a new charge. The `finally` block ensures the event is always fired — even on errors — so waiters are never permanently blocked.

### In-memory store
A plain Python `dict` is used deliberately to keep the project dependency-free and instantly runnable. In production, replace with Redis using `SET key value NX PX <ttl-ms>` for atomic, distributed idempotency.

### Idempotency-Key max length: 255 chars
This mirrors the limit used by Stripe. UUIDs are 36 characters; 255 provides ample room for any custom key format while blocking runaway inputs.

---

## Developer's Choice Feature — Key TTL (24-hour Expiry)

### What it is
Every stored entry records a `cached_at` Unix timestamp. A background `asyncio` task runs every 5 minutes and purges entries older than 24 hours.

### Why it matters for Fintech
1. **Memory safety** — Without expiry, the store grows unboundedly under load.
2. **Regulatory alignment** — Major processors (Stripe, Paystack) guarantee idempotency for 24 hours only. After expiry, the same key *may* be reused for a new payment.
3. **Auditability** — `cached_at` enables reporting on "when was this key first used" and "is this key still active."
4. **Zero migration cost** — The `cached_at` field is already stored, so adding a Redis `EXPIREAT` when scaling requires no schema changes.

```python
async def _cleanup_expired_keys() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        expired = [k for k, v in list(idempotency_store.items())
                   if now - v["cached_at"] > KEY_TTL_SECONDS]
        for k in expired:
            idempotency_store.pop(k, None)
```

---

## Test Coverage

| User Story | Tests | Result |
|---|---|---|
| US1 — Happy Path | 9 |  All pass |
| US2 — Duplicate / Cache Hit | 5 | All pass |
| US3 — Conflict Detection | 3 |  All pass |
| Bonus — Race Condition | 2 |  All pass |
| Developer's Choice — TTL | 4 |  All pass |
| **Total** | **23** | **23/23** |

---

## Pre-Submission Checklist

- [x] Repository is public
- [x] No `node_modules`, `.env`, or secrets committed
- [x] `python main.py` starts the server immediately
- [x] Architecture diagram included in README
- [x] Original instructions replaced with own documentation
- [x] API endpoints and example requests documented
- [x] Multiple meaningful commits in git history
- [x] `tests/__init__.py` present
- [x] `Dockerfile` and `docker-compose.yml` included
