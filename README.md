# Fuel Ops Intelligence Platform

Decision-support backend for the BUP Fuel Supply Simulator. Each cycle it:

1. observes the network,
2. forecasts demand and detects risk,
3. generates plans with several algorithms plus a reinforcement-learning policy,
4. scores every plan on a **digital twin calibrated against the real simulator**,
5. applies the best plan, automatically or with operator approval,
6. reconciles the outcome.

The design is in [PLAN.md](PLAN.md). The architecture diagram is in [docs/architecture.md](docs/architecture.md).

All data and decisions are **simulated**. Nothing touches real fuel infrastructure.

## Run with Docker

```bash
cp .env.example .env        # set ADMIN_TOKEN at minimum; optional: JEV_API_KEY, GROQ_API_KEYS, GEMINI_API_KEYS
docker compose up --build
```

| URL | What |
|---|---|
| http://localhost:8080 | Operator console (paste ADMIN_TOKEN in the header box) |
| http://localhost:8080/docs | API (Swagger) |
| http://localhost:3000 | Grafana: `Fuel Ops Intelligence` (home dashboard) |
| http://localhost:9090 | Prometheus + alert rules |
| http://localhost:8000/admin | Simulator console |

**Starting the simulator:** it starts `paused` (`SIMULATOR_START_MODE`). Press **Run** in the console or call `POST /api/sim/run`.

**Pointing at another simulator:** set `SIM_BASE_URL` to use a judge's instance.

**Optional services:** every optional component degrades gracefully.

| If this is missing | The backend falls back to |
|---|---|
| Laya (`ml-service`) | Rule router |
| Jev key | Laya or rules |
| LLM keys | Deterministic templates |
| PPO model | Tournament without the PPO candidate |

## Run locally without Docker

The local run uses `app/devsim.py`, a stand-in simulator with the same API and fault injection, built on the twin.

```bash
python -m venv .venv && .venv/Scripts/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd backend
SIMULATION_SPEED=2 SIMULATOR_START_MODE=running python -m uvicorn app.devsim:app --port 8000 &
ADMIN_TOKEN=dev python -m uvicorn app.main:app --port 8080
python -m scripts.demo --token dev --out ../docs/demo-run.md   # scripted demo story (resets the simulator)
```

| Command (in `backend/`) | What it does |
|---|---|
| `python -m pytest -q` | 34 tests: solvers, simulator-rule parity, resilience, end-to-end with faults, router, LLM pool |
| `python -m scripts.bench 400` | Closed-loop benchmark of every algorithm on the twin |
| `python -m scripts.calibrate --base URL` | Twin vs simulator calibration (**resets** the simulator) |
| `python -m scripts.router_eval` | Laya vs Jev vs rules on 24 labeled situations |
| `python -m scripts.loadtest` | Load test, including a simulator-fault phase |
| `python -m scripts.ppo_eval vN --promote` | Evaluate a trained PPO model and promote it if it passes the gate |

## How a decision is made

1. **Observe:** 9 parallel GETs, then schema validation and semantic checks. The last good snapshot is kept on failure. Demand history is persisted per simulator epoch.
2. **Predict:** the simulator's documented demand formula (verified against the real image: no hour deviates), with:
   - the live `demand_multiplier`,
   - scheduled event windows,
   - a per-series EWMA correction.

   One-step forecast error on the real simulator: **5.1%**, which is close to the noise floor.
3. **Detect:** stockout risk (P50 time-to-stockout and P(stockout)), spikes, CUSUM on unexplained shifts, outages, disruptions, depot supply gaps, single-route dependency, and inventory anomalies. It also tracks a supply outlook (days of cover, end of the supply schedule).
4. **Route:** a System-One model (Jev or Laya) or the rule table proposes a regime and an algorithm.
5. **Tournament:** `greedy`, `lp`, `robust_lp`, `mpc` (multi-tick LP with transit lags and event windows), `rationing` (max-min fair), `hold` and `ppo` all plan.
   - Each plan is checked against the simulator's rules, then rolled forward 16+16 ticks on the twin under P50, P90 and ×1.5 demand.
   - The best robust score wins, and the router's pick wins ties within 2%.
   - In budget mode, a Thompson-sampling bandit picks which candidates run.
