"""
Idempotency Gateway — FinSafe Transactions Ltd.
------------------------------------------------
Guarantees every payment is processed exactly once,
regardless of client retries or concurrent duplicate requests.

Run:
    python main.py
    uvicorn app:app --reload
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

# ── Configuration (overridable via environment variables) ───────────────────
KEY_TTL_SECONDS: int      = int(os.getenv("KEY_TTL_SECONDS", 86_400))
CLEANUP_INTERVAL: int     = int(os.getenv("CLEANUP_INTERVAL_SECONDS", 300))
PROCESSING_DELAY: float   = float(os.getenv("PROCESSING_DELAY_SECONDS", 2.0))
INFLIGHT_TIMEOUT: float   = float(os.getenv("INFLIGHT_TIMEOUT_SECONDS", 10.0))
MAX_KEY_LENGTH: int       = 255

SUPPORTED_CURRENCIES: set[str] = set(
    os.getenv("SUPPORTED_CURRENCIES", "GHS,USD,EUR,GBP,NGN").upper().split(",")
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("idempotency_gateway")

# ── In-memory stores ──────────────────────────────────────────────────────────
idempotency_store: dict[str, dict[str, Any]] = {}
in_flight: dict[str, asyncio.Event]          = {}


# ── Background TTL cleanup (Developer's Choice) ───────────────────────────────
async def _cleanup_expired_keys() -> None:
    """Purge idempotency keys older than KEY_TTL_SECONDS."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        expired = [
            k for k, v in list(idempotency_store.items())
            if now - v["cached_at"] > KEY_TTL_SECONDS
        ]
        for k in expired:
            idempotency_store.pop(k, None)
            logger.info("TTL expired — removed key: %s", k)
        if expired:
            logger.info("Cleanup complete. Removed %d expired keys.", len(expired))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_expired_keys())
    logger.info(
        "Idempotency Gateway started. TTL=%ds, CleanupInterval=%ds",
        KEY_TTL_SECONDS, CLEANUP_INTERVAL,
    )
    yield
    task.cancel()
    logger.info("Idempotency Gateway shutting down.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Idempotency Gateway",
    description="Pay-Once Protocol — FinSafe Transactions Ltd.",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Schemas ───────────────────────────────────────────────────────────────────
class PaymentRequest(BaseModel):
    amount:   float = Field(..., gt=0, description="Payment amount (must be > 0)")
    currency: str   = Field(..., description="ISO 4217 currency code")

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(
                f"Unsupported currency '{v}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_CURRENCIES))}"
            )
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────
def _body_hash(payload: dict) -> str:
    """Deterministic SHA-256 of the canonical JSON payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _fmt_amount(amount: float) -> str:
    """Format a float cleanly: 100.0 -> '100', 99.99 -> '99.99'."""
    return f"{amount:.10g}"


def _validate_key_header(key: str | None) -> None:
    if not key:
        raise HTTPException(400, "Missing required header: Idempotency-Key")
    if len(key) > MAX_KEY_LENGTH:
        raise HTTPException(
            400, f"Idempotency-Key must not exceed {MAX_KEY_LENGTH} characters."
        )


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["ops"], summary="Health check")
async def health():
    return {
        "status": "ok",
        "keys_cached": len(idempotency_store),
        "keys_in_flight": len(in_flight),
        "key_ttl_seconds": KEY_TTL_SECONDS,
        "supported_currencies": sorted(SUPPORTED_CURRENCIES),
    }


@app.post("/process-payment", tags=["payments"], summary="Submit a payment")
async def process_payment(payment: PaymentRequest, request: Request):
    """
    Process a payment exactly once.

    - **First call**: simulates processing and caches the result.
    - **Duplicate call** (same key + same body): returns cached result instantly.
    - **Conflict** (same key + different body): returns 409.
    - **Concurrent duplicate**: second request waits for the first to finish.
    """
    # 1. Validate header
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip() or None
    _validate_key_header(idempotency_key)

    payload      = payment.model_dump()
    current_hash = _body_hash(payload)

    # 2. Cache hit check
    if idempotency_key in idempotency_store:
        stored = idempotency_store[idempotency_key]

        if stored["body_hash"] != current_hash:            # US3 — conflict
            logger.warning("Conflict: key=%s", idempotency_key)
            raise HTTPException(
                409,
                "Idempotency key already used for a different request body.",
            )

        logger.info("Cache hit: key=%s", idempotency_key)  # US2 — replay
        return JSONResponse(
            status_code=stored["status_code"],
            content=stored["response"],
            headers={"X-Cache-Hit": "true"},
        )

    # 3. In-flight guard (Bonus — race condition)
    if idempotency_key in in_flight:
        logger.info("In-flight wait: key=%s", idempotency_key)
        try:
            await asyncio.wait_for(in_flight[idempotency_key].wait(),
                                   timeout=INFLIGHT_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Upstream processing timed out. Please retry.")
        stored = idempotency_store.get(idempotency_key)
        if stored:
            return JSONResponse(
                status_code=stored["status_code"],
                content=stored["response"],
                headers={"X-Cache-Hit": "true"},
            )
        raise HTTPException(503, "Processing result unavailable.")

    # 4. First request — process
    event = asyncio.Event()
    in_flight[idempotency_key] = event
    logger.info(
        "Processing: key=%s amount=%s %s",
        idempotency_key, payment.amount, payment.currency,
    )

    try:
        await asyncio.sleep(PROCESSING_DELAY)

        response_body = {
            "message":         f"Charged {_fmt_amount(payment.amount)} {payment.currency}",
            "idempotency_key": idempotency_key,
            "amount":          payment.amount,
            "currency":        payment.currency,
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
        logger.info(
            "Cached result: key=%s txn=%s",
            idempotency_key, response_body["transaction_id"],
        )
        return JSONResponse(status_code=status_code, content=response_body)

    finally:
        event.set()
        in_flight.pop(idempotency_key, None)
