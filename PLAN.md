# Fuel Ops Intelligence Platform: Implementation Plan

Scope: backend and intelligence first; the operator UI stays minimal. It targets the BUP Fuel Supply Simulator v1.0.0 (2 regions, 2 depots, 4 stations, 6 routes, 3 fuels, 15-minute ticks).

---

## 0. Core design idea

Every decision passes through **four tiers, cheapest first**. Each tier falls back to the one below it.

| Tier | What | Latency | Cost | When it runs |
|---|---|---|---|---|
| T0 | **Math toolbox** (greedy, LP, MPC, robust LP, rationing) + forecast + detectors | 1–30 ms | free | every decision cycle |
| T1 | **System-One router**: Laya (local), later Jev (OpenRouter) | 30–450 ms | free / ~$0 | only when the situation *changes* (state-signature cache) |
| T2 | **PPO policy** (RL) + **counterfactual check** on digital twin | 5–50 ms | free | every cycle; twin picks the winner vs the T0 plan |
| T3 | **Groq / Gemini LLM** (text only) | 0.5–3 s | quota | only explanations for human-review items, incident summaries, operator Q&A; cached by content hash; template fallback |

Jev and Laya output typed choices, not text. That makes them good fits for regime classification, choosing the algorithm, risk scoring, and "needs human?" gating. Text generation is left to Groq and Gemini, which are called rarely.

---

## 1. Architecture (docker compose)

```
┌────────────┐  REST+SSE   ┌──────────────────────────── backend (FastAPI) ─────────────────────────────┐
│ simulator  │◄───────────►│ sim_client ─► state_store ─► forecast/detect ─► orchestrator ─► gate ─► executor │
│ (organizer │  /admin/*   │   retry·breaker·validate·cache      │            │  ▲            │              │
│   image)   │             │                                      ▼            ▼  │            ▼              │
└────────────┘             │                               math toolbox   ml_client  llm_pool  audit(SQLite)  │
                           │                                                  │     (groq+gemini keys)       │
                           │  /metrics  /api/*  /ui (static HTML)             │                               │
                           └──────────────────────────────────────────────────┼───────────────────────────────┘
                                                                              ▼
                                                   ml-service (Laya + PPO inference)   ← killable: backend falls back
                                                   Jev via OpenRouter (remote)          ← optional, shadow-compared
prometheus ─scrapes─► backend, ml-service     grafana ◄─ provisioned dashboard
k6 (profile: loadtest)                         trainer (profile: train) → PPO on digital twin
```

| Service | Image / stack | Why separate |
|---|---|---|
| `simulator` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | given |
| `backend` | Python 3.12, FastAPI, httpx, pydantic v2, scipy (HiGHS), numpy, aiosqlite, prometheus_client | the brain; stays lean (no torch) |
| `ml-service` | Python 3.12, `laya`, torch-cpu, stable-baselines3 | ~2 GB RAM, slow cold start; killing it gives a clean resilience demo of "ML model unavailable → fallback" |
| `prometheus`, `grafana` | official images | observability |
| `trainer` | same image as ml-service, compose profile `train` | offline PPO training, not always running |
| `k6` | `grafana/k6`, compose profile `loadtest` | load test |

Storage: SQLite (WAL) on a volume holds the decision audit, the LLM cache, the RL transitions, and the model registry. *ponytail: SQLite single writer. Switch to Postgres if the load test shows write contention.*

Python 3.12 runs in containers. The local Python 3.14 is too new for torch and Laya.

---

## 2. Simulator integration and resilience (the most important part)

**`sim_client.py`**, used for every `/v1/*` call:
- `httpx.AsyncClient` with connect/read timeouts (2 s / 5 s).
- **Retry** on 503 `FAULT_INJECTED`, timeouts, and connection errors: exponential backoff with full jitter, 4 attempts. Never retry a 4xx.
- **Circuit breaker** per endpoint group (closed → open after 5 failures within 30 s → half-open probe via `/v1/health`, which bypasses faults).
- **Validation**: every response is parsed into pydantic models plus semantic checks (inventory ≥ 0, inventory ≤ capacity, known ids, tick monotonic). Invalid responses are rejected, raise a `sim_invalid_response` alert, and leave the last good snapshot in place.
- **Stale data**: `X-Simulator-Stale: true` → mark the snapshot `stale`, invalidate the cache, and block **auto**-execution (humans can still approve).
- **Cached state / degraded mode**: the last good snapshot is served with an `age_ticks` field. If the breaker is open, the system is DEGRADED: the UI keeps working from cache, decisions are queued, and nothing is auto-submitted.
- **Idempotency**: `key = sha1(tick|depot|station|route|fuel|qty|plan_id)`. Retries are safe, a mismatch cannot happen by accident, and we never reuse a key.
- **POST error mapping** (see spec §9): `ROUTE_CAPACITY_EXCEEDED` → split the shipment; `DISPATCH_CAPACITY_EXCEEDED` → defer to the next tick; `INSUFFICIENT_INVENTORY`/`DESTINATION_CAPACITY_EXCEEDED` → re-fetch and re-solve; `ROUTE_DISRUPTED` → re-solve without that arc; `STATION_CLOSED`/`DEPOT_CLOSED` → drop and alert.
- **Pre-submit constraint checker**: the same rules the simulator enforces (§5.2) run locally first, so bad allocations never leave the backend.

