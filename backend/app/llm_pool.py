"""Groq + Gemini key pool, used as little as possible (text only; every caller has a deterministic template).

- Load spreading: each call goes to the healthy key with the fewest in-flight requests (keys work in parallel).
- Failover: 429 -> that key cools down (Retry-After), 401/403 -> key disabled, other errors -> short cooldown;
  the call moves on to the next key, then the other provider. All keys down -> caller uses its template.
- Cache: identical prompts are answered from SQLite, never re-billed."""
import asyncio
import hashlib
import logging
import time

import httpx

from . import config
from . import metrics as M

log = logging.getLogger("llm")


class LLMUnavailable(Exception):
    pass


def _groq_reasoning(model):
    """Short operator texts don't need hidden reasoning tokens (they cost latency and budget)."""
    if "qwen" in model:
        return {"reasoning_effort": "none"}
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    return {}


def _gemini_thinking(model):
    """Thinking tokens count against maxOutputTokens and can leave no text: keep them minimal."""
    if "2.5" in model:
        return {"thinkingConfig": {"thinkingBudget": 0}}
    if "gemini-3" in model:
        return {"thinkingConfig": {"thinkingLevel": "minimal"}}
    return {}


class Key:
    def __init__(self, provider, idx, secret):
        self.provider, self.idx, self.secret = provider, idx, secret
        self.inflight, self.cooldown_until, self.fails, self.disabled, self.last_used = 0, 0.0, 0, None, 0.0

    @property
    def usable(self):
        return not self.disabled and time.time() >= self.cooldown_until

    def state(self):
        return {"provider": self.provider, "key": self.idx, "inflight": self.inflight, "disabled": self.disabled,
                "cooling_s": max(0, round(self.cooldown_until - time.time(), 1)), "fails": self.fails}


