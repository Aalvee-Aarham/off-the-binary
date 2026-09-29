"""Observe -> Detect -> Predict -> Decide (tournament) -> Gate -> Act -> Reconcile."""
import asyncio
import json
import logging
import time
import uuid

from . import config
from . import metrics as M
from .detect import Detector
from .forecast import Forecaster, arrivals, risks as compute_risks
from .router import Router, route_rules
from .sim_client import SimError, SimRejected
from .solvers import SolverError, check, evaluate, tournament

log = logging.getLogger("orchestrator")
TERMINAL = ("ARRIVED", "FAILED", "CANCELLED")


class Orchestrator:
    def __init__(self, sim, store, audit, routing=None):
        self.sim, self.store, self.audit = sim, store, audit
        self.routing = routing or Router()
        self.fc, self.det = Forecaster(), Detector()
        self.mode = config.MODE
        self.trigger = asyncio.Event()
        self.lock = asyncio.Lock()
        self.last_decision_tick = -10 ** 9
        self.analyzed_tick = None
        self.budget = False
        self.view = None  # cached /api/state payload, rebuilt once per cycle
        self.view_json = b"null"
        self.paths = self.arr = self.risks = None
        self.flags, self.router = set(), None
        self.last_cycle = {"ok": False, "at": None, "ms": None, "error": "not started"}
        self.invalid = None
        store.on_invalid = self.on_invalid

    # ---------- loop ----------
    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self.trigger.wait(), timeout=config.POLL_SECONDS)  # SSE hint or poll fallback
            except asyncio.TimeoutError:
                pass
            self.trigger.clear()
            try:
                await self.cycle()
            except Exception:  # the loop must survive anything
                log.exception("cycle.crashed", extra={"event": "cycle.crashed"})

    def on_invalid(self, e):
        self.invalid = {"at": time.time(), "error": f"{e.code}: {e.message}"}
        a = {"type": "sim_invalid_response", "severity": "critical", "entity": "simulator", "message": e.message[:300]}
        self.audit.alert("raised", a)
        M.ALERTS.labels(a["type"], a["severity"]).inc()

    async def cycle(self, force=False):
        async with self.lock:
            t0 = time.perf_counter()
            try:
                snap = await self.store.refresh()
            except SimError as e:
                self.last_cycle = {"ok": False, "at": time.time(), "ms": None, "error": f"{e.code}: {e.message}"}
                M.FALLBACKS.labels("sim_unavailable_cached_state").inc()
                self._build_view()
                return None
            if self.store.reset_seen:
                log.warning("sim.reset_detected", extra={"event": "sim.reset", "tick": snap["tick"]})
                self.fc.reset(), self.det.reset()
                self.last_decision_tick = -10 ** 9
                for d in self.audit.list("PENDING_APPROVAL", 200):
                    self._set_status(d, "EXPIRED", "simulator reset")
            try:
                self.fc.ingest(await self.store.demand_rows(self.fc.last_tick), snap)
            except SimError as e:  # forecast keeps its previous corrections
                log.warning("demand_history.failed", extra={"event": "demand_history.failed", "error": str(e)})
            new_tick = snap["tick"] != self.analyzed_tick
            raised = self._analyze(snap)
            self.router = await self.routing.route(snap, self.flags, self.risks, list(self.det.active.values()))
            self._reconcile(snap)
            await self._process_pending(snap)
            decision = None
            due = snap["tick"] - self.last_decision_tick >= config.DECIDE_EVERY_TICKS or any(
                a["severity"] == "critical" for a in raised)
            operator_editing = any(d.get("edited_by") for d in self.audit.list("PENDING_APPROVAL", 20))
            if force or (new_tick and due and not operator_editing):
                decision = await self.decide(snap, execute=True)
            ms = (time.perf_counter() - t0) * 1000
            self.budget = ms > config.CYCLE_BUDGET_MS
            M.CYCLE_LAT.observe(ms / 1000)
            self.last_cycle = {"ok": True, "at": time.time(), "ms": round(ms, 1), "error": None, "tick": snap["tick"]}
            self._build_view()
            return decision

    def _analyze(self, snap):
        H = config.HORIZON_TICKS
        self.paths = self.fc.paths(snap, H)
        self.arr = arrivals(snap, H)
        self.risks = compute_risks(snap, self.fc, self.paths, self.arr, H)
        raised, resolved, self.flags = self.det.run(snap, self.store.prev, self.risks, self.paths, self.fc)
        for a in raised:
            self.audit.alert("raised", a)
        for a in resolved:
            self.audit.alert("resolved", a)
        self.analyzed_tick = snap["tick"]
        self.router = route_rules(self.flags, self.risks, list(self.det.active.values()))
        return raised

    # ---------- decide ----------
    def _gate(self, shipments, snap):
        """Hard reasons never auto-execute. Soft reasons open a review window, then auto-execute (AUTO_GATED)."""
        hard, reasons, notes = [], [], []
        if self.mode == "MANUAL":
            hard.append("manual approval mode")
        if snap["stale"]:
            hard.append("simulator reports stale data")
        if self.store.degraded:
            hard.append("degraded: serving cached state")
        # Model doubts only gate when the model's pick drives the plan unverified (budget mode = partial tournament).
        # After a full twin tournament they are recorded as notes: the twin, not the model, vouched for the plan.
        doubts = []
        if self.router["confidence"] < config.GATE_MIN_CONFIDENCE:
            doubts.append(f"router confidence {self.router['confidence']:.2f} < {config.GATE_MIN_CONFIDENCE}")
        if self.router.get("agrees_with_rules") is False:
            doubts.append(f"{self.router['source']} says {self.router['regime']}, rules say {self.router['rules_regime']}")
        if self.router["needs_human"] > 0.5:
            (reasons if self.router["source"] == "rules" else doubts).append(f"router flags review ({self.router['regime']})")
        (reasons if self.budget else notes).extend(doubts)
        for s in shipments:
            inv = snap["depots"][s["source_depot_id"]]["inventory"][s["fuel_type"]]
            if s["quantity"] > config.GATE_MAX_DEPOT_SHARE * inv:
                reasons.append(f"{s['quantity']:.0f} L is >{config.GATE_MAX_DEPOT_SHARE:.0%} of {s['source_depot_id']} {s['fuel_type']}")
                break
        return {"auto": not (hard or reasons), "hard": bool(hard), "reasons": hard + reasons, "notes": notes,
                "review_window_ticks": None if hard else config.REVIEW_WINDOW_TICKS}

    async def decide(self, snap, execute=True):
        router = dict(self.router)
        try:
            t = await asyncio.to_thread(tournament, snap, self.fc, self.paths, self.arr, self.risks,
                                        router["algorithm"], self.budget)
        except SolverError as e:
            M.FALLBACKS.labels("all_solvers_failed").inc()
            log.error("decide.failed", extra={"event": "decide.failed", "error": str(e)})
            return None
        gate = self._gate(t["shipments"], snap)
        top = sorted(self.risks.values(), key=lambda r: -r["p_stockout"])[:5]
        d = {"id": uuid.uuid4().hex[:12], "created_at": time.time(), "tick": snap["tick"], "sim_time": snap["sim_time"],
             "mode": self.mode, "router": router, "algorithm": t["winner"], "best_algorithm": t["best"],
             "router_hit": t["router_hit"], "budget_mode": self.budget,
             "candidates": [{k: c.get(k) for k in ("algorithm", "score", "solve_ms", "error")} |
                            {"shipments": len(c["shipments"]), "p50": (c.get("scenarios") or {}).get("p50")}
                            for c in t["candidates"]],
             "shipments": t["shipments"], "expected": t["expected"], "top_risks": top,
             "alerts": list(self.det.active.values())[:20], "gate": gate, "status": "PROPOSED",
             "actor": None, "results": []}
        if not execute:
            return d
        self.last_decision_tick = snap["tick"]
        self.routing.record_outcome(t["winner"])
        M.DECISIONS.labels(t["winner"], "auto" if gate["auto"] else "human").inc()
        if not t["shipments"]:
            d["status"] = "NO_ACTION"
            if router["regime"] != "normal" or self.det.active:
                self.audit.save(d)
            return d
        for old in self.audit.list("PENDING_APPROVAL", 20):  # fresher plan replaces unreviewed ones
            if not old.get("edited_by"):
                self._set_status(old, "SUPERSEDED", f"replaced by {d['id']}")
        if gate["auto"]:
            await self.execute(d, "auto")
        else:
            d["status"] = "PENDING_APPROVAL"
            self.audit.save(d)
            log.info("decision.pending", extra={"event": "decision.pending", "decision_id": d["id"], "reasons": gate["reasons"]})
        return d

    # ---------- act ----------
    async def execute(self, d, actor):
        d["actor"], d["status"], d["executed_at"] = actor, "EXECUTING", time.time()
        self.audit.save(d)  # persist intent first: idempotency keys survive a crash mid-submit
        ok = 0
        for i, s in enumerate(d["shipments"]):
            body = {"idempotency_key": f"fo-{d['id']}-{i}", **s}
            try:
                a = await self.sim.create_allocation(body)
                d["results"].append({"i": i, "ok": True, "allocation_id": a["id"], "status": a["status"]})
                M.SHIPMENTS.labels("accepted").inc()
                ok += 1
            except SimRejected as e:
                d["results"].append({"i": i, "ok": False, "code": e.code, "message": e.message})
                M.SHIPMENTS.labels(f"rejected_{e.code}").inc()
            except SimError as e:
                d["results"].append({"i": i, "ok": False, "code": e.code, "message": e.message, "retryable": True})
                M.SHIPMENTS.labels("unavailable").inc()
        d["status"] = "EXECUTED" if ok == len(d["shipments"]) else "PARTIAL" if ok else "FAILED"
        self.audit.save(d)
        log.info("decision.executed", extra={"event": "decision.executed", "decision_id": d["id"], "actor": actor,
                                             "status": d["status"], "algorithm": d["algorithm"]})
        return d

    def _set_status(self, d, status, note=None, actor=None):
        d["status"] = status
        if note:
            d["note"] = note
        if actor:
            d["actor"] = actor
        self.audit.save(d)
        return d

    async def approve(self, did, actor="operator"):
        d = self.audit.get(did)
        if not d or d["status"] != "PENDING_APPROVAL":
            raise ValueError("decision is not pending approval")
        snap = self.store.snap
        try:
            snap = await self.store.refresh()
        except SimError:
            pass  # check against cached state; the simulator re-validates anyway
        valid, rejected = check(d["shipments"], snap)
        d["shipments"], d["dropped_on_approve"] = valid, rejected
        return await self.execute(d, actor)

    def reject(self, did, actor="operator", note=None):
        d = self.audit.get(did)
        if not d or d["status"] != "PENDING_APPROVAL":
            raise ValueError("decision is not pending approval")
        return self._set_status(d, "REJECTED", note, actor)

    def edit(self, did, shipments, actor="operator"):
        d = self.audit.get(did)
        if not d or d["status"] != "PENDING_APPROVAL":
            raise ValueError("decision is not pending approval")
        valid, rejected = check(shipments, self.store.snap)
        if rejected:
            return None, rejected
        d.update(shipments=valid, edited_by=actor, edited_at=time.time())
        d["expected"]["with_plan"] = evaluate(valid, self.store.snap, self.fc)
        self.audit.save(d)
        return d, []

    async def manual(self, shipments, actor="operator"):
        """Operator-authored plan (override). Constraint-checked, then executed."""
        snap = self.store.snap
        valid, rejected = check(shipments, snap)
        if rejected:
            return None, rejected
        d = {"id": uuid.uuid4().hex[:12], "created_at": time.time(), "tick": snap["tick"], "sim_time": snap["sim_time"],
             "mode": self.mode, "router": {"source": "operator", "regime": "manual"}, "algorithm": "manual",
             "shipments": valid, "expected": {"with_plan": evaluate(valid, snap, self.fc)},
             "gate": {"auto": False, "reasons": ["operator override"]}, "status": "PROPOSED", "results": []}
        return await self.execute(d, actor), []

    # ---------- reconcile ----------
    def _reconcile(self, snap):
        by_id = {a["id"]: a for a in snap["allocations"]}
        for d in self.audit.list("EXECUTED", 100) + self.audit.list("PARTIAL", 100):
            ids = [r["allocation_id"] for r in d["results"] if r.get("ok")]
            states = {str(i): by_id[i]["status"] for i in ids if i in by_id}
            if states != d.get("allocation_states"):
                d["allocation_states"] = states
                if states and all(s in TERMINAL for s in states.values()):
                    d["closed_tick"] = snap["tick"]
                    d["status"] = "COMPLETED" if d["status"] == "EXECUTED" else "COMPLETED_PARTIAL"
                self.audit.save(d)

    async def _process_pending(self, snap):
        """AUTO_GATED: soft-gated plans execute once their review window passes untouched. Everything else expires."""
        for d in self.audit.list("PENDING_APPROVAL", 50):
            age = snap["tick"] - d["tick"]
            soft = not d["gate"].get("hard") and not d.get("edited_by") and self.mode == "AUTO_GATED"
            if soft and not snap["stale"] and not self.store.degraded and age >= config.REVIEW_WINDOW_TICKS:
                valid, rejected = check(d["shipments"], snap)
                d["shipments"], d["dropped_on_approve"] = valid, rejected
                d["note"] = f"no operator action within {config.REVIEW_WINDOW_TICKS}-tick review window"
                M.FALLBACKS.labels("review_window_auto_execute").inc()
                await self.execute(d, "auto-after-review-window")
            elif age > config.PLAN_TTL_TICKS:
                self._set_status(d, "EXPIRED", f"not approved within {config.PLAN_TTL_TICKS} ticks")

    # ---------- read model ----------
    def _build_view(self):
        snap = self.store.snap
        if not snap:
            self.view = None
            return
        self.view = {
            "tick": snap["tick"], "sim_time": snap["sim_time"], "sim_status": snap["status"],
            "stale": snap["stale"], "degraded": self.store.degraded, "snapshot_age_s": round(self.store.age or 0, 1),
            "last_error": self.store.last_error, "mode": self.mode, "router": self.router,
            "metrics": snap["metrics"], "depots": list(snap["depots"].values()), "stations": list(snap["stations"].values()),
            "routes": list(snap["routes"].values()),
            "supply_upcoming": [a for a in snap["supply"] if a["status"] != "ARRIVED"][:10],
            "events": [e for e in snap["events"] if e["status"] != "RESOLVED"],
            "in_flight": [a for a in snap["allocations"] if a["status"] in ("PENDING", "IN_TRANSIT")],
            "risks": sorted((self.risks or {}).values(), key=lambda r: -r["p_stockout"]),
            "alerts": list(self.det.active.values()),
            "forecast_mape": self.fc.mape,
        }
        self.view_json = json.dumps(self.view, default=str).encode()  # serialized once per cycle, not per request
        M.SNAPSHOT_AGE.set(self.store.age or 0)