**`sse_listener.py`**: connects to `/v1/stream`, reconnects with backoff (503 while `stream_disconnect` is active), and treats 15 s keepalives as normal. After any reconnect it does a full REST refetch. SSE events are **hints** that trigger a refresh; REST is the source of truth. A **polling fallback** (every 2 s on `/v1/instance`) runs whenever SSE is down.

**Reconciler**: each tick it compares our decision ledger with `/v1/allocations`, marks FAILED allocations, and updates realized reward for RL.

**Self-test chaos API** (backend `/api/chaos/*`, admin-only): a wrapper over `/admin/faults` and `/admin/events`, plus a local switch that kills or blocks `ml-service`, Jev, and the LLM pool. It is used for the demo and for CI.

| Failure | Behavior |
|---|---|
| ml-service down | router = rule table, policy = LP; `fallback_activations_total{reason="ml_down"}` |
| Laya/Jev low confidence (< 0.6) | decision goes to the human queue |
| Laya vs rule disagreement on regime | human queue + both shown |
| Invalid simulator response | reject, alert, keep the cached snapshot |
| Simulator 503 / latency | retry, then breaker, then degraded read-only mode |
| Stale data | no auto-execution |
| All Groq+Gemini keys failing | templated explanation (deterministic, from solver outputs) |
| PPO plan fails twin check | LP plan used |
| New PPO model worse on eval suite | not promoted; manual **rollback** to previous version via API |

---

## 3. Prediction and detection (T0)

The demand generator is documented: profile base × hour-of-day factor × region factor × `demand_multiplier` × noise. We use that structure directly.

- **Forecast** per (station, fuel), H = 16 ticks ahead: `expected = base_profile/96 × hour_factor(t) × region_factor × m̂`. `m̂` is an EWMA of observed/expected demand, which tracks spikes within about 2 ticks. Uncertainty comes from the residual std, giving P10/P50/P90. We report the forecast error (MAPE) as a metric.
- **ML residual model** (scikit-learn `HistGradientBoostingRegressor`, quantile loss): learns the formula's error from hour, station, fuel, the recent residual trend, active events, and multiplier drift. This sharpens P10/P90. A **rolling backtest** compares formula-only against formula + ML live, uses whichever wins, shows both errors in Grafana, and retrains on drift.
- **Delay/failure classifier** (`HistGradientBoostingClassifier`): P(supply arrival delayed) and P(allocation fails) from events, route status, and depot constraint. Feeds MPC and the twin scenarios.
- **Projected inventory / time-to-stockout**: inventory − cumulative forecast + in-transit arrivals (from `/v1/allocations`) → ticks to zero. **Stockout probability** is computed with a normal approximation over the cumulative demand variance.
- **Detectors**
  - demand anomaly: CUSUM on standardized residuals, so spikes are caught *before or without* reading `/v1/events`
  - inventory anomaly: Δinventory not explained by served demand + arrivals + dispatch
  - bottleneck: depot dispatch utilization > 90% for 3 or more ticks, or a route with max_shipment binding
  - disruption: route/station/depot status changes, supply-arrival `DELAYED`, or a quantity drop (shortfall)
- Output: `RiskItem{station, fuel, hours_to_stockout, p_stockout, signals[], confidence}` → alerts.

---

## 4. Math toolbox (T0). The router picks one, and all of them respect the same constraints.

