"""Observe -> Detect -> Predict -> Decide (tournament) -> Gate -> Act -> Reconcile."""
import asyncio
import json
import logging
import time
import uuid

from . import config
from . import metrics as M
from .detect import Detector, supply_outlook
from .world import FUELS
from .forecast import Forecaster, arrivals, risks as compute_risks
from .bandit import Bandit
from .policy import load_active
from .router import Router, route_rules
from .sim_client import SimError, SimRejected
from .llm_pool import SYSTEM, LLMUnavailable, explanation_prompt, incident_prompt, template_explanation, template_incident
from .solvers import ALGOS, SolverError, check, doomed_routes, evaluate, tournament

log = logging.getLogger("orchestrator")
TERMINAL = ("ARRIVED", "FAILED", "CANCELLED")


def binding_constraints(ships, snap):
    """What limits this plan (D8): depot dispatch and stock left, station headroom left."""
    from .detect import dispatch_used
    out, by_depot, by_station = [], {}, {}
    for s in ships:
        by_depot[s["source_depot_id"]] = by_depot.get(s["source_depot_id"], 0) + s["quantity"]
        k = (s["destination_station_id"], s["fuel_type"])
        by_station[k] = by_station.get(k, 0) + s["quantity"]
    for did, q in by_depot.items():
        d = snap["depots"][did]
        used, cap = dispatch_used(snap, did) + q, d["dispatch_capacity_per_tick"]
        out.append(f"{did} dispatch {used:,.0f}/{cap:,.0f} L this tick ({used / cap:.0%})" + (" - BINDING" if used >= 0.95 * cap else ""))
    for (sid, f), q in by_station.items():
        s = snap["stations"][sid]
        head = s["capacity"][f] - s["inventory"][f] - q
        out.append(f"{sid} {f} headroom after plan {head:,.0f} L" + (" - BINDING" if head < 0.05 * s["capacity"][f] else ""))
    return out
BUG_CODES = ("IDEMPOTENCY_KEY_MISMATCH", "ROUTE_MISMATCH", "NOT_FOUND", "VALIDATION_ERROR")


