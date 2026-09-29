"""Closed-loop policy benchmark on the digital twin: every algorithm runs the world for N ticks.
Usage: python -m scripts.bench [ticks]"""
import sys
import time

from app import config
from app.forecast import Forecaster, arrivals, risks
from app.policy import features, load_active
from app.solvers import ALGOS, algo_ppo, build_problem, check, finalize, tournament
from app.world import Twin, baseline_world

H = config.HORIZON_TICKS


def crisis(tw):
    tw.add_event("shipment_delay", 50, 1, {"delay_ticks": 20})
    tw.add_event("demand_spike", 60, 40, {"region_ids": ["region-dhaka"], "multiplier": 1.8})
    tw.add_event("route_disruption", 80, 30, {"route_ids": ["route-gazipur-mirpur"]})
    tw.add_event("supply_shortfall", 120, 1, {"factor": 0.5})


def run(policy, ticks=400, scenario=None, seed=1, every=2, ppo=None):
    """policy: none | an ALGOS name | ppo | tournament. `ppo` = a PPOPolicy (tournament includes it when given)."""
    w = baseline_world()
    w["seed"] = seed
    tw = Twin(w)
    if scenario:
        scenario(tw)
    fc, seen, ms = Forecaster(), 0, []
    for t in range(ticks):
        tw.step()
        if policy == "none" or t % every:
            continue
        s = tw.snapshot()
        fc.ingest(tw.demand_log[seen:], s)
        seen = len(tw.demand_log)
        p, a = fc.paths(s, H), arrivals(s, H)
        r = risks(s, fc, p, a, H)
        t0 = time.perf_counter()
        if policy == "tournament":
            ships = tournament(s, fc, p, a, r, "mpc", policy=ppo)["shipments"]
        elif policy == "ppo":
            ships, _ = check(finalize(algo_ppo(build_problem(s, fc, p, a, r), ppo.act(features(s, r, p))), s), s)
        else:
            ships, _ = check(finalize(ALGOS[policy](build_problem(s, fc, p, a, r)), s), s)
        ms.append((time.perf_counter() - t0) * 1000)
        for sh in ships:
            tw.submit(sh["source_depot_id"], sh["destination_station_id"], sh["route_id"], sh["fuel_type"], sh["quantity"])
    m = tw.metrics()
    return {"service_level": m["service_level"], "unmet_l": round(m["unmet_demand_liters"]),
            "failures": m["allocation_failures"], "avg_ms": round(sum(ms) / max(1, len(ms)), 1)}


if __name__ == "__main__":
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    model, version = load_active()
    for name, sc in (("normal", None), ("combined crisis", crisis)):
        print(f"== {name} ({ticks} ticks)")
        pols = ["none", *[a for a in ALGOS if a != "hold"]] + (["ppo"] if model else []) + ["tournament"]
        for pol in pols:
            print(f"  {pol:11s} {run(pol, ticks, sc, ppo=model)}")
    print("ppo model:", version or "none promoted")
