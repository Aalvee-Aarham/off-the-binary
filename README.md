# Fuel Ops Intelligence Platform

Decision-support backend for the BUP Fuel Supply Simulator. Each cycle it observes the network, forecasts demand, detects risk, generates plans with several algorithms, scores every plan on a digital twin, applies the best one (automatically or with operator approval), and reconciles the outcome. The design is in [PLAN.md](PLAN.md).

All data and decisions are **simulated**. Nothing touches real fuel infrastructure.

## Run with Docker

```bash
cp .env.example .env        # set ADMIN_TOKEN at minimum
docker compose up --build
```

| URL | What |
|---|---|
| http://localhost:8080 | Operator console (paste ADMIN_TOKEN in the header box) |
| http://localhost:8080/docs | API (Swagger) |
| http://localhost:8080/metrics | Prometheus metrics |
| http://localhost:9090 | Prometheus |
| http://localhost:3000 | Grafana |
| http://localhost:8000/admin | Simulator console |

## Run locally without Docker

The local run uses `app/devsim.py`, a stand-in simulator with the same API and fault injection, built on the digital twin.

```bash
python -m venv .venv && .venv/Scripts/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd backend
SIMULATION_SPEED=4 SIMULATOR_START_MODE=running python -m uvicorn app.devsim:app --port 8000 &
ADMIN_TOKEN=dev python -m uvicorn app.main:app --port 8080
```

Tests and the policy benchmark:

```bash
cd backend && python -m pytest -q          # 19 tests: solvers, sim-rule parity, resilience, end-to-end with faults
python -m scripts.bench 400                # closed-loop benchmark of every algorithm on the twin
```

## How a decision is made

1. **Observe:** 9 parallel GETs, then schema validation and semantic checks. The previous good snapshot is kept on failure.
2. **Predict:** the simulator's documented demand formula × a per-series EWMA correction × known future spike windows. Uncertainty comes from residuals.
3. **Detect:** stockout risk, spikes, CUSUM on unexplained demand shifts, outages, disruptions, depot supply gaps, dispatch bottlenecks, and inventory anomalies.
4. **Route:** the regime is mapped to an algorithm by a rule table. Laya/Jev replace this in phase 4, and the rules stay as the safety net.
5. **Tournament:** `greedy`, `lp`, `robust_lp`, `mpc`, `rationing`, and `hold` all produce plans. Each plan is checked against the simulator's rules, then rolled forward 16+16 ticks on the twin under P50, P90 and ×1.5 spike demand. The best robust score wins, and the router's pick wins ties within 2%.
6. **Gate:**
   - AUTO_GATED executes immediately unless the plan is flagged.
   - *Soft* flags (router asks for review, low confidence, a shipment over 50% of depot stock) open a 2-tick review window. After that the plan auto-executes unless the operator rejects or edits it.
   - *Hard* flags (MANUAL mode, stale data, degraded) always wait for approval.
7. **Act:** idempotent POSTs (`fo-<decision>-<i>`), so retries never double-ship.
8. **Reconcile:** allocation outcomes are written back to the audit trail.

## Router: Laya / Jev (System-One models)