6. **Gate:**
   - **AUTO_GATED:** plans execute immediately unless flagged. *Soft* flags (a rule-flagged combined crisis, a shipment over 50% of depot stock, stale data) open a 2-tick review window, after which the plan auto-executes unless the operator rejects or edits it. A *hard* flag (degraded mode) always waits.
   - **MANUAL:** every plan waits, and plans under review are never replaced.
   - Every decision records its binding constraints and uncertainty notes.
7. **Act:** idempotent POSTs (`fo-<decision>-<i>`). Allocations on routes about to be disrupted are blocked, and any already PENDING are cancelled, because the simulator does *not* refund failed allocations.
8. **Reconcile:** outcomes go to the audit trail. The bandit and router accuracy learn from tournament results.

## Digital twin: calibrated against the real simulator

[`scripts/calibrate.py`](backend/scripts/calibrate.py) runs in CI against the organizer image, before the backend starts so nothing else writes to the simulator.

**Probes (one assumption each):**
- Stock is deducted at creation.
- The dispatch cap counts only allocations created this tick.
- `CONSTRAINED` does **not** reduce dispatch in the simulator. Our 50% cap is a self-imposed policy, so the constraint visibly changes behavior.
- Allocations on a disrupted route FAIL and **lose their fuel**.
- Arrivals above station capacity are clipped.
- A step processes the current tick (departures and demand are labeled t) and then advances.
- Events are in force from `start_tick` through `end_tick` inclusive.

**One-step check:** the twin is loaded with the simulator's exact state and both are stepped once with identical demand, every tick. **119/120 ticks matched exactly.** The one mismatch (event end is inclusive) is now fixed.

## Router: Laya and Jev (System-One models)

- **Protocol:** both speak `POST /v1/systemone`. Laya runs in `ml-service` (`laya-serve`), and Jev runs on OpenRouter (`JEV_API_KEY`, capped by `JEV_MAX_CALLS_PER_HOUR`). One batched call asks four typed questions: `regime`, `algorithm`, `severity`, `needs_human`.
- **Never blocks:** calls run in the background with at most one in flight per model. A cycle uses the cached answer for the current situation signature, or the rules until that answer arrives.
- **Fallback:** the rule table takes over when the model is down, slow, out-of-range, or auth-rejected, or its budget is spent.
- **Comparison:** the other model runs in shadow, and every source is scored against the tournament winner (`GET /api/router`).

Results on 24 realistic labeled situations ([docs/router_eval_jev.json](docs/router_eval_jev.json); the earlier Laya numbers used no-shipping states):

| Router | Regime accuracy | Algorithm ≈ tournament best | Latency p50 | Cost |
|---|---|---|---|---|
| rules | labels | 75% | 0 ms | 0 |
| **Jev** `jev-1.13` | **87.5%** | **75%** | **452 ms** | ~$0.00004/call |
| Laya `typed-decisions` (zero-shot, CPU) | 67% | 29% (always `mpc`) | 9.6 s | 0 |
| Laya `multilingual` (zero-shot, CPU) | 46% | 0% (mostly `hold`) | 3.9 s | 0 |

Fine-tuning Laya on (state, tournament winner) labels: [docs/laya-finetune.md](docs/laya-finetune.md).

## Reinforcement learning

- **PPO** ([ml/rl/](ml/rl/)):
  - Trained on the calibrated twin with domain randomization: supply ×0.6–1.3 and 0–3 random crises of all six types per episode.
  - The action is a priority weight and safety factor per station×fuel, which the LP turns into shipments, so every action is feasible.
  - The backend runs the exported actor in numpy (no torch).
  - Versions live in `backend/models/ppo/vN`, and a version is promoted only if it beats LP and the active model on a fixed 5-scenario suite. Rollback: `POST /api/models/ppo/rollback`.
  - Results: [docs/rl.md](docs/rl.md).
