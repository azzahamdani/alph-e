"""
Leaky demo service.

Exposes:
  GET /         -> allocates ~5MB, returns OK
  GET /healthz  -> liveness probe
  GET /metrics  -> Prometheus metrics

It also leaks ~2MB/sec in the background so the pod OOMs even without traffic,
which makes the agent's investigation reproducible.
"""
import os
import threading
import time

from flask import Flask
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)

REQUESTS = Counter("demo_requests_total", "Total requests handled")
LEAK_BYTES = Gauge("demo_leaked_bytes", "Bytes leaked by the background leaker")

# The "leak" — a module-level list that only grows.
_leaked: list[bytearray] = []


def background_leaker() -> None:
    """Append ~2MB to the leak list every second."""
    chunk_size = 2 * 1024 * 1024  # 2 MiB
    while True:
        _leaked.append(bytearray(chunk_size))
        LEAK_BYTES.set(sum(len(b) for b in _leaked))
        time.sleep(1)


@app.route("/")
def index():
    REQUESTS.inc()
    # Additional allocation on each request to accelerate the OOM under load
    _leaked.append(bytearray(5 * 1024 * 1024))
    LEAK_BYTES.set(sum(len(b) for b in _leaked))
    return {"status": "ok", "leaked_mb": sum(len(b) for b in _leaked) // (1024 * 1024)}


@app.route("/healthz")
def healthz():
    return {"status": "healthy"}


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    threading.Thread(target=background_leaker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
