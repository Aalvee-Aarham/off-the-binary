"""Full backend against the in-process stand-in simulator: decisions, faults, degraded mode, recovery, API."""
import asyncio
import time

import httpx
from fastapi.testclient import TestClient

from app import config, devsim
from app.audit import Audit
from app.orchestrator import Orchestrator
from app.sim_client import SimClient
from app.state import StateStore


def stack():
    devsim.S.reset()
    sim = SimClient("http://sim", transport=httpx.ASGITransport(app=devsim.app))
    store = StateStore(sim)
    return sim, store, Orchestrator(sim, store, Audit(":memory:"))


def test_loop_decides_survives_faults_and_recovers():
    async def go():
        sim, store, o = stack()
        for _ in range(60):
            devsim.S.step()
            await o.cycle()
        executed = o.audit.list("EXECUTED") + o.audit.list("COMPLETED")
        assert executed, "auto-gated loop should have shipped fuel"
        assert devsim.S.tw.metrics()["service_level"] > 0.95

        # simulator outage -> cached state, degraded, no crash
        devsim.S.faults = [{"type": "unavailable", "parameters": {}, "end": time.time() + 60}]
        devsim.S.step()
        assert await o.cycle() is None
        assert store.degraded and o.view["degraded"] and o.view["tick"] == 60
        # recovery (breaker cooldown elapses)
        devsim.S.faults = []
        sim.breaker.opened_at -= config.BREAKER_COOLDOWN_S
        devsim.S.step()
        await o.cycle()
        assert not store.degraded and o.view["tick"] == 62

        # stale data -> nothing auto-executes
        devsim.S.faults = [{"type": "stale_data", "parameters": {}, "end": time.time() + 60}]
        for _ in range(3):
            devsim.S.step()
        d = await o.cycle(force=True)
        assert d is None or d["status"] in ("PENDING_APPROVAL", "NO_ACTION")
        devsim.S.faults = []

    asyncio.run(go())


def test_manual_mode_requires_approval_then_executes():
    async def go():
        sim, store, o = stack()
        o.mode = "MANUAL"
        devsim.S.tw.add_event("demand_spike", 1, 80, {"multiplier": 2.5})
        for _ in range(30):
            devsim.S.step()
        d = await o.cycle(force=True)
        assert d["status"] == "PENDING_APPROVAL" and d["shipments"]
        before = len(devsim.S.tw.allocations)
        done = await o.approve(d["id"])
        assert done["status"] in ("EXECUTED", "PARTIAL")
        assert len(devsim.S.tw.allocations) > before

    asyncio.run(go())


def test_unattended_crisis_does_not_starve_network():
    """Combined crisis flags review; with no operator, the review window elapses and the plan still ships."""
    async def go():
        sim, store, o = stack()
        tw = devsim.S.tw
        tw.add_event("demand_spike", 1, 200, {"region_ids": ["region-dhaka"], "multiplier": 2.0})
        tw.add_event("route_disruption", 1, 200, {"route_ids": ["route-gazipur-mirpur"]})
        for _ in range(80):
            devsim.S.step()
            await o.cycle()
        assert o.router["regime"] == "combined"
        auto = [d for d in o.audit.list(limit=200) if d.get("actor") == "auto-after-review-window"]
        assert auto, "soft-gated plans must execute after the review window"
        assert tw.metrics()["service_level"] > 0.95

    asyncio.run(go())


def test_api_surface_and_auth():
    config.DB_PATH, config.ADMIN_TOKEN = ":memory:", "test-token"
    devsim.S.reset()
    from app.main import create_app
    app = create_app(transport=httpx.ASGITransport(app=devsim.app), start_loops=False)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 503  # no snapshot yet
        assert c.post("/api/decisions/run").status_code == 401
        h = {"X-Admin-Token": "test-token"}
        assert c.post("/api/decisions/run", headers=h).status_code == 200
        assert c.get("/api/state").json()["tick"] == 0
        assert c.post("/api/decisions/recommend").status_code == 200
        assert c.get("/api/health").json()["components"]["simulator"]["status"] == "healthy"
        assert c.put("/api/mode", json={"mode": "BOGUS"}, headers=h).status_code == 422
        assert c.post("/api/chaos/event", json={"type": "demand_spike", "parameters": {"multiplier": 2}}, headers=h).status_code == 200
        assert c.post("/api/sim/step", headers=h).json()["tick"] == 1
        bad = {"shipments": [{"source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
                              "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 99999}]}
        assert c.post("/api/decisions/manual", json=bad, headers=h).status_code == 422
        assert "http_requests_total" in c.get("/metrics").text


def test_doomed_route_blocked_and_pending_cancelled():
    """Calibrated: an allocation departing onto a disrupted route FAILS and loses its fuel."""
    async def go():
        sim, store, o = stack()
        for _ in range(10):
            devsim.S.step()
        snap = await store.refresh()
        ship = {"source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 1000.0}
        a = await sim.create_allocation({"idempotency_key": "t-doom", **ship})  # PENDING now
        devsim.S.tw.add_event("route_disruption", snap["tick"], 5, {"route_ids": ["route-gazipur-mirpur"]})
        snap = await store.refresh()
        from app.solvers import check
        _, rej = check([ship], snap)
        assert rej and rej[0]["code"] == "ROUTE_DISRUPTED_AT_DEPARTURE"
        inv = snap["depots"]["depot-gazipur"]["inventory"]["DIESEL"]
        await o._cancel_doomed(snap)
        snap = await store.refresh()
        assert next(x for x in snap["allocations"] if x["id"] == a["id"])["status"] == "CANCELLED"
        assert snap["depots"]["depot-gazipur"]["inventory"]["DIESEL"] == inv + 1000  # refunded, not lost

    asyncio.run(go())


def test_view_regions_outlook_constraints_recovery_and_demand_store():
    async def go():
        sim, store, o = stack()
        devsim.S.tw.add_event("station_outage", 12, 6, {"station_ids": ["station-mirpur"]})
        for _ in range(30):
            devsim.S.step()
            await o.cycle(force=devsim.S.tw.tick % 4 == 0)
        v = o.view
        assert {r["id"] for r in v["regions"]} == {"region-dhaka", "region-chattogram"}
        assert len(v["supply_outlook"]) == 6 and all("days_of_cover" in x for x in v["supply_outlook"])
        assert v["scenario"]["epoch"] == o.epoch
        assert o.audit.demand_count(o.epoch) >= 12 * 25  # demand history persisted locally
        shipped = [d for d in o.audit.list(limit=100) if d.get("shipments")]
        assert shipped and all(d.get("constraints") for d in shipped)
        assert o.last_recovery and o.last_recovery["ticks"] > 0  # the outage raised a critical alert and recovered

    asyncio.run(go())
