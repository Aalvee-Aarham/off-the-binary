"""Digital twin of the BUP Fuel Supply Simulator (integration guide sections 5, 7.8, 8).

Used for: plan scoring (tournament), the local dev simulator (devsim.py), and PPO training.
Tick semantics were calibrated against the real image (scripts/calibrate.py in CI): a step *processes the current
tick t* (events, supply, arrivals, departures at t, demand rows labeled t) and only then advances to t+1.
Allocations on a disrupted route FAIL at departure and the fuel is NOT refunded. Events are in force for processed
ticks start_tick..end_tick inclusive. Arrivals beyond station capacity are clipped. (One-step check: 119/120 ticks exact.)
"""
import copy
import random
from datetime import datetime, timedelta

FUELS = ("DIESEL", "PETROL", "OCTANE")
ACTIVE_ALLOC = ("PENDING", "IN_TRANSIT")

# liters per simulated day, noise (guide 8.5)
PROFILES = {
    "urban_high": ({"DIESEL": 8500, "PETROL": 10500, "OCTANE": 5600}, 0.10),
    "industrial": ({"DIESEL": 14000, "PETROL": 4500, "OCTANE": 2200}, 0.08),
    "highway": ({"DIESEL": 10500, "PETROL": 11000, "OCTANE": 6200}, 0.12),
    "regional": ({"DIESEL": 7200, "PETROL": 7600, "OCTANE": 3600}, 0.10),
}


def hour_factor(profile, h):  # guide 8.6
    if profile == "industrial":
        return 1.55 if 6 <= h < 18 else 0.45
    if profile == "highway":
        return 1.35 if 6 <= h < 10 or 16 <= h < 21 else 0.75
    if profile == "urban_high":
        return 1.45 if 7 <= h < 10 or 16 <= h < 21 else 0.70
    if profile == "regional":
        return 1.25 if 7 <= h < 21 else 0.65
    return 1.0


def base_rate(profile, fuel, hour, region_factor, multiplier, tick_minutes):
    """Expected liters demanded in one tick (no noise)."""
    daily = PROFILES.get(profile, PROFILES["regional"])[0][fuel]
    return daily * tick_minutes / 1440 * hour_factor(profile, hour) * region_factor * multiplier


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _supply_schedule():
    """22 arrivals (guide 8.7). Exact quantities aren't published; ours are sized to ~1 day of regional demand."""
    rows = [("depot-gazipur", "DIESEL", 18000, 12), ("depot-patiya", "DIESEL", 16000, 14),
            ("depot-gazipur", "PETROL", 14000, 16), ("depot-patiya", "PETROL", 18000, 20)]
    daily = {"depot-gazipur": {"DIESEL": 22500, "PETROL": 15000, "OCTANE": 7800},
             "depot-patiya": {"DIESEL": 19100, "PETROL": 20100, "OCTANE": 10600}}
    for rnd in range(3):
        for d in ("depot-gazipur", "depot-patiya"):
            for f in FUELS:
                rows.append((d, f, daily[d][f], 64 * (rnd + 1)))
    return [{"id": f"supply-{i + 1:03d}", "depot_id": d, "fuel_type": f, "quantity": float(q),
             "planned_tick": t, "actual_tick": None, "status": "SCHEDULED"} for i, (d, f, q, t) in enumerate(rows)]


