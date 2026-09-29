"""Offline router comparison: Laya vs Jev vs rules on labeled twin situations.

    python -m scripts.router_eval --laya http://localhost:8001 [--jev-key $JEV_API_KEY] [--out ../docs/router_eval.json]

Label for `regime` = the rule/detector regime (ground truth: we injected the events).
Label for `algorithm` = the tournament winner (best plan on the twin under 3 demand scenarios)."""
import argparse
import asyncio
import json
import statistics
import time

from app import config
from app.detect import Detector
from app.forecast import Forecaster, arrivals, risks
from app.router import QUESTIONS, parse, route_rules, state_text
from app.solvers import tournament
from app.systemone import SystemOneClient
from app.world import Twin, baseline_world

H = config.HORIZON_TICKS
SCENARIOS = {
    "normal": [],
    "demand_spike": [("demand_spike", 2, 400, {"region_ids": ["region-dhaka"], "multiplier": 2.0})],
    "route_disruption": [("route_disruption", 2, 400, {"route_ids": ["route-gazipur-mirpur", "route-patiya-karnaphuli"]})],
    "supply_crisis": [("shipment_delay", 2, 1, {"delay_ticks": 120}), ("supply_shortfall", 3, 1, {"factor": 0.3})],
    "depot_constraint": [("depot_constraint", 2, 400, {"depot_ids": ["depot-patiya"]})],
    "combined": [("demand_spike", 2, 400, {"multiplier": 1.8}),
                 ("route_disruption", 2, 400, {"route_ids": ["route-gazipur-mirpur"]}),
                 ("station_outage", 2, 400, {"station_ids": ["station-coxsbazar"]})],
}


def situations(ticks=(30, 70, 110, 150)):
    for name, evs in SCENARIOS.items():
        for T in ticks:
            tw = Twin(baseline_world())
            for e in evs:
                tw.add_event(*e)
            fc = Forecaster()
            for _ in range(T):
                tw.step()
            s = tw.snapshot()
            fc.ingest(tw.demand_log, s)
            p, a = fc.paths(s, H), arrivals(s, H)
            r = risks(s, fc, p, a, H)
            _, _, flags = (det := Detector()).run(s, None, r, p, fc)
            alerts = list(det.active.values())
            rules = route_rules(flags, r, alerts)
            t = tournament(s, fc, p, a, r, rules["algorithm"])
            scores = {c["algorithm"]: c.get("score") for c in t["candidates"]}
            yield {"scenario": name, "tick": T, "state": state_text(s, flags, r, alerts), "rules": rules,
                   "winner": t["best"], "scores": scores}


async def ask(client, sit):
    try:
        ans, ms, usage = await client.ask(sit["state"], QUESTIONS)
        res = parse(ans, sit["rules"])
        return {**res, "latency_ms": ms}
    except Exception as e:
        return {"error": str(e)[:200]}


def near_best(sit, algo):
    """Within 2% of the best score = as good as the winner (ties are common: several plans are identical)."""
    best = sit["scores"][sit["winner"]]
    s = sit["scores"].get(algo)
    return s is not None and s <= best * 1.02 + 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--laya", default="http://localhost:8001")
    ap.add_argument("--laya-model", default=config.LAYA_MODEL)
    ap.add_argument("--jev-key", default=config.JEV_API_KEY)
    ap.add_argument("--out")
    a = ap.parse_args()
    clients = {}
    if a.laya:
        clients["laya"] = SystemOneClient("laya", a.laya, model=a.laya_model, timeout=30)
    if a.jev_key:
        clients["jev"] = SystemOneClient("jev", config.JEV_URL, api_key=a.jev_key, model=config.JEV_MODEL, timeout=30)
    t0 = time.time()
    sits = list(situations())
    print(f"{len(sits)} situations built in {time.time() - t0:.0f}s")
    rows = []
    for sit in sits:
        row = {"scenario": sit["scenario"], "tick": sit["tick"], "label_regime": sit["rules"]["regime"],
               "rules_algorithm": sit["rules"]["algorithm"], "winner": sit["winner"]}
        for name, c in clients.items():
            row[name] = await ask(c, sit)
        row["rules_near_best"] = near_best(sit, sit["rules"]["algorithm"])
        for name in clients:
            if "algorithm" in row[name]:
                row[name]["near_best"] = near_best(sit, row[name]["algorithm"])
        rows.append(row)
        print(json.dumps({k: (v if not isinstance(v, dict) else {x: v.get(x) for x in ("regime", "algorithm", "confidence", "latency_ms", "error")})
                          for k, v in row.items()}))
    summary = {"n": len(rows), "rules": {"algorithm_near_best": round(statistics.mean(r["rules_near_best"] for r in rows), 3)}}
    for name in clients:
        ok = [r[name] for r in rows if "regime" in r[name]]
        summary[name] = {
            "answered": len(ok), "errors": len(rows) - len(ok),
            "regime_accuracy": round(statistics.mean(x["regime"] == r["label_regime"] for r, x in zip([r for r in rows if "regime" in r[name]], ok)), 3) if ok else None,
            "algorithm_near_best": round(statistics.mean(x["near_best"] for x in ok), 3) if ok else None,
            "latency_ms_p50": statistics.median(x["latency_ms"] for x in ok) if ok else None,
            "mean_confidence": round(statistics.mean(x["confidence"] for x in ok), 3) if ok else None,
            "regimes_predicted": {g: sum(x["regime"] == g for x in ok) for g in QUESTIONS["regime"]["criteria"]},
            "algorithms_predicted": {g: sum(x["algorithm"] == g for x in ok) for g in QUESTIONS["algorithm"]["criteria"]},
        }
    if "laya" in clients and "jev" in clients:
        both = [r for r in rows if "regime" in r["laya"] and "regime" in r["jev"]]
        summary["laya_vs_jev_agreement"] = {
            "regime": round(statistics.mean(r["laya"]["regime"] == r["jev"]["regime"] for r in both), 3) if both else None,
            "algorithm": round(statistics.mean(r["laya"]["algorithm"] == r["jev"]["algorithm"] for r in both), 3) if both else None}
    print(json.dumps(summary, indent=2))
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"summary": summary, "rows": rows}, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
