# Load test

**Tool:** [backend/scripts/loadtest.py](../backend/scripts/loadtest.py). It is a closed-loop test: N concurrent virtual users, each sending its next request as soon as the previous one returns. It uses httpx, so there's no extra dependency. It runs locally, in CI, and against the containers.

**Workload:**

| Path | What it exercises | VUs |
|---|---|---|
| `GET /api/state` | Operator dashboard read path. Serves the snapshot JSON serialized once per cycle. | 10 / 50 / 100 |
| `POST /api/decisions/recommend` | End-to-end decision: forecast, risks, 6 plans, each scored on the twin under 3 demand scenarios (up to 18 twin rollouts of 32 ticks; identical plans are scored once). Nothing is executed. | 1 / 4 / 8 |
| Same two paths under an injected simulator `latency` fault (500 ms on every `/v1` call) | Shows the read and decide paths are decoupled from simulator latency | same |

## Results: CI containers (real simulator image, backend container, GitHub-hosted runner)

This is the `evidence` artifact of CI run 36536985884. The load generator runs outside the backend container.

| Path | VUs | RPS | p50 ms | p95 ms | p99 ms | Errors | Backend CPU (cores) | RSS MB |
|---|---|---|---|---|---|---|---|---|
| `GET /api/state` | 10 | 606 | 9 | 45 | 66 | 0% | 0.21 | 120 |
| `GET /api/state` | 50 | 480 | 77 | 284 | 433 | 0% | 0.17 | 120 |
| `GET /api/state` | 100 | 410 | 174 | 681 | 1081 | 0% | 0.15 | 120 |
| `POST /recommend` | 1 | 28 | 35 | 39 | 48 | 0% | 0.97 | 120 |
| `POST /recommend` | 4 | 32 | 122 | 163 | 186 | 0% | 1.35 | 148 |
| `POST /recommend` | 8 | 33 | 245 | 324 | 379 | 0% | 1.34 | 154 |
| `/api/state` under 500 ms sim fault | 10 | 632 | 9 | 40 | 70 | 0% | 0.22 | 154 |
| `/recommend` under 500 ms sim fault | 8 | 33 | 239 | 313 | 361 | 0% | 1.34 | 154 |

**What the CI numbers show:**
- **The read path stays fast.** `/api/state` sustains 400–600 RPS with p95 under 700 ms at 100 concurrent users.
- **The decision pipeline tops out at about 33 decisions/s on roughly 1.3 cores.** Each decision is a 6-way twin tournament; the GIL plus thread offload caps it there. Latency grows linearly with concurrency, the expected queueing signature. The live loop needs about 0.5 decisions/s.
- **Zero errors everywhere, including under the simulator fault.** Neither path calls the simulator per request.
- **Memory is flat at 120–155 MB.**

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
