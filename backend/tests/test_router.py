import asyncio
import json

import httpx

from app import config, devsim
from app.audit import Audit
from app.orchestrator import Orchestrator
from app.router import QUESTIONS, Router
from app.sim_client import SimClient
from app.state import StateStore
from app.systemone import SystemOneClient
from tests.test_core import warm


def answers(regime="normal", algo="mpc", conf=0.9, needs=0.1):
    return {"answers": {
        "regime": {"type": "choice", "choice": regime, "confidence": conf, "probabilities": {regime: conf}},
        "algorithm": {"type": "choice", "choice": algo, "confidence": conf, "probabilities": {algo: conf}},
        "severity": {"type": "score", "score": 1.2, "confidence": 0.8},
        "needs_human": {"type": "noul", "noul": needs, "confidence": 0.9}},
        "usage": {"input_tokens": 300, "output_tokens": 0}}


def fake(name, handler):
    return SystemOneClient(name, "http://model", transport=httpx.MockTransport(handler))


def reply(body, calls):
    def h(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(body)
    return h


def situation():
    _, s, fc, p, a, r = warm(40)
    return s, set(), r, []


async def route_settled(router, *sit):
    """First call returns rules while the model is asked in the background; second uses the answer."""
    first = await router.route(*sit)
    await router.drain()
    return first, await router.route(*sit)


def test_never_blocks_then_uses_cached_model_answer():
    calls = []
    router = Router({"laya": fake("laya", reply(answers("normal", "lp"), calls))}, primary="laya", shadow=False)
    first, res = asyncio.run(route_settled(router, *situation()))
    assert first["source"] == "rules" and "pending" in first["fallback"]
    assert res["source"] == "laya" and res["algorithm"] == "lp" and res["agrees_with_rules"] and res["cached"]
    assert set(calls[0]["questions"]) == set(QUESTIONS) and len(calls[0]["state"]) < 2500
    asyncio.run(router.route(*situation()))
    assert len(calls) == 1  # same situation -> no second model call


def test_disagreement_with_rules_is_flagged():
    router = Router({"laya": fake("laya", reply(answers("combined", "rationing"), []))}, primary="laya", shadow=False)
    _, res = asyncio.run(route_settled(router, *situation()))
    assert res["agrees_with_rules"] is False and res["rules_regime"] == "normal"


def test_model_failure_falls_back_to_rules_and_breaker_opens():
    calls = []
    router = Router({"laya": fake("laya", reply(500, calls))}, primary="laya", shadow=False)
    s, flags, r, al = situation()
    for i in range(5):
        _, res = asyncio.run(route_settled(router, s, {f"x{i}"}, r, al))  # new signature each time
        assert res["source"] == "rules" and "failed" in res["fallback"]
    assert len(calls) == 3  # breaker opened after 3 failures; later cycles never wait on the model


def test_out_of_range_answer_rejected():
    router = Router({"laya": fake("laya", reply(answers("normal", "teleport"), []))}, primary="laya", shadow=False)
    _, res = asyncio.run(route_settled(router, *situation()))
    assert res["source"] == "rules" and "outside allowed" in res["fallback"]


def test_slow_model_times_out(monkeypatch):
    monkeypatch.setattr(config, "ROUTER_TIMEOUT_S", 0.05)

    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json=answers())

    c = SystemOneClient("laya", "http://model", transport=httpx.MockTransport(slow))
    _, res = asyncio.run(route_settled(Router({"laya": c}, primary="laya", shadow=False), *situation()))
    assert res["source"] == "rules" and "TimeoutError" in res["fallback"]


def test_shadow_jev_scored_against_tournament():
    async def go():
        router = Router({"laya": fake("laya", reply(answers("normal", "mpc"), [])),
                         "jev": fake("jev", reply(answers("normal", "lp"), []))}, primary="laya", shadow=True)
        await route_settled(router, *situation())
        router.record_outcome("mpc")
        return router.compare()

    cmp = asyncio.run(go())
    assert cmp["laya"]["tournament_accuracy"] == 1.0 and cmp["jev"]["tournament_accuracy"] == 0.0
    assert cmp["rules"]["tournament_accuracy"] == 1.0 and cmp["jev"]["calls"] == 1


def test_orchestrator_uses_model_router():
    async def go():
        devsim.S.reset()
        sim = SimClient("http://sim", transport=httpx.ASGITransport(app=devsim.app))
        store = StateStore(sim)
        router = Router({"laya": fake("laya", reply(answers("normal", "greedy"), []))}, primary="laya", shadow=False)
        o = Orchestrator(sim, store, Audit(":memory:"), router)
        for _ in range(20):
            devsim.S.step()
        await o.cycle(force=True)  # model asked in background; rules drive this cycle
        await router.drain()
        d = await o.cycle(force=True)
        assert d["router"]["source"] == "laya" and d["router"]["algorithm"] == "greedy"
        assert router.compare()["laya"]["tournaments"] >= 1  # scored against the tournament winner
        assert not d["gate"]["reasons"] or d["gate"]["hard"]  # full tournament: model doubts are notes only

    asyncio.run(go())
