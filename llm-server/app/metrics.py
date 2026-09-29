"""Prometheus metrics exposed on GET /metrics."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("llm_requests_total", "Gateway requests", ["endpoint", "model", "status"])
LATENCY = Histogram(
    "llm_request_latency_seconds", "End-to-end gateway latency", ["endpoint", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
TOKENS = Counter("llm_tokens_total", "Tokens processed", ["model", "kind"])
TOKENS_PER_SECOND = Histogram(
    "llm_generation_tokens_per_second", "Decode speed reported by llama-server", ["model"],
    buckets=(1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 250),
)
CACHE_HITS = Counter("llm_cache_hits_total", "Responses served from cache", ["endpoint"])
FALLBACKS = Counter("llm_fallback_total", "Requests served by a fallback model", ["requested", "served"])
BACKEND_UP = Gauge("llm_backend_up", "1 if the model backend is ready", ["model"])
BACKEND_RESTARTS = Gauge("llm_backend_restarts", "Automatic restarts of the model backend", ["model"])
