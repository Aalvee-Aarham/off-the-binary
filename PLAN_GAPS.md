# PLAN.md Gap Analysis

**Compared against:**
1. *BUP CSE FEST 2026 Hackathon Finals: Problem Statement* (the PDF, cited as **PS §n**)
2. *BUP Fuel Supply Simulator: Integration and Interaction Guide* (cited as **IG §n**)
3. `PLAN.md` (cited as **PLAN §n**)

**Severity**

| Tag | Meaning |
|---|---|
| 🔴 Critical | Will break at runtime, or will visibly lose marks in a heavily weighted criterion |
| 🟠 High | Likely to hurt during judging or a surprise event |
| 🟡 Medium | Missing polish, evidence, or a requirement that is only partly met |
| ⚪ Low | Nice to fix, or a verify-before-building item |

Items marked **VERIFY** depend on simulator behavior the guide does not document. Test them early with `POST /admin/pause` + `POST /admin/step`.

## Summary

**48 gaps** in total: 22 against the integration guide (Part A), 12 against the problem statement (Part B), and 14 more found in the second, line-by-line pass (Part D). Part C is the full coverage matrix, and Part E is the fix order.

| Severity | Count | Where |
|---|---|---|
| 🔴 Critical | 7 | A1–A5, B1, B2 |
| 🟠 High | 13 | A6–A13, B3–B5, D1, D2 |
| 🟡 Medium | 18 | A14–A18, B6–B10, D3–D9, D13 |
| ⚪ Low | 10 | A19–A22, B11, B12, D10–D12, D14 |

The plan's intelligence and resilience **design** is strong. Most critical gaps are integration bugs that only show up against the real simulator: reset, the fault-bypassing health endpoint, breaker thresholds, the default speed of 8 ticks/s, and human review at that speed.

---

## Part A: Plan vs the Simulator Integration Guide

These are mostly runtime bugs: places where the plan assumes simulator behavior that the guide contradicts or leaves undocumented.

### A1 🔴 `/admin/reset` will make every response fail validation

- **Guide:** `POST /admin/reset` wipes all tables (including allocations) and reloads the scenario. Tick goes back to 0 and allocation ids restart at 1. It publishes the SSE event `simulator.notice {"message": "Simulation reset"}` (IG §7.6, §6.3).
- **Plan:** validation rejects any response where the tick is not monotonic (PLAN §2). The reconciler, audit, and RL transitions key on simulator allocation ids (PLAN §2, §5).
- **Impact:** after any reset, which judges are likely to do before a run, every snapshot is rejected as invalid. The system stays stuck on the pre-reset cache and old allocation ids collide with new ones.
- **Fix:** add a **run epoch**. Treat `simulator.notice` "Simulation reset", or tick decreasing together with `/v1/instance` confirming it, as a reset. Start a new epoch, clear the snapshot, ledger, forecaster state, and breaker, and store `(epoch, sim_allocation_id)` everywhere instead of the bare id.

### A2 🔴 Circuit-breaker probe and health page use `/v1/health`, which ignores faults

- **Guide:** `/v1/health` bypasses all fault injection (IG §2, §4.1).
- **Plan:** the half-open probe calls `/v1/health` (PLAN §2), and `/api/health` reports simulator health (PLAN §8).
- **Impact:** during an `unavailable` or `error_rate` fault, the probe always succeeds, so the breaker closes, fails again, and reopens in a loop. The status panel says "Fuel Simulator: Healthy" while every real call fails, which is exactly what judges will be looking at during the failure demo.
- **Fix:** probe with a cheap faultable call such as `GET /v1/instance`. Report simulator health from two signals: **liveness** from `/v1/health` and **API availability** from breaker state and the recent `/v1/*` error rate.

### A3 🔴 Breaker threshold trips on faults that retries already handle

- **Guide:** `error_rate` returns 503 with probability `rate` (default 0.25) on every `/v1/*` call (IG §7.10).
- **Plan:** the breaker opens after 5 failures in 30 s per endpoint group, and each call retries 4 times (PLAN §2).
- **Impact:** a full refresh is about 9 GETs per tick. At 8 ticks/s that is roughly 70 requests/s, so a 25% error rate produces far more than 5 raw failures in 30 s. The breaker opens and the system drops to degraded mode even though retries would have succeeded (0.25⁴ ≈ 0.4% chance a call fails all 4 attempts).
- **Fix:** count a failure only after retries are exhausted, and use a failure **rate** over a window (for example, more than 50% of the last 20 logical calls) rather than a raw count.

### A4 🔴 The pipeline cannot keep up with the default simulator speed

- **Guide:** default `SIMULATION_SPEED` is **8 ticks/s**, one tick every 125 ms (IG §1, §3).
- **Plan:** decisions run every tick when there is risk (PLAN §8). The full tournament takes 150–300 ms (PLAN §5). The router call takes 30–450 ms. Budget mode only reduces the candidate count (PLAN §5). Demos run at speed 1–2.
- **Impact:** at the default speed, one decision cycle spans 2–4 ticks, work queues up, and decisions act on stale state. Judges run your submission against the published image (IG §2) and may not use your speed.
- **Fix:**
  - Make the loop **latest-state-wins**: coalesce ticks and never queue per-tick work.
  - Add a `decision_lag_ticks` metric and alert.
  - Choose the decision cadence from the measured tick rate instead of a fixed "every tick".
  - Put the default-speed behavior in the README and resilience doc.

