"""Allocation algorithms + constraint checker + digital-twin tournament.

Every algorithm returns raw (route_id, fuel, liters) triples; `finalize` rounds/splits, `check` mirrors the
simulator's validation (guide 5.2) so nothing invalid is ever submitted, `tournament` scores plans on the twin.
"""
import math
import time
from types import SimpleNamespace

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from . import config
from . import metrics as M
from .detect import dispatch_used
from .state import world_from_snapshot
from .world import FUELS, Twin

COVER = config.COVER_TICKS  # ticks of demand a delivery should cover
USABLE_DEPOT = ("OPEN", "CONSTRAINED")
SCENARIOS = (("p50", 1.0), ("p90", 1.15), ("spike", 1.5))


class SolverError(Exception):
    pass


def build_problem(snap, fc, paths, arr, risks, H=config.HORIZON_TICKS):
    depots = {}
    for d in snap["depots"].values():
        factor = config.CONSTRAINED_DISPATCH_FACTOR if d["status"] == "CONSTRAINED" else 1.0
        left = d["dispatch_capacity_per_tick"] * factor - dispatch_used(snap, d["id"]) if d["status"] in USABLE_DEPOT else 0
        depots[d["id"]] = {"inv": dict(d["inventory"]), "disp": max(0.0, left), "cap": d["dispatch_capacity_per_tick"]}
    routes = [r for r in snap["routes"].values() if r["status"] == "AVAILABLE"
              and snap["depots"][r["source_depot_id"]]["status"] in USABLE_DEPOT
              and snap["stations"][r["destination_station_id"]]["status"] == "OPEN"]
    sig = {(s["id"], f): fc.sigma(s, f) for s in snap["stations"].values() for f in FUELS}
    return SimpleNamespace(snap=snap, H=H, depots=depots, routes=routes, demand=paths, arr=arr, risks=risks,
                           sigma=sig, zeros=np.zeros(H + 1))


def needs(P, scale=1.0, z=0.0):
    """(station, fuel) -> need, headroom, fastest transit. Need = cover target minus what will be there."""
    out = {}
    for s in P.snap["stations"].values():
        rts = [r for r in P.routes if r["destination_station_id"] == s["id"]]
        if not rts:
            continue
        tmin = min(r["transit_ticks"] for r in rts)
        k = 1 + tmin  # created now -> departs next tick -> lands k ticks from now
        for f in FUELS:
            d = P.demand[(s["id"], f)] * scale
            a = P.arr.get((s["id"], f), P.zeros)
            inv, cap = s["inventory"][f], s["capacity"][f]
            pos_before = max(0.0, inv + a[1:k + 1].sum() - d[:k - 1].sum())
            cover = d[k - 1:k - 1 + COVER].sum()
            zk = z.get((s["id"], f), 0.0) if isinstance(z, dict) else z
            target = cover * (1 + zk * P.sigma[(s["id"], f)])
            later = a[k + 1:k + COVER].sum()
            head = max(0.0, min(cap - inv, cap - pos_before))
            out[(s["id"], f)] = (min(max(0.0, target - pos_before - later), head), head, tmin)
    return out


