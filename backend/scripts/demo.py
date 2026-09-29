"""Scripted demo of the problem statement's story (PS section 22), driven only through the backend API.

    python -m scripts.demo --base http://localhost:8080 --token $ADMIN_TOKEN --out ../docs/demo-run.md

Resets the simulator. Steps: normal ops -> demand spike (detect, decide, explain) -> operator review in MANUAL
mode -> combined crisis -> simulator outage (degraded, recovery) -> router model failure (rule fallback) -> summary."""
import argparse
import time

import httpx


class Demo:
    def __init__(self, base, token):
        self.c = httpx.Client(base_url=base, timeout=60, headers={"X-Admin-Token": token})
        self.log = []

    def get(self, p, **q):
        r = self.c.get(p, params=q or None)
        r.raise_for_status()
        return r.json()

    def post(self, p, body=None, method="POST"):
        r = self.c.request(method, p, json=body)
        r.raise_for_status()
        return r.json()

    def note(self, step, text):
        s = self.get("/api/state")
        line = f"| {step} | {s['tick']} | {s['metrics']['service_level']:.2%} | {text} |"
        print(line)
        self.log.append(line)
        return s

    def wait_ticks(self, n, timeout=120):
        start = self.get("/api/state")["tick"]
        t0 = time.time()
        while self.get("/api/state")["tick"] < start + n and time.time() - t0 < timeout:
            time.sleep(0.5)

    def event(self, typ, dur, **params):
        return self.post("/api/chaos/event", {"type": typ, "duration_ticks": dur, "parameters": params})

    def latest(self, **q):
        ds = self.get("/api/decisions", limit=20, **q)
        return next((d for d in ds if d.get("shipments")), None)


def run(d, speed_ticks):
    d.post("/api/sim/reset")
    d.post("/api/mode", {"mode": "AUTO_GATED"}, "PUT")
    d.post("/api/chaos/clear")
    time.sleep(3)
    d.post("/api/sim/run")
    d.wait_ticks(speed_ticks)
    dec = d.latest()
    d.note("1 Normal operations", f"{len(d.get('/api/decisions', limit=200))} stored decisions (calm no-action cycles "
                                  f"are not stored); latest winner "
                                  f"`{dec['algorithm'] if dec else '-'}`; health `{d.get('/api/health')['status']}`")

    d.event("demand_spike", 80, region_ids=["region-dhaka"], multiplier=2.0)
    d.wait_ticks(6)
    s = d.get("/api/state")
    alerts = [a["message"] for a in s["alerts"] if a["type"] in ("demand_spike", "stockout_risk")][:3]
    d.note("2 Demand spike x2.0 (Dhaka)", "detected: " + "; ".join(alerts))
    dec = d.latest()
    if dec:
        ex = d.get(f"/api/decisions/{dec['id']}/explain", llm="false")
        d.note("3 Decision + expected impact", f"`{dec['algorithm']}` beat {len(dec['candidates']) - 1} candidates on the twin; "
                                              f"{ex['text'][:400]}")

    d.post("/api/mode", {"mode": "MANUAL"}, "PUT")
    pending = None
    for _ in range(40):
        pending = next((x for x in d.get("/api/decisions", status="PENDING_APPROVAL", limit=5)), None)
        if pending:
            break
        time.sleep(1)
    if pending:
        d.post("/api/sim/pause")  # review with the clock stopped (allowed for self-test, IG section 2)
        ex = d.get(f"/api/decisions/{pending['id']}/explain")  # LLM rewrite when keys work
        d.post(f"/api/decisions/{pending['id']}/approve")
        d.post("/api/sim/run")
        d.note("4 Operator review (MANUAL)", f"plan `{pending['id']}` inspected ({ex['source']}) and approved: "
                                             f"{len(pending['shipments'])} shipments")
    d.post("/api/mode", {"mode": "AUTO_GATED"}, "PUT")

    d.event("route_disruption", 40, route_ids=["route-gazipur-mirpur"])
    d.event("shipment_delay", 1, delay_ticks=40)
    d.event("station_outage", 12, station_ids=["station-coxsbazar"])
    d.wait_ticks(10)
    s = d.get("/api/state")
    d.note("5 Combined crisis", f"regime `{s['router']['regime']}`; alerts: "
                                + "; ".join(a["message"] for a in s["alerts"] if a["severity"] == "critical")[:400])

    d.post("/api/chaos/fault", {"type": "unavailable", "duration_seconds": 15})
    time.sleep(8)
    h = d.get("/api/health")
    d.note("6 Simulator outage injected", f"health `{h['status']}`, simulator `{h['components']['simulator']['status']}`, "
                                          f"breaker `{h['components']['simulator']['breaker']}`, degraded mode {h['degraded_mode']}")
    time.sleep(15)
    d.post("/api/chaos/clear")
    time.sleep(8)
    h = d.get("/api/health")
    d.note("7 Recovery", f"health `{h['status']}`, breaker `{h['components']['simulator']['breaker']}`, "
                         f"degraded {h['degraded_mode']}")

    before = d.get("/api/router")["primary"]
    try:
        d.post("/api/router", {"primary": "laya"}, "PUT")
        d.wait_ticks(6)
        r = d.get("/api/router")["current"]
        d.note("8 Router model", f"primary laya -> current source `{r['source']}` ({r.get('fallback') or 'model answering'})")
        d.post("/api/router", {"primary": before}, "PUT")
    except httpx.HTTPStatusError:
        d.note("8 Router model", "laya not configured in this deployment: rule router active (fallback path)")

    d.wait_ticks(20)
    s = d.get("/api/state")
    inc = s["incident"]
    d.note("9 Operations continue", f"final service level {s['metrics']['service_level']:.2%}, unmet "
                                    f"{s['metrics']['unmet_demand_liters']:,.0f} L, allocation failures "
                                    f"{s['metrics']['allocation_failures']}; last recovery {inc['last_recovery']}")
    d.post("/api/sim/pause")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--token", required=True)
    ap.add_argument("--normal-ticks", type=int, default=20)
    ap.add_argument("--out")
    a = ap.parse_args()
    d = Demo(a.base, a.token)
    head = ["| Step | Tick | Service level | What happened |", "|---|---|---|---|"]
    print("\n".join(head))
    run(d, a.normal_ticks)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(f"# Demo run ({time.strftime('%Y-%m-%d %H:%M')}, simulated)\n\n" + "\n".join(head + d.log) + "\n")


if __name__ == "__main__":
    main()
