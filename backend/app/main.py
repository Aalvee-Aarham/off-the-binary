import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import config
from . import metrics as M
from . import sse
from .audit import Audit
from .orchestrator import Orchestrator
from .router import Router
from .sim_client import SimClient, SimError, SimRejected
from .state import StateStore
from .systemone import SystemOneClient


class JsonLog(logging.Formatter):
    STD = set(vars(logging.makeLogRecord({})))

    def format(self, r):
        out = {"ts": round(r.created, 3), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
        out.update({k: v for k, v in vars(r).items() if k not in self.STD and not k.startswith("_")})
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, default=str)


def setup_logging():
    h = logging.StreamHandler()
    h.setFormatter(JsonLog())
    logging.basicConfig(level=logging.INFO, handlers=[h], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request lines are covered by metrics


def build_router(model_transport=None):
    laya = SystemOneClient("laya", config.LAYA_URL, model=config.LAYA_MODEL, timeout=config.ROUTER_TIMEOUT_S,
                           transport=model_transport) if config.LAYA_URL else None
    jev = SystemOneClient("jev", config.JEV_URL, api_key=config.JEV_API_KEY, model=config.JEV_MODEL,
                          timeout=config.ROUTER_TIMEOUT_S, transport=model_transport) if config.JEV_API_KEY else None
    return Router({"laya": laya, "jev": jev})


class Observe:
    """Pure ASGI request metrics (BaseHTTPMiddleware adds measurable per-request overhead)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0, status = time.perf_counter(), [500]

        async def send_wrapper(msg):
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            if route != "/metrics":
                dt = time.perf_counter() - t0
                M.HTTP_REQS.labels(scope["method"], route, str(status[0])).inc()
                M.HTTP_LAT.labels(scope["method"], route).observe(dt)
                M.WINDOW.add(dt, status[0] >= 500)


def create_app(transport=None, start_loops=True, model_transport=None):
    """transport: inject an httpx transport (tests / in-process devsim)."""

    @asynccontextmanager
    async def lifespan(app):
        setup_logging()
        sim = SimClient(config.SIM_BASE_URL, transport=transport)
        store = StateStore(sim)
        orch = Orchestrator(sim, store, Audit(config.DB_PATH), build_router(model_transport))
        app.state.sim, app.state.orch = sim, orch
        tasks = []
        if start_loops:
            tasks.append(asyncio.create_task(orch.run()))
            tasks.append(asyncio.create_task(sse.listen(config.SIM_BASE_URL, lambda n, p: orch.trigger.set(), transport)))
        yield
        for t in tasks:
            t.cancel()
        await sim.close()

    app = FastAPI(title="Fuel Ops Intelligence", version=config.VERSION, lifespan=lifespan)

    app.add_middleware(Observe)

    def orch(request: Request) -> Orchestrator:
        return request.app.state.orch

    def admin(x_admin_token: str = Header(default="")):
        if not config.ADMIN_TOKEN:
            raise HTTPException(403, "admin actions disabled: ADMIN_TOKEN not configured")
        if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
            raise HTTPException(401, "invalid admin token")
        return "operator"

    def view_or_503(o):
        if o.view is None:
            raise HTTPException(503, "no snapshot yet: simulator not reached")
        return o.view

    # ---------- read ----------
    @app.get("/api/health")
    async def health(request: Request):
        o = orch(request)
        sim_h = await o.sim.health()
        br = o.sim.breaker.name
        sim_status = "down" if not sim_h else "healthy" if br == "closed" else "degraded"
        lc = o.last_cycle
        fresh = lc["at"] and time.time() - lc["at"] < max(30, 5 * config.POLL_SECONDS)
        comps = {
            "backend_api": {"status": "healthy"},
            "database": {"status": "healthy" if o.audit.ping() else "down"},
            "simulator": {"status": sim_status, "breaker": br, "stale": bool(o.store.snap and o.store.snap["stale"]),
                          "last_error": o.store.last_error, "sim": (sim_h or {}).get("simulation")},
            "event_stream": {"status": "healthy" if M.SSE_CONNECTED._value.get() else "degraded",
                             "note": "polling fallback active" if not M.SSE_CONNECTED._value.get() else None},
            "prediction": {"status": "healthy" if o.risks is not None else "down", "mape": o.fc.mape},
            "decision_engine": {"status": "healthy" if lc["ok"] and fresh else "degraded", "last_cycle": lc,
                                "budget_mode": o.budget},
            "ml_service": await model_health(o, "laya"),
            "jev": await model_health(o, "jev"),
            "llm_pool": {"status": "not_configured"},
        }
        states = [c["status"] for c in comps.values()]
        overall = "down" if "down" in states[:4] else "degraded" if "degraded" in states else "healthy"
        return {"status": overall, "version": config.VERSION, "mode": o.mode, "degraded_mode": o.store.degraded,
                "components": comps, "api": M.WINDOW.summary(), "last_invalid_response": o.invalid}

    async def model_health(o, name):
        c = o.routing.clients.get(name)
        if not c:
            return {"status": "not_configured", "fallback": "rule table"}
        if c.disabled:
            return {"status": "down", "error": c.disabled, "fallback": "rule table"}
        if name == "laya":
            up = await c.health()
        else:  # don't spend OpenRouter credits on probes: judge by the breaker
            up = c.breaker.name == "closed"
        return {"status": "healthy" if up and c.breaker.name == "closed" else "degraded", "breaker": c.breaker.name,
                "primary": o.routing.primary == name}

    @app.get("/api/router")
    async def router_stats(request: Request):
        o = orch(request)
        return {"primary": o.routing.primary, "current": {k: v for k, v in (o.router or {}).items() if k != "state"},
                "compare": o.routing.compare()}

    class RouterBody(BaseModel):
        primary: Literal["laya", "jev", "rules"]

    @app.put("/api/router")
    async def set_router(body: RouterBody, request: Request, who: str = Depends(admin)):
        o = orch(request)
        if body.primary != "rules" and body.primary not in o.routing.clients:
            raise HTTPException(409, f"{body.primary} is not configured")
        o.routing.primary = body.primary
        logging.getLogger("api").info("router.primary", extra={"event": "router.primary", "primary": body.primary, "actor": who})
        return {"primary": body.primary}

    @app.get("/api/state")
    async def state(request: Request):
        o = orch(request)
        view_or_503(o)
        return Response(o.view_json, media_type="application/json")

    @app.get("/api/forecast")
    async def forecast(request: Request, station_id: str | None = None):
        o = orch(request)
        view_or_503(o)
        return {f"{s}/{f}": {"p50": [round(x, 1) for x in p.tolist()], "risk": o.risks[(s, f)]}
                for (s, f), p in o.paths.items() if station_id in (None, s)}

    @app.get("/api/alerts")
    async def alerts(request: Request, limit: int = Query(100, ge=1, le=1000)):
        o = orch(request)
        return {"active": list(o.det.active.values()), "history": o.audit.alerts(limit)}

    @app.get("/api/decisions")
    async def decisions(request: Request, status: str | None = None, limit: int = 50):
        return orch(request).audit.list(status, max(1, min(limit, 500)))

    @app.get("/api/decisions/{did}")
    async def decision(did: str, request: Request):
        d = orch(request).audit.get(did)
        if not d:
            raise HTTPException(404, "decision not found")
        return d

    @app.post("/api/decisions/recommend")
    async def recommend(request: Request):
        """Full pipeline on the cached snapshot, nothing executed or stored. This is the load-tested path."""
        o = orch(request)
        view_or_503(o)
        d = await o.decide(o.store.snap, execute=False)
        if d is None:
            raise HTTPException(503, "all solvers failed")
        return d

    # ---------- operator (admin) ----------
    class Shipment(BaseModel):
        source_depot_id: str = Field(min_length=1, max_length=100)
        destination_station_id: str = Field(min_length=1, max_length=100)
        route_id: str = Field(min_length=1, max_length=100)
        fuel_type: Literal["DIESEL", "PETROL", "OCTANE"]
        quantity: float = Field(gt=0, le=100000)

    class Plan(BaseModel):
        shipments: list[Shipment] = Field(min_length=1, max_length=50)

    class Note(BaseModel):
        note: str | None = Field(None, max_length=500)

    @app.post("/api/decisions/{did}/approve")
    async def approve(did: str, request: Request, who: str = Depends(admin)):
        try:
            return await orch(request).approve(did, who)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/decisions/{did}/reject")
    async def reject(did: str, request: Request, body: Note | None = None, who: str = Depends(admin)):
        try:
            return orch(request).reject(did, who, body.note if body else None)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.put("/api/decisions/{did}")
    async def edit(did: str, plan: Plan, request: Request, who: str = Depends(admin)):
        try:
            d, rejected = orch(request).edit(did, [s.model_dump() for s in plan.shipments], who)
        except ValueError as e:
            raise HTTPException(409, str(e))
        if rejected:
            raise HTTPException(422, {"rejected": rejected})
        return d

    @app.post("/api/decisions/manual")
    async def manual(plan: Plan, request: Request, who: str = Depends(admin)):
        o = orch(request)
        view_or_503(o)
        d, rejected = await o.manual([s.model_dump() for s in plan.shipments], who)
        if rejected:
            raise HTTPException(422, {"rejected": rejected})
        return d

    @app.post("/api/decisions/run")
    async def run_now(request: Request, who: str = Depends(admin)):
        return await orch(request).cycle(force=True) or {"status": "no decision (simulator unreachable)"}

    @app.post("/api/allocations/{alloc_id}/cancel")
    async def cancel(alloc_id: int, request: Request, who: str = Depends(admin)):
        try:
            return await orch(request).sim.cancel_allocation(alloc_id)
        except SimRejected as e:
            raise HTTPException(e.status or 409, {"code": e.code, "message": e.message})
        except SimError as e:
            raise HTTPException(503, {"code": e.code, "message": e.message})

    class ModeBody(BaseModel):
        mode: Literal["AUTO_GATED", "MANUAL"]

    @app.get("/api/mode")
    async def get_mode(request: Request):
        return {"mode": orch(request).mode}

    @app.put("/api/mode")
    async def set_mode(body: ModeBody, request: Request, who: str = Depends(admin)):
        orch(request).mode = body.mode
        logging.getLogger("api").info("mode.changed", extra={"event": "mode.changed", "mode": body.mode, "actor": who})
        return {"mode": body.mode}

    # ---------- chaos / simulator control (admin) ----------
    class FaultBody(BaseModel):
        type: Literal["latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"]
        duration_seconds: int = Field(60, gt=0, le=3600)
        parameters: dict = {}

    class EventBody(BaseModel):
        type: Literal["demand_spike", "route_disruption", "station_outage", "depot_constraint",
                      "shipment_delay", "supply_shortfall"]
        start_tick: int | None = Field(None, ge=0)
        duration_ticks: int = Field(12, gt=0, le=2000)
        parameters: dict = {}

    async def sim_admin(request, method, path, body=None):
        try:
            return await orch(request).sim.admin(method, path, body)
        except SimRejected as e:
            raise HTTPException(e.status or 400, {"code": e.code, "message": e.message})
        except SimError as e:
            raise HTTPException(503, {"code": e.code, "message": e.message})

    @app.post("/api/chaos/fault")
    async def fault(body: FaultBody, request: Request, who: str = Depends(admin)):
        return await sim_admin(request, "POST", "/admin/faults", body.model_dump())

    @app.post("/api/chaos/event")
    async def event(body: EventBody, request: Request, who: str = Depends(admin)):
        o = orch(request)
        b = body.model_dump()
        if b["start_tick"] is None:
            b["start_tick"] = (o.store.snap["tick"] + 1) if o.store.snap else 0
        r = await sim_admin(request, "POST", "/admin/events", b)
        o.trigger.set()
        return r

    @app.post("/api/chaos/clear")
    async def clear(request: Request, who: str = Depends(admin)):
        return await sim_admin(request, "POST", "/admin/faults/clear")

    @app.post("/api/sim/{action}")
    async def sim_control(action: Literal["run", "pause", "toggle", "step", "reset"], request: Request,
                          who: str = Depends(admin)):
        r = await sim_admin(request, "POST", f"/admin/{action}")
        orch(request).trigger.set()
        return r

    # ---------- ops ----------
    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    static = Path(__file__).parent / "static"

    @app.get("/")
    async def ui():
        return FileResponse(static / "index.html")

    @app.exception_handler(SimError)
    async def sim_error(request, e: SimError):
        return JSONResponse({"detail": {"code": e.code, "message": e.message}}, status_code=503)

    return app


app = create_app()