def _lp(P, N, *, fair=False, min_in=None, weights=None):
    xs = [(r, f) for r in P.routes for f in FUELS]
    keys = [k for k, v in N.items() if v[0] > 1]
    if not xs or (not keys and not min_in):
        return [], 0.0
    nx, nk = len(xs), len(keys)
    tmin = {sid: v[2] for (sid, _), v in N.items()}
    c = [0.002 * r["transit_ticks"] + 0.02 * (r["transit_ticks"] - tmin.get(r["destination_station_id"], 0)) for r, _ in xs]
    if fair:
        c += [-1000.0]
        bounds = [(0, None)] * nx + [(0, 1)]
    else:
        c += [(weights or {}).get(k, 1 + 4 * P.risks[k]["p_stockout"]) for k in keys]
        bounds = [(0, None)] * (nx + nk)
    nv = len(c)
    A, b = [], []

    def row(entries, rhs):
        r = np.zeros(nv)
        for i, v in entries:
            r[i] += v
        A.append(r)
        b.append(rhs)

    into = {}
    for i, (r, f) in enumerate(xs):
        into.setdefault((r["destination_station_id"], f), []).append(i)
    for j, k in enumerate(keys):
        ins = [(i, -1.0) for i in into.get(k, [])]
        if fair:
            row(ins + [(nx, N[k][0])], 0.0)          # t * need <= delivered
        else:
            row(ins + [(nx + j, -1.0)], -N[k][0])     # delivered + shortfall >= need
    for k, q in (min_in or {}).items():
        row([(i, -1.0) for i in into.get(k, [])], -q)
    for k, idx in into.items():
        row([(i, 1.0) for i in idx], N[k][1] if k in N else 0.0)  # station headroom
    for did, d in P.depots.items():
        for f in FUELS:
            idx = [i for i, (r, ff) in enumerate(xs) if ff == f and r["source_depot_id"] == did]
            if idx:
                row([(i, 1.0) for i in idx], d["inv"][f])
        idx = [i for i, (r, _) in enumerate(xs) if r["source_depot_id"] == did]
        if idx:
            row([(i, 1.0) for i in idx], d["disp"])
    res = linprog(c, A_ub=np.array(A) if A else None, b_ub=np.array(b) if b else None, bounds=bounds, method="highs")
    if res.status != 0:
        raise SolverError(f"LP failed: {res.message}")
    plan = [(r["id"], f, float(q)) for (r, f), q in zip(xs, res.x[:nx]) if q > 1]
    return plan, float(res.x[nx]) if fair else 0.0


def algo_lp(P):
    return _lp(P, needs(P))[0]


def algo_robust_lp(P):
    return _lp(P, needs(P, scale=1.0, z=1.28))[0]  # P90 demand + uncertainty buffer


def algo_rationing(P):
    """Max-min fairness: maximise the smallest coverage fraction, then fill the rest at least cost."""
    N = needs(P)
    _, t = _lp(P, N, fair=True)
    return _lp(P, N, min_in={k: t * v[0] * 0.999 for k, v in N.items() if v[0] > 1})[0]


def algo_greedy(P):
    N = needs(P)
    inv = {d: dict(v["inv"]) for d, v in P.depots.items()}
    disp = {d: v["disp"] for d, v in P.depots.items()}
    order = sorted(N, key=lambda k: (P.risks[k]["hours_to_stockout"] or 1e9, -P.risks[k]["p_stockout"]))
    plan = []
    for sid, f in order:
        need = N[(sid, f)][0]
        for r in sorted((r for r in P.routes if r["destination_station_id"] == sid), key=lambda r: r["transit_ticks"]):
            d = r["source_depot_id"]
            q = min(need, inv[d][f], disp[d])
            if q >= config.MIN_SHIPMENT:
                plan.append((r["id"], f, q))
                inv[d][f] -= q
                disp[d] -= q
                need -= q
    return plan


def _windows(snap, H):
    """Per relative *processed* tick r (0 = current tick, processed next): route ok / station open / depot open."""
    t0 = snap["tick"]

    def window(kind, ids_key, entities, is_on):
        out = {e["id"]: np.full(H + 1, is_on(e)) for e in entities}
        for ev in snap["events"]:
            if ev["type"] != kind or ev["status"] == "RESOLVED":
                continue
            ids = ev["parameters"].get(ids_key) or list(out)
            s, e = max(0, ev["start_tick"] - t0), max(0, ev["end_tick"] - t0 + 1)  # end_tick inclusive (calibrated)
            for i in ids:
                if i in out:
                    if ev["status"] == "ACTIVE":
                        out[i][e:] = True
                    else:
                        out[i][s:e] = False
        return out

    route_ok = window("route_disruption", "route_ids", snap["routes"].values(), lambda r: r["status"] == "AVAILABLE")
    st_open = window("station_outage", "station_ids", snap["stations"].values(), lambda s: s["status"] == "OPEN")
    dep_open = window("depot_constraint", "depot_ids", snap["depots"].values(), lambda d: d["status"] == "OPEN")
    return route_ok, st_open, dep_open


