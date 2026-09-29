# Demo run (2026-09-29 13:09, simulated)

| Step | Tick | Service level | What happened |
|---|---|---|---|
| 1 Normal operations | 21 | 100.00% | 4 stored decisions (calm no-action cycles are not stored); latest winner `mpc`; health `healthy` |
| 2 Demand spike x2.0 (Dhaka) | 27 | 100.00% | detected: station-mirpur DIESEL: p(stockout)=77%; station-mirpur PETROL: p(stockout)=92%; station-mirpur OCTANE: p(stockout)=91% |
| 3 Decision + expected impact | 27 | 100.00% | `robust_lp` beat 5 candidates on the twin; Tick 24, regime demand_spike. The twin tournament chose robust_lp (rules suggested robust_lp). At risk: station-tongi DIESEL: p(stockout)=100%, stockout in 5.0h, inventory 9,236 L; station-mirpur PETROL: p(stockout)=96%, stockout in 8.25h, inventory 6,959 L; station-mirpur OCTANE: p(stockout)=95%, stockout in 8.25h, inventory 3,930 L; station-tongi PETROL: p(stockout)=93%, stockout in 9.0h, invent |
| 4 Operator review (MANUAL) | 29 | 100.00% | plan `8dff5b2c6994` inspected (llm:groq) and approved: 4 shipments |
| 5 Combined crisis | 39 | 94.98% | regime `combined`; alerts: station-coxsbazar is OUTAGE |
| 6 Simulator outage injected | 39 | 94.98% | health `degraded`, simulator `degraded`, breaker `open`, degraded mode True |
| 7 Recovery | 112 | 95.11% | health `healthy`, breaker `closed`, degraded False |
| 8 Router model | 112 | 95.11% | laya not configured in this deployment: rule router active (fallback path) |
| 9 Operations continue | 132 | 95.72% | final service level 95.72%, unmet 7,011 L, allocation failures 0; last recovery {'from_tick': 92, 'to_tick': 95, 'ticks': 3} |
