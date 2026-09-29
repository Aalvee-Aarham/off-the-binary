import time
from collections import deque

from prometheus_client import Counter, Gauge, Histogram

# app
HTTP_REQS = Counter("http_requests_total", "API requests", ["method", "route", "status"])
HTTP_LAT = Histogram("http_request_duration_seconds", "API latency", ["method", "route"],
                     buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5))
# simulator link
SIM_LAT = Histogram("sim_call_duration_seconds", "Simulator call latency", ["method", "path"],
                    buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5))
SIM_CALLS = Counter("sim_calls_total", "Simulator calls", ["method", "path", "outcome"])
SIM_RETRIES = Counter("sim_retries_total", "Simulator retries", ["path"])
SIM_BREAKER = Gauge("sim_breaker_state", "0 closed, 1 half-open, 2 open")
SIM_STALE = Gauge("sim_stale", "1 when simulator flags stale data")
SIM_INVALID = Counter("sim_invalid_responses_total", "Rejected simulator responses", ["path"])
SSE_CONNECTED = Gauge("sse_connected", "1 when SSE stream is connected")
SSE_RECONNECTS = Counter("sse_reconnects_total", "SSE reconnects")
SNAPSHOT_AGE = Gauge("snapshot_age_seconds", "Age of last good snapshot")
DEGRADED = Gauge("degraded_mode", "1 when serving cached state")
# intelligence
FORECAST_MAPE = Gauge("forecast_mape", "One-step forecast MAPE (EWMA)")
ALERTS = Counter("alerts_total", "Alerts raised", ["type", "severity"])
ACTIVE_ALERTS = Gauge("active_alerts", "Currently active alerts")
DECISIONS = Counter("decisions_total", "Decisions", ["algorithm", "gate"])
FALLBACKS = Counter("fallback_activations_total", "Fallback activations", ["reason"])
ROUTER_HIT = Counter("router_tournament_total", "Router pick vs tournament winner", ["source", "hit"])
SOLVER_LAT = Histogram("solver_duration_seconds", "Solver latency", ["algorithm"],
                       buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1))
CYCLE_LAT = Histogram("decision_cycle_seconds", "Full decision cycle", buckets=(.01, .05, .1, .25, .5, 1, 2, 5))
SHIPMENTS = Counter("shipments_total", "Allocations submitted", ["outcome"])
# business
SERVICE_LEVEL = Gauge("service_level", "Simulator service level")
UNMET = Gauge("unmet_liters", "Simulator unmet demand liters")
SIM_TICK = Gauge("sim_tick", "Current simulator tick")
ALLOC_FAILURES = Gauge("allocation_failures", "Simulator ground truth: FAILED allocations (fuel lost)")
DECISION_LAG = Gauge("decision_lag_ticks", "Ticks between the snapshot a plan used and its first accepted allocation")
AUTO_CANCELS = Counter("auto_cancels_total", "PENDING allocations cancelled before a known route disruption")
INTEGRATION_BUGS = Counter("integration_bug_responses_total", "Simulator codes that mean our request was wrong", ["code"])


class Window:
    """Rolling request window for /api/health p95 + error rate without querying Prometheus."""

    def __init__(self, n=2000):
        self.buf = deque(maxlen=n)

    def add(self, seconds, error):
        self.buf.append((time.time(), seconds, error))

    def summary(self, horizon=300):
        cut = time.time() - horizon
        rows = [r for r in self.buf if r[0] >= cut]
        if not rows:
            return {"p95_ms": None, "error_rate": 0.0, "requests": 0}
        lat = sorted(r[1] for r in rows)
        return {"p95_ms": round(lat[int(0.95 * (len(lat) - 1))] * 1000, 1),
                "error_rate": round(sum(r[2] for r in rows) / len(rows), 4), "requests": len(rows)}


WINDOW = Window()
# system-one (Laya / Jev)
SYSTEMONE_CALLS = Counter("systemone_calls_total", "System-One model calls", ["model", "outcome"])
SYSTEMONE_LAT = Histogram("systemone_duration_seconds", "System-One latency", ["model"],
                          buckets=(.025, .05, .1, .2, .3, .5, .75, 1, 1.5, 2, 3))
ROUTER_AGREE = Counter("router_rules_agreement_total", "Model regime vs rule regime", ["model", "agree"])
ROUTER_CACHE = Counter("router_cache_total", "Router signature cache", ["result"])