def algo_mpc(P):
    """Rolling-horizon LP over H ticks with transit lags, scheduled supply and known event windows.
    Only the t=0 shipments are executed; the rest is re-planned next cycle."""
    snap, H, t0 = P.snap, P.H, P.snap["tick"]
    route_ok, st_open, dep_open = _windows(snap, H)
    idx, lb, ub, cost = {}, [], [], []

    def var(key, lo, hi, c):
        idx[key] = len(lb)
        lb.append(lo), ub.append(hi), cost.append(c)

    for r in snap["routes"].values():
        if snap["depots"][r["source_depot_id"]]["status"] not in USABLE_DEPOT:
            continue
        for t in range(H):
            if t + 1 + r["transit_ticks"] > H:
                break
            # created before processing t0+t (sees status after t0+t-1), departs while processing t0+t
            if t == 0:
                created_ok = r["status"] == "AVAILABLE" and snap["stations"][r["destination_station_id"]]["status"] == "OPEN"
            else:
                created_ok = route_ok[r["id"]][t - 1] and st_open[r["destination_station_id"]][t - 1]
            if created_ok and route_ok[r["id"]][t]:
                for f in FUELS:
                    var(("x", r["id"], f, t), 0, None, 0.002 * r["transit_ticks"] + 0.0005 * t)
    for s in snap["stations"].values():
        for f in FUELS:
            d = P.demand[(s["id"], f)] * (1 + 0.5 * P.sigma[(s["id"], f)])
            for k in range(1, H + 1):
                var(("I", s["id"], f, k), 0, s["capacity"][f], 0)
                var(("sv", s["id"], f, k), 0, float(d[k - 1]) if st_open[s["id"]][k - 1] else 0, -(0.99 ** k))
                var(("o", s["id"], f, k), 0, None, 5.0)
    for did in snap["depots"]:
        for f in FUELS:
            for t in range(H):
                var(("D", did, f, t), 0, None, 0)

    rows, cols, vals, beq = [], [], [], []
    ri = 0

    def eq(entries, rhs):
        nonlocal ri
        for key, v in entries:
            if key in idx:
                rows.append(ri), cols.append(idx[key]), vals.append(v)
        beq.append(rhs)
        ri += 1

    xkeys = [k for k in idx if k[0] == "x"]
    land = {}
    for _, rid, f, t in xkeys:
        r = snap["routes"][rid]
        land.setdefault((r["destination_station_id"], f, t + 1 + r["transit_ticks"]), []).append(("x", rid, f, t))
    for s in snap["stations"].values():
        for f in FUELS:
            a = P.arr.get((s["id"], f), P.zeros)
            for k in range(1, H + 1):
                ent = [(("I", s["id"], f, k), 1), (("sv", s["id"], f, k), 1), (("o", s["id"], f, k), 1)]
                ent += [(x, -1) for x in land.get((s["id"], f, k), [])]
                if k > 1:
                    ent.append((("I", s["id"], f, k - 1), -1))
                eq(ent, float(a[k]) + (s["inventory"][f] if k == 1 else 0))
    for did, d in snap["depots"].items():
        for f in FUELS:
            sup = np.zeros(H)
            for a in snap["supply"]:
                if a["depot_id"] == did and a["fuel_type"] == f and a["status"] != "ARRIVED":
                    t = max(1, a["planned_tick"] - t0 + 1)  # arrives while processing planned_tick
                    if t < H:
                        sup[t] += a["quantity"]
            for t in range(H):
                ent = [(("D", did, f, t), 1)] + [(x, 1) for x in xkeys if x[2] == f and x[3] == t
                                                 and snap["routes"][x[1]]["source_depot_id"] == did]
                if t:
                    ent.append((("D", did, f, t - 1), -1))
                eq(ent, float(sup[t]) + (d["inventory"][f] if t == 0 else 0))
    A_eq = coo_matrix((vals, (rows, cols)), shape=(ri, len(lb))).tocsr()

    urows, ucols, uvals, bub = [], [], [], []
    for j, (did, d) in enumerate(snap["depots"].items()):
        for t in range(H):
            cap = P.depots[did]["disp"] if t == 0 else d["dispatch_capacity_per_tick"] * (
                1.0 if dep_open[did][t - 1] else config.CONSTRAINED_DISPATCH_FACTOR)
            ks = [x for x in xkeys if x[3] == t and snap["routes"][x[1]]["source_depot_id"] == did]
            if ks:
                for x in ks:
                    urows.append(len(bub)), ucols.append(idx[x]), uvals.append(1.0)
                bub.append(cap)
    A_ub = coo_matrix((uvals, (urows, ucols)), shape=(len(bub), len(lb))).tocsr() if bub else None
    res = linprog(cost, A_ub=A_ub, b_ub=bub or None, A_eq=A_eq, b_eq=beq, bounds=list(zip(lb, ub)), method="highs")
    if res.status != 0:
        raise SolverError(f"MPC failed: {res.message}")
    return [(rid, f, float(res.x[idx[("x", rid, f, t)]])) for _, rid, f, t in xkeys
            if t == 0 and res.x[idx[("x", rid, f, t)]] > 1]


