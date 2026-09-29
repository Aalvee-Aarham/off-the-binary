"""Closed-loop load test: N concurrent virtual users hammer one path for D seconds per stage.

    python -m scripts.loadtest --base http://localhost:8080 --token $ADMIN_TOKEN --out ../docs/loadtest

Workload (default):
  A. GET  /api/state                   operator dashboard read path (cached snapshot)     VUs 10 / 50 / 100
  B. POST /api/decisions/recommend     full decision pipeline: forecast + 6-way twin       VUs 1 / 4 / 8
                                       tournament on the cached snapshot (nothing executed)
  C. A and B again under an injected simulator `latency` fault (500 ms on every /v1 call) -> shows the
     read/decide paths are decoupled from simulator latency (needs --token).
Reports avg/p50/p95/p99/max latency, throughput, error rate, and backend CPU/RSS deltas from /metrics
(process_* metrics exist on Linux only, i.e. in the container)."""
import argparse
import asyncio
import json
import os
import re
import time

import httpx

STAGES = [("GET", "/api/state", [10, 50, 100]), ("POST", "/api/decisions/recommend", [1, 4, 8])]


def pct(xs, p):
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else None


async def proc_stats(c):
    try:
        t = (await c.get("/metrics")).text
    except httpx.HTTPError:
        return {}
    out = {}
    for k in ("process_cpu_seconds_total", "process_resident_memory_bytes"):
        m = re.search(rf"^{k} (\S+)$", t, re.M)
        if m:
            out[k] = float(m.group(1))
    return out


async def stage(c, method, path, vus, seconds):
    lat, errors, codes = [], 0, {}
    stop = time.perf_counter() + seconds

    async def user():
        nonlocal errors
        while time.perf_counter() < stop:
            t0 = time.perf_counter()
            try:
                r = await c.request(method, path)
                code = r.status_code
            except httpx.HTTPError as e:
                code = type(e).__name__
            dt = time.perf_counter() - t0
            codes[code] = codes.get(code, 0) + 1
            if code != 200:
                errors += 1
            lat.append(dt)

    before = await proc_stats(c)
    t0 = time.perf_counter()
    await asyncio.gather(*(user() for _ in range(vus)))
    wall = time.perf_counter() - t0
    after = await proc_stats(c)
    lat.sort()
    ms = lambda x: round(x * 1000, 1) if x is not None else None
    row = {"method": method, "path": path, "vus": vus, "seconds": round(wall, 1), "requests": len(lat),
           "rps": round(len(lat) / wall, 1), "error_rate": round(errors / max(1, len(lat)), 4), "codes": codes,
           "avg_ms": ms(sum(lat) / len(lat)) if lat else None, "p50_ms": ms(pct(lat, 50)), "p95_ms": ms(pct(lat, 95)),
           "p99_ms": ms(pct(lat, 99)), "max_ms": ms(lat[-1] if lat else None)}
    if "process_cpu_seconds_total" in before and "process_cpu_seconds_total" in after:
        row["backend_cpu_cores"] = round((after["process_cpu_seconds_total"] - before["process_cpu_seconds_total"]) / wall, 2)
        row["backend_rss_mb"] = round(after["process_resident_memory_bytes"] / 2 ** 20, 1)
    return row


async def run(base, token, seconds, fault, quick):
    rows = []
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as c:
        (await c.get("/api/state")).raise_for_status()  # backend must have a snapshot
        phases = [("baseline", None)] + ([("sim latency fault 500ms", "latency")] if fault and token else [])
        for label, f in phases:
            if f:
                (await c.post("/api/chaos/fault", headers={"X-Admin-Token": token},
                              json={"type": f, "duration_seconds": int(seconds * 8 + 60),
                                    "parameters": {"delay_ms": 500}})).raise_for_status()
            for method, path, vus_list in STAGES:
                for vus in (vus_list[:1] if quick else vus_list):
                    r = await stage(c, method, path, vus, seconds)
                    r["phase"] = label
                    rows.append(r)
                    print(json.dumps(r))
            if f:
                await c.post("/api/chaos/clear", headers={"X-Admin-Token": token})
        health = (await c.get("/api/health")).json()
    return rows, health


def markdown(rows, health, base):
    lines = [f"Target `{base}` · backend {health.get('version')} · generated {time.strftime('%Y-%m-%d %H:%M')}", "",
             "| Phase | Path | VUs | Requests | RPS | Error % | avg ms | p50 | p95 | p99 | max | CPU cores | RSS MB |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['phase']} | `{r['method']} {r['path']}` | {r['vus']} | {r['requests']} | {r['rps']} | "
                     f"{r['error_rate'] * 100:.2f} | {r['avg_ms']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | "
                     f"{r['max_ms']} | {r.get('backend_cpu_cores', '–')} | {r.get('backend_rss_mb', '–')} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--token", default=os.getenv("ADMIN_TOKEN", ""))
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--no-fault", action="store_true")
    ap.add_argument("--quick", action="store_true", help="one concurrency level per path (CI smoke)")
    ap.add_argument("--out", help="path prefix: writes <out>.json and <out>.md")
    a = ap.parse_args()
    rows, health = asyncio.run(run(a.base, a.token, a.seconds, not a.no_fault, a.quick))
    md = markdown(rows, health, a.base)
    print(md)
    if a.out:
        with open(a.out + ".json", "w") as f:
            json.dump({"rows": rows, "health": health}, f, indent=2)
        with open(a.out + ".md", "w", encoding="utf-8") as f:
            f.write(md + "\n")
    if any(r["error_rate"] > 0.01 for r in rows if r["phase"] == "baseline"):
        raise SystemExit("baseline error rate above 1%")


if __name__ == "__main__":
    main()
