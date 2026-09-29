#!/usr/bin/env python3
"""End-to-end smoke test + mini benchmark for the LLM server (stdlib only).

    python scripts/smoke_test.py --url http://localhost:8000 --api-key KEY
    python scripts/smoke_test.py --url http://<vm> --api-key KEY --bench 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

OK, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"


class Client:
    def __init__(self, url: str, api_key: str | None):
        self.url = url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 180) -> tuple[int, dict | str, float]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, code = r.read().decode(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read().decode(), e.code
        ms = (time.perf_counter() - t0) * 1000
        try:
            return code, json.loads(raw), ms
        except json.JSONDecodeError:
            return code, raw, ms


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"[{OK if cond else FAIL}] {name}  {detail}")
    return cond


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8100")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--bench", type=int, default=0, help="requests per model for the latency benchmark")
    args = ap.parse_args()
    c = Client(args.url, args.api_key)
    ok = True

    code, health, _ = c.call("GET", "/health")
    ok &= check("GET /health", code == 200, json.dumps(health))
    code, models, _ = c.call("GET", "/api/models")
    ok &= check("GET /api/models", code == 200)
    ids = [m["id"] for m in models.get("models", [])] if isinstance(models, dict) else []

    for mid in ids:
        code, r, ms = c.call("POST", "/api/generate", {
            "model": mid, "prompt": "In one sentence, why do fuel stations need demand forecasting?",
            "max_tokens": 60, "temperature": 0, "use_cache": False})
        text = r.get("text", "")[:90].replace("\n", " ") if isinstance(r, dict) else r
        tps = r.get("tokens_per_second") if isinstance(r, dict) else None
        ok &= check(f"POST /api/generate [{mid}]", code == 200 and bool(text), f"{ms:.0f} ms, {tps} tok/s | {text}")

    code, r, ms = c.call("POST", "/api/chat", {"messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "What is 2+2? Answer with a number."}], "max_tokens": 8, "temperature": 0})
    ok &= check("POST /api/chat", code == 200, f"{ms:.0f} ms | {r.get('text') if isinstance(r, dict) else r}")

    schema = {"type": "object", "properties": {
        "station": {"type": "string"}, "fuel": {"type": "string", "enum": ["diesel", "petrol", "octane"]},
        "litres": {"type": "number"}}, "required": ["station", "fuel", "litres"]}
    code, r, ms = c.call("POST", "/api/structured", {
        "model": "gemma3-1b", "schema": schema,
        "prompt": "Station S-3 reports it needs 4500 litres of diesel by tonight."})
    ok &= check("POST /api/structured", code == 200 and isinstance(r, dict) and isinstance(r.get("data"), dict),
                f"{ms:.0f} ms | {r.get('data') if isinstance(r, dict) else r}")

    labels = ["normal", "shortage", "demand_surge", "route_disruption"]
    body = {"text": "Depot D1 inventory of diesel dropped 40% in 2 hours and three stations report empty tanks.",
            "labels": labels, "instruction": "Classify the fuel supply situation."}
    code, r, ms = c.call("POST", "/api/classify", body)
    ok &= check("POST /api/classify", code == 200 and isinstance(r, dict) and r.get("label") in labels,
                f"{ms:.0f} ms | {r.get('label') if isinstance(r, dict) else r} conf={r.get('confidence') if isinstance(r, dict) else ''}")
    code, r2, ms2 = c.call("POST", "/api/classify", body)
    ok &= check("classify cache hit", code == 200 and isinstance(r2, dict) and r2.get("cached") is True, f"{ms2:.0f} ms")

    code, r, ms = c.call("POST", "/v1/chat/completions", {
        "model": "gemma", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 5})
    ok &= check("POST /v1/chat/completions (OpenAI)", code == 200 and isinstance(r, dict) and "choices" in r, f"{ms:.0f} ms")

    code, r, _ = c.call("POST", "/api/generate", {"model": "nope", "prompt": "x"})
    ok &= check("unknown model -> 404", code == 404)

    if args.bench:
        print(f"\nBenchmark: {args.bench} sequential requests per model (64 new tokens, no cache)")
        print(f"{'model':<14}{'p50 ms':>9}{'p95 ms':>9}{'avg tok/s':>11}")
        for mid in ids:
            lat, tps = [], []
            for i in range(args.bench):
                code, r, ms = c.call("POST", "/api/generate", {
                    "model": mid, "prompt": f"Write a short note (#{i}) about fuel logistics.",
                    "max_tokens": 64, "temperature": 0.7, "use_cache": False, "allow_fallback": False})
                if code == 200:
                    lat.append(ms)
                    if r.get("tokens_per_second"):
                        tps.append(r["tokens_per_second"])
            if lat:
                lat.sort()
                p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
                print(f"{mid:<14}{statistics.median(lat):>9.0f}{p95:>9.0f}{statistics.mean(tps) if tps else 0:>11.1f}")

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
