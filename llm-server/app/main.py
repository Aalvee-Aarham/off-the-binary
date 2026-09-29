"""FastAPI gateway in front of the llama-server model backends.

Endpoints (see README.md for full reference):
  GET  /                      service info
  GET  /ui                    browser test page (app/static/index.html)
  GET  /chat                  streaming chatbot page (app/static/chat.html)
  GET  /health                readiness (200 if >=1 model ready)
  GET  /health/live           liveness (always 200 while the process runs)
  GET  /metrics               Prometheus metrics
  GET  /api/models            model registry + live state
  POST /api/generate          prompt -> text
  POST /api/chat              messages -> text
  POST /api/structured        prompt + JSON schema -> guaranteed-valid JSON
  POST /api/classify          text + labels -> label + confidence
  GET  /v1/models             OpenAI-compatible model list
  POST /v1/chat/completions   OpenAI-compatible chat (supports stream=true)
  POST /api/admin/...         stop/start/restart a model, logs, cache clear
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

if __name__ == "__main__":
    # Launched as a file (`python app/main.py`, VS Code "Run"): the relative imports below
    # need the `app` package context, so hand off to uvicorn, which imports us as app.main.
    import sys
    from pathlib import Path

    import uvicorn

    _root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_root))
    os.chdir(_root)
    _local_llama = _root / ".tools" / "llama-cpu" / "llama-server.exe"
    if "LLAMA_SERVER_BIN" not in os.environ and _local_llama.exists():
        os.environ["LLAMA_SERVER_BIN"] = str(_local_llama)
    uvicorn.run("app.main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
    sys.exit(0)

from . import metrics as M
from .cache import ResponseCache
from .config import load_settings
from .manager import Backend, ModelManager
from .tunnel import Tunnel

__version__ = "1.0.0"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("llm.gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream call is too noisy

settings = load_settings()
cache = ResponseCache(settings.cache_size, settings.cache_ttl_s)
_state: dict[str, Any] = {}

TRACKED_PATHS = {"/api/generate", "/api/chat", "/api/structured", "/api/classify", "/v1/chat/completions"}


async def _update_gauges(manager: ModelManager) -> None:
    while True:
        for b in manager.backends.values():
            M.BACKEND_UP.labels(b.spec.id).set(1 if b.ready else 0)
            M.BACKEND_RESTARTS.labels(b.spec.id).set(b.restarts)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout_s, connect=5),
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
    )
    manager = ModelManager(settings, http)
    _state["http"], _state["manager"] = http, manager
    manager.start_all()
    gauges = asyncio.create_task(_update_gauges(manager))
    tunnel = None
    if settings.public_tunnel:
        if not settings.api_keys:
            log.error("PUBLIC_TUNNEL=1 refused: set API_KEY first (the server would be open to the internet)")
        else:
            tunnel = Tunnel(settings)
            tunnel.run()
    _state["tunnel"] = tunnel
    log.info("gateway %s up; models=%s default=%s auth=%s (%d key%s, admin key %s) public=%s",
             __version__, [m.id for m in settings.models], settings.default_model,
             "on" if settings.api_keys else "OFF", len(settings.api_keys),
             "" if len(settings.api_keys) == 1 else "s", "separate" if settings.admin_api_key else "shared",
             "tunnel" if tunnel else "no")
    yield
    gauges.cancel()
    if tunnel:
        await tunnel.shutdown()
    await manager.shutdown()
    await http.aclose()


app = FastAPI(
    title="BUP LLM Server",
    version=__version__,
    description="llama.cpp-backed gateway serving SmolLM2-360M, SmolLM2-1.7B and Gemma-3-1B.",
    lifespan=lifespan,
)


# Browser access (test UI opened from another origin or a local file). Auth is still the API key.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Served-Model", "X-Fallback-Used"],
)

UI_FILE = os.path.join(os.path.dirname(__file__), "static", "index.html")
CHAT_FILE = os.path.join(os.path.dirname(__file__), "static", "chat.html")


def manager() -> ModelManager:
    return _state["manager"]


def http() -> httpx.AsyncClient:
    return _state["http"]


# ---------------------------------------------------------------- observability


@app.middleware("http")
async def observe(request: Request, call_next):
    t0 = time.perf_counter()
    request.state.model = "-"
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        path = request.url.path
        if path in TRACKED_PATHS:
            M.REQUESTS.labels(path, request.state.model, str(status)).inc()
            M.LATENCY.labels(path, request.state.model).observe(time.perf_counter() - t0)


# ---------------------------------------------------------------- auth

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _key_matches(supplied: str, allowed: tuple[str, ...]) -> bool:
    # Compare against every key (no early exit) so timing doesn't reveal which one matched.
    ok = False
    for k in allowed:
        ok |= hmac.compare_digest(supplied.encode(), k.encode())
    return ok


def _supplied_key(creds: HTTPAuthorizationCredentials | None, key: str | None) -> str:
    return key or (creds.credentials if creds else "")


async def require_key(
    creds: HTTPAuthorizationCredentials | None = Security(_bearer),
    key: str | None = Security(_api_key_header),
) -> None:
    if not settings.api_keys:
        return
    supplied = _supplied_key(creds, key)
    if not supplied or not _key_matches(supplied, settings.api_keys):
        raise HTTPException(401, "invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})


async def require_admin(
    creds: HTTPAuthorizationCredentials | None = Security(_bearer),
    key: str | None = Security(_api_key_header),
) -> None:
    """Admin endpoints need ADMIN_API_KEY when it is set, otherwise any client key."""
    if not settings.admin_api_key:
        return await require_key(creds, key)
    supplied = _supplied_key(creds, key)
    if not supplied or not _key_matches(supplied, (settings.admin_api_key,)):
        raise HTTPException(403, "admin key required", headers={"WWW-Authenticate": "Bearer"})


# ---------------------------------------------------------------- schemas


class GenParams(BaseModel):
    model: str | None = Field(None, description="Model id or alias. Default: server default model.")
    max_tokens: int = Field(256, ge=1, le=4096)
    temperature: float = Field(0.7, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    stop: list[str] | None = None
    seed: int | None = None
    allow_fallback: bool = Field(True, description="Use another ready model if this one is down.")
    use_cache: bool = Field(True, description="Cache responses when temperature == 0.")


class GenerateRequest(GenParams):
    prompt: str = Field(..., min_length=1)
    system: str | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(GenParams):
    messages: list[Message] = Field(..., min_length=1)


class StructuredRequest(GenParams):
    model_config = ConfigDict(populate_by_name=True)
    prompt: str = Field(..., min_length=1)
    system: str | None = None
    json_schema: dict[str, Any] = Field(..., alias="schema", description="JSON Schema the output must match.")
    temperature: float = Field(0.0, ge=0, le=2)
    max_tokens: int = Field(512, ge=1, le=4096)


class ClassifyRequest(BaseModel):
    text: str = Field(..., min_length=1)
    labels: list[str] = Field(..., min_length=2, max_length=50)
    instruction: str | None = Field(None, description="Task description, e.g. 'Classify the supply regime.'")
    model: str | None = None
    allow_fallback: bool = True
    use_cache: bool = True


# ---------------------------------------------------------------- core


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str, retryable: bool):
        super().__init__(detail)
        self.status, self.detail, self.retryable = status, detail, retryable


def resolve_or_404(name: str | None) -> Backend:
    b = manager().resolve(name)
    if b is None:
        known = sorted({a for m in settings.models for a in (m.id, *m.aliases)})
        raise HTTPException(404, f"unknown model '{name}'. Known: {known}")
    return b


async def _post_chat(b: Backend, payload: dict) -> dict:
    if not b.ready:
        raise UpstreamError(503, f"model '{b.spec.id}' is {b.state}", retryable=True)
    try:
        r = await http().post(f"{b.url}/v1/chat/completions", json=payload)
    except httpx.TimeoutException:
        raise UpstreamError(504, f"model '{b.spec.id}' timed out", retryable=False)
    except httpx.HTTPError as exc:
        raise UpstreamError(503, f"model '{b.spec.id}' unreachable: {exc}", retryable=True)
    if r.status_code >= 500:
        raise UpstreamError(502, f"model '{b.spec.id}' error: {r.text[:500]}", retryable=True)
    if r.status_code >= 400:
        raise UpstreamError(r.status_code, r.text[:1000], retryable=False)
    return r.json()


def _record_usage(b: Backend, data: dict) -> None:
    usage = data.get("usage") or {}
    M.TOKENS.labels(b.spec.id, "prompt").inc(usage.get("prompt_tokens", 0))
    M.TOKENS.labels(b.spec.id, "completion").inc(usage.get("completion_tokens", 0))
    tps = (data.get("timings") or {}).get("predicted_per_second")
    if tps:
        M.TOKENS_PER_SECOND.labels(b.spec.id).observe(tps)


async def run_chat(request: Request, model: str | None, payload: dict, allow_fallback: bool) -> tuple[Backend, dict, bool]:
    """Send a chat request to the requested model, falling back to other ready models."""
    requested = resolve_or_404(model)
    request.state.model = requested.spec.id
    last: UpstreamError | None = None
    for b in manager().candidates(requested, allow_fallback):
        try:
            data = await _post_chat(b, {**payload, "model": b.spec.id})
        except UpstreamError as exc:
            last = exc
            log.warning("request to %s failed: %s", b.spec.id, exc.detail)
            if not exc.retryable:
                break
            continue
        fallback = b is not requested
        if fallback:
            M.FALLBACKS.labels(requested.spec.id, b.spec.id).inc()
            request.state.model = b.spec.id
        _record_usage(b, data)
        return b, data, fallback
    assert last is not None
    raise HTTPException(last.status, last.detail)


def _gen_payload(p: GenParams, messages: list[dict]) -> dict:
    payload: dict[str, Any] = {"messages": messages, "max_tokens": p.max_tokens, "temperature": p.temperature}
    if p.top_p is not None:
        payload["top_p"] = p.top_p
    if p.stop:
        payload["stop"] = p.stop
    if p.seed is not None:
        payload["seed"] = p.seed
    return payload


def _shape(b: Backend, data: dict, fallback: bool, t0: float) -> dict:
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    timings = data.get("timings") or {}
    tps = timings.get("predicted_per_second")
    return {
        "model": b.spec.id,
        "text": (choice.get("message") or {}).get("content") or "",
        "finish_reason": choice.get("finish_reason"),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "tokens_per_second": round(tps, 1) if tps else None,
        "fallback_used": fallback,
        "cached": False,
    }


async def _cached(request: Request, endpoint: str, req: BaseModel, cacheable: bool, produce) -> dict:
    """Serve from cache if possible, otherwise call produce() and cache the result."""
    t0 = time.perf_counter()
    key = None
    if cacheable:
        requested = resolve_or_404(getattr(req, "model", None))
        key = cache.key(endpoint, requested.spec.id, req.model_dump(exclude={"use_cache", "allow_fallback"}))
        hit = cache.get(key)
        if hit is not None:
            M.CACHE_HITS.labels(endpoint).inc()
            request.state.model = hit["model"]
            return {**hit, "cached": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    result = await produce(t0)
    if key and not result.get("fallback_used"):
        cache.put(key, result)
    return result


# ---------------------------------------------------------------- public endpoints


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "bup-llm-server",
        "version": __version__,
        "models": [m.id for m in settings.models],
        "default_model": settings.default_model,
        "ui": "/ui",
        "chat": "/chat",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(UI_FILE, media_type="text/html")


@app.get("/chat", include_in_schema=False)
async def chat_page() -> FileResponse:
    return FileResponse(CHAT_FILE, media_type="text/html")


@app.get("/health/live", tags=["ops"])
async def live() -> dict:
    return {"status": "alive"}


@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    backends = manager().backends.values()
    ready = [b for b in backends if b.ready]
    status = "ok" if len(ready) == len(backends) else ("degraded" if ready else "unavailable")
    body = {"status": status, "models": {b.spec.id: b.state for b in backends}, "cache_items": len(cache)}
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/metrics", tags=["ops"], include_in_schema=False)
async def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/models", tags=["models"], dependencies=[Depends(require_key)])
async def list_models() -> dict:
    return {
        "default_model": settings.default_model,
        "fallback_order": list(settings.fallback_order),
        "models": [b.info() for b in manager().backends.values()],
    }


@app.post("/api/generate", tags=["inference"], dependencies=[Depends(require_key)])
async def generate(req: GenerateRequest, request: Request) -> dict:
    messages = ([{"role": "system", "content": req.system}] if req.system else []) + [
        {"role": "user", "content": req.prompt}
    ]

    async def produce(t0: float) -> dict:
        b, data, fb = await run_chat(request, req.model, _gen_payload(req, messages), req.allow_fallback)
        return _shape(b, data, fb, t0)

    return await _cached(request, "generate", req, req.use_cache and req.temperature == 0, produce)


@app.post("/api/chat", tags=["inference"], dependencies=[Depends(require_key)])
async def chat(req: ChatRequest, request: Request) -> dict:
    messages = [m.model_dump() for m in req.messages]

    async def produce(t0: float) -> dict:
        b, data, fb = await run_chat(request, req.model, _gen_payload(req, messages), req.allow_fallback)
        return _shape(b, data, fb, t0)

    return await _cached(request, "chat", req, req.use_cache and req.temperature == 0, produce)


@app.post("/api/structured", tags=["inference"], dependencies=[Depends(require_key)])
async def structured(req: StructuredRequest, request: Request) -> dict:
    system = (req.system or "You extract information and answer strictly in JSON.") + (
        "\nRespond ONLY with a JSON object matching this JSON Schema:\n" + json.dumps(req.json_schema)
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": req.prompt}]
    payload = _gen_payload(req, messages)
    payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": req.json_schema}}

    async def produce(t0: float) -> dict:
        b, data, fb = await run_chat(request, req.model, payload, req.allow_fallback)
        out = _shape(b, data, fb, t0)
        raw = out.pop("text")
        try:
            out["data"] = json.loads(raw)
        except json.JSONDecodeError:
            hint = " (output truncated: raise max_tokens)" if out["finish_reason"] == "length" else ""
            raise HTTPException(502, {"error": f"model returned invalid JSON{hint}", "raw": raw})
        return out

    return await _cached(request, "structured", req, req.use_cache and req.temperature == 0, produce)


def _gbnf_literal(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _label_scores(labels: list[str], first_token: dict | None) -> dict[str, float]:
    """Approximate P(label) from the first generated token's top logprobs.

    Each label gets the highest probability among top tokens that are a
    (case-insensitive) prefix of it; scores are then normalised to sum to 1.
    """
    if not first_token:
        return {}
    mass = {label: 0.0 for label in labels}
    for cand in first_token.get("top_logprobs") or []:
        tok = (cand.get("token") or "").strip().lower()
        if not tok:
            continue
        p = math.exp(cand.get("logprob", -math.inf))
        for label in labels:
            if label.lower().startswith(tok):
                mass[label] = max(mass[label], p)
    total = sum(mass.values())
    return {label: round(m / total, 4) for label, m in mass.items()} if total > 0 else {}


@app.post("/api/classify", tags=["inference"], dependencies=[Depends(require_key)])
async def classify(req: ClassifyRequest, request: Request) -> dict:
    labels = list(dict.fromkeys(label.strip() for label in req.labels if label.strip()))
    if len(labels) < 2:
        raise HTTPException(422, "need at least 2 distinct non-empty labels")
    instruction = req.instruction or "Classify the text into exactly one of the allowed labels."
    messages = [
        {"role": "system", "content": "You are a precise classifier. Reply with exactly one allowed label and nothing else."},
        {"role": "user", "content": f"{instruction}\n\nAllowed labels: {', '.join(labels)}\n\nText:\n{req.text}\n\nLabel:"},
    ]
    payload = {
        "messages": messages,
        "max_tokens": 64,
        "temperature": 0,
        "grammar": "root ::= " + " | ".join(_gbnf_literal(label) for label in labels),
        "logprobs": True,
        "top_logprobs": 20,
    }

    async def produce(t0: float) -> dict:
        b, data, fb = await run_chat(request, req.model, payload, req.allow_fallback)
        out = _shape(b, data, fb, t0)
        text = out.pop("text").strip()
        label = next((l for l in labels if l == text), None) or next(
            (l for l in labels if l.lower() == text.lower()), text
        )
        tokens = ((data["choices"][0].get("logprobs") or {}).get("content")) or []
        scores = _label_scores(labels, tokens[0] if tokens else None)
        if label in scores and scores[label] > 0:
            confidence = scores[label]
        elif tokens:
            confidence = round(math.exp(tokens[0].get("logprob", -math.inf)), 4)
        else:
            confidence = None
        return {"label": label, "confidence": confidence, "scores": scores, **out}

    return await _cached(request, "classify", req, req.use_cache, produce)


# ---------------------------------------------------------------- OpenAI-compatible


@app.get("/v1/models", tags=["openai"], dependencies=[Depends(require_key)])
async def openai_models() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": b.spec.id, "object": "model", "owned_by": "local", "ready": b.ready}
            for b in manager().backends.values()
        ],
    }


@app.post("/v1/chat/completions", tags=["openai"], dependencies=[Depends(require_key)])
async def openai_chat(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(400, "'messages' is required")

    if not body.get("stream"):
        b, data, fb = await run_chat(request, body.get("model"), body, allow_fallback=True)
        data["model"] = b.spec.id
        return JSONResponse(data, headers={"X-Served-Model": b.spec.id, "X-Fallback-Used": str(fb).lower()})

    # Streaming: pass llama-server's SSE stream straight through (no fallback mid-stream).
    requested = resolve_or_404(body.get("model"))
    candidates = manager().candidates(requested, allow_fallback=True)
    b = next((c for c in candidates if c.ready), None)
    if b is None:
        raise HTTPException(503, f"model '{requested.spec.id}' is {requested.state} and no fallback is ready")
    request.state.model = b.spec.id
    body["model"] = b.spec.id
    try:
        upstream = await http().send(
            http().build_request("POST", f"{b.url}/v1/chat/completions", json=body), stream=True
        )
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"model '{b.spec.id}' unreachable: {exc}")
    if upstream.status_code != 200:
        content = await upstream.aread()
        await upstream.aclose()
        return Response(content, status_code=upstream.status_code, media_type="application/json")

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"X-Served-Model": b.spec.id})


# ---------------------------------------------------------------- admin


def _backend_or_404(model: str) -> Backend:
    b = manager().resolve(model)
    if b is None:
        raise HTTPException(404, f"unknown model '{model}'")
    return b


@app.post("/api/admin/models/{model}/stop", tags=["admin"], dependencies=[Depends(require_admin)])
async def admin_stop(model: str) -> dict:
    b = _backend_or_404(model)
    await b.stop(admin=True)
    return b.info()


@app.post("/api/admin/models/{model}/start", tags=["admin"], dependencies=[Depends(require_admin)])
async def admin_start(model: str) -> dict:
    b = _backend_or_404(model)
    await b.start()
    return b.info()


@app.post("/api/admin/models/{model}/restart", tags=["admin"], dependencies=[Depends(require_admin)])
async def admin_restart(model: str) -> dict:
    b = _backend_or_404(model)
    await b.stop()
    await b.start()
    return b.info()


@app.get("/api/admin/models/{model}/logs", tags=["admin"], dependencies=[Depends(require_admin)],
         response_class=PlainTextResponse)
async def admin_logs(model: str, lines: int = Query(100, ge=1, le=5000)) -> str:
    return _backend_or_404(model).tail_log(lines)


@app.post("/api/admin/cache/clear", tags=["admin"], dependencies=[Depends(require_admin)])
async def admin_cache_clear() -> dict:
    return {"cleared": cache.clear()}