| # | Algorithm | Use when | Implementation |
|---|---|---|---|
| A1 | **Greedy urgency** | fallback, trivial load | sort by time-to-stockout; fill from the nearest feasible depot |
| A2 | **Single-tick LP** (min-cost flow) | normal ops | `scipy.optimize.linprog` (HiGHS): min Σ unmet-risk penalty + transit_ticks·liters, s.t. route max, dispatch/tick, depot inv, station headroom |
| A3 | **Rolling-horizon MPC** | shipment delay, supply shortfall | multi-tick LP over H ticks with transit lags and scheduled arrivals; rations depot stock across time; executes only the first step |
| A4 | **Robust LP** | demand spike, high forecast variance | A2 using P90 demand plus a safety stock sized to cover uncertainty |
| A5 | **Fair-share rationing** | total supply < total demand (combined crisis) | max-min fairness on service level across stations: LP maximizing the minimum coverage ratio, with priority weights |
| A6 | **Hold** | baseline | ship nothing. This is the "without action" reference for expected impact. (Rerouting needs no separate algorithm: A2–A5 only see AVAILABLE arcs, and MPC knows the disruption window from `/v1/events`.) |
| RL | **PPO policy** | any regime; must beat the chosen Ax on the twin | see §6 |

Integer shipments are rounded afterward and split by `max_shipment`. *ponytail: LP relaxation plus rounding instead of MILP. Switch to `scipy.optimize.milp` if rounding ever causes a constraint violation (the constraint checker catches it).*

---

## 5. Router and orchestrator (T1 + T2 + gate)

**State signature** = (regime flags, bucketed risk levels, active events, breaker states). Laya or Jev is called **only when the signature changes**. Otherwise the cached routing is reused, which keeps decisions fast.

**System-One questions** (one batched call):
```
regime:        choice  {normal, demand_spike, supply_crisis, route_disruption, depot_constraint, combined}
algorithm:     choice  {greedy, lp, mpc, robust_lp, rationing, reroute}   (criteria text = "use when..." from §4)
severity:      score   ["normal","watch","warning","critical"]
needs_human:   noul    "Is this decision unusual, high-impact or uncertain enough to need operator review?"
```
The state is a compact JSON (< 512 tokens) with risks, events, depot slack, and route statuses.

**Laya ↔ Jev comparison mode**: Laya is primary; Jev runs in **shadow** once the OpenRouter key arrives. We log both answers, latency, cost, agreement rate, and *which one's pick produced the better twin outcome*. We expose this at `/api/compare` and in Grafana. The config switch `ROUTER_PRIMARY=laya|jev|rules` can be flipped live.

**Safety net**: a deterministic **rule table** also maps the regime to an algorithm. When they disagree, or confidence is below 0.6 → human queue. Laya's zero-shot accuracy on typed decisions is modest (reported around 0.36–0.77 depending on checkpoint), so the rules protect us. We collect labeled (state, best-algorithm-by-twin) pairs, and we can fine-tune Laya later if time allows (a Kaggle notebook of about 4 hours).

**Bandit over algorithms** (online RL, live): Thompson sampling per regime over {A1…A6, PPO}. The reward is the realized Δunmet demand over the next N ticks. Laya/Jev supply the prior, and the bandit learns from real outcomes over time.

**Tournament (the "Simulate" step): run many algorithms, apply the best one**
- **Full tournament** (default when time allows, and always in testing): all of A1–A6 plus PPO produce a plan. Each plan is rolled forward 8 ticks on the **digital twin** from the current state, under 3 demand scenarios (P50, P90, spike ×1.5). Score = mean(unmet + λ·cost) + β·worst-case, so a plan that only works in the average case loses. The lowest score is applied. This runs in about 150–300 ms.
- **Jev/Laya's role**: they pick the algorithm *before* the tournament. We record whether that pick = the tournament winner, which gives **router accuracy** per regime (the core Laya-vs-Jev comparison). The router pick wins ties (score within 2%), which keeps decisions stable.
- **Budget mode** (simulator speed > 2 ticks/s, load test, or the cycle is over its budget): only the top-3 = router pick + bandit pick + PPO are scored.
- **Twin down or drift > 5%**: the router's pick is applied directly, with a rule-table cross-check.
- Every tournament result (state, all scores, winner) is stored. The same log serves as the bandit's reward signal, **labeled data to fine-tune Laya**, and the table for "why this algorithm."
- The winning plan's scores become the "Expected result: risk 72% → 19%" and "alternatives considered" sections in the UI (problem statement §9).

