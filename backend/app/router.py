"""Regime -> algorithm routing.

Primary: a System-One model (Laya local, or Jev via OpenRouter) answering 4 typed questions in one call.
Safety net: a deterministic rule table, used when the model is down, slow, unconfigured, or answers garbage.
Models are asked only when the situation *signature* changes; otherwise the cached answer is reused.
The other model runs in shadow so Laya vs Jev (vs rules) can be compared against tournament winners."""
import asyncio
import json
import logging
import time
from collections import OrderedDict, deque

from . import config
from . import metrics as M

log = logging.getLogger("router")

REGIMES = ("normal", "demand_spike", "supply_crisis", "route_disruption", "depot_constraint", "combined")
RULES = {"normal": "mpc", "demand_spike": "robust_lp", "supply_crisis": "mpc", "route_disruption": "mpc",
         "depot_constraint": "mpc", "combined": "rationing"}

QUESTIONS = {
    "regime": {"type": "choice", "instructions": "Which operating regime best describes this fuel supply network state?",
               "criteria": {
                   "normal": "no active disruption, stations stocked, no demand spike",
                   "demand_spike": "one or more stations or regions have elevated demand multipliers",
                   "supply_crisis": "depot stock or incoming supply is short, delayed or reduced",
                   "route_disruption": "a delivery route is disrupted or a station is in outage",
                   "depot_constraint": "a depot is constrained or closed",
                   "combined": "two or more of the above problems at the same time"}},
    "algorithm": {"type": "choice", "instructions": "Which allocation algorithm should plan the next fuel shipments?",
                  "criteria": {
                      "mpc": "multi-tick planner; best when supply timing, delays or known future events matter",
                      "lp": "single-step optimal allocation; fine for calm, normal operations",
                      "robust_lp": "plans for high (P90) demand with safety stock; best during demand spikes",
                      "rationing": "fair sharing when total supply cannot cover total demand",
                      "greedy": "simple most-urgent-first fallback",
                      "hold": "ship nothing; stations are well stocked and nothing is at risk"}},
    "severity": {"type": "score", "instructions": "How severe is the current fuel supply risk?",
                 "criteria": ["normal", "watch", "warning", "critical"]},
    "needs_human": {"type": "noul", "instructions": "Is this situation unusual, high-impact or uncertain enough that a "
                                                    "human operator should review the allocation plan?"},
}


def regime_of(flags):
    active = [r for r in REGIMES[1:-1] if r in flags]
    return "combined" if len(active) >= 2 else (active[0] if active else "normal")


def severity_of(risks, alerts):
    crit = sum(a["severity"] == "critical" for a in alerts)
    top = max((r["p_stockout"] for r in risks.values()), default=0)
    return 3 if crit else 2 if top >= 0.5 else 1 if top >= 0.2 else 0


def route_rules(flags, risks, alerts):
    regime = regime_of(flags)
    return {"source": "rules", "regime": regime, "algorithm": RULES[regime], "severity": severity_of(risks, alerts),
            "needs_human": 0.9 if regime == "combined" else 0.0, "confidence": 1.0, "latency_ms": 0.0}


def _worst_by_station(risks):
    out = {}
    for r in risks.values():
        w = out.get(r["station_id"])
        if not w or r["p_stockout"] > w["p_stockout"]:
            out[r["station_id"]] = r
    return out


def state_text(snap, flags, risks, alerts):
    """Compact JSON (< ~400 tokens) so it fits Laya's context with the question headers."""
    worst = _worst_by_station(risks)
    counts = {}
    for a in alerts:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    return json.dumps({
        "signals": sorted(flags),
        "stations": [{"id": s["id"].replace("station-", ""), "status": s["status"],
                      "demand_x": round(s["demand_multiplier"], 2), "worst_fuel": worst[s["id"]]["fuel"],
                      "p_stockout": worst[s["id"]]["p_stockout"],
                      "hours_to_stockout": worst[s["id"]]["hours_to_stockout"]} for s in snap["stations"].values()],
        "depots": [{"id": d["id"].replace("depot-", ""), "status": d["status"],
                    "stock_l": {f: round(v) for f, v in d["inventory"].items()}} for d in snap["depots"].values()],
        "disrupted_routes": [r["id"].replace("route-", "") for r in snap["routes"].values() if r["status"] != "AVAILABLE"],
        "active_events": [{"type": e["type"], "ends_in_ticks": e["end_tick"] - snap["tick"]}
                          for e in snap["events"] if e["status"] == "ACTIVE"],
        "delayed_supply": sum(a["status"] == "DELAYED" for a in snap["supply"]),
        "alerts": counts,
    }, separators=(",", ":"))


def signature(snap, flags, risks, rules):
    """Changes only when the picture meaningfully changes -> bounded model calls.
    Deliberately coarse: stations *at risk* (p>=0.5), not raw probabilities, which flicker every tick."""
    worst = _worst_by_station(risks)
    return (rules["regime"], rules["severity"], tuple(sorted(flags)),
            tuple(sorted(s for s, r in worst.items() if r["p_stockout"] >= 0.5)),
            tuple(sorted(r["id"] for r in snap["routes"].values() if r["status"] != "AVAILABLE")),
            tuple(sorted(s["id"] for s in snap["stations"].values() if s["status"] != "OPEN")),
            tuple(sorted(d["id"] for d in snap["depots"].values() if d["status"] != "OPEN")))