- **Bandit:** Beta-Bernoulli Thompson sampling per regime learns online which algorithm wins tournaments (`GET /api/models`).

## LLM pool: Groq and Gemini, used as little as possible

- **Models:** `qwen/qwen3.8-27b` (Groq) and `gemini-3.5-flash-lite`, both with minimal reasoning.
- **Key handling:**
  - Each call goes to the healthy key with the fewest in-flight requests.
  - A 429 puts that key on cooldown, honoring `Retry-After`.
  - A 401 disables the key; expired keys are detected automatically.
  - On failure a call moves to the next key, then the other provider.
  - Prompts are cached in SQLite.
- **When it's used:** deterministic templates are the default. The LLM is called only for plans awaiting review, incident summaries, and `POST /api/ask` (admin). Measured latency: Groq ~0.7 s, Gemini ~2 s.

## Observability, load test, CI, demo

- **Grafana:** 43 panels covering operations, API RED metrics, simulator integration, intelligence (winning algorithms, fallbacks, router accuracy, model and solver latency, forecast MAPE), learning, LLM and recovery (incident, time to recover, failures, auto-cancels, decision lag), and system.
- **Alerts:** [ops/alerts.yml](ops/alerts.yml) (11 rules). Logs are JSON lines with `event`, `decision_id`, `actor`.
- **Load test:** [docs/loadtest.md](docs/loadtest.md). On CI containers: `/api/state` ~500 RPS at p95 62 ms; the full decision pipeline ~20 decisions/s; 0% errors, including under a 500 ms simulator fault.
- **Demo:** [docs/demo-run.md](docs/demo-run.md) covers the problem statement's story end to end (spike, review, combined crisis, outage, recovery, router fallback).
- **CI:** [.github/workflows/ci.yml](.github/workflows/ci.yml) runs:
  1. tests and the twin benchmark
  2. promtool, compose and dashboard checks
  3. calibration against the real simulator image
  4. build, deploy and the health gate
  5. the scripted demo
  6. the load test

  The evidence is uploaded as artifacts.

## Resilience

| Failure | Behaviour |
|---|---|
| Simulator 5xx / timeout | Retry with jittered backoff (4 attempts). A rate-based breaker opens at ≥50% post-retry failures and probes with a real `/v1` call after 5 s. |
| Simulator down | Serve the cached snapshot, flagged `degraded`. No auto-execution. Visible in health and alerts. |
| Invalid response (schema or semantic) | Rejected, `sim_invalid_response` alert raised, last good snapshot kept |
| `X-Simulator-Stale` | Soft gate (review window). The simulator still validates every POST against true state. |
| SSE dropped or silently stalled | Reconnect with backoff, plus a tick watchdog. Polling every 2 s continues regardless. |
| Simulator crash notice | Critical alert |
| Simulator reset | New epoch: forecast, detectors, pending plans and open decisions reset |
| Route about to be disrupted | Shipments blocked, and PENDING ones on it cancelled (refunded instead of lost) |
| Solver exception | That candidate is dropped and the others compete |
| Laya/Jev down, slow or unauthorized | Rule router |
| All LLM keys failing | Deterministic templates |
| Our own malformed request (`ROUTE_MISMATCH`, idempotency mismatch, 422) | Never retried; `integration_bug` alert |

## Known limits

- **The scenario's supply schedule ends** (see `supply_outlook` in `/api/state`). After that, depots only drain whatever the policy does. Reset between demo segments.
- **`/v1/allocations` has no paging,** so snapshots grow over very long runs.
- **The bandit posterior and router cache are in memory.** Decisions, alerts, demand history and the LLM cache persist in SQLite.
