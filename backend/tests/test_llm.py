import asyncio
import json

import httpx
import pytest

from app.audit import Audit
from app.llm_pool import LLMPool, LLMUnavailable, template_explanation


def groq_ok(text="groq says hi"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def gemini_ok(text="gemini says hi"):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def pool(handler, groq=("g0", "g1"), gemini=("m0",), audit=None):
    return LLMPool(list(groq), list(gemini), audit, transport=httpx.MockTransport(handler))


def key_of(req):
    return req.headers.get("authorization", "").replace("Bearer ", "") or req.headers.get("x-goog-api-key")


def test_parallel_calls_spread_across_keys():
    used = []

    async def h(req):
        used.append(key_of(req))
        await asyncio.sleep(0.05)
        return groq_ok() if "groq" in str(req.url) else gemini_ok()

    p = pool(h)

    async def go():
        await asyncio.wait_for(asyncio.gather(*(p.complete("s", f"q{i}") for i in range(3))), 5)

    asyncio.run(go())
    assert sorted(used) == ["g0", "g1", "m0"]  # least in-flight first: every key busy once, none twice


def test_rate_limited_key_cools_down_and_call_fails_over():
    calls = []

    def h(req):
        calls.append(key_of(req))
        if key_of(req) == "g0":
            return httpx.Response(429, headers={"retry-after": "60"})
        return groq_ok("from g1")

    p = pool(h, gemini=())
    r = asyncio.run(p.complete("s", "q"))
    assert r["text"] == "from g1" and calls == ["g0", "g1"]
    assert p.keys[0].cooldown_until > 0 and not p.keys[0].usable
    asyncio.run(p.complete("s", "q2"))
    assert calls[-1] == "g1"  # cooling key is skipped entirely


def test_bad_key_disabled_then_other_provider():
    def h(req):
        return httpx.Response(401) if "groq" in str(req.url) else gemini_ok("gemini fallback")

    p = pool(h)
    r = asyncio.run(p.complete("s", "q"))
    assert r["provider"] == "gemini" and all(k.disabled for k in p.keys if k.provider == "groq")
    assert p.health()["usable_keys"] == {"groq": 0, "gemini": 1}


def test_all_keys_failing_raises_and_template_still_explains():
    p = pool(lambda req: httpx.Response(503))
    with pytest.raises(LLMUnavailable):
        asyncio.run(p.complete("s", "q"))
    d = {"tick": 5, "router": {"regime": "normal", "source": "rules", "algorithm": "mpc"}, "algorithm": "mpc",
         "shipments": [{"quantity": 3000, "fuel_type": "DIESEL", "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur", "route_id": "route-gazipur-mirpur"}],
         "expected": {"without_action": {"unmet": 900, "stockouts": 2}, "with_plan": {"unmet": 100, "stockouts": 0}},
         "candidates": [{"algorithm": "mpc", "score": 120.0}], "top_risks": [], "gate": {"reasons": []}}
    text = template_explanation(d)
    assert "3,000 L DIESEL" in text and "900 L -> 100 L" in text


def test_identical_prompt_served_from_cache():
    n = []

    def h(req):
        n.append(1)
        return groq_ok()

    p = pool(h, audit=Audit(":memory:"))
    a = asyncio.run(p.complete("s", "same"))
    b = asyncio.run(p.complete("s", "same"))
    assert not a["cached"] and b["cached"] and len(n) == 1


def test_gemini_request_shape():
    seen = {}

    def h(req):
        seen.update(url=str(req.url), body=json.loads(req.content))
        return gemini_ok()

    asyncio.run(pool(h, groq=()).complete("sys", "hello"))
    assert ":generateContent" in seen["url"] and seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
