"""Local stand-in for the organizer simulator (same /v1 + /admin API, faults included), backed by the twin.
Dev/CI only: `uvicorn app.devsim:app --port 8000`. The real image is always the source of truth."""
import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .world import Twin, baseline_world

SPEED = float(os.getenv("SIMULATION_SPEED", "8"))


class Sim:
    def __init__(self):
        self.reset()
        self.subs = []

    def reset(self):
        self.tw = Twin(baseline_world())
        self.status = os.getenv("SIMULATOR_START_MODE", "paused").upper()
        self.keys, self.faults = {}, []

    def publish(self, name, data):
        for q in list(self.subs):
            try:
                q.put_nowait((name, data))
            except asyncio.QueueFull:
                self.subs.remove(q)  # slow consumer silently dropped, like the real one

    def step(self):
        r = self.tw.step()
        self.publish("simulation.tick", r)
        return r

    def fault(self, kind):
        now = time.time()
        self.faults = [f for f in self.faults if f["end"] > now]
        return next((f for f in self.faults if f["type"] == kind), None)


S = Sim()


@asynccontextmanager
async def lifespan(app):
    async def runner():
        while True:
            await asyncio.sleep(1 / SPEED)
            if S.status == "RUNNING":
                S.step()

    t = asyncio.create_task(runner())
    yield
    t.cancel()


app = FastAPI(title="devsim", lifespan=lifespan)


def err(status, code, msg=""):
    return JSONResponse({"detail": {"code": code, "message": msg}}, status_code=status)


@app.middleware("http")
async def faults(request: Request, call_next):
    p = request.url.path
    if p.startswith("/v1/") and p != "/v1/health":
        if f := S.fault("latency"):
            await asyncio.sleep(f["parameters"].get("delay_ms", 500) / 1000)
        if S.fault("unavailable"):
            return JSONResponse({"error": {"code": "FAULT_INJECTED", "message": "Simulator API temporarily unavailable."}}, 503)
        if (f := S.fault("error_rate")) and random.random() < f["parameters"].get("rate", 0.25):
            return JSONResponse({"error": {"code": "FAULT_INJECTED", "message": "Injected transient API error."}}, 503)
        if p == "/v1/stream" and S.fault("stream_disconnect"):
            return JSONResponse({"detail": {"code": "FAULT_INJECTED"}}, 503)
    resp = await call_next(request)
    if p.startswith("/v1/") and request.method == "GET" and S.fault("stale_data"):
        resp.headers["X-Simulator-Stale"] = "true"
    return resp


@app.get("/v1/health")
def health():
    return {"status": "ok", "database": "ok", "simulation": {"status": S.status, "tick": S.tw.tick}}


@app.get("/v1/instance")
def instance():
    t = S.tw
    return {"id": 1, "scenario_id": "baseline", "scenario_version": "1.0", "seed": t.seed,
            "sim_time": t.sim_time.isoformat(), "tick": t.tick, "tick_minutes": t.tick_minutes, "status": S.status}


@app.get("/v1/regions")
def regions():
    return list(S.tw.regions.values())


@app.get("/v1/depots")
def depots():
    return list(S.tw.depots.values())


@app.get("/v1/stations")
def stations():
    return list(S.tw.stations.values())


@app.get("/v1/depots/{eid}")
def depot(eid: str):
    return S.tw.depots.get(eid) or err(404, "NOT_FOUND")


@app.get("/v1/stations/{eid}")
def station(eid: str):
    return S.tw.stations.get(eid) or err(404, "NOT_FOUND")


@app.get("/v1/routes")
def routes():
    return list(S.tw.routes.values())


@app.get("/v1/supply-arrivals")
def supply():
    return sorted(S.tw.supply, key=lambda a: a["planned_tick"])


@app.get("/v1/events")
def events():
    return sorted(S.tw.events, key=lambda e: -e["id"])


@app.get("/v1/allocations")
def allocations():
    return sorted(S.tw.allocations, key=lambda a: -a["id"])


@app.get("/v1/demand-history")
def demand_history(station_id: str | None = None, limit: int = 200):
    rows = [r for r in S.tw.demand_log if station_id in (None, r["station_id"])]
    return list(reversed(rows[-max(1, min(limit, 2000)):]))


@app.get("/v1/metrics")
def metrics():
    return S.tw.metrics()


@app.post("/v1/allocations", status_code=201)
async def create(request: Request):
    b = await request.json()
    try:
        key, qty = b["idempotency_key"], float(b["quantity"])
        assert 1 <= len(key) <= 150 and qty > 0 and b["fuel_type"] in ("DIESEL", "PETROL", "OCTANE")
    except (KeyError, ValueError, TypeError, AssertionError):
        return JSONResponse({"detail": [{"msg": "invalid body"}]}, 422)
    if key in S.keys:
        body, a = S.keys[key]
        return a if body == b else err(409, "IDEMPOTENCY_KEY_MISMATCH")
    a, code = S.tw.submit(b["source_depot_id"], b["destination_station_id"], b["route_id"], b["fuel_type"], qty, key)
    if code:
        return err(404 if code == "NOT_FOUND" else 409, code)
    S.keys[key] = (b, a)
    S.publish("allocation.status_changed", a)
    return a


@app.post("/v1/allocations/{aid}/cancel")
def cancel(aid: int):
    a, code = S.tw.cancel(aid)
    return err(404 if code == "ALLOCATION_NOT_FOUND" else 409, code) if code else a


@app.get("/v1/stream")
async def stream():
    q = asyncio.Queue(maxsize=200)
    S.subs.append(q)

    async def gen():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    name, data = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if q in S.subs:
                S.subs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/admin/run")
def run():
    S.status = "RUNNING"
    return {"status": S.status}


@app.post("/admin/pause")
def pause():
    S.status = "PAUSED"
    return {"status": S.status}


@app.post("/admin/toggle")
def toggle():
    S.status = "PAUSED" if S.status == "RUNNING" else "RUNNING"
    return {"status": S.status}


@app.post("/admin/step")
def step():
    return S.step()


@app.post("/admin/reset")
def reset():
    S.reset()
    S.publish("simulator.notice", {"message": "Simulation reset"})
    return {"status": "reset"}


@app.post("/admin/events", status_code=201)
async def add_event(request: Request):
    b = await request.json()
    return S.tw.add_event(b["type"], int(b["start_tick"]), int(b["duration_ticks"]), b.get("parameters") or {})


@app.post("/admin/faults", status_code=201)
async def add_fault(request: Request):
    b = await request.json()
    f = {"id": len(S.faults) + 1, "type": b["type"], "parameters": b.get("parameters") or {},
         "end": time.time() + int(b["duration_seconds"]), "active": True}
    S.faults.append(f)
    return f


@app.post("/admin/faults/clear")
def clear():
    S.faults = []
    return {"status": "cleared"}