def algo_ppo(P, action):
    """RL policy: PPO picks per-series priority weights and safety factors; the LP keeps it feasible."""
    from .policy import decode
    w, z = decode(action, P.snap)
    return _lp(P, needs(P, scale=1.0, z=z), weights=w)[0]


ALGOS = {"greedy": algo_greedy, "lp": algo_lp, "robust_lp": algo_robust_lp, "mpc": algo_mpc,
         "rationing": algo_rationing, "hold": lambda P: []}


def finalize(raw, snap):
    """Merge, round down to 100 L, drop dust, split to route.max_shipment."""
    merged = {}
    for rid, f, q in raw:
        merged[(rid, f)] = merged.get((rid, f), 0) + q
    out = []
    for (rid, f), q in merged.items():
        q = math.floor(q / 100) * 100
        if q < config.MIN_SHIPMENT:
            continue
        r = snap["routes"].get(rid)
        if not r or r.get("max_shipment", 0) <= 0:
            continue
        n = max(1, math.ceil(q / r["max_shipment"]))
        for i in range(n):
            part = math.floor(q / n / 100) * 100 if i < n - 1 else q - (n - 1) * math.floor(q / n / 100) * 100
            if part > 0:
                out.append({"source_depot_id": r["source_depot_id"], "destination_station_id": r["destination_station_id"],
                            "route_id": rid, "fuel_type": f, "quantity": float(part)})
    return out


def doomed_routes(snap):
    """Routes AVAILABLE now but disrupted when the current tick is processed (event start_tick <= tick).
    An allocation created now departs during that processing and FAILS, losing its fuel (calibrated)."""
    out = set()
    for e in snap.get("events", []):
        if e.get("type") == "route_disruption" and e.get("status") == "SCHEDULED" and e.get("start_tick", 999999) <= snap["tick"]:
            out |= set(e.get("parameters", {}).get("route_ids") or snap["routes"])
    return out


def check(ships, snap):
    """Mirror of the simulator's validation order with running totals, plus doomed-route protection.
    Returns (valid, rejected)."""
    doomed = doomed_routes(snap)
    inv = {d: dict(v["inventory"]) for d, v in snap["depots"].items()}
    used = {d: dispatch_used(snap, d) for d in snap["depots"]}
    added = {}
    valid, rejected = [], []
    for sh in ships:
        r = snap["routes"].get(sh["route_id"])
        d = snap["depots"].get(sh["source_depot_id"])
        s = snap["stations"].get(sh["destination_station_id"])
        f, q = sh["fuel_type"], sh["quantity"]
        k = (s["id"], f) if s else None
        code = ("NOT_FOUND" if not (r and d and s) else
                "ROUTE_MISMATCH" if (r["source_depot_id"], r["destination_station_id"]) != (d["id"], s["id"]) else
                "DEPOT_CLOSED" if d["status"] not in USABLE_DEPOT else
                "STATION_CLOSED" if s["status"] != "OPEN" else
                "ROUTE_DISRUPTED" if r["status"] != "AVAILABLE" else
                "ROUTE_DISRUPTED_AT_DEPARTURE" if r["id"] in doomed else
                "INVALID_QUANTITY" if not q > 0 else
                "ROUTE_CAPACITY_EXCEEDED" if q > r["max_shipment"] + 1e-6 else
                "INSUFFICIENT_INVENTORY" if q > inv[d["id"]].get(f, 0.0) + 1e-6 else
                "DISPATCH_CAPACITY_EXCEEDED" if used[d["id"]] + q > d["dispatch_capacity_per_tick"] + 1e-6 else
                "DESTINATION_CAPACITY_EXCEEDED" if s["inventory"].get(f, 0.0) + added.get(k, 0) + q > s["capacity"].get(f, 0.0) + 1e-6 else None)
        if code:
            rejected.append({**sh, "code": code})
            continue
        inv[d["id"]][f] -= q
        used[d["id"]] += q
        added[k] = added.get(k, 0) + q
        valid.append(sh)
    return valid, rejected


