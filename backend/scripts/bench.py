"""Closed-loop policy benchmark on the digital twin: every algorithm runs the world for N ticks.
Usage: python -m scripts.bench [ticks]"""
import sys
import time

from app import config
from app.forecast import Forecaster, arrivals, risks
from app.solvers import ALGOS, build_problem, check, finalize, tournament
from app.world import Twin, baseline_world

H = config.HORIZON_TICKS


def crisis(tw):
    tw.add_event("shipment_delay", 50, 1, {"delay_ticks": 20})
    tw.add_event("demand_spike", 60, 40, {"region_ids": ["region-dhaka"], "multiplier": 1.8})
    tw.add_event("route_disruption", 80, 30, {"route_ids": ["route-gazipur-mirpur"]})
    tw.add_event("supply_shortfall", 120, 1, {"factor": 0.5})


def run(policy, ticks=400, scenario=None, seed=1, every=2):
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
            ships = tournament(s, fc, p, a, r, "lp")["shipments"]
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
    for name, sc in (("normal", None), ("combined crisis", crisis)):
        print(f"== {name} ({ticks} ticks)")
        for pol in ("none", *[a for a in ALGOS if a != "hold"], "tournament"):
            print(f"  {pol:11s} {run(pol, ticks, sc)}")