class LLMPool:
    def __init__(self, groq_keys=(), gemini_keys=(), audit=None, transport=None):
        self.keys = [Key("groq", i, k) for i, k in enumerate(groq_keys) if k] + \
                    [Key("gemini", i, k) for i, k in enumerate(gemini_keys) if k]
        self.audit = audit
        self.http = httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S, transport=transport)
        self.sem = asyncio.Semaphore(config.LLM_MAX_CONCURRENT)

    @property
    def configured(self):
        return bool(self.keys)

    def health(self):
        usable = [k for k in self.keys if k.usable]
        by = {p: sum(k.provider == p and k.usable for k in self.keys) for p in ("groq", "gemini")}
        status = "not_configured" if not self.keys else "healthy" if usable else "degraded"
        return {"status": status, "usable_keys": by, "total_keys": len(self.keys),
                "fallback": "deterministic templates" if status != "healthy" else None}

    def _order(self):
        prio = {"groq": 0, "gemini": 1}  # groq first: lower latency
        return sorted((k for k in self.keys if k.usable), key=lambda k: (k.inflight, prio[k.provider], k.last_used))

    async def _call(self, k, system, prompt, max_tokens):
        if k.provider == "groq":
            r = await self.http.post("https://api.groq.com/openai/v1/chat/completions",
                                     headers={"Authorization": f"Bearer {k.secret}"},
                                     json={"model": config.GROQ_MODEL, "max_tokens": max_tokens + 400, "temperature": 0.2,
                                           **_groq_reasoning(config.GROQ_MODEL),
                                           "messages": [{"role": "system", "content": system},
                                                        {"role": "user", "content": prompt}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        r = await self.http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": k.secret},
            json={"systemInstruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2,
                                       # thinking tokens would eat the budget and return no text
                                       **_gemini_thinking(config.GEMINI_MODEL)}})
        r.raise_for_status()
        parts = r.json()["candidates"][0].get("content", {}).get("parts") or []  # MAX_TOKENS -> no parts
        return "".join(p.get("text", "") for p in parts).strip()

    async def complete(self, system, prompt, max_tokens=350, purpose="other"):
        """Returns {text, provider, key, cached, latency_ms}. Raises LLMUnavailable when every key failed."""
        h = hashlib.sha1(f"{config.GROQ_MODEL}|{config.GEMINI_MODEL}|{system}|{prompt}".encode()).hexdigest()
        if self.audit and (hit := self.audit.llm_get(h)):
            M.LLM_CALLS.labels("cache", "-", "hit").inc()
            return {**hit, "cached": True}
        if not self.keys:
            raise LLMUnavailable("no LLM keys configured")
        async with self.sem:
            errors = []
            for k in self._order()[:config.LLM_MAX_ATTEMPTS]:
                k.inflight += 1
                k.last_used = time.time()
                t0 = time.perf_counter()
                try:
                    text = await self._call(k, system, prompt, max_tokens)
                    if not text:
                        raise ValueError("empty completion")
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    if code == 429:
                        k.cooldown_until = time.time() + float(e.response.headers.get("retry-after") or 30)
                    elif code in (401, 403):
                        k.disabled = f"auth rejected ({code})"
                    else:
                        k.fails += 1
                        k.cooldown_until = time.time() + 10
                    M.LLM_CALLS.labels(k.provider, str(k.idx), f"http_{code}").inc()
                    errors.append(f"{k.provider}#{k.idx}: HTTP {code}")
                    continue
                except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
                    k.fails += 1
                    k.cooldown_until = time.time() + 10
                    M.LLM_CALLS.labels(k.provider, str(k.idx), "error").inc()
                    errors.append(f"{k.provider}#{k.idx}: {type(e).__name__}")
                    continue
                finally:
                    k.inflight -= 1
                    M.LLM_LAT.labels(k.provider).observe(time.perf_counter() - t0)
                k.fails = 0
                M.LLM_CALLS.labels(k.provider, str(k.idx), "ok").inc()
                out = {"text": text, "provider": k.provider, "key": k.idx,
                       "latency_ms": round((time.perf_counter() - t0) * 1000), "purpose": purpose}
                if self.audit:
                    self.audit.llm_put(h, out)
                return {**out, "cached": False}
        M.FALLBACKS.labels("llm_all_keys_failed").inc()
        log.warning("llm.unavailable", extra={"event": "llm.unavailable", "errors": errors[:6]})
        raise LLMUnavailable("; ".join(errors[:6]) or "no usable key (all cooling down or disabled)")


# ---------- prompts + deterministic templates (the template is the default; the LLM only rewrites it) ----------
SYSTEM = ("You are an assistant in a fuel-supply operations center working on a SIMULATED network. "
          "Use only the facts given; never invent numbers, stations or events. Be concise and concrete. "
          "Refer to it as simulated where relevant. Plain text, no markdown headings.")


def _fmt(n):
    return f"{n:,.0f}" if isinstance(n, (int, float)) else str(n)


def decision_facts(d):
    exp = d.get("expected") or {}
    wa, wp = exp.get("without_action") or {}, exp.get("with_plan") or {}
    cands = sorted((c for c in d.get("candidates", []) if c.get("score") is not None), key=lambda c: c["score"])
    risks = [f"{r['station_id']} {r['fuel']}: p(stockout)={r['p_stockout']:.0%}, "
             f"stockout in {r['hours_to_stockout']}h, inventory {_fmt(r['inventory'])} L"
             for r in d.get("top_risks", [])[:4] if r["p_stockout"] > 0.05]
    ships = [f"{s['quantity']:,.0f} L {s['fuel_type']} {s['source_depot_id']} -> {s['destination_station_id']} "
             f"via {s['route_id']}" for s in d.get("shipments", [])]
    return {
        "tick": d.get("tick"), "regime": (d.get("router") or {}).get("regime"),
        "router": f"{(d.get('router') or {}).get('source')} suggested {(d.get('router') or {}).get('algorithm')}",
        "chosen": d.get("algorithm"),
        "alternatives": [f"{c['algorithm']} score {c['score']:,.0f}" for c in cands[:4]],
        "risks": risks, "shipments": ships,
        "impact": f"projected unmet {_fmt(wa.get('unmet'))} L -> {_fmt(wp.get('unmet'))} L, "
                  f"stockouts {wa.get('stockouts', '?')} -> {wp.get('stockouts', '?')} over the next "
                  f"{config.EVAL_TICKS} ticks" if wa else "n/a",
        "review_reasons": (d.get("gate") or {}).get("reasons", []),
        "alerts": [a["message"] for a in d.get("alerts", [])[:5]],
        "constraints": d.get("constraints", []),
        "notes": (d.get("gate") or {}).get("notes", []),
    }


def template_explanation(d):
    f = decision_facts(d)
    parts = [f"Tick {f['tick']}, regime {f['regime']}. The twin tournament chose {f['chosen']} ({f['router']})."]
    if f["risks"]:
        parts.append("At risk: " + "; ".join(f["risks"]) + ".")
    if f["shipments"]:
        parts.append("Plan: " + "; ".join(f["shipments"]) + ".")
    else:
        parts.append("Plan: no shipments needed.")
    parts.append(f"Expected (simulated): {f['impact']}.")
    if f["alternatives"]:
        parts.append("Alternatives scored: " + ", ".join(f["alternatives"]) + " (lower is better).")
    if f["constraints"]:
        parts.append("Constraints: " + "; ".join(f["constraints"]) + ".")
    if f["notes"]:
        parts.append("Notes: " + "; ".join(f["notes"]) + ".")
    if f["review_reasons"]:
        parts.append("Held for review because: " + "; ".join(f["review_reasons"]) + ".")
    return " ".join(parts)


def explanation_prompt(d):
    f = decision_facts(d)
    return ("Explain this allocation decision to the on-shift operator in 4-6 sentences: why these stations are at "
            "risk, what the plan does, the expected impact, why it beat the alternatives, and what to check before "
            f"approving.\nFacts: {f}")


def template_incident(event, snap, risks):
    at_risk = [f"{r['station_id']} {r['fuel']} (p={r['p_stockout']:.0%})" for r in risks[:4] if r["p_stockout"] >= 0.3]
    return (f"{event['type']} active from tick {event['start_tick']} to {event['end_tick']} "
            f"with parameters {event['parameters']}. Service level so far {snap['metrics']['service_level']:.2%}. "
            + (f"Most exposed: {', '.join(at_risk)}." if at_risk else "No station currently above 30% stockout risk."))


def incident_prompt(event, snap, risks):
    return ("Write a 3-4 sentence incident summary for the operations log: what happened, which stations/fuels are "
            "exposed, and what the automated planner will do (it re-plans every few ticks with a digital-twin "
            f"tournament). Facts: {template_incident(event, snap, risks)}")
