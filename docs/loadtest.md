# Load test

**Tool:** [backend/scripts/loadtest.py](../backend/scripts/loadtest.py). It is a closed-loop test: N concurrent virtual users, each sending its next request as soon as the previous one returns. It uses httpx, so there's no extra dependency. It runs locally, in CI, and against the containers.

**Workload:**

| Path | What it exercises | VUs |
|---|---|---|
| `GET /api/state` | Operator dashboard read path. Serves the snapshot JSON serialized once per cycle. | 10 / 50 / 100 |
| `POST /api/decisions/recommend` | End-to-end decision: forecast, risks, 6 plans, each scored on the twin under 3 demand scenarios (up to 18 twin rollouts of 32 ticks; identical plans are scored once). Nothing is executed. | 1 / 4 / 8 |
| Same two paths under an injected simulator `latency` fault (500 ms on every `/v1` call) | Shows the read and decide paths are decoupled from simulator latency | same |

## Results: local, indicative ([loadtest-local.md](loadtest-local.md))

Everything ran on one 4-core laptop (i5-1145G7): the stand-in simulator, the backend, and the load generator. A CPU-heavy fine-tuning job was also running at the same time. CI produces clean container numbers (the `evidence` artifact).

| Path | VUs | RPS | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| `GET /api/state` | 10 | 226 | 35 | 82 | 307 | 0% |
| `GET /api/state` | 50 | 112 | 307 | 1269 | 2095 | 0% |
| `GET /api/state` | 100 | 105 | 676 | 2564 | 3916 | 0% |
| `POST /recommend` | 1 | 6.2 | 156 | 207 | 252 | 0% |
| `POST /recommend` | 4 | 9.6 | 404 | 565 | 680 | 0% |
| `POST /recommend` | 8 | 11.4 | 694 | 864 | 1178 | 0% |
| `/recommend` under 500 ms sim fault | 8 | 12.5 | 633 | 814 | 918 | 0% |
| `/api/state` under 500 ms sim fault | 10 | 257 | 33 | 55 | 172 | 0% |

## What the numbers say

- **Zero errors at every level, including during the simulator fault.** Neither path calls the simulator per request. `/api/state` serves the cached snapshot, and `/recommend` plans on it. A 500 ms simulator delay only slows the background refresh, which is visible as `snapshot_age_seconds` in Grafana.
- **`/recommend` is CPU-bound, at about 11–12 decisions/s per process.** Latency grows linearly with concurrency (156 → 404 → 694 ms at 1/4/8 VUs) while throughput flattens. That is the signature of the GIL: the tournament runs in a thread, but the twin rollouts are Python. The live loop needs about 1 decision per 4 ticks, so capacity is 50–100× what operations need. Recommend traffic is for operators and load tests only.
  *ponytail: one process with thread offload. If this path ever needs more, move the tournament to a `ProcessPoolExecutor` (or parallelize the 18 rollouts), which scales with cores.*
- **`/api/state` throughput drops past 10 VUs because of the test setup, not the endpoint.**
  - A trivial endpoint (`/api/mode`) shows the same curve (334 → 162 RPS).
  - Splitting 50 VUs across two generator processes gives 254 RPS total versus 141 from one.
  - So the ceiling is the single Python load generator competing for the same 4 cores. Run it from a separate machine or container (CI does) for true server numbers.
- **Fixes made because of this test:**
  1. The `/api/state` JSON is now serialized once per cycle instead of per request (218 → 332 RPS at 10 VUs in back-to-back runs).
  2. The metrics middleware is now pure ASGI instead of `BaseHTTPMiddleware`.

## Reproduce

```bash
cd backend
python -m scripts.loadtest --base http://localhost:8080 --token $ADMIN_TOKEN --seconds 20 --out ../docs/loadtest-local
```