def evaluate(ships, snap, fc, scale=1.0, ticks=config.EVAL_TICKS):
    """Roll the plan forward on the twin with deterministic forecast demand. Lower score is better."""
    tw = Twin(world_from_snapshot(snap), demand_fn=lambda s, f, t, h: fc.expected(snap, s, f, h) * scale, noise=False)
    rejected, cost = 0, 0.0
    for sh in ships:
        a, err = tw.submit(sh["source_depot_id"], sh["destination_station_id"], sh["route_id"], sh["fuel_type"], sh["quantity"])
        if err:
            rejected += 1
        else:
            cost += sh["quantity"] * snap["routes"][sh["route_id"]]["transit_ticks"]
    for _ in range(ticks):
        tw.step()
    near = tw.totals["unmet"]
    stockouts = len({(r["station_id"], r["fuel_type"]) for r in tw.demand_log if r["unmet_liters"] > 1})
    for _ in range(ticks):
        tw.step()
    tail = tw.totals["unmet"] - near
    score = near + 0.5 * tail + 0.01 * cost + tw.totals["overflow"] + 500 * rejected + 200 * tw.totals["failed"]
    return {"score": round(score, 1), "unmet": round(near, 1), "unmet_tail": round(tail, 1), "stockouts": stockouts,
            "cost_liter_ticks": round(cost), "overflow": round(tw.totals["overflow"], 1), "rejected": rejected,
            "failed": tw.totals["failed"]}


def tournament(snap, fc, paths, arr, risks, router_pick, budget=False, policy=None, bandit=None, regime=None):
    """Every candidate plans; each plan is scored on the twin under 3 demand scenarios; best robust score wins.
    The router's pick wins near-ties (<=2%) for stability. `policy` adds the PPO candidate. In budget mode the
    bandit's Thompson draw picks which 2 extra candidates run next to the router's pick and `hold`."""
    P = build_problem(snap, fc, paths, arr, risks)
    algos = dict(ALGOS)
    if policy is not None:
        from .policy import features
        action = policy.act(features(snap, risks, paths))
        algos["ppo"] = lambda P: algo_ppo(P, action)
    if not budget:
        names = list(algos)
    else:
        pool = [a for a in algos if a not in (router_pick, "hold")]
        extra = bandit.top(regime, 2, pool) if bandit else ["mpc", "lp"]
        names = list(dict.fromkeys([router_pick, *extra, "hold"]))
    names = [n for n in names if n in algos]
    cands, seen = [], {}
    for name in names:
        t0 = time.perf_counter()
        c = {"algorithm": name}
        try:
            ships, rejected = check(finalize(algos[name](P), snap), snap)
            c.update(shipments=ships, rejected=rejected)
        except Exception as e:  # a broken solver must never break the cycle
            c.update(error=f"{type(e).__name__}: {e}", shipments=[])
            M.FALLBACKS.labels(f"solver_{name}").inc()
        c["solve_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        M.SOLVER_LAT.labels(name).observe(c["solve_ms"] / 1000)
        if "error" not in c:
            sig = tuple(sorted((s["route_id"], s["fuel_type"], s["quantity"]) for s in c["shipments"]))
            if sig not in seen:
                runs = {n: evaluate(c["shipments"], snap, fc, sc) for n, sc in SCENARIOS}
                vals = [r["score"] for r in runs.values()]
                seen[sig] = (round(float(np.mean(vals)) + 0.25 * max(vals), 1), runs)
            c["score"], c["scenarios"] = seen[sig]
        cands.append(c)
    ok = [c for c in cands if "score" in c]
    if not ok:
        raise SolverError("every candidate failed")
    best = min(ok, key=lambda c: c["score"])
    pick = next((c for c in ok if c["algorithm"] == router_pick), None)
    winner = pick if pick and pick["score"] <= best["score"] * 1.02 + 1 else best
    hold = next((c for c in ok if c["algorithm"] == "hold"), None)
    return {"candidates": cands, "winner": winner["algorithm"], "best": best["algorithm"], "router_pick": router_pick,
            "router_hit": winner["algorithm"] == router_pick or best["score"] == (pick or {}).get("score"),
            "shipments": winner.get("shipments", []),
            "expected": {"without_action": hold["scenarios"]["p50"] if hold and "scenarios" in hold else None,
                         "with_plan": winner["scenarios"]["p50"] if "scenarios" in winner else None}}
