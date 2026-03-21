"""
Entry point for the Idempotency Gateway.

Run directly:
    python main.py

Or via uvicorn (recommended for production):
    uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,         # disable in production
        log_level="info",
    )