**Gate / autonomy**:
- `mode=AUTO_GATED` (default): the plan auto-executes unless it is flagged.
  - **Soft** flags (needs_human > 0.5, confidence < 0.6, router/rule disagreement, a shipment > 50% of depot stock) open a **2-tick review window**. After that the plan auto-executes unless the operator rejects or edits it.
  - **Hard** flags (stale data, degraded) always wait.
  - A newer plan supersedes older unreviewed ones. *(Changed during the build: waiting indefinitely let the network run dry in an unattended combined crisis, with service level falling to 86%.)*
- `mode=MANUAL`: every plan waits for approval.
- The operator can **edit** a pending plan (quantity, route, source), **reject** it, **cancel** a submitted allocation while PENDING (`/v1/allocations/{id}/cancel`), or **override** an auto-executed one (cancel it and submit a replacement).
- Admin actions need an `X-Admin-Token` (from the env). Read endpoints are open.

Every decision is written to the **audit** table: inputs snapshot hash, risks, router answers (Laya/Jev), candidates, twin scores, chosen plan, gate reason, actor (auto or operator), sim allocation ids, and outcome.

---

## 6. Digital twin and reinforcement learning (the other most important part)

**Digital twin** (`twin.py`, pure numpy): a re-implementation of the documented dynamics covering demand (profiles, hour factors, noise with seed), serving/unmet, dispatch capacity, transit ticks, supply arrivals, and all 6 event types. **Calibration test**: reset the real simulator, then apply the same actions and step both for 200 ticks. Assert that the served/unmet/inventory drift is below 2%, and expose `twin_drift` as a metric. If the simulator's hidden details differ, we fit the parameters from `/v1/demand-history`.

**Gymnasium env** on the twin:
- **State** (~70 floats): per station×fuel inventory/capacity, forecast demand H=4/8/16, p_stockout, in-transit; per depot×fuel inventory, dispatch slack; route availability; upcoming supply (next 64 ticks); hour-of-day sin/cos; active event flags.
- **Action** (hybrid, always feasible): PPO outputs per station×fuel **priority weights + safety-stock targets** (12 + 12 continuous values). These feed the A2/A3 LP, which produces feasible shipments. The RL learns *what to prioritize and how much buffer to hold*, and the LP guarantees constraints. This trains in hours instead of days and cannot emit an invalid allocation.
- **Reward** per tick: `−unmet_liters/1000 − 0.02·transit_ticks·liters/1000 − 5·[new stockout] − 2·[failed allocation] − 0.1·overflow_attempts`. It is shaped toward the judged `service_level`.
- **Domain randomization** during training: random demand spikes (1.2–2.5×), route disruptions, station outages, depot constraints, shipment delays, supply shortfalls, and random combos. This is what makes the policy hold up against the organizers' surprise events.
- **Training**: SB3 PPO, 8 vector envs, about 2–5M steps on CPU in the `trainer` container. Versioned to `models/ppo/vN/` with a `metrics.json`.
- **Evaluation suite** (fixed seeds × 6 scenarios: baseline, spike, route_disruption, delay, shortfall, combined): PPO vs greedy vs LP vs MPC. We report service level, unmet liters, transport cost, and failures. This is the "why RL beats heuristics" evidence the problem statement asks for (§8).
- **Promotion gate**: a new model is promoted only if it beats the current model *and* the LP baseline on the suite. There is a rollback endpoint `POST /api/models/ppo/rollback`.
- **Online learning**: real transitions (state, action, reward from the reconciler) are stored. An API-triggered fine-tune runs on twin + real replay, then goes through the promotion gate again. We track **drift** (forecast residual distribution and twin drift) to trigger it.

---

## 7. LLM pool (T3: Groq + Gemini, minimal use)

- Keys come from the env `GROQ_API_KEYS` and `GEMINI_API_KEYS` (comma lists) and are **never** in code or git.
- Each key keeps its state: in-flight count, cooldown until (from 429 `retry-after`), and consecutive failures. **Pick** = the healthy key with the fewest in-flight requests (spreads load across keys in parallel). **Failover**: key → next key → other provider → template.
- A key with 3 consecutive auth errors is disabled.
- Calls are **only** made for: (1) explanations for human-queue decisions, (2) incident summaries when an event starts or resolves, (3) `/api/ask` operator questions over the current state. They are cached by `sha1(prompt)` in SQLite, with a hard timeout of 6 s.
- Metrics: `llm_calls_total{provider,key_idx,outcome}` and the template-fallback count, so we can show how little we depend on the LLMs.

---

## 8. Backend API (FastAPI)