class Orchestrator:
    def __init__(self, sim, store, audit, routing=None, llm=None):
        self.sim, self.store, self.audit = sim, store, audit
        self.routing = routing or Router()
        self.llm = llm  # optional; every text has a deterministic template first
        self.bg = set()
        self.seen_active = set()
        self.policy, self.policy_version = load_active()  # PPO; None -> tournament runs without it
        self.bandit = Bandit([*ALGOS, "ppo"])
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
        self.last_sse_tick = time.time()
        self.epoch = int(time.time())  # demand history / allocation ids are per simulator epoch (reset -> new epoch)
        self.incident_start, self.last_recovery = None, None
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
                self.seen_active.clear()
                self.epoch, self.incident_start = int(time.time()), None
                self.last_decision_tick = -10 ** 9
                for d in self.audit.list("PENDING_APPROVAL", 200):
                    self._set_status(d, "EXPIRED", "simulator reset")
                for st in ("EXECUTED", "PARTIAL", "EXECUTING"):  # allocation ids restart at 1 after a reset
                    for d in self.audit.list(st, 500):
                        self._set_status(d, "EPOCH_ENDED", "simulator reset; allocation ids no longer refer to this plan")
            try:
                rows = await self.store.demand_rows(self.fc.last_tick)
                self.audit.store_demand(self.epoch, rows)
                self.fc.ingest(rows, snap)
            except SimError as e:  # forecast keeps its previous corrections
                log.warning("demand_history.failed", extra={"event": "demand_history.failed", "error": str(e)})
            new_tick = snap["tick"] != self.analyzed_tick
            raised = self._analyze(snap)
            self.router = await self.routing.route(snap, self.flags, self.risks, list(self.det.active.values()))
            self._reconcile(snap)
            await self._cancel_doomed(snap)
            await self._process_pending(snap)
            decision = None
            due = snap["tick"] - self.last_decision_tick >= config.DECIDE_EVERY_TICKS or any(
                a["severity"] == "critical" for a in raised)
            pending = self.audit.list("PENDING_APPROVAL", 20)
            # an operator is working on a plan: edited, or MANUAL mode (never yank a plan out from under a reviewer)
            operator_owns = any(d.get("edited_by") for d in pending) or (self.mode == "MANUAL" and pending)
            if force or (new_tick and due and not operator_owns):
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
        crit = any(a["severity"] == "critical" for a in self.det.active.values())
        M.INCIDENT_ACTIVE.set(int(crit))
        if crit and self.incident_start is None:
            self.incident_start = snap["tick"]
        elif not crit and self.incident_start is not None:
            self.last_recovery = {"from_tick": self.incident_start, "to_tick": snap["tick"],
                                  "ticks": snap["tick"] - self.incident_start}
            M.RECOVERY_TICKS.set(self.last_recovery["ticks"])
            self.audit.alert("recovered", {"type": "recovery", "severity": "info", "entity": "network", "tick": snap["tick"],
                                           "message": f"no critical alerts after {self.last_recovery['ticks']} ticks "
                                                      f"(since tick {self.incident_start})"})
            self.incident_start = None
        self.analyzed_tick = snap["tick"]
        self.router = route_rules(self.flags, self.risks, list(self.det.active.values()))
        for e in snap["events"]:  # incident log: one summary per event activation
            if e["status"] == "ACTIVE" and e["id"] not in self.seen_active:
                self.seen_active.add(e["id"])
                self._incident(e, snap)
        return raised

    # ---------- decide ----------
    def _gate(self, shipments, snap):
        """Hard reasons never auto-execute. Soft reasons open a review window, then auto-execute (AUTO_GATED)."""
        hard, reasons, notes = [], [], []
        if self.mode == "MANUAL":
            hard.append("manual approval mode")
        if snap["stale"]:  # soft: stale only flags GETs; the simulator still validates every POST against true state
            reasons.append("simulator reports stale data")
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
                                        router["algorithm"], self.budget, self.policy, self.bandit, router["regime"])
        except SolverError as e:
            M.FALLBACKS.labels("all_solvers_failed").inc()
            log.error("decide.failed", extra={"event": "decide.failed", "error": str(e)})
            return None
        gate = self._gate(t["shipments"], snap)
        win = next(c for c in t["candidates"] if c["algorithm"] == t["winner"])
        sc = win.get("scenarios") or {}
        if sc and sc["p90"]["unmet"] - sc["p50"]["unmet"] > 1000:
            gate["notes"].append(f"plan is sensitive to demand uncertainty: P90 unmet {sc['p90']['unmet']:,.0f} L "
                                 f"vs P50 {sc['p50']['unmet']:,.0f} L")
        top = sorted(self.risks.values(), key=lambda r: -r["p_stockout"])[:5]
        d = {"id": uuid.uuid4().hex[:12], "created_at": time.time(), "tick": snap["tick"], "sim_time": snap["sim_time"],
             "mode": self.mode, "router": router, "algorithm": t["winner"], "best_algorithm": t["best"],
             "router_hit": t["router_hit"], "budget_mode": self.budget, "ppo_version": self.policy_version,
             "candidates": [{k: c.get(k) for k in ("algorithm", "score", "solve_ms", "error")} |
                            {"shipments": len(c["shipments"]), "p50": (c.get("scenarios") or {}).get("p50")}
                            for c in t["candidates"]],
             "shipments": t["shipments"], "expected": t["expected"], "top_risks": top,
             "constraints": binding_constraints(t["shipments"], snap),
             "alerts": list(self.det.active.values())[:20], "gate": gate, "status": "PROPOSED",
             "actor": None, "results": []}
        if not execute:
            return d
        self.last_decision_tick = snap["tick"]
        self.routing.record_outcome(t["winner"])
        if not self.budget:  # only full tournaments are fair observations for the bandit
            self.bandit.update(router["regime"], t["candidates"])
        M.DECISIONS.labels(t["winner"], "auto" if gate["auto"] else "human").inc()
        if not t["shipments"]:
            d["status"] = "NO_ACTION"
            if router["regime"] != "normal" or self.det.active:
                self.audit.save(d)
            return d
        for old in self.audit.list("PENDING_APPROVAL", 20):  # fresher plan replaces unreviewed ones
            if not old.get("edited_by"):
                self._set_status(old, "SUPERSEDED", f"replaced by {d['id']}")
        d["explanation"] = {"text": template_explanation(d), "source": "template"}
        if gate["auto"]:
            await self.execute(d, "auto")
        else:
            d["status"] = "PENDING_APPROVAL"
            self.audit.save(d)
            self._spawn(self._llm_explain(d["id"]))  # a human will read this one: worth an LLM call
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
                if not ok:
                    d["lag_ticks"] = a["created_tick"] - d["tick"]
                    M.DECISION_LAG.set(d["lag_ticks"])
                ok += 1
            except SimRejected as e:
                d["results"].append({"i": i, "ok": False, "code": e.code, "message": e.message})
                M.SHIPMENTS.labels(f"rejected_{e.code}").inc()
                if e.code in BUG_CODES:  # our request was malformed: never retry, page someone
                    M.INTEGRATION_BUGS.labels(e.code).inc()
                    self.audit.alert("raised", {"type": "integration_bug", "severity": "critical", "entity": "executor",
                                                "tick": d["tick"], "message": f"{e.code}: {e.message}"[:300]})
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

    # ---------- LLM (background, never on the decision path) ----------
    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)

    async def explain(self, d, use_llm=True):
        """LLM rewrite of the template when available; the template otherwise. Cached by prompt."""
        if use_llm and self.llm and self.llm.configured:
            try:
                r = await self.llm.complete(SYSTEM, explanation_prompt(d), purpose="explanation")
                return {"text": r["text"], "source": f"llm:{r['provider']}", "cached": r["cached"]}
            except LLMUnavailable as e:
                return {"text": template_explanation(d), "source": "template", "llm_error": str(e)[:200]}
        return {"text": template_explanation(d), "source": "template"}

    async def _llm_explain(self, did):
        d = self.audit.get(did)
        if d:
            ex = await self.explain(d)
            d = self.audit.get(did)  # may have changed while we waited
            if d:
                d["explanation"] = ex
                self.audit.save(d)

    def _incident(self, e, snap):
        risks = sorted((self.risks or {}).values(), key=lambda r: -r["p_stockout"])
        text = template_incident(e, snap, risks)
        self.audit.alert("incident", {"type": f"incident:{e['type']}", "severity": "info", "entity": f"event-{e['id']}",
                                      "tick": snap["tick"], "message": text})
        if self.llm and self.llm.configured:
            async def rewrite():
                try:
                    r = await self.llm.complete(SYSTEM, incident_prompt(e, snap, risks), max_tokens=250, purpose="incident")
                    self.audit.alert("incident", {"type": f"incident:{e['type']}", "severity": "info",
                                                  "entity": f"event-{e['id']}", "tick": snap["tick"],
                                                  "message": f"[{r['provider']}] {r['text']}"[:1500]})
                except LLMUnavailable:
                    pass  # the template summary is already logged
            self._spawn(rewrite())

    async def _cancel_doomed(self, snap):
        """A PENDING allocation on a route that is (or is about to be) disrupted will FAIL and lose its fuel.
        Cancelling refunds the depot, and the next plan reroutes."""
        doomed = doomed_routes(snap) | {r["id"] for r in snap["routes"].values() if r["status"] != "AVAILABLE"}
        for a in snap["allocations"]:
            if a["status"] == "PENDING" and a["route_id"] in doomed:
                try:
                    await self.sim.cancel_allocation(a["id"])
                    M.AUTO_CANCELS.inc()
                    log.info("allocation.auto_cancelled", extra={"event": "allocation.auto_cancelled",
                                                                 "allocation_id": a["id"], "route": a["route_id"]})
                except SimError as e:  # already departed (CANNOT_CANCEL) or sim down: nothing more to do
                    log.info("allocation.cancel_failed", extra={"event": "allocation.cancel_failed", "error": str(e)})

    def on_sse(self, name, payload):
        """SSE is a hint: wake the loop. Also surface simulator crash notices and feed the tick watchdog."""
        if name in ("simulation.tick", "sse.connected"):  # a fresh connection gets a full grace period
            self.last_sse_tick = time.time()
        if name == "simulator.notice" and payload.get("level") == "error":
            a = {"type": "simulator_error", "severity": "critical", "entity": "simulator",
                 "message": str(payload.get("message"))[:300]}
            self.audit.alert("raised", a)
            M.ALERTS.labels(a["type"], a["severity"]).inc()
        self.trigger.set()

    def sse_silent(self):
        """True when the simulator is RUNNING but no tick event arrived for 3s: a silently dropped subscriber."""
        snap = self.store.snap
        return bool(snap and snap["status"] == "RUNNING" and time.time() - self.last_sse_tick > 3.0)

    async def _process_pending(self, snap):
        """AUTO_GATED: soft-gated plans execute once their review window passes untouched. Everything else expires."""
        for d in self.audit.list("PENDING_APPROVAL", 50):
            age = snap["tick"] - d["tick"]
            soft = not d["gate"].get("hard") and not d.get("edited_by") and self.mode == "AUTO_GATED"
            if soft and not self.store.degraded and age >= config.REVIEW_WINDOW_TICKS:
                valid, rejected = check(d["shipments"], snap)
                d["shipments"], d["dropped_on_approve"] = valid, rejected
                d["note"] = f"no operator action within {config.REVIEW_WINDOW_TICKS}-tick review window"
                M.FALLBACKS.labels("review_window_auto_execute").inc()
                await self.execute(d, "auto-after-review-window")
            elif age > config.PLAN_TTL_TICKS:
                self._set_status(d, "EXPIRED", f"not approved within {config.PLAN_TTL_TICKS} ticks")

    # ---------- read model ----------
    def _regions(self, snap):
        out = []
        for rid, reg in snap["regions"].items():
            sts = [s for s in snap["stations"].values() if s["region_id"] == rid]
            rk = [r for r in (self.risks or {}).values() if r["station_id"] in {s["id"] for s in sts}]
            out.append({"id": rid, "name": reg["name"], "stations": len(sts),
                        "stock": {f: round(sum(s["inventory"][f] for s in sts)) for f in FUELS},
                        "demand_4h": {f: round(sum(r["demand_4h"] for r in rk if r["fuel"] == f)) for f in FUELS},
                        "worst_p_stockout": max((r["p_stockout"] for r in rk), default=0),
                        "outages": [s["id"] for s in sts if s["status"] != "OPEN"]})
        return out

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
            "scenario": {"id": snap.get("scenario_id"), "seed": snap.get("seed"), "epoch": self.epoch},
            "regions": self._regions(snap),
            "supply_outlook": supply_outlook(snap, self.paths) if self.paths else [],
            "incident": {"active_since_tick": self.incident_start, "last_recovery": self.last_recovery},
            "alerts": list(self.det.active.values()),
            "forecast_mape": self.fc.mape,
        }
        self.view_json = json.dumps(self.view, default=str).encode()  # serialized once per cycle, not per request
        M.SNAPSHOT_AGE.set(self.store.age or 0)
