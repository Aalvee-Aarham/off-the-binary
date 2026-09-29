import asyncio

import httpx
import pytest

from app import config
from app.forecast import Forecaster, arrivals, risks
from app.sim_client import SimClient, SimInvalid, SimRejected, SimUnavailable
from app.solvers import ALGOS, build_problem, check, finalize, tournament
from app.world import Twin, baseline_world

H = config.HORIZON_TICKS


def warm(ticks=40, events=()):
    tw = Twin(baseline_world())
    for e in events:
        tw.add_event(*e)
    for _ in range(ticks):
        tw.step()
    s = tw.snapshot()
    fc = Forecaster()
    fc.ingest(tw.demand_log, s)
    p, a = fc.paths(s, H), arrivals(s, H)
    return tw, s, fc, p, a, risks(s, fc, p, a, H)


# ---------- constraint checker == simulator rules ----------
def test_check_matches_simulator_validation():
    tw, s, *_ = warm(5)
    ok = {"source_depot_id": "depot-gazipur", "destination_station_id": "station-mirpur",
          "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL", "quantity": 1000.0}
    cases = [
        (ok, None),
        ({**ok, "route_id": "route-gazipur-tongi"}, "ROUTE_MISMATCH"),
        ({**ok, "quantity": 7001.0}, "ROUTE_CAPACITY_EXCEEDED"),
        ({**ok, "route_id": "nope"}, "NOT_FOUND"),
        ({**ok, "quantity": 6999.0, "fuel_type": "OCTANE"}, "DESTINATION_CAPACITY_EXCEEDED"),
    ]
    for sh, code in cases:
        valid, rej = check([sh], s)
        got = rej[0]["code"] if rej else None
        assert got == code, (sh, got)
        _, twin_code = Twin(dict(baseline_world(), **{k: s[k] for k in ("tick", "depots", "stations", "routes")})).submit(
            sh["source_depot_id"], sh["destination_station_id"], sh["route_id"], sh["fuel_type"], sh["quantity"])
        assert twin_code == code


def test_check_tracks_running_dispatch_capacity():
    _, s, *_ = warm(5)
    ships = [{"source_depot_id": "depot-gazipur", "destination_station_id": st, "route_id": f"route-gazipur-{st[8:]}",
              "fuel_type": "DIESEL", "quantity": 5000.0} for st in ("station-mirpur", "station-tongi", "station-karnaphuli")]
    valid, rej = check(ships, s)  # 15000 > 12000 dispatch/tick
    assert len(valid) == 2 and rej[0]["code"] == "DISPATCH_CAPACITY_EXCEEDED"


# ---------- every algorithm is feasible, also under disruption ----------
@pytest.mark.parametrize("algo", [a for a in ALGOS])
def test_algorithms_feasible_under_disruption(algo):
    ev = [("route_disruption", 1, 200, {"route_ids": ["route-gazipur-mirpur"]}),
          ("demand_spike", 1, 200, {"region_ids": ["region-dhaka"], "multiplier": 2.0}),
          ("station_outage", 1, 200, {"station_ids": ["station-coxsbazar"]})]
    tw, s, fc, p, a, r = warm(60, ev)
    ships = finalize(ALGOS[algo](build_problem(s, fc, p, a, r)), s)
    valid, rej = check(ships, s)
    assert not rej, rej
    assert all(x["route_id"] != "route-gazipur-mirpur" for x in valid)
    assert all(x["destination_station_id"] != "station-coxsbazar" for x in valid)
    for x in valid:  # the twin (simulator rules) accepts every one
        _, err = tw.submit(x["source_depot_id"], x["destination_station_id"], x["route_id"], x["fuel_type"], x["quantity"])
        assert err is None


def test_tournament_picks_and_reports_impact():
    *_, s, fc, p, a, r = warm(200, [("demand_spike", 150, 60, {"multiplier": 1.8})])
    t = tournament(s, fc, p, a, r, "mpc")
    assert t["winner"] in ALGOS
    scores = {c["algorithm"]: c["score"] for c in t["candidates"] if "score" in c}
    assert scores[t["winner"]] <= min(scores.values()) * 1.02 + 1
    assert t["expected"]["with_plan"]["unmet"] <= t["expected"]["without_action"]["unmet"]


def test_closed_loop_beats_doing_nothing():
    from scripts.bench import run
    assert run("mpc", 150)["service_level"] > run("none", 150)["service_level"] + 0.2


# ---------- resilient client ----------
def client(handler):
    return SimClient("http://sim", transport=httpx.MockTransport(handler))


INSTANCE = {"id": 1, "sim_time": "2026-01-01T00:00:00+00:00", "tick": 3, "tick_minutes": 15, "status": "PAUSED"}


def test_retries_transient_503_then_succeeds():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
        return httpx.Response(200, json=INSTANCE)

    assert asyncio.run(client(h).get("/v1/instance"))["tick"] == 3
    assert len(calls) == 3


def test_domain_rejection_not_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(409, json={"detail": {"code": "ROUTE_DISRUPTED", "message": "x"}})

    with pytest.raises(SimRejected) as e:
        asyncio.run(client(h).create_allocation({"idempotency_key": "k"}))
    assert e.value.code == "ROUTE_DISRUPTED" and len(calls) == 1


def test_breaker_opens_and_fails_fast():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED"}})

    c = client(h)

    async def go():
        for _ in range(config.BREAKER_MIN_CALLS + 3):
            with pytest.raises(SimUnavailable):
                await c.get("/v1/instance")

    asyncio.run(go())
    assert c.breaker.name == "open"
    assert len(calls) == config.BREAKER_MIN_CALLS * config.SIM_RETRIES  # later calls never hit the network


def test_flaky_simulator_does_not_trip_breaker():
    n = []

    def h(req):
        n.append(1)
        if len(n) % 2:  # every other attempt fails: retries always win
            return httpx.Response(503, json={"error": {"code": "FAULT_INJECTED"}})
        return httpx.Response(200, json=INSTANCE)

    c = client(h)

    async def go():
        for _ in range(30):
            await c.get("/v1/instance")

    asyncio.run(go())
    assert c.breaker.name == "closed"


def test_invalid_response_rejected_and_stale_flagged():
    def bad(req):
        return httpx.Response(200, json={**INSTANCE, "tick": -1})

    with pytest.raises(SimInvalid):
        asyncio.run(client(bad).get("/v1/instance"))

    c = client(lambda req: httpx.Response(200, json=INSTANCE, headers={"X-Simulator-Stale": "true"}))
    asyncio.run(c.get("/v1/instance"))
    assert c.stale