```
GET  /api/health            component health (sim, breaker, ml-service, jev, llm pool, db, sse) + p95 + error rate
GET  /api/state             latest snapshot (+stale/age), risks, alerts
GET  /api/forecast          per station×fuel forecast + p_stockout
GET  /api/decisions         audit history (filter by status)
POST /api/decisions/recommend   run pipeline now → candidate plan (no execute)       ← load-tested path
POST /api/decisions/{id}/approve | reject | edit          (admin)
POST /api/allocations/{sim_id}/cancel                      (admin)
GET/PUT /api/mode           AUTO_GATED | MANUAL                (admin)
PUT  /api/router            laya | jev | rules primary         (admin)
GET  /api/compare           Laya vs Jev stats
GET  /api/models  POST /api/models/ppo/{promote|rollback}     (admin)
POST /api/chaos/*           inject sim faults/events, kill ML/LLM (admin)
POST /api/ask               operator question → LLM (cached)
GET  /metrics               Prometheus
GET  /ui                    static single-page dashboard
```
Background loop: on each tick (from SSE or polling), refresh → forecast → detect → decide (every tick when there is risk, otherwise every 4 ticks) → gate → execute → reconcile. The simulator speed is configurable; for demos we run `SIMULATION_SPEED=1–2` so humans can follow.

---

## 9. Observability

- **Prometheus metrics**:
  - app: `http_requests_total`, `http_request_duration_seconds` (histogram → p50/p95/p99), errors
  - sim: `sim_call_duration`, `sim_retries_total`, `sim_breaker_state`, `sim_stale`, `sse_connected`, `snapshot_age_ticks`
  - intelligence: `forecast_mape`, `router_confidence`, `router_agreement{laya,jev,rules}`, `alerts_total{type}`, `decisions_total{algo,gate}`, `fallback_activations_total{reason}`, `twin_drift`, `ppo_model_version`, `bandit_arm_mean`
  - business: `service_level`, `unmet_liters`
  - LLM: calls and failures per key/provider
  - system: process CPU and memory (the default process collector)
- **Grafana**: one provisioned dashboard (JSON in the repo) with rows for App, Simulator link, Intelligence, Business, and LLM.
- **Logs**: JSON structured logs (stdlib `logging` with a JSON formatter). Each line has `event`, `tick`, `decision_id`, and `trace_id`. Key events: decision.created, decision.executed, fallback, breaker.open/close, sse.reconnect, sim.invalid.
- **Prometheus alert rules**: breaker open, service_level < 0.95, fallback rate spike, snapshot stale for more than 30 s.

---

## 10. Operator UI (minimal)

One `static/index.html` with vanilla JS polling `/api/state`, `/api/decisions`, and `/api/health` every 2 s. It has five panels: inventory table (color by p_stockout), alerts, pending decisions (approve / reject / edit qty), decision history, and system status. It also has a mode toggle and router toggle. No build step and no framework. Grafana covers charts.

---

## 11. Load testing

k6 script (`loadtest/k6.js`), compose profile `loadtest`:
1. `GET /api/state`: ramp 10 → 200 VUs
2. `POST /api/decisions/recommend`: ramp 5 → 50 VUs (full pipeline, twin included, router cached)
3. The same runs under an injected `latency 500ms` simulator fault, to show the cache and degraded behavior.

We report avg, p50, p95, p99, RPS, error rate, and CPU/memory (from Grafana) in `docs/loadtest.md`, including where it breaks and why.

---

## 12. DevOps

- `docker compose up` brings up the simulator, backend, ml-service, prometheus, and grafana. Every service has a healthcheck, and `depends_on: condition: service_healthy` is set.
- `.env.example` documents all config, and `.env` is gitignored.
- **GitHub Actions**: ruff → pytest (unit: solvers, constraint checker, forecast, key pool, sim_client with `respx` fault mocks; twin-vs-sim calibration) → docker build → compose up with the real simulator image → smoke test (`/api/health` green, one recommend, one chaos fault is survived) → push images.
- Model versioning in `models/` with the promotion/rollback API. Deployment version is exposed in `/api/health`.

---

## 13. Repo layout

```
backend/app/{main.py, config.py, sim_client.py, sse.py, state.py, forecast.py, detect.py,
             solvers.py, twin.py, router.py, bandit.py, orchestrator.py, gate.py, llm_pool.py,
             audit.py, metrics.py, api.py}  static/index.html  tests/  Dockerfile
ml/{service.py (Laya + PPO inference), env.py, train.py, evaluate.py}  Dockerfile
ops/{prometheus.yml, alerts.yml, grafana/…}   loadtest/k6.js   docs/{architecture.md, loadtest.md, resilience.md}
docker-compose.yml  .env.example  .github/workflows/ci.yml
```

