"""Calibrate the digital twin against a running simulator (real image or devsim).

    python -m scripts.calibrate --base http://localhost:8000 --ticks 120 --out ../docs/calibration.json

1. Probes: one hypothesis each (stock deducted at create? dispatch cap semantics? CONSTRAINED cuts dispatch?
   disrupted route -> FAILED + refund? departure/arrival timing? demand-history order?).
2. Lockstep drift run: sim and twin step together under a scripted crisis + our LP policy. The twin is fed the
   sim's observed demand, so any divergence is dynamics (tick order, capacity, events), not noise.
3. Demand formula check: observed / documented rate, per profile x hour.
RESETS THE SIMULATOR. Never point it at a judged run.
"""
import argparse
import json
import statistics
from collections import defaultdict

import httpx

from app.forecast import Forecaster, arrivals, risks
from app.solvers import ALGOS, build_problem, check, finalize
from app.state import validate
from app.world import FUELS, Twin, base_rate, parse_time

H = 40


class Sim:
    def __init__(self, base):
        self.c = httpx.Client(base_url=base, timeout=10)

    def get(self, p, **q):
        r = self.c.get(p, params=q or None)
        r.raise_for_status()
        return r.json()

    def post(self, p, body=None):
        r = self.c.post(p, json=body)
        return r.status_code, (r.json() if r.content else None)

    def snap(self):
        inst = self.get("/v1/instance")
        s = {"tick": inst["tick"], "sim_time": inst["sim_time"], "tick_minutes": inst["tick_minutes"],
             "status": inst["status"], "seed": inst.get("seed"),
             "regions": {r["id"]: r for r in self.get("/v1/regions")},
             "depots": {d["id"]: d for d in self.get("/v1/depots")},
             "stations": {x["id"]: x for x in self.get("/v1/stations")},
             "routes": {r["id"]: r for r in self.get("/v1/routes")},
             "supply": self.get("/v1/supply-arrivals"), "events": self.get("/v1/events"),
             "allocations": self.get("/v1/allocations"), "metrics": self.get("/v1/metrics"), "stale": False}
        assert not validate(s), validate(s)
        return s

    def reset(self):
        self.post("/admin/reset")
        self.post("/admin/pause")
        self.post("/admin/faults/clear")

    def alloc(self, key, depot, station, route, fuel, qty):
        return self.post("/v1/allocations", {"idempotency_key": key, "source_depot_id": depot,
                                             "destination_station_id": station, "route_id": route,
                                             "fuel_type": fuel, "quantity": qty})


def code_of(status, body):
    if status in (200, 201):
        return None
    d = (body or {}).get("detail") or (body or {}).get("error") or {}
    return d.get("code") if isinstance(d, dict) else f"HTTP_{status}"