- **Serving:** Laya runs in `ml-service` (its own `laya-serve`, which speaks Jev's `POST /v1/systemone` protocol). One backend client ([systemone.py](backend/app/systemone.py)) talks to both. Jev is enabled by setting `JEV_API_KEY`.
- **Questions:** one batched call asks four typed questions: `regime` (choice), `algorithm` (choice), `severity` (score), and `needs_human` (noul). The state is a compact JSON summary of about 400 tokens.
- **Never blocks:** model calls run in the background, with at most one in flight per model. A cycle uses the model's cached answer for the current situation *signature* (regime, stations at risk, disrupted routes, outages), or the rule table until that answer arrives.
- **Safety net:** the rule table is used whenever the model is unconfigured, down, times out (30 s background), errors, or returns an option that isn't allowed. The model's breaker opens after 3 failures, and a 401/403 disables the client with the reason shown in `/api/health`.
- **Model doubts:** low confidence, disagreement with the rules, and the model's own `needs_human` only gate a plan in budget mode, when the model's pick drives the plan without a full twin tournament. Otherwise they're recorded as `gate.notes`, because the twin, not the model, vouched for the plan.
- **Comparison:** the non-primary model runs in shadow. Every source (Laya, Jev, rules) is scored against the tournament winner. See `GET /api/router` (`tournament_accuracy`). Switch the primary at runtime with `PUT /api/router {"primary": "laya"|"jev"|"rules"}`.
- **Offline comparison:** `python -m scripts.router_eval --laya http://localhost:8001 [--jev-key ...]` runs 24 labeled twin situations (6 regimes × 4 ticks).

**Measured (zero-shot Laya, CPU i5-1145G7, 4 threads, [docs/router_eval_laya.json](docs/router_eval_laya.json)):**

| Router | Regime accuracy | Algorithm ≈ tournament best | Latency p50 |
|---|---|---|---|
| rules | 100% (labels) | 46% | 0 ms |
| Laya `typed-decisions` | 67% (never says normal/combined) | 29% (always `mpc`) | 9.6 s |
| Laya `multilingual` | 46% | 0% (mostly `hold`) | 3.9 s |

Zero-shot Laya is slow on this CPU and weak on this domain, which is why the twin tournament, not the router, picks the plan. Next steps for Laya: fine-tune it on the logged (state, tournament winner) pairs, and run it on a GPU.

## Twin calibration

`python -m scripts.calibrate --base http://localhost:8000 --out ../docs/calibration.json` (this **resets** the simulator):

- **Probes:** stock deducted at create, dispatch-cap semantics, whether CONSTRAINED cuts dispatch, FAILED plus refund on disruption, departure/arrival timing, and demand-history order.
- **Lockstep run:** the real simulator and the twin run 120 ticks with a scripted crisis and the LP policy. The twin is fed the simulator's own demand, and the report shows the first divergence.
- **Demand-formula check:** observed demand ÷ documented rate, per profile and hour.
- **Against devsim:** 0 drift, as expected. The real image has not been run yet.

## Observability, load test, CI

- **Architecture diagram:** [docs/architecture.md](docs/architecture.md)
- **Grafana:** `Fuel Ops Intelligence` is provisioned as the home dashboard (33 panels). It has rows for operations stat tiles, API RED metrics, simulator integration, intelligence (winning algorithms, fallbacks, router accuracy vs tournament, System-One and solver latency, forecast MAPE), and system (CPU/RSS plus a firing-alerts table).
- **Prometheus alert rules:** [ops/alerts.yml](ops/alerts.yml) covers breaker open, degraded mode, stale snapshot or data, service level < 95%, fallback spikes, failing router model, SSE down, API 5xx > 5%, p95 > 500 ms, and a slow decision cycle.
- **Logs:** JSON lines with `event`, `decision_id`, `actor`, and errors. Examples: `decision.executed`, `breaker.open`, `router.model_failed`, `sim.reset`.
- **Load test:** [docs/loadtest.md](docs/loadtest.md). It covers `/api/state` and the end-to-end `/recommend` path, including a simulator-fault phase, with 0% errors throughout.
- **CI:** [.github/workflows/ci.yml](.github/workflows/ci.yml) runs tests + twin benchmark → promtool/compose/dashboard checks → build → deploy (real simulator image) → health gate → twin calibration → crisis run → load test. Evidence (calibration, decisions, load test, metrics, logs) is uploaded as artifacts.

## Resilience

| Failure | Behaviour |
|---|---|
| Simulator 5xx / timeout | Retry with jittered backoff (4 attempts). A rate-based breaker opens at ≥50% post-retry failures, probes half-open after 5 s. |
| Simulator down | Serve the cached snapshot, flagged `degraded`. No auto-execution. Health shows it. |
| Invalid response (schema or semantic) | Rejected, `sim_invalid_response` alert raised, last good snapshot kept |
| `X-Simulator-Stale` | Plans are held for human approval |
| SSE dropped / 503 | Reconnect with backoff. Polling every 2 s continues regardless. |
| Solver exception | That candidate is dropped (fallback metric recorded) and the others still compete |
| Simulator reset (tick goes backwards) | Forecast, detectors, and pending plans are reset |

## Assumptions (checked against the real image in phase 3)

- The twin's tick order is: events, then supply, then allocations (arrive before depart), then demand. Depot stock is deducted when an allocation is created.
- The 22 supply-arrival quantities aren't published. The twin uses ~1 day of regional demand per arrival.
- A `CONSTRAINED` depot is treated as having 50% dispatch capacity (`CONSTRAINED_DISPATCH_FACTOR`).
- `/v1/demand-history` is assumed to return the newest rows first. A warning is logged if it doesn't.