---

## 14. Build order (each phase ends runnable)

| Phase | Deliverable | Done when |
|---|---|---|
| 1 | compose + sim_client (retry/breaker/validate/cache) + SSE/poll + state + `/api/health`, `/api/state` + metrics | survives every `/admin/faults` type |
| 2 | forecast + detectors + solvers A1–A6 + constraint checker + executor + audit + gate + minimal UI | runs 500 ticks autonomously with LP, service_level logged |
| 3 | digital twin + calibration test | twin drift < 2%. *Status: done. One-step check against the real image in CI: 119/120 ticks exact; the last mismatch (event end inclusive) is fixed. Probes documented in README.* |
| 4 | ml-service with Laya router + rule safety net + state-signature cache + compare mode | Laya routing live, fallback on kill works. *Status: done. Routing is non-blocking; zero-shot Laya measured weak and slow on CPU (see README); killing Laya falls back to rules with no service loss.* |
| 5 | PPO env + training + eval suite + promotion/rollback + bandit + algorithm tournament on twin | PPO vs baselines table. *Status: code, bandit and tournament done; PPO trained on the calibrated twin, results in docs/rl.md. ML residual forecast and delay classifier **dropped**: demand = documented formula + i.i.d. noise (5.1% MAPE ≈ noise floor), and delays/failures are directly observable (PLAN_GAPS A12).* |
| 6 | LLM key pool + explanations/incident summaries/ask | all-keys-down → template. *Status: done. Verified live: qwen3.8-27b (Groq, ~0.7 s), gemini-3.5-flash-lite (~2 s); expired keys auto-disabled.* |
| 7 | Grafana dashboard, alerts, load test + report, CI workflow, docs + architecture diagram | full demo script passes end-to-end. *Status: done. 43 panels, 11 alert rules, load test (CI: 500 RPS state, 20 decisions/s, 0% errors), scripted demo runs in CI against the real image.* |
| 8 | Jev integration (when key arrives) + Laya-vs-Jev report | compare stats in Grafana. *Status: done. Jev: regime 87.5%, algorithm near-best 75%, p50 452 ms, ~$0.00004/call, hourly budget cap. Laya fine-tuning: docs/laya-finetune.md.* |

Demo script (maps to problem statement §22): normal ops → inject demand spike → CUSUM detects before the event is read → risk alert → router: `demand_spike → robust_lp` → twin shows 72%→19% → operator inspects/edits → route disruption + shipment delay (combined) → reroute/MPC/rationing → kill ml-service → fallback to rules + LP, alert fires → restore → inject `error_rate` 0.5 → retries/breaker/degraded mode visible in Grafana → recovery.

---

## 15. Gap analysis status (PLAN_GAPS.md)

**Done:**
- A1 reset/epochs · A2 probe uses a faultable call · A3 rate breaker · A4 coalescing loop + decision-lag metric · A5 review window, TTL, stale is soft, MANUAL never supersedes · A6 start mode explicit + controls · A7 error envelopes + integration-bug alerts · A8 SSE watchdog + crash notices
- A9 demand history persisted per epoch · A10 live multiplier + scheduled events · A11 supply outlook · A12 classifier dropped · A13 self-imposed constraint (documented) · A15 auto-cancel before disruption · A16 route-dependency bottleneck · A18 ground-truth KPIs · A19/A22 verified by calibration · A20 configurable `SIM_BASE_URL` · A21 `/admin/audit` timeline
- B2 README · B5 evidence in CI · B8 observability · B9 model versioning/rollback/audit · B10 scripted demo
- D2 plan TTL · D3 recovery metric · D6 region view · D7 uncertainty notes · D8 binding constraints · D12 scenario identity · D13 timing (calibrated) · D14 alerts in Grafana

**Deliberately not done (with reason):**
- A14 stricter capacity check: the twin measures overflow in plan scoring, and the calibration showed the simulator clips arrivals.
- A17 partial snapshots: all-or-nothing keeps a consistent state; the rate breaker and retries handle flakiness.
- B1 richer operator UI: owner's choice (backend priority).
- D11 `/v1/allocations` growth: the API has no paging.
- D5 RL on the real simulator: twin-only for now; the twin is calibrated (one-step exact), so the gap is small.