def probes(sim):
    out = {}
    R = ("depot-gazipur", "station-mirpur", "route-gazipur-mirpur")

    sim.reset()
    inv0 = sim.get("/v1/depots/depot-gazipur")["inventory"]["DIESEL"]
    st, a = sim.alloc("cal-p1", *R, "DIESEL", 1000)
    inv1 = sim.get("/v1/depots/depot-gazipur")["inventory"]["DIESEL"]
    out["stock_deducted_at_create"] = {"value": inv0 - inv1 == 1000, "before": inv0, "after": inv1}

    t0 = a["created_tick"]
    seen = {}
    for _ in range(8):
        sim.post("/admin/step")
        x = next(v for v in sim.get("/v1/allocations") if v["id"] == a["id"])
        seen = x
        if x["status"] == "ARRIVED":
            break
    transit = sim.get("/v1/routes")[0]["transit_ticks"]
    out["timing"] = {"created": t0, "departure": seen.get("departure_tick"), "arrival": seen.get("actual_arrival_tick"),
                     "transit_ticks": transit,
                     "matches_twin": seen.get("departure_tick") == t0 and seen.get("actual_arrival_tick") == t0 + transit}

    sim.reset()
    cap = sim.get("/v1/depots/depot-gazipur")["dispatch_capacity_per_tick"]
    # each leg stays under its route max and station headroom, so only dispatch capacity can bind
    c1 = code_of(*sim.alloc("cal-p2a", "depot-gazipur", "station-tongi", "route-gazipur-tongi", "DIESEL", 6000))
    c2 = code_of(*sim.alloc("cal-p2b", "depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", cap - 6000 + 100))
    sim.post("/admin/step")  # pending -> in transit: does in-flight still count next tick?
    c3 = code_of(*sim.alloc("cal-p2c", "depot-gazipur", "station-karnaphuli", "route-gazipur-karnaphuli", "DIESEL", 5000))
    out["dispatch_cap"] = {"same_tick_overflow_rejected": c1 is None and c2 == "DISPATCH_CAPACITY_EXCEEDED",
                           "resets_next_tick": c3 is None, "codes": [c1, c2, c3],
                           "twin_assumes": "only allocations created this tick count"}

    sim.reset()
    tick = sim.get("/v1/instance")["tick"]
    sim.post("/admin/events", {"type": "depot_constraint", "start_tick": tick, "duration_ticks": 5,
                               "parameters": {"depot_ids": ["depot-gazipur"]}})
    sim.post("/admin/step")
    d = sim.get("/v1/depots/depot-gazipur")
    ca = code_of(*sim.alloc("cal-p3a", "depot-gazipur", "station-tongi", "route-gazipur-tongi", "DIESEL", 6000))
    cb = code_of(*sim.alloc("cal-p3b", "depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 5000))
    out["constrained_depot"] = {"status": d["status"], "dispatch_capacity_per_tick": d["dispatch_capacity_per_tick"],
                                "full_dispatch_accepted": ca is None and cb is None, "codes": [ca, cb],
                                "recommend_CONSTRAINED_DISPATCH_FACTOR": 1.0 if ca is None and cb is None else 0.5}

    sim.reset()
    st, a = sim.alloc("cal-p4", *R, "DIESEL", 2000)
    inv_mid = sim.get("/v1/depots/depot-gazipur")["inventory"]["DIESEL"]
    tick = sim.get("/v1/instance")["tick"]
    sim.post("/admin/events", {"type": "route_disruption", "start_tick": tick, "duration_ticks": 3,
                               "parameters": {"route_ids": ["route-gazipur-mirpur"]}})
    route_now = next(r for r in sim.get("/v1/routes") if r["id"] == R[2])["status"]
    sim.post("/admin/step")
    x = next(v for v in sim.get("/v1/allocations") if v["id"] == a["id"])
    inv_after = sim.get("/v1/depots/depot-gazipur")["inventory"]["DIESEL"]
    out["disruption_at_departure"] = {"route_status_right_after_inject": route_now, "allocation_status": x["status"],
                                      "failure_reason": x.get("failure_reason"),
                                      "refunded": inv_after >= inv_mid + 2000 - 1e-6}

    sim.reset()
    for _ in range(3):
        sim.post("/admin/step")
    rows = sim.get("/v1/demand-history", limit=12)
    ticks = [r["tick"] for r in rows]
    out["demand_history"] = {"limit12_ticks": sorted(set(ticks)), "newest_first": bool(ticks) and max(ticks) == 2,  # rows carry the processed tick
                             "rows_per_tick": len([r for r in sim.get("/v1/demand-history", limit=2000) if r["tick"] == 1])}
    return out


SCRIPT = [("demand_spike", 20, 25, {"region_ids": ["region-dhaka"], "multiplier": 1.8}),
          ("shipment_delay", 25, 1, {"delay_ticks": 10, "depot_ids": ["depot-patiya"]}),
          ("route_disruption", 30, 20, {"route_ids": ["route-gazipur-mirpur"]}),
          ("station_outage", 45, 10, {"station_ids": ["station-coxsbazar"]}),
          ("depot_constraint", 50, 10, {"depot_ids": ["depot-gazipur"]}),
          ("supply_shortfall", 60, 1, {"factor": 0.6})]


def lockstep(sim, ticks):
    sim.reset()
    s = sim.snap()
    for typ, start, dur, p in SCRIPT:
        sim.post("/admin/events", {"type": typ, "start_tick": start, "duration_ticks": dur, "parameters": p})
    s = sim.snap()
    observed = {}
    tw = Twin(dict(tick=s["tick"], sim_time=s["sim_time"], tick_minutes=s["tick_minutes"], seed=0,
                   regions=s["regions"], depots=s["depots"], stations=s["stations"], routes=s["routes"],
                   supply=s["supply"], allocations=[], events=s["events"]),
              demand_fn=lambda st, f, t, h: observed.get((st["id"], f, t), 0.0))
    fc = Forecaster()
    drift, first, accept_mismatch, rows_all = [], None, [], []
    for i in range(ticks):
        if i % 2 == 0:  # act with our real LP policy on the sim's state
            p, a = fc.paths(s, H), arrivals(s, H)
            ships, _ = check(finalize(ALGOS["lp"](build_problem(s, fc, p, a, risks(s, fc, p, a, H), H)), s), s)
            for j, x in enumerate(ships):
                key = f"cal-{i}-{j}"
                sc = code_of(*sim.alloc(key, x["source_depot_id"], x["destination_station_id"], x["route_id"],
                                        x["fuel_type"], x["quantity"]))
                _, tc = tw.submit(x["source_depot_id"], x["destination_station_id"], x["route_id"], x["fuel_type"],
                                  x["quantity"], key)
                if sc != tc:
                    accept_mismatch.append({"tick": s["tick"], "key": key, "sim": sc, "twin": tc})
        sim.post("/admin/step")
        rows = [r for r in sim.get("/v1/demand-history", limit=24) if r["tick"] == s["tick"]]  # rows of the tick just processed
        rows_all += rows
        for r in rows:
            observed[(r["station_id"], r["fuel_type"], r["tick"])] = r["demand_liters"]
        tw.step()
        s = sim.snap()
        fc.ingest(rows, s)
        diffs = {}
        for kind in ("depots", "stations"):
            for eid, e in s[kind].items():
                te = getattr(tw, kind)[eid]
                for f in FUELS:
                    dv = e["inventory"][f] - te["inventory"][f]
                    if abs(dv) > 1:
                        diffs[f"{eid}/{f}"] = round(dv, 1)
                if e["status"] != te["status"]:
                    diffs[f"{eid}/status"] = f"sim={e['status']} twin={te['status']}"
        for rid, r in s["routes"].items():
            if r["status"] != tw.routes[rid]["status"]:
                diffs[f"{rid}/status"] = f"sim={r['status']} twin={tw.routes[rid]['status']}"
        tw_alloc = {a["idempotency_key"]: a for a in tw.allocations}
        for a in s["allocations"]:
            t = tw_alloc.get(a["idempotency_key"])
            if t and (t["status"], t["actual_arrival_tick"]) != (a["status"], a["actual_arrival_tick"]):
                diffs[f"alloc/{a['idempotency_key']}"] = f"sim={a['status']}@{a['actual_arrival_tick']} twin={t['status']}@{t['actual_arrival_tick']}"
        tw_sup = {a["id"]: a for a in tw.supply}
        for a in s["supply"]:
            t = tw_sup[a["id"]]
            if (a["status"], a["planned_tick"], round(a["quantity"])) != (t["status"], t["planned_tick"], round(t["quantity"])):
                diffs[f"supply/{a['id']}"] = f"sim={a['status']}@{a['planned_tick']}x{a['quantity']:.0f} twin={t['status']}@{t['planned_tick']}x{t['quantity']:.0f}"
        drift.append(len(diffs))
        if diffs and first is None:
            first = {"tick": s["tick"], "diffs": dict(list(diffs.items())[:15])}
    m = s["metrics"]
    return {"ticks": ticks, "ticks_with_drift": sum(1 for d in drift if d), "first_divergence": first,
            "acceptance_mismatches": accept_mismatch[:20],
            "service_level": {"sim": m["service_level"], "twin": tw.metrics()["service_level"]},
            "served_liters": {"sim": m["served_demand_liters"], "twin": round(tw.totals["served"], 1)}}, rows_all, s


def demand_formula(rows, snap):
    ratios = defaultdict(list)
    for r in rows:
        st = snap["stations"][r["station_id"]]
        h = parse_time(r["sim_time"]).hour
        exp = base_rate(st["demand_profile"], r["fuel_type"], h, snap["regions"][st["region_id"]]["demand_factor"],
                        1.0, snap["tick_minutes"])
        ratios[(st["demand_profile"], h)].append(r["demand_liters"] / exp if exp else 0)
    table = {f"{p}@{h:02d}": round(statistics.mean(v), 3) for (p, h), v in sorted(ratios.items())}
    off = {k: v for k, v in table.items() if abs(v - 1) > 0.15}
    return {"mean_ratio_by_profile_hour": table, "suspect_hours(|ratio-1|>0.15, spikes included)": off}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--ticks", type=int, default=120)
    ap.add_argument("--out")
    a = ap.parse_args()
    sim = Sim(a.base)
    rep = {"probes": probes(sim)}
    rep["lockstep"], rows, snap = lockstep(sim, a.ticks)
    rep["demand_formula"] = demand_formula(rows, snap)
    sim.reset()
    print(json.dumps({k: v for k, v in rep.items() if k != "demand_formula"}, indent=2))
    print("demand formula suspects:", json.dumps(rep["demand_formula"]["suspect_hours(|ratio-1|>0.15, spikes included)"]))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=2)


if __name__ == "__main__":
    main()
