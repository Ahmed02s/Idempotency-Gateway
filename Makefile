.PHONY: install start test test-logic lint docker-build docker-run clean

# ── Setup ──────────────────────────────────────────────────────────────────
install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	@echo "\n✅  Dependencies installed. Activate with: source .venv/bin/activate"

# ── Run ────────────────────────────────────────────────────────────────────
start:
	uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# ── Test ───────────────────────────────────────────────────────────────────
test:
	pytest tests/test_gateway.py -v --tb=short

test-logic:
	python3 tests/test_logic.py

test-all: test-logic test

# ── Docker ─────────────────────────────────────────────────────────────────
docker-build:
	docker build -t idempotency-gateway .

docker-run:
	docker run -p 8000:8000 idempotency-gateway

# ── Clean ──────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage htmlcov