### A5 🔴 Human review cannot work at simulator speed, and several gates block execution for long periods

- **Guide:** at speed 8, 1 wall-clock second is 2 simulated hours. Station stock covers roughly one day of demand (IG §8.3, §8.5). A `stale_data` fault can last up to 3600 s (IG §7.9).
- **Plan:** plans wait for a human when the regime is `combined`, the data is stale, confidence is low, and so on. `MANUAL` mode waits for every plan (PLAN §5). There is no expiry on pending plans.
- **Impact:**
  - A plan waiting 30 s for approval is about 240 ticks, or 2.5 simulated days, old. Stations will have run dry and the plan's quantities may no longer be valid.
  - The `combined` crisis, the most important judged scenario, never auto-executes anything.
  - A long stale fault blocks all auto-execution for up to an hour.
- **Fix:**
  - Give pending plans a **TTL in ticks**. Re-validate and re-solve on approval, and show "plan is N ticks old" in the UI.
  - While waiting for review, auto-execute a conservative **safe floor** (for example, rationing or greedy capped at a small quantity) instead of doing nothing.
  - Optionally let the operator **pause the simulator** during review via `/admin/pause`. That is allowed for self-test (IG §2); document it as a demo-mode choice.
  - Note that `stale_data` only adds a header to GETs and does not block POSTs, and the simulator validates every allocation against true state (IG §5.2, §7.10). A bad stale-based allocation is rejected with a 409, not silently applied, so auto-executing small allocations under stale data is lower risk than the plan assumes.

### A6 🟠 Nobody starts the simulator clock

- **Guide:** `SIMULATOR_START_MODE` defaults to **`paused`** (IG §1).
- **Plan:** does not mention `/admin/run`, the start mode, or behavior while paused.
- **Fix:** set `SIMULATOR_START_MODE` and `SIMULATION_SPEED` explicitly in compose and `.env.example`. Show the simulator's PAUSED/RUNNING status in the UI. Add a UI or `/api/sim/run|pause` control for the demo. Handle a paused simulator as a normal state, not an outage.

### A7 🟠 Error-response parsing and status codes are incomplete

- **Guide:** errors come in four shapes (IG §9):
  - `{"detail": {"code", "message"}}` for allocation errors;
  - `{"error": {"code": "FAULT_INJECTED", ...}}` for faults;
  - `{"detail": {"code": "FAULT_INJECTED"}}` for `stream_disconnect`;
  - `{"detail": [...]}` for 422 validation errors.

  An idempotent replay is documented as **201** in IG §5.4 but **200** in IG §9.
- **Plan:** maps 7 of the 12 allocation error codes, plus retry on `FAULT_INJECTED` (PLAN §2). It has no handling for `ROUTE_MISMATCH`, `NOT_FOUND`, `IDEMPOTENCY_KEY_MISMATCH`, `CANNOT_CANCEL`, `ALLOCATION_NOT_FOUND`, or 422, and it doesn't cover the envelope differences.
- **Fix:** write one error parser that accepts all four shapes, and treat both 200 and 201 as success. Map `IDEMPOTENCY_KEY_MISMATCH`, `ROUTE_MISMATCH`, `NOT_FOUND`, and 422 to "bug in our code": alert and never retry. Map `CANNOT_CANCEL` to "already departed", so the override flow must submit a compensating allocation.

### A8 🟠 SSE gaps: silent drops, missing event types, notices

- **Guide:** SSE sends only `simulation.tick`, `allocation.status_changed`, `inventory.updated` (**depot inventory only**), and `simulator.notice` (IG §6.3). A subscriber that falls more than 200 events behind is **silently dropped** (IG §6.1). There is no replay (IG §6.2).
- **Plan:** treats SSE as a hint and REST as the source of truth, which is correct. It does not handle:
  - **Silent drop:** the connection can look alive (keepalives) while no ticks arrive.
  - **No SSE event for route, station, or event status changes or supply arrivals:** these must come from REST polling every tick, and the plan's detectors depend on them.
  - **`simulator.notice` with `level: error`**, which signals a simulator background-runner crash.
- **Fix:**
  - Add a **tick watchdog**: if `/v1/instance` says RUNNING but no `simulation.tick` has arrived for about 2 s (or 3 × the expected tick interval), force a reconnect and a full refetch.
  - Surface `simulator.notice` errors as critical alerts.
  - Keep SSE processing off the solver's event loop so slow decisions can't cause drops.

### A9 🟠 Demand history must be ingested locally or it is lost

- **Guide:** `/v1/demand-history` has `limit` capped at 2000, supports only a `station_id` filter, and has **no `since` or offset** (IG §4.11). It returns 12 rows per tick.
- **Plan:** the ML residual model, backtests, and twin calibration read demand history (PLAN §3, §6), but SQLite stores only audit, LLM cache, RL transitions, and the model registry (PLAN §1).
- **Impact:** one unfiltered call covers only about 166 ticks (about 21 s at speed 8). After a longer outage, the missing history is unrecoverable.
- **Fix:** ingest demand rows into SQLite every tick, deduplicated by row `id`. After a gap, backfill per station: `station_id=X&limit=2000` gives about 666 ticks per station.

### A10 🟠 The forecast ignores information the simulator gives directly

