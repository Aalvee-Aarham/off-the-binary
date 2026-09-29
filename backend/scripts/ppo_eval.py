"""Evaluate a PPO version against baselines on a fixed suite, write metrics.json, optionally promote.

    python -m scripts.ppo_eval v1 [--promote] [--ticks 300] [--seeds 3]
Promotion gate: mean unmet (PPO) must beat the LP baseline it builds on AND the currently active model."""
import argparse
import json
import statistics

from app.policy import MODELS, PPOPolicy, active_version, set_active
from scripts.bench import run


def spike(tw):
    tw.add_event("demand_spike", 40, 120, {"region_ids": ["region-dhaka"], "multiplier": 2.0})


def disruption(tw):
    """Tongi and Cox's Bazar each have exactly ONE route. Scheduled in advance (visible in /v1/events), so a policy
    with foresight pre-positions stock; a reactive one runs them dry."""
    tw.add_event("route_disruption", 60, 60, {"route_ids": ["route-gazipur-tongi", "route-patiya-coxsbazar"]})


def shortfall(tw):
    tw.add_event("shipment_delay", 5, 1, {"delay_ticks": 90})
    tw.add_event("supply_shortfall", 6, 1, {"factor": 0.25})


def combined(tw):
    tw.add_event("demand_spike", 30, 100, {"multiplier": 1.7})
    tw.add_event("route_disruption", 50, 50, {"route_ids": ["route-gazipur-tongi"]})
    tw.add_event("station_outage", 80, 16, {"station_ids": ["station-mirpur"]})
    tw.add_event("supply_shortfall", 10, 1, {"factor": 0.5})


SUITE = {"normal": None, "demand_spike": spike, "single_route_disruption": disruption, "supply_shortfall": shortfall,
         "combined": combined}


def evaluate(policies, ticks, seeds):
    out = {}
    for name, (pol, model) in policies.items():
        per = {}
        for sc_name, sc in SUITE.items():
            rs = [run(pol, ticks, sc, seed=100 + s, ppo=model) for s in range(seeds)]
            per[sc_name] = {"unmet_l": round(statistics.mean(r["unmet_l"] for r in rs)),
                            "service_level": round(statistics.mean(r["service_level"] for r in rs), 5),
                            "failures": sum(r["failures"] for r in rs)}
        per["mean_unmet_l"] = round(statistics.mean(v["unmet_l"] for v in per.values()))
        out[name] = per
        print(name, json.dumps(per))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--ticks", type=int, default=300)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--promote", action="store_true")
    a = ap.parse_args()
    model = PPOPolicy(MODELS / a.version / "policy.npz")
    pols = {"ppo": ("ppo", model), "lp": ("lp", None), "mpc": ("mpc", None), "greedy": ("greedy", None),
            "tournament_with_ppo": ("tournament", model)}
    cur = active_version()
    if cur and cur != a.version:
        pols["active_" + cur] = ("ppo", PPOPolicy(MODELS / cur / "policy.npz"))
    res = evaluate(pols, a.ticks, a.seeds)
    gate = res["ppo"]["mean_unmet_l"] <= res["lp"]["mean_unmet_l"] and all(
        res["ppo"]["mean_unmet_l"] <= v["mean_unmet_l"] for k, v in res.items() if k.startswith("active_"))
    metrics = {"version": a.version, "ticks": a.ticks, "seeds": a.seeds, "results": res, "passes_gate": gate}
    (MODELS / a.version / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print("gate (beats LP and active):", gate)
    if a.promote and gate:
        set_active(a.version)
        print("promoted", a.version)
    elif a.promote:
        print("NOT promoted: failed the gate")


if __name__ == "__main__":
    main()