def baseline_world():
    def fuels(d, p, o):
        return {"DIESEL": float(d), "PETROL": float(p), "OCTANE": float(o)}

    return dict(
        tick=0, sim_time="2026-01-01T00:00:00+00:00", tick_minutes=15, seed=12345,
        regions={"region-dhaka": {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.00},
                 "region-chattogram": {"id": "region-chattogram", "name": "Chattogram Division", "demand_factor": 1.08}},
        depots={
            "depot-gazipur": {"id": "depot-gazipur", "name": "Gazipur Depot", "region_id": "region-dhaka", "status": "OPEN",
                              "dispatch_capacity_per_tick": 12000, "capacity": fuels(90000, 70000, 45000),
                              "inventory": fuels(60000, 45000, 26000)},
            "depot-patiya": {"id": "depot-patiya", "name": "Patiya Depot", "region_id": "region-chattogram", "status": "OPEN",
                             "dispatch_capacity_per_tick": 11000, "capacity": fuels(85000, 65000, 40000),
                             "inventory": fuels(55000, 42000, 24000)}},
        stations={
            sid: {"id": sid, "name": name, "region_id": reg, "status": "OPEN", "demand_profile": prof,
                  "demand_multiplier": 1.0, "capacity": fuels(*cap), "inventory": fuels(*inv)}
            for sid, name, reg, prof, cap, inv in [
                ("station-mirpur", "Mirpur Fuel Station", "region-dhaka", "urban_high", (15000, 14000, 9000), (9000, 9000, 5000)),
                ("station-tongi", "Tongi Fuel Station", "region-dhaka", "industrial", (18000, 9000, 6000), (11000, 6000, 3500)),
                ("station-karnaphuli", "Karnaphuli Fuel Station", "region-chattogram", "highway", (14000, 15000, 9000), (8500, 9500, 5200)),
                ("station-coxsbazar", "Cox's Bazar Fuel Station", "region-chattogram", "regional", (12000, 12000, 7000), (7500, 7500, 4200))]},
        routes={
            rid: {"id": rid, "source_depot_id": d, "destination_station_id": s, "transit_ticks": t,
                  "max_shipment": float(m), "status": "AVAILABLE"}
            for rid, d, s, t, m in [
                ("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2, 7000),
                ("route-gazipur-tongi", "depot-gazipur", "station-tongi", 2, 6500),
                ("route-patiya-karnaphuli", "depot-patiya", "station-karnaphuli", 2, 7000),
                ("route-patiya-coxsbazar", "depot-patiya", "station-coxsbazar", 3, 6000),
                ("route-gazipur-karnaphuli", "depot-gazipur", "station-karnaphuli", 4, 5000),
                ("route-patiya-mirpur", "depot-patiya", "station-mirpur", 4, 5000)]},
        supply=_supply_schedule(), allocations=[], events=[],
    )


class Twin:
    """Mutable world. `demand_fn(station, fuel, tick, hour) -> liters` overrides stochastic demand (used for scoring)."""

    def __init__(self, world, demand_fn=None, noise=True):
        w = copy.deepcopy(world)
        self.tick, self.tick_minutes = w["tick"], w["tick_minutes"]
        self.sim_time = parse_time(w["sim_time"])
        self.regions, self.depots, self.stations, self.routes = w["regions"], w["depots"], w["stations"], w["routes"]
        self.supply, self.allocations, self.events = w["supply"], w["allocations"], w["events"]
        self.seed = w.get("seed", 0)
        self.rng = random.Random(self.seed)
        self.demand_fn, self.noise = demand_fn, noise
        self.demand_log = []  # rows like /v1/demand-history
        self.totals = {"served": 0.0, "unmet": 0.0, "overflow": 0.0, "failed": 0}
        self._next_alloc_id = max((a["id"] for a in self.allocations), default=0) + 1

    # ---- writes (mirror guide 5.2 validation order) ----
    def dispatch_used(self, depot_id):
        return sum(a["quantity"] for a in self.allocations
                   if a["source_depot_id"] == depot_id and a["status"] in ACTIVE_ALLOC and a["created_tick"] == self.tick)

    def submit(self, depot_id, station_id, route_id, fuel, qty, key=None):
        d, s, r = self.depots.get(depot_id), self.stations.get(station_id), self.routes.get(route_id)
        if not (d and s and r):
            return None, "NOT_FOUND"
        if (r["source_depot_id"], r["destination_station_id"]) != (depot_id, station_id):
            return None, "ROUTE_MISMATCH"
        if d["status"] not in ("OPEN", "CONSTRAINED"):
            return None, "DEPOT_CLOSED"
        if s["status"] != "OPEN":
            return None, "STATION_CLOSED"
        if r["status"] != "AVAILABLE":
            return None, "ROUTE_DISRUPTED"
        if qty > r["max_shipment"]:
            return None, "ROUTE_CAPACITY_EXCEEDED"
        if d["inventory"][fuel] < qty:
            return None, "INSUFFICIENT_INVENTORY"
        if self.dispatch_used(depot_id) + qty > d["dispatch_capacity_per_tick"]:
            return None, "DISPATCH_CAPACITY_EXCEEDED"
        if s["inventory"][fuel] + qty > s["capacity"][fuel]:
            return None, "DESTINATION_CAPACITY_EXCEEDED"
        d["inventory"][fuel] -= qty
        a = {"id": self._next_alloc_id, "idempotency_key": key or f"twin-{self._next_alloc_id}",
             "source_depot_id": depot_id, "destination_station_id": station_id, "route_id": route_id,
             "fuel_type": fuel, "quantity": float(qty), "created_tick": self.tick, "departure_tick": None,
             "expected_arrival_tick": None, "actual_arrival_tick": None, "status": "PENDING", "failure_reason": None}
        self._next_alloc_id += 1
        self.allocations.append(a)
        return a, None

    def cancel(self, alloc_id):
        a = next((a for a in self.allocations if a["id"] == alloc_id), None)
        if not a:
            return None, "ALLOCATION_NOT_FOUND"
        if a["status"] != "PENDING":
            return None, "CANNOT_CANCEL"
        self.depots[a["source_depot_id"]]["inventory"][a["fuel_type"]] += a["quantity"]
        a["status"] = "CANCELLED"
        return a, None

    def add_event(self, etype, start_tick, duration_ticks, parameters=None):
        e = {"id": len(self.events) + 1, "type": etype, "start_tick": start_tick,
             "end_tick": start_tick + duration_ticks, "status": "SCHEDULED", "parameters": parameters or {}}
        self.events.append(e)  # takes effect when tick start_tick is processed (never immediately)
        return e

    # ---- events (guide 7.8) ----
    @staticmethod
    def _match(ids, value):
        return not ids or value in ids

    def _event_stations(self, p):
        sids, rids = p.get("station_ids") or [], p.get("region_ids") or []
        if not sids and not rids:
            return list(self.stations.values())
        return [s for s in self.stations.values() if s["id"] in sids or s["region_id"] in rids]

    def _apply(self, e, on):
        p, t = e["parameters"], e["type"]
        if t == "demand_spike":
            m = float(p.get("multiplier", 1.5))
            for s in self._event_stations(p):
                s["demand_multiplier"] = s["demand_multiplier"] * m if on else max(0.01, s["demand_multiplier"] / m)
        elif t == "route_disruption":
            for r in self.routes.values():
                if self._match(p.get("route_ids"), r["id"]):
                    r["status"] = "DISRUPTED" if on else "AVAILABLE"
        elif t == "station_outage":
            for s in self.stations.values():
                if self._match(p.get("station_ids"), s["id"]):
                    s["status"] = "OUTAGE" if on else "OPEN"
        elif t == "depot_constraint":
            for d in self.depots.values():
                if self._match(p.get("depot_ids"), d["id"]):
                    d["status"] = "CONSTRAINED" if on else "OPEN"
        elif on and t in ("shipment_delay", "supply_shortfall"):  # one-shot, never undone
            for a in self.supply:
                if a["status"] != "ARRIVED" and self._match(p.get("depot_ids"), a["depot_id"]) \
                        and self._match(p.get("fuel_types"), a["fuel_type"]):
                    if t == "shipment_delay":
                        a["planned_tick"] += int(p.get("delay_ticks", 2))
                        a["status"] = "DELAYED"
                    else:
                        a["quantity"] *= float(p.get("factor", 0.5))

    def _start_event(self, e):
        e["status"] = "ACTIVE"
        self._apply(e, True)

    # ---- clock ----
    def step(self):
        t = self.tick  # the tick being processed
        for e in self.events:
            if e["status"] == "SCHEDULED" and e["start_tick"] <= t:
                self._start_event(e)
        for a in self.supply:
            if a["status"] != "ARRIVED" and a["planned_tick"] <= t:
                d = self.depots[a["depot_id"]]
                d["inventory"][a["fuel_type"]] = min(d["capacity"][a["fuel_type"]], d["inventory"][a["fuel_type"]] + a["quantity"])
                a["status"], a["actual_tick"] = "ARRIVED", t
        for a in self.allocations:
            if a["status"] == "IN_TRANSIT" and a["expected_arrival_tick"] <= t:
                s = self.stations[a["destination_station_id"]]
                f = a["fuel_type"]
                room = s["capacity"][f] - s["inventory"][f]
                self.totals["overflow"] += max(0.0, a["quantity"] - room)
                s["inventory"][f] += min(a["quantity"], room)
                a["status"], a["actual_arrival_tick"] = "ARRIVED", t
            elif a["status"] == "PENDING":
                r = self.routes[a["route_id"]]
                if r["status"] != "AVAILABLE":  # calibrated: fuel is lost, not refunded
                    a["status"], a["failure_reason"] = "FAILED", "ROUTE_UNAVAILABLE"
                    self.totals["failed"] += 1
                else:
                    a["status"], a["departure_tick"] = "IN_TRANSIT", t
                    a["expected_arrival_tick"] = t + r["transit_ticks"]
        hour = self.sim_time.hour
        for s in self.stations.values():
            rf = self.regions[s["region_id"]]["demand_factor"]
            for f in FUELS:
                if self.demand_fn:
                    dem = self.demand_fn(s, f, t, hour)
                else:
                    dem = base_rate(s["demand_profile"], f, hour, rf, s["demand_multiplier"], self.tick_minutes)
                    if self.noise:
                        dem *= max(0.0, 1 + self.rng.gauss(0, PROFILES.get(s["demand_profile"], PROFILES["regional"])[1]))
                served = min(dem, s["inventory"][f]) if s["status"] == "OPEN" else 0.0
                s["inventory"][f] -= served
                self.totals["served"] += served
                self.totals["unmet"] += dem - served
                self.demand_log.append({"id": len(self.demand_log) + 1, "station_id": s["id"], "fuel_type": f,
                                        "tick": t, "sim_time": self.sim_time.isoformat(),
                                        "demand_liters": round(dem, 3), "served_liters": round(served, 3),
                                        "unmet_liters": round(dem - served, 3)})
        for e in self.events:  # calibrated (sim audit order): resolution is the last thing processed in end_tick
            if e["status"] == "ACTIVE" and e["end_tick"] <= t:
                e["status"] = "RESOLVED"
                self._apply(e, False)
        self.tick += 1
        self.sim_time += timedelta(minutes=self.tick_minutes)
        return {"tick": self.tick, "sim_time": self.sim_time.isoformat()}

    # ---- read model (same shapes as /v1/*) ----
    def snapshot(self):
        """Same dict shape StateStore builds from the real API."""
        c = copy.deepcopy
        return {"tick": self.tick, "sim_time": self.sim_time.isoformat(), "tick_minutes": self.tick_minutes,
                "status": "RUNNING", "seed": self.seed, "regions": c(self.regions), "depots": c(self.depots),
                "stations": c(self.stations), "routes": c(self.routes), "supply": c(self.supply),
                "events": c(self.events), "allocations": c(self.allocations), "metrics": self.metrics(), "stale": False}

    def metrics(self):
        t = self.totals
        tot = t["served"] + t["unmet"]
        return {"served_demand_liters": round(t["served"], 3), "unmet_demand_liters": round(t["unmet"], 3),
                "service_level": round(t["served"] / tot, 6) if tot else 1.0,
                "allocation_liters": sum(a["quantity"] for a in self.allocations if a["status"] in ("IN_TRANSIT", "ARRIVED")),
                "allocation_failures": sum(1 for a in self.allocations if a["status"] == "FAILED")}