def parse(answers, rules):
    reg, alg = answers["regime"], answers["algorithm"]
    if reg.get("choice") not in QUESTIONS["regime"]["criteria"] or alg.get("choice") not in QUESTIONS["algorithm"]["criteria"]:
        raise ValueError(f"answer outside allowed options: {reg.get('choice')}/{alg.get('choice')}")
    sev = answers.get("severity", {}).get("score")
    return {"regime": reg["choice"], "algorithm": alg["choice"],
            "severity": int(round(sev)) if isinstance(sev, (int, float)) else rules["severity"],
            "needs_human": float(answers.get("needs_human", {}).get("noul", 0.0)),
            "confidence": round(min(reg.get("confidence", 1.0), alg.get("confidence", 1.0)), 4),
            "probabilities": {"regime": reg.get("probabilities"), "algorithm": alg.get("probabilities")}}


class Router:
    def __init__(self, clients=None, primary=config.ROUTER_PRIMARY, shadow=bool(config.ROUTER_SHADOW)):
        self.clients = {k: v for k, v in (clients or {}).items() if v}
        self.primary, self.shadow = primary, shadow
        self.cache = OrderedDict()
        self.last = {}  # source -> latest answer, scored against the next tournament winner
        self.stats = {s: {"calls": 0, "errors": 0, "cache_hits": 0, "lat": deque(maxlen=500), "agree_rules": 0,
                          "answered": 0, "tournaments": 0, "tournament_hits": 0}
                      for s in ("laya", "jev", "rules")}
        self.inflight = {}  # (source, sig) -> task
        self.failed = {}    # (source, sig) -> error text
        self.sig = None

    async def route(self, snap, flags, risks, alerts):
        """Never waits on a model: returns the cached model answer for this situation if there is one,
        otherwise the rule answer while the model is asked in the background (CPU Laya takes seconds)."""
        rules = route_rules(flags, risks, alerts)
        self.last["rules"] = {**rules, "sig": None}
        sig = self.sig = signature(snap, flags, risks, rules)
        text = None
        busy = {k[0] for k in self.inflight}
        for src in [c for c in self.clients if c == self.primary or self.shadow]:
            key = (src, sig)
            # one call in flight per model: a CPU model can't keep up with a new situation every tick
            if src not in busy and key not in self.cache and key not in self.failed:
                text = text or state_text(snap, flags, risks, alerts)
                task = asyncio.create_task(self._background(src, sig, text, rules))
                self.inflight[key] = task
                task.add_done_callback(lambda _t, k=key: self.inflight.pop(k, None))
        if self.primary == "rules":
            return rules
        if self.primary not in self.clients:
            return {**rules, "fallback": f"{self.primary} not configured"}
        key = (self.primary, sig)
        if key in self.cache:
            self.stats[self.primary]["cache_hits"] += 1
            M.ROUTER_CACHE.labels("hit").inc()
            return {**self.cache[key], "cached": True}
        if key in self.failed:
            return {**rules, "fallback": f"{self.primary} failed: {self.failed[key]}"}
        return {**rules, "fallback": f"{self.primary} answer pending"}

    async def drain(self):
        """Wait for in-flight model calls (tests / offline eval)."""
        while self.inflight:
            await asyncio.gather(*list(self.inflight.values()), return_exceptions=True)

    async def _ask(self, src, sig, text, rules):
        st = self.stats[src]
        key = (src, sig)
        M.ROUTER_CACHE.labels("miss").inc()
        st["calls"] += 1
        try:
            answers, ms, usage = await asyncio.wait_for(self.clients[src].ask(text, QUESTIONS), config.ROUTER_TIMEOUT_S)
            res = parse(answers, rules)
        except Exception:
            st["errors"] += 1
            raise
        agree = res["regime"] == rules["regime"]
        st["lat"].append(ms)
        st["answered"] += 1
        st["agree_rules"] += agree
        M.ROUTER_AGREE.labels(src, str(agree).lower()).inc()
        res.update(source=src, latency_ms=ms, usage=usage, rules_regime=rules["regime"],
                   rules_algorithm=rules["algorithm"], agrees_with_rules=agree, state=text, sig=sig)
        self.cache[key] = res
        if len(self.cache) > 256:
            self.cache.popitem(last=False)
        self.last[src] = res
        return res

    async def _background(self, src, sig, text, rules):
        try:
            await self._ask(src, sig, text, rules)
        except Exception as e:  # model down/slow/garbage: rules keep driving; retried on the next new situation
            self.failed[(src, sig)] = f"{type(e).__name__}: {str(e)[:120]}"
            if len(self.failed) > 256:
                self.failed.pop(next(iter(self.failed)))
            M.FALLBACKS.labels(f"router_{src}_failed").inc()
            log.warning("router.model_failed", extra={"event": "router.model_failed", "model": src, "error": str(e)[:200]})

    def record_outcome(self, winner):
        """Score each source's answer *for the current situation* against the tournament winner."""
        for src, res in self.last.items():
            if res.get("sig") not in (None, self.sig):
                continue  # stale answer from an earlier situation
            st = self.stats[src]
            st["tournaments"] += 1
            hit = res["algorithm"] == winner
            st["tournament_hits"] += hit
            M.ROUTER_HIT.labels(src, str(hit).lower()).inc()

    def compare(self):
        out = {}
        for src, st in self.stats.items():
            lat = sorted(st["lat"])
            out[src] = {
                "configured": src == "rules" or src in self.clients, "primary": src == self.primary,
                "calls": st["calls"], "errors": st["errors"], "cache_hits": st["cache_hits"],
                "latency_ms_p50": lat[len(lat) // 2] if lat else None,
                "latency_ms_p95": lat[int(0.95 * (len(lat) - 1))] if lat else None,
                "agreement_with_rules": round(st["agree_rules"] / st["answered"], 3) if st["answered"] else None,
                "tournament_accuracy": round(st["tournament_hits"] / st["tournaments"], 3) if st["tournaments"] else None,
                "tournaments": st["tournaments"],
            }
        return out
