# Reinforcement learning: results

All numbers are **simulated**, on the digital twin. The twin is calibrated against the organizer image: 120/120 ticks matched exactly in CI's one-step check, and it starts from the real scenario's initial world and supply schedule.

## What is learned

| Learner | Kind | What it controls | Where |
|---|---|---|---|
| **PPO** | Deep RL (actor-critic, SB3) | 12 priority weights + 12 safety factors (per station × fuel). The LP turns them into a feasible plan. | [ml/rl/](../ml/rl/), numpy inference in [policy.py](../backend/app/policy.py) |
| **Bandit** | Online RL (Beta-Bernoulli Thompson sampling) | Which algorithms to run when the cycle is over budget, learned per regime from tournament wins | [bandit.py](../backend/app/bandit.py) |

**PPO setup:**

| Component | Setting |
|---|---|
| State (110 features) | Per station×fuel: stock/capacity, 4h demand, incoming, P(stockout), time to stockout, multiplier, open. Per depot×fuel: stock, cover vs next supply, time to next supply. Plus route availability and hour of day. |
| Reward (per 2-tick decision) | −unmet/100, minus a small transport cost, −2 per failed allocation (the simulator doesn't refund those), and an overflow penalty |
| Training | 300k steps, 4 parallel envs, about 25 min CPU. Episodes of 300 ticks on the real scenario. |
| Domain randomization | Supply ×0.6–1.3, plus 0–3 random crises per episode drawn from all six event types |

**Why this action design:** PPO sets the LP's objective instead of emitting raw shipments. So it **cannot** produce an invalid allocation, and it trains in minutes instead of days.

## Evaluation suite

Five fixed scenarios, 3 seeds each, 300 ticks on the real scenario. Numbers are mean unmet liters; lower is better. Source: `backend/models/ppo/v2/metrics.json`, produced by `python -m scripts.ppo_eval v2`.

| Scenario | What it tests |
|---|---|
| normal | baseline scenario |
| demand_spike | Dhaka ×2.0 for 120 ticks |
| single_route_disruption | Tongi's and Cox's Bazar's *only* routes, scheduled in advance |
| supply_shortfall | Every remaining arrival delayed 90 ticks and cut to 25% |
| combined | Global spike ×1.7, Tongi route down, Mirpur outage, supply ×0.5 |

| Policy | normal | spike | single-route | shortfall | combined | **mean** |
|---|---|---|---|---|---|---|
| greedy (heuristic) | 580 | 262 | 7,196 | 580 | 11,924 | 4,108 |
| LP (single-tick optimizer) | 580 | 262 | 7,196 | 580 | 11,591 | 4,042 |
| PPO v1 (trained on a placeholder supply schedule) | 351 | 155 | 6,099 | 351 | 10,250 | 3,441 |
| **PPO v2** (trained on the real scenario) | 566 | 248 | 5,916 | 400 | **9,558** | 3,338 |
| MPC (multi-tick LP) | 284 | **65** | 5,400 | 284 | 9,983 | 3,203 |
| **Tournament incl. PPO** (what production runs) | 284 | 128 | **5,366** | 284 | 9,897 | **3,192** |

Promotion gate: v2 beat LP and v1, so it was promoted (`models/ppo/ACTIVE = v2`). Rollback is `POST /api/models/ppo/rollback`.

## Why RL is useful here (the problem statement asks us to show this against heuristics)

- **PPO vs the heuristic and optimizer baselines:** −17% mean unmet vs LP and −19% vs greedy. In the combined crisis it saves 2,000–2,400 L.
- **PPO is the best single policy in the combined crisis,** beating even MPC by 4%. Randomized training exposed it to many overlapping crises, and it learned to hold more safety stock where several risks interact. A fixed single-tick objective can't express that.
- **MPC still wins on scheduled single-route disruptions and plain spikes,** because its explicit multi-tick horizon sees a known event coming.
- **The production tournament runs PPO next to MPC and the rest,** so the system is never worse than its best candidate at any decision.

## Honest limits

- **The tournament never picks PPO in these runs.** It scores plans on the twin over a 16+16-tick window using forecast (P50/P90/spike) demand, and in that window MPC's plan always scores at least as well. PPO's crisis advantage appears over whole episodes. Longer windows (24, 32 ticks) and making PPO the router's tie-break pick changed nothing.
  - **Next step:** score candidates on sampled stochastic demand over a longer horizon, or let the bandit learn from *realized* service rather than twin scores.
- **Training reward is noisy** because crisis severity varies between randomized episodes. Model selection therefore relies on the fixed evaluation suite, not the training curve.
- **The supply-shortfall scenario barely bites within 300 ticks,** since depots start with about 3 days of stock. Longer episodes past the end of the supply schedule (tick 212) are where rationing matters.
- **Evaluated on the twin only, not the real simulator.** The twin is one-step exact against the real image, and noise and seeds differ, so this is a small gap. A real-simulator A/B would need exclusive use of the simulator for about 15 minutes per policy.

## Reproduce

```bash
.venv-ml/Scripts/python ml/rl/train.py --steps 300000 --envs 4      # -> backend/models/ppo/vN
cd backend && python -m scripts.ppo_eval vN --promote                # gate: beat LP and the active model
python -m scripts.bench 400                                          # closed-loop comparison incl. ppo
```
