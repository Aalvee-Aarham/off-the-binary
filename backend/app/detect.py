"""Alerting + regime flags. Alerts are keyed (type, entity) and only fire on raise/resolve transitions."""
import numpy as np

from . import metrics as M
from .world import ACTIVE_ALLOC, FUELS


def dispatch_used(snap, depot_id):
    return sum(a["quantity"] for a in snap["allocations"]
               if a["source_depot_id"] == depot_id and a["status"] in ACTIVE_ALLOC and a["created_tick"] == snap["tick"])


def depot_cover(snap, paths, depot_id, fuel):
    """Ticks the depot can feed its home-region stations before the next supply arrival (None = fine)."""
    d = snap["depots"][depot_id]
    home = [s for s in snap["stations"].values() if s["region_id"] == d["region_id"]]
    rate = sum(float(np.mean(paths[(s["id"], fuel)])) for s in home) or 1e-6
    nxt = min((a["planned_tick"] for a in snap["supply"]
               if a["depot_id"] == depot_id and a["fuel_type"] == fuel and a["status"] != "ARRIVED"), default=None)
    need_ticks = (nxt - snap["tick"]) if nxt is not None else 96
    cover = d["inventory"][fuel] / rate
    return cover, need_ticks


class Detector:
    def __init__(self):
        self.active = {}  # key -> alert

    def reset(self):
        self.active.clear()

    def run(self, snap, prev, risks, paths, fc):
        found = {}

        def add(typ, entity, severity, msg, **extra):
            found[(typ, entity)] = {"type": typ, "entity": entity, "severity": severity, "message": msg,
                                    "tick": snap["tick"], **extra}

        flags = set()
        for (sid, f), r in risks.items():
            if r["p_stockout"] >= 0.8 and (r["hours_to_stockout"] or 99) < 4:
                add("stockout_risk", f"{sid}/{f}", "critical", f"{sid} {f}: stockout in {r['hours_to_stockout']}h "
                    f"(p={r['p_stockout']:.0%})", signals=r["signals"])
            elif r["p_stockout"] >= 0.5:
                add("stockout_risk", f"{sid}/{f}", "warning", f"{sid} {f}: p(stockout)={r['p_stockout']:.0%}",
                    signals=r["signals"])
        for s in snap["stations"].values():
            if s["status"] != "OPEN":
                add("station_outage", s["id"], "critical", f"{s['id']} is {s['status']}")
                flags.add("route_disruption")
            if s["demand_multiplier"] >= 1.15:
                add("demand_spike", s["id"], "warning", f"{s['id']} demand x{s['demand_multiplier']:.2f}")
                flags.add("demand_spike")
            for f in FUELS:
                if fc.alarm_tick.get((s["id"], f), -99) >= snap["tick"] - 4:
                    add("demand_anomaly", f"{s['id']}/{f}", "warning", f"{s['id']} {f}: demand shift not explained by model")
                    flags.add("demand_spike")
        for r in snap["routes"].values():
            if r["status"] != "AVAILABLE":
                add("route_disruption", r["id"], "warning", f"{r['id']} is {r['status']}")
                flags.add("route_disruption")
        for d in snap["depots"].values():
            if d["status"] != "OPEN":
                add("depot_constraint", d["id"], "warning", f"{d['id']} is {d['status']}")
                flags.add("depot_constraint")
            used = dispatch_used(snap, d["id"])
            if d["dispatch_capacity_per_tick"] and used >= 0.9 * d["dispatch_capacity_per_tick"]:
                add("bottleneck", d["id"], "info", f"{d['id']} dispatch {used:.0f}/{d['dispatch_capacity_per_tick']:.0f} this tick")
            for f in FUELS:
                cover, need = depot_cover(snap, paths, d["id"], f)
                if cover < need:
                    add("supply_gap", f"{d['id']}/{f}", "warning" if cover > need / 2 else "critical",
                        f"{d['id']} {f}: {cover:.0f} ticks of stock, next supply in {need} ticks")
                    flags.add("supply_crisis")
        for a in snap["supply"]:
            if a["status"] == "DELAYED":
                add("shipment_delay", a["id"], "warning", f"{a['id']} ({a['depot_id']} {a['fuel_type']}) delayed to tick {a['planned_tick']}")
                flags.add("supply_crisis")
        for e in snap["events"]:
            if e["status"] == "ACTIVE" and e["type"] == "supply_shortfall":
                flags.add("supply_crisis")
        # inventory not explained by served demand + arrivals (only across exactly one tick)
        if prev and snap["tick"] == prev["tick"] + 1:
            arrived = {}
            for a in snap["allocations"]:
                if a["actual_arrival_tick"] == snap["tick"]:
                    k = (a["destination_station_id"], a["fuel_type"])
                    arrived[k] = arrived.get(k, 0) + a["quantity"]
            for s in snap["stations"].values():
                ps = prev["stations"].get(s["id"])
                if not ps:
                    continue
                for f in FUELS:
                    delta = s["inventory"][f] - ps["inventory"][f] - arrived.get((s["id"], f), 0)
                    if delta > max(300.0, 0.03 * s["capacity"][f]):  # gained fuel from nowhere
                        add("inventory_anomaly", f"{s['id']}/{f}", "warning", f"{s['id']} {f} +{delta:.0f} L unexplained")

        raised = [a for k, a in found.items() if k not in self.active]
        resolved = [a for k, a in self.active.items() if k not in found]
        for a in raised:
            M.ALERTS.labels(a["type"], a["severity"]).inc()
        self.active = found
        M.ACTIVE_ALERTS.set(len(found))
        return raised, resolved, flags
