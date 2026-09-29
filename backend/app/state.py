"""Last-good snapshot of the simulated world. REST is truth; failures keep the cached snapshot (degraded mode)."""
import asyncio
import logging
import time

from . import metrics as M
from .sim_client import SimError, SimInvalid
from .world import ACTIVE_ALLOC, FUELS

log = logging.getLogger("state")

LISTS = {"regions": "/v1/regions", "depots": "/v1/depots", "stations": "/v1/stations", "routes": "/v1/routes"}


def validate(snap):
    """Semantic checks beyond the schema. Returns list of problems."""
    bad = []
    for kind in ("depots", "stations"):
        for e in snap[kind].values():
            for f in FUELS:
                if e["inventory"][f] > e["capacity"][f] * 1.01 + 1:
                    bad.append(f"{e['id']} {f} inventory {e['inventory'][f]} > capacity {e['capacity'][f]}")
            if e["region_id"] not in snap["regions"]:
                bad.append(f"{e['id']} unknown region {e['region_id']}")
    for r in snap["routes"].values():
        if r["source_depot_id"] not in snap["depots"] or r["destination_station_id"] not in snap["stations"]:
            bad.append(f"{r['id']} references unknown depot/station")
    if not snap["depots"] or not snap["stations"] or not snap["routes"]:
        bad.append("empty world")
    return bad


def world_from_snapshot(snap):
    """Twin-ready world dict: only in-flight allocations, unarrived supply, unresolved events."""
    return dict(
        tick=snap["tick"], sim_time=snap["sim_time"], tick_minutes=snap["tick_minutes"], seed=snap.get("seed") or 0,
        regions=snap["regions"], depots=snap["depots"], stations=snap["stations"], routes=snap["routes"],
        supply=[a for a in snap["supply"] if a["status"] != "ARRIVED"],
        allocations=[a for a in snap["allocations"] if a["status"] in ACTIVE_ALLOC],
        events=[e for e in snap["events"] if e["status"] != "RESOLVED"],
    )


class StateStore:
    def __init__(self, sim, on_invalid=None):
        self.sim, self.on_invalid = sim, on_invalid
        self.snap = None
        self.prev = None
        self.fetched_at = 0.0
        self.degraded = False
        self.last_error = None
        self.reset_seen = False
        self.lock = asyncio.Lock()

    @property
    def age(self):
        return time.time() - self.fetched_at if self.fetched_at else None

    async def refresh(self):
        """Fetch everything; on any failure keep the last good snapshot and flag degraded."""
        async with self.lock:
            try:
                snap = await self._fetch()
            except SimError as e:
                self.degraded, self.last_error = True, f"{e.code}: {e.message}"
                M.DEGRADED.set(1)
                if isinstance(e, SimInvalid) and self.on_invalid:
                    self.on_invalid(e)
                log.warning("snapshot.failed", extra={"event": "snapshot.failed", "error": self.last_error})
                raise
            self.reset_seen = bool(self.snap and snap["tick"] < self.snap["tick"])
            self.prev, self.snap = self.snap, snap
            self.fetched_at, self.degraded, self.last_error = time.time(), False, None
            M.DEGRADED.set(0)
            M.SIM_TICK.set(snap["tick"])
            M.SERVICE_LEVEL.set(snap["metrics"]["service_level"])
            M.UNMET.set(snap["metrics"]["unmet_demand_liters"])
            return snap

    async def _fetch(self):
        g = self.sim.get
        if self.sim.breaker.state != self.sim.breaker.CLOSED:
            await g("/v1/instance")  # single half-open probe; the parallel fan-out below would trip it again
        inst, regions, depots, stations, routes, supply, events, allocs, metrics = await asyncio.gather(
            g("/v1/instance"), g(LISTS["regions"]), g(LISTS["depots"]), g(LISTS["stations"]), g(LISTS["routes"]),
            g("/v1/supply-arrivals"), g("/v1/events"), g("/v1/allocations"), g("/v1/metrics"))
        stale = self.sim.stale
        snap = {
            "tick": inst["tick"], "sim_time": inst["sim_time"], "tick_minutes": inst["tick_minutes"],
            "status": inst["status"], "seed": inst.get("seed"), "scenario_id": inst.get("scenario_id"),
            "regions": {r["id"]: r for r in regions}, "depots": {d["id"]: d for d in depots},
            "stations": {s["id"]: s for s in stations}, "routes": {r["id"]: r for r in routes},
            "supply": supply, "events": events, "allocations": allocs, "metrics": metrics, "stale": stale,
        }
        bad = validate(snap)
        if bad:
            raise SimInvalid("SEMANTIC", "; ".join(bad[:5]))
        return snap

    async def demand_rows(self, since_tick):
        """Only the rows we haven't seen: 12 per tick, clamped to the API's 2000 max."""
        n = 2000 if since_tick < 0 else min(2000, max(24, 12 * (self.snap["tick"] - since_tick + 2)))
        rows = await self.sim.get("/v1/demand-history", limit=n)
        if rows and len(rows) == n and max(r["tick"] for r in rows) < self.snap["tick"] - 2:
            # guide doesn't state sort order; if it's oldest-first we need station_id paging instead
            log.warning("demand_history.oldest_first", extra={"event": "demand_history.order"})
        return [r for r in rows if r["tick"] > since_tick]