- **Guide:** `demand_spike` multiplies each station's `demand_multiplier`, which is readable in `/v1/stations` (IG §4.6, §7.8). Events appear in `/v1/events` with status **SCHEDULED**, a future `start_tick`, `end_tick`, and parameters such as the exact multiplier (IG §4.9).
- **Plan:** the forecast uses an EWMA estimate `m̂` that "tracks spikes within about 2 ticks" (PLAN §3). It uses active events only.
- **Impact:** the plan adds lag and noise to a value it could read exactly, and misses the chance to **pre-position stock before a scheduled spike, disruption, or outage**. That proactive behavior is the most impressive thing to show for PS §10.
- **Fix:**
  - Forecast with the live `demand_multiplier`, and apply scheduled events' parameters to future ticks.
  - Keep the EWMA and CUSUM as a cross-check that catches anything not explained by known events (which is the honest role for the anomaly detector).
  - Add "known upcoming events" to the MPC horizon and the PPO state.

### A11 🟠 The scenario has a finite supply schedule the plan doesn't account for

- **Guide:** every scenario shares one schedule of **22 arrivals**: 4 at ticks 12–20, then 18 spaced 64 ticks apart (IG §8.7). The schedule is over by roughly tick 1,100–1,200. Initial stock covers about **3.4–3.7 days** (about 330–350 ticks) of demand. (These figures assume daily demand ≈ profile × region factor, which gives about 95,000 L/day in total.)
- **Plan:** no mention of the scenario horizon or of what happens after the last arrival.
- **Impact:**
  - At speed 8, the entire supply schedule plays out in about **2.5 minutes** of wall-clock time. After that, depots only drain and service level falls whatever you do.
  - Load tests and demos that run the simulator will burn through the scenario.
  - PPO episodes and the 500-tick "done when" criterion (PLAN §14) need to sit inside the supply window.
  - If recurring top-ups are smaller than demand, rationing (A5 in the plan's toolbox) is the **normal** regime, not a crisis-only one.
- **Fix:**
  - On startup, compute the total supply vs forecast demand balance from `/v1/supply-arrivals` and show "days of cover" per depot and fuel.
  - Document the horizon, and reset (`/admin/reset`) between demo segments and load-test runs.
  - **VERIFY:** what happens when an arrival would exceed depot capacity.

### A12 🟠 The delay/failure classifier has nothing to learn

- **Guide:** transit times are fixed per route. A `shipment_delay` event changes `planned_tick` and sets status `DELAYED` **immediately and visibly** in `/v1/supply-arrivals`. An allocation fails only if its route is DISRUPTED at departure (IG §7.8, §4.12). Nothing in the simulator is random except demand noise.
- **Plan:** trains `HistGradientBoostingClassifier` models for P(supply delayed) and P(allocation fails) (PLAN §3).
- **Impact:** training a classifier on deterministic, directly observable facts, with only 22 arrivals and a handful of injected events as data, is the kind of thing judges will mark down under "appropriate methodology" (PS §23). It also costs build time.
- **Fix:** replace it with direct reads. The estimated supply arrival is the current `planned_tick`. The allocation failure risk comes from route status plus scheduled `route_disruption` events. You still satisfy PS §7's "estimated supply arrival" and "transport delay prediction" honestly, and you can say why ML isn't needed there.

### A13 🟠 `depot_constraint` doesn't actually constrain anything in the simulator

- **Guide:** a depot becomes `CONSTRAINED` but is "still shippable, but signals reduced capacity". `DEPOT_CLOSED` only fires for statuses outside {OPEN, CONSTRAINED}, so it can never fire with this world (IG §5.2, §7.8).
- **Problem statement:** for a depot constraint, judges expect to see "constraint handling, reallocation, service impact" (PS §10).
- **Plan:** has a `depot_constraint` regime label but no defined behavior (PLAN §4, §5).
- **Fix:** define the policy yourselves. For example, when a depot is CONSTRAINED, cap its effective dispatch at X% and reserve stock for its own region, so the reallocation is visible. Document that the cap is self-imposed. **VERIFY** whether `dispatch_capacity_per_tick` changes while constrained.

### A14 🟡 The destination capacity check ignores in-transit fuel

- **Guide:** `DESTINATION_CAPACITY_EXCEEDED` checks `station.inventory + quantity > capacity` at **creation**, without counting in-transit shipments (IG §5.2).
- **Plan:** the pre-submit checker mirrors the simulator's rules exactly (PLAN §2).
- **Impact:** several allocations to one station can each pass the check but together overflow on arrival. What happens then isn't documented: overflow might be discarded, clipped, or rejected. **VERIFY.**
- **Fix:** make our checker stricter than the simulator: headroom = capacity − inventory − in-transit + expected demand until arrival.

### A15 🟡 Cancel PENDING allocations before a known disruption hits

- **Guide:** a PENDING allocation fails if its route is DISRUPTED at departure. `allocation_failures` is a ground-truth metric in `/v1/metrics`. Cancel works only while PENDING and refunds the depot (IG §4.12, §5.5).
- **Plan:** cancel exists only as an operator action (PLAN §5).
- **Fix:** when a `route_disruption` becomes ACTIVE or is SCHEDULED for the next tick, automatically cancel PENDING allocations on that route and re-solve. It is a cheap, visible win for the regional disruption scenario and the judged failure count.

### A16 🟡 The bottleneck detector will never fire

- **Guide:** depot dispatch capacity totals **23,000 L/tick**. Average total demand is about **1,000 L/tick**, and the peak for a single station-fuel pair is about 230 L/tick (IG §8.2, §8.5).
- **Plan:** a bottleneck is dispatch utilization > 90% for 3 ticks (PLAN §3).
- **Fix:** redefine bottlenecks around the constraints that actually bind in this world:
  - depot days of cover per fuel;
  - station headroom;
  - the two long cross-region routes (4 ticks, 5,000 L max);
  - `max_shipment` on a single disrupted-region route.

### A17 🟡 Refreshes need per-resource freshness and an overall deadline

- **Guide:** `latency` delays every `/v1/*` call by `delay_ms`, and faults last up to 3600 s (IG §7.10).
- **Plan:** timeouts are 2 s connect and 5 s read with 4 attempts. The snapshot is all-or-nothing (PLAN §2).
- **Impact:** a large `delay_ms` can make one refresh take more than 20 s. With `error_rate` at 0.5, about 40% of refreshes will have at least one resource fail after retries, discarding otherwise-good data.
- **Fix:** keep a per-resource snapshot with a per-resource `age_ticks`. Set a total deadline per refresh cycle. Allow decisions on partial data when the critical resources (stations, depots, routes) are fresh.

### A18 🟡 Use the simulator's ground-truth KPIs

- **Guide:** `/v1/metrics` returns `service_level`, `unmet_demand_liters`, `allocation_failures`, and `allocation_liters`, and every team faces the same world so judges can compare fairly (IG §4.12, intro).
- **Plan:** never calls `/v1/metrics`. It computes its own `service_level` and `unmet_liters` and has no `allocation_failures` metric (PLAN §9). It also weights transport cost in objectives (PLAN §5, §6), which the simulator does not measure.
- **Fix:** export `/v1/metrics` values as Prometheus gauges and show them in the UI header. Keep transport cost as a tie-breaker only, never traded against service level.

### A19 ⚪ VERIFY demand-model assumptions against real data

- Hour-of-day factors must use the **simulated** time (`sim_time`, UTC +00:00), not wall-clock time.
- The guide does not say whether hour ranges are inclusive ("06–09"), whether per-tick base demand is `daily/96`, or what shape the noise has (IG §8.5–8.6).
- **Fix:** before building the twin, pause the simulator, step 96–192 ticks, and fit the formula to `/v1/demand-history`. That becomes calibration evidence for the docs.

### A20 ⚪ Deployment details

- The simulator uses host port **8000** (IG §1). Don't publish the backend on 8000 too.
- The guide's compose names the service `simulator-api`, and the plan uses `simulator`. Make `SIM_BASE_URL` configurable so judges can point the backend at their own simulator instance.

### A21 ⚪ Unused tools that would help

- `POST /admin/pause` + `/admin/step` is the guide's recommended way to run deterministic tests (IG §7.5). Use it in CI integration tests and the twin calibration test.
- `GET /admin/audit` has `event.started`, `event.resolved`, and `supply.arrived` actions (IG §7.12). It gives a ground-truth incident timeline for the UI and for LLM incident summaries.

### A22 ⚪ VERIFY inventory accounting before building the inventory-anomaly detector

- Cancel "refunds the depot inventory" (IG §5.5), which implies stock is deducted at **creation**, not departure.
- Whether FAILED allocations are refunded is not documented.
- The inventory-anomaly detector (PLAN §3) and the reconciler depend on both.

---

## Part B: Plan vs the Problem Statement

### B1 🔴 The operator UI is too thin for a 20% criterion

- **Problem statement:** Working Product & UX is **20%**, tied for the highest weight (PS §23). The UI should include a meaningful subset of the §6 list, and important recommendations must be inspectable (PS §9).
- **Plan:** a minimal page with five panels, with charts left to Grafana (PLAN §10).
- **Missing from the UI:**
  - depot status;
  - regional demand;
  - incoming supply;
  - a disruptions or events view;
  - expected impact of decisions;
  - a **decision detail view** showing signals, constraints, expected impact, confidence, and alternatives (PS §9).

  The plan computes all of these (tournament scores, `RiskItem.signals`, the audit record) but never shows them.
- **Fix:** add a decision detail drawer and three small panels (depots, incoming supply, events timeline). Add one or two inline charts (inventory vs forecast) so judges don't have to switch to Grafana.

### B2 🔴 No README in the repo layout

- **Problem statement:** the source repository must include setup instructions, dependencies, and deployment instructions (PS §19.2).
- **Plan:** the repo layout has no `README.md` (PLAN §13).
- **Fix:** add a README with prerequisites, `docker compose up`, ports and URLs, `.env` setup, how to run the load test and trainer profiles, and how to trigger each demo scenario.

### B3 🟠 Surprise engineering events: the system assumes a fixed world

- **Problem statement:** "Organizers may introduce surprise domain and engineering events" and "the environment may change" (PS glance table, §26).
- **Plan:**
  - Validation rejects unknown ids (PLAN §2).
  - PPO state and action sizes are fixed to 4 stations × 3 fuels (PLAN §6).
  - Region factors and topology are assumed (PLAN §0), and `/v1/regions` is never fetched.
- **Impact:** if a station, route, or field is added, every response is rejected as invalid and the system is stuck on its cache.
- **Fix:**
  - Treat unknown ids as a **topology change**, not invalid data: rebuild the model of the network.
  - Drop PPO from the tournament when the topology doesn't match its training shape (the LP solvers handle any size).
  - Read region factors from `/v1/regions`.
  - Use pydantic `extra="ignore"` so added fields don't break parsing.

### B4 🟠 Reproducibility: model files, internet, and hardware

- **Problem statement:** the system must be reproducibly runnable (PS glance table, §12, §19.7).
- **Plan gaps:**
  - It doesn't say where PPO weights live (git? Git LFS? a release asset?) or how Laya weights get into the ml-service image.
  - Groq, Gemini, and OpenRouter need internet access, and venue Wi-Fi can fail.
  - ml-service needs about 2 GB of RAM. With the simulator, Prometheus, and Grafana, the stack needs roughly 4 GB or more on a judge's laptop.
- **Fix:**
  - Ship a small PPO checkpoint in the repo or pull it at build time with a checksum.
  - The system must start and work with **no** models and no internet (rules + LP + templates). Say so in the README.
  - Document minimum RAM, and add a `lite` compose profile without ml-service.

### B5 🟠 High-weight evidence is scheduled last

- **Problem statement:** DevOps & Engineering Quality (15%) plus Observability & Performance (10%) together make up **25%** (PS §23).
- **Plan:** CI, the k6 load test, the Grafana dashboard, the architecture diagram, and the docs all land in phase 7 of 8 (PLAN §14).
- **Fix:** move a basic CI workflow (lint, tests, docker build) and one k6 run into phase 1–2, and grow them afterwards.

### B6 🟡 Security and hygiene details

- **Problem statement:** validate external input, handle failed requests, avoid exposing credentials, and restrict sensitive operator actions (PS §18).
- **Plan gaps:**
  - The UI is a static page, but approve, reject, and edit need `X-Admin-Token`. The plan doesn't say how the operator authenticates.
  - `/api/ask` is open to everyone and spends LLM quota: there is no rate limit, and no guard against prompt injection through operator text.
  - Operator edits (quantity, route, source) need server-side validation, not just the simulator's 409s.
  - `/api/health` must not echo key indexes or any part of an API key.
- **Fix:** add a simple operator login or token entry in the UI, a rate limit on `/api/ask`, and pydantic bounds on edit payloads.

### B7 🟡 Documentation the problem statement asks for

- **Problem statement:** generated or external data must be documented (PS §16). Important assumptions must be documented, and simulated results must be distinguishable from real-world conditions (PS §24).
- **Plan:** `docs/` has architecture, load test, and resilience docs only (PLAN §13). It covers neither the twin's generated training data nor the domain-randomization ranges nor its assumptions, and has no "simulated" marker in the UI.
- **Fix:**
  - Add `docs/data-and-assumptions.md`, covering demand-model assumptions, the twin, generated data, randomization ranges, and self-imposed policies such as the depot-constraint cap.
  - Put a persistent "SIMULATION: BUP Fuel Supply Simulator" banner in the UI.

### B8 🟡 Observability evidence gaps

- **Problem statement:** requires CPU and memory at the system layer and logs of important actions (PS §14), and load-test reports must include resource usage (PS §17).
- **Plan:** uses the default process collector for the backend only, and writes JSON logs to stdout with no aggregation (PLAN §9).
- **Fix:**
  - Add **cAdvisor** for per-container CPU and memory.
  - Add **Loki + Promtail** (or at minimum a UI log tail) so judges can see logs next to metrics.
  - Take Grafana screenshots during each resilience drill for the docs.

### B9 🟡 Recommended deliverables partly missing

- **Problem statement:** recommends simulation replay, scenario configuration, deployment versioning, and rollback (PS §20).
- **Plan status:**
  - There is no replay feature, although the tournament log and audit could support one.
  - Scenario configuration exists only as the raw chaos API, with no named presets.
  - Rollback covers models only, not deployments.
  - Experiment tracking is only `metrics.json` per model.
- **Fix:**
  - Add named scenario presets such as "combined crisis" (spike + disruption + delay).
  - Add a replay view that steps through audit records.
  - Tag images with a version and document `docker compose` rollback to the previous tag.

### B10 🟡 The demo script needs real numbers and an honest detection story

- The plan's demo says "twin shows 72%→19%" (PLAN §14), which is the example from PS §9. Show your own measured numbers.
- "CUSUM detects before the event is read" is less impressive once you know `demand_multiplier` is directly visible (see A10). Reframe the demo around **acting early** on scheduled events and on multiplier changes.

### B11 ⚪ Complexity vs "appropriate methodology"

- **Problem statement:** complexity alone won't raise the score, and implementations must meaningfully contribute (PS §21, §25).
- **Plan:** stacks a Thompson-sampling bandit, a tournament, PPO, Laya, Jev, a residual GBM, and a delay classifier.
- **Fix:** for each component, name the evidence you will show (a table, a chart, a comparison). Cut anything that has no evidence by phase 5. A12 is the first candidate to cut.

### B12 ⚪ Optional items from the handwritten notes that aren't planned

Kubernetes, autoscaling, and multi-agent systems were circled on the printout but aren't in the plan. They're optional (PS §13, §21), and skipping them is fine.

---

## Part C: Second pass, requirement-by-requirement coverage

Every requirement in both documents, checked against the plan. ✅ covered · ⚠️ partly covered or has a bug (see gap id) · ❌ missing.

### C1. Problem statement

| Ref | Requirement | Status | Gap |
|---|---|---|---|
| Glance | Operator app, backend, intelligence, deployment, observability, resilience, load testing | ⚠️ | B1, B8 |
| Glance | Reproducibly runnable, preferably containerized | ⚠️ | B4 |
| Glance | Surprise domain and engineering events | ⚠️ | B3 |
| §1 | Observe the network | ⚠️ | A8, A9 |
| §1 | Identify emerging shortages and risks | ✅ | |
| §1 | Help decide how constrained fuel is allocated | ✅ | |
| §1 | Respond to unexpected disruptions | ⚠️ | A5, A10 |
| §1 | Expose reasoning behind recommendations | ⚠️ | B1 |
| §1 | Stay observable and usable during failures | ⚠️ | A2, A3, D1 |
| §1 | Measurable performance under load | ✅ | |
| §2 | Observe → Detect → Predict → Decide → Simulate → Act → Monitor | ✅ | |
| §2 | **Recover** | ⚠️ | D2, D3 |
| §6 | Current fuel inventory | ✅ | |
| §6 | Depot and station status | ⚠️ | B1 |
| §6 | Regional fuel demand | ❌ | B1, D6 |
| §6 | Shortage alerts / projected shortage risk | ✅ | |
| §6 | Incoming supply | ❌ | B1 |
| §6 | Disruptions | ⚠️ | B1 |
| §6 | Recommended allocations | ✅ | |
| §6 | Expected impact of decisions | ⚠️ | B1 (computed, not shown) |
| §6 | System alerts / decision history / service health | ✅ | A2 |
| §7 | Demand forecasting / shortage prediction / stockout probability | ✅ | A10 |
| §7 | Estimated supply arrival / transport delay prediction | ⚠️ | A12 |
| §7 | Anomalous demand | ✅ | |
| §7 | Abnormal inventory changes | ⚠️ | A22 |
| §7 | Supply-chain bottlenecks | ⚠️ | A16 |
| §7 | Emerging **regional** disruptions | ⚠️ | D6 |
| §7 | Decision intelligence (all six bullets) | ✅ | B11 |
| §7 | GenAI: incident explanation / investigation assistance | ✅ | D10 |
| §7 | GenAI: supply-chain state summarization | ⚠️ | D10 |
| §7 | GenAI: human-readable decision explanations | ⚠️ | D10 |
| §7 | LLMs support operations, not just a chatbot | ✅ | |
| §8 | RL compared with a rule-based baseline | ⚠️ | D5 (twin-only evidence) |
| §9 | Why the area is at risk / which signals | ⚠️ | B1 |
| §9 | Relevant **constraints** | ❌ | D8 |
| §9 | Expected impact / confidence / alternatives | ⚠️ | B1 |
| §9 | Human operators can inspect decisions | ⚠️ | A5, B1 |
| §10 | Shipment delay | ✅ | A12 |
| §10 | Demand spike | ✅ | A10 |
| §10 | Depot constraint | ⚠️ | A13 |
| §10 | Regional disruption | ✅ | A15 |
| §10 | Combined crisis, including **failure boundaries** | ⚠️ | A5, D9 |
| §10 | Detect, evaluate, respond, explain, **monitor recovery** | ⚠️ | D3 |
| §11 | ML model unavailable → fallback | ✅ | |
| §11 | Invalid simulator response → reject + alert | ⚠️ | A1, B3 |
| §11 | **Prediction** confidence too low → human review | ⚠️ | D7 |
| §11 | Backend dependency unavailable → retry / cache / degraded | ⚠️ | A2, A3, A17, D1 |
| §11 | Retries, timeouts, health checks, cached state, validation, circuit breakers, rollback | ✅ | A2, A3 |
| §12 | `docker compose up` or equivalent | ✅ | A6, A20 |
| §12 | Build → Test → Package → Deploy → Health check workflow | ✅ | B5 |
| §14 | Application layer metrics | ✅ | |
| §14 | System layer: CPU, memory | ⚠️ | B8 |
| §14 | Intelligence layer metrics | ✅ | |
| §14 | Logs | ⚠️ | B8 |
| §15 | Component health visible to judges | ⚠️ | A2 |
| §16 | Generated / external data documented | ❌ | B7 |
| §17 | avg / p50 / p95 / p99 / throughput / error rate / concurrency | ✅ | D4 |
| §17 | Resource usage | ⚠️ | B8 |
| §18 | No hard-coded secrets / credentials not exposed | ✅ | B6 |
| §18 | Validate external input | ⚠️ | B6 |
| §18 | Handle failed requests | ✅ | A7 |
| §18 | Document required configuration | ✅ | |
| §18 | Restrict sensitive operator actions | ⚠️ | B6 |
| §19.1, .3, .4, .7–.11 | Working app, sim integration, intelligence, deployment, observability, resilience, load test, demo | ✅ | |
| §19.2 | Repo with setup, dependency, deployment instructions | ❌ | B2 |
| §19.5 | Operator interface | ⚠️ | B1 |
| §19.6 | Architecture diagram | ✅ | (render as an image, not only ASCII) |
| §20 | CI/CD, automated tests, model versioning, audit history, deployment versioning, automated fallback | ✅ | |
| §20 | Experiment tracking | ⚠️ | B9 |
| §20 | Simulation replay | ❌ | B9 |
| §20 | Scenario configuration | ⚠️ | B9 |
| §20 | Rollback | ⚠️ | B9 (models only) |
| §22 | 14-step demo story | ✅ | A4, A5, B10 |
| §24 | Simulation only; no real infrastructure, purchases, or credentials | ✅ | |
| §24 | Distinguish simulated from real-world results | ❌ | B7 |
| §24 | Document important assumptions | ❌ | B7 |
| §24 | Human review for consequential decisions | ⚠️ | A5 |

### C2. Simulator integration guide

| Ref | Requirement or behavior | Status | Gap |
|---|---|---|---|
| §1 | `SIMULATION_SPEED` default 8, `SIMULATOR_START_MODE` default paused | ❌ | A4, A6 |
| §2 | REST is truth, SSE is a hint | ✅ | |
| §2 | Deterministic world (seed) | ⚠️ | D5, D12 |
| §2 | Scenario baked in; other scenarios may preload events | ⚠️ | A10 |
| §2 | `/v1/health` and `/admin/*` bypass faults | ❌ | A2 |
| §4.2 | `/v1/instance` status / seed | ⚠️ | A6, D12 |
| §4.4 | `/v1/regions` | ❌ | B3 |
| §4.5–4.7 | Depots, stations, routes | ✅ | A13 |
| §4.8 | Supply arrivals, including `DELAYED` | ⚠️ | A11, A12 |
| §4.9 | Events, including `SCHEDULED` | ⚠️ | A10 |
| §4.10 | Allocations ledger | ⚠️ | D11 |
| §4.11 | Demand history limit 2000, no offset | ❌ | A9 |
| §4.12 | `/v1/metrics` ground truth | ❌ | A18 |
| §5.1–5.2 | Payload constraints and validation order | ⚠️ | A14 |
| §5.2 | Departure happens the tick after creation | ❌ | D13 |
| §5.4 | Idempotency: same key + same body is a safe replay; keys never freed | ✅ | A7 (200 vs 201) |
| §5.5 | Cancel only while PENDING, refunds depot | ⚠️ | A15, A22 |
| §6.1 | Queue of 200, silent drop | ❌ | A8 |
| §6.2 | No replay, refetch after reconnect | ✅ | |
| §6.3 | Only 4 SSE event types; `simulator.notice` | ⚠️ | A1, A8 |
| §6.4 | `stream_disconnect` → 503; stale header on GETs only | ✅ | A5 |
| §7.5 | Pause + step for deterministic tests | ❌ | A21, D5 |
| §7.6 | Reset wipes everything | ❌ | A1 |
| §7.8 | All six event types | ⚠️ | A13 |
| §7.10 | `latency` fault | ⚠️ | A17 |
| §7.10 | `unavailable` fault | ⚠️ | A2 |
| §7.10 | `error_rate` fault | ⚠️ | A3 |
| §7.10 | `stale_data` fault | ⚠️ | A5 |
| §7.10 | `stream_disconnect` fault | ✅ | |
| §7.12 | `/admin/audit` | ❌ | A21 |
| §8 | Fixed world, finite supply schedule | ❌ | A11, A16 |
| §9 | Error envelopes and all status codes | ⚠️ | A7 |
| §10 | Defensive client checklist | ⚠️ | A2, A3, A7, A8, A9 |

---

## Part D: New gaps found in the second pass

These did not show up in Parts A and B. They surfaced only when checking each requirement line by line.

### D1 🟠 Backend restart loses its in-memory state

- **Problem statement:** the demo includes "Application or dependency failure is injected" (PS §22). Killing the backend container itself is an obvious choice for judges.
- **Plan:** no compose `restart:` policy, and no description of what happens on backend restart. Breaker state, EWMA/CUSUM state, pending plans, and the SSE connection live in memory.
- **Fix:**
  - Set `restart: unless-stopped` on every service.
  - On startup, reload the ledger and pending plans from SQLite, reconcile against `/v1/allocations`, and warm-start the forecaster from locally stored demand history (A9).
  - Report "recovered after restart" as a logged event and a metric.

### D2 🟠 Decisions queued during degraded mode go stale

- **Plan:** in degraded mode "decisions are queued" (PLAN §2), but it doesn't say what happens to the queue on recovery.
- **Impact:** at speed 8, a 30-second outage is 240 ticks. Executing the queue afterwards would submit allocations based on data that is days old in simulated time.
- **Fix:** on recovery, **drop** the queue, refetch, and re-solve. Log how many queued decisions were discarded. This shares the TTL mechanism from A5.

### D3 🟡 "Recover" is not defined or measured

- **Problem statement:** the loop ends in **Recover**, and teams must show how the system "monitors recovery" (PS §2, §10).
- **Plan:** defines fallbacks, but not how it returns to normal (for example, when ml-service comes back, does the router switch back to Laya automatically?) or how recovery is measured.
- **Fix:** for each failure type, define the exit condition and the automatic return path. Add `incident_duration_seconds` and time-to-recover metrics, plus a "recovered" log event, so the resilience demo ends with a number instead of a vague "it's fine now".

### D4 🟡 Load tests can disturb the live control loop

- **Plan:** load-tests `POST /api/decisions/recommend` at 50 VUs with the full pipeline (PLAN §11). It is unclear whether each request refetches from the simulator or writes an audit row.
- **Impact:** if it does, the load test becomes a load test of the simulator. It can also trip the breaker and degrade the live loop, or cause SQLite write contention, and the numbers won't mean what the report says.
- **Fix:** have `recommend` read the cached snapshot only and write nothing (or write to a separate table). State in `docs/loadtest.md` what each request does. Run one test with the live loop active and one without, and compare.

### D5 🟡 RL evidence is twin-only when a fair comparison on the real simulator is possible

- **Guide:** same seed + same actions + same events ⇒ byte-identical state, and pause + step drives deterministic runs (IG §2, §7.5).
- **Plan:** the evaluation suite runs only on the digital twin (PLAN §6).
- **Impact:** judges may doubt results measured on your own re-implementation of the world.
- **Fix:** also run greedy vs LP vs MPC vs PPO on the **real simulator**: reset, inject the same events, and step N ticks per policy. Report `/v1/metrics` for each run. This is strong, cheap evidence for PS §8 and for the intelligence criterion.

### D6 🟡 No region-level view

- **Problem statement:** mentions regional shortages, regional fuel demand, and emerging regional disruptions (PS §1, §6, §7).
- **Plan:** works per station and per route. Regions appear only as a demand factor.
- **Fix:** add region rollups: demand, days of cover, and risk per region and fuel. Add a regional risk alert, for example when Chattogram octane cover drops below N hours across both stations.

### D7 🟡 The human-review gate ignores forecast uncertainty

- **Problem statement:** "Prediction confidence too low → Human review requested" (PS §11).
- **Plan:** gates on the **router's** confidence and `needs_human` (PLAN §5). Forecast uncertainty (P10–P90 width, recent MAPE) only feeds the robust LP.
- **Fix:** add a gate condition such as "forecast band wider than X% or MAPE above threshold → human review", and show it as a gate reason.

### D8 🟡 Explanations don't include binding constraints

- **Problem statement:** recommendations should show the relevant constraints (PS §9).
- **Plan:** the audit stores candidates and scores, but not which constraint limited the decision (PLAN §5).
- **Fix:** record the binding constraints from the LP solution, such as "limited by depot-patiya OCTANE stock" or "route max 5,000 L". Show them in the decision detail view and pass them to the explanation template.

### D9 🟡 Failure boundaries are not documented

- **Problem statement:** for the combined crisis, show "end-to-end resilience and failure boundaries" (PS §10).
- **Plan:** documents load-test limits, but not the domain or system limits.
- **Fix:** add a "What we can't handle" section to `docs/resilience.md`. Examples: total supply exhausted after the schedule ends (A11), both depots constrained while a cross-region route is disrupted, the simulator unavailable for longer than station cover lasts. For each, state what the system does (alerts, rationing, holds) and demonstrate one live.

### D10 ⚪ GenAI coverage details

- **State summarization:** the plan summarizes incidents only when events start or resolve. A periodic "shift summary" (for example, every simulated 8 hours or on demand) covers the PS §7 bullet directly.
- **Decision explanations:** the LLM explains only human-queue decisions. Make sure every decision shows at least the template explanation.
- **Investigation assistance:** `/api/ask` sends one prompt over the state. Native function calling over read-only tools (state, risks, forecast, decision by id, events) is a stronger answer to "operator investigation assistance".
- **Key pool:** rotating several free-tier keys per provider to get around rate limits may break Groq's or Gemini's terms of service. With aggressive caching and templates, one key per provider should be enough. Check the terms.

### D11 ⚪ `/v1/allocations` grows without bound

- **Guide:** returns every allocation ever created, and no `limit` parameter is documented (IG §4.10).
- **Plan:** the reconciler fetches it every tick (PLAN §2).
- **Fix:** reconcile mainly from `allocation.status_changed` SSE events. Do a full `/v1/allocations` sweep only every N ticks and after reconnects.

### D12 ⚪ Scenario identity isn't read or shown

- **Guide:** `/v1/instance` returns `scenario_id`, `scenario_version`, and `seed`. Other scenarios differ in seed and preloaded events (IG §4.2, §8).
- **Fix:** show the scenario and seed in the UI header, and record them in every audit row and load-test report. Initialize the twin from the live seed.

### D13 🟡 Timing is off by one tick

- **Guide:** an allocation created at tick 5 departs at tick 6 and arrives at tick 8 on a 2-tick route (IG §4.10, §5.3). Lead time is **transit_ticks + 1**.
- **Plan:** LP cost, MPC lags, and time-to-stockout use `transit_ticks` only (PLAN §3, §4).
- **Fix:** use `transit_ticks + 1` everywhere. The twin calibration test would eventually catch this, but it's cheaper to build it in.

### D14 ⚪ Prometheus alerts have nowhere to go

- **Plan:** defines Prometheus alert rules (PLAN §9) but has no Alertmanager, and the operator UI shows only backend-generated alerts.
- **Fix:** either add Alertmanager and a webhook back into the backend so infrastructure alerts appear in the UI, or state clearly that infrastructure alerts live in Grafana and application alerts in the UI.

---

## Part E: What to fix first

1. **Runtime correctness:** A1 (reset), A2 (health probe), A3 (breaker), A4 + A5 (speed and review TTL), D1 (restart), D2 (stale queue). Any one of these can break the live demo.
2. **Cheap high-value intelligence fixes:** A10 (read multiplier and scheduled events), A15 (auto-cancel before disruption), D13 (timing), A12 (drop the classifier).
3. **Score protection:** B1 (UI and decision detail), B2 (README), B5 (CI and k6 early), B7 (docs and banner).
4. **Evidence:** D5 (real-simulator benchmark), D3 (recovery metrics), B8 (cAdvisor, Loki, screenshots).
5. **VERIFY list, test in the first hours with pause + step:** A11 (depot overflow on arrival), A13 (`dispatch_capacity_per_tick` while constrained), A14 (station overflow on arrival), A19 (demand formula), A22 (inventory deduction timing and FAILED refunds).
