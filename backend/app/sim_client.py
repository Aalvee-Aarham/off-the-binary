"""Resilient simulator client: timeouts, jittered retries, circuit breaker, schema validation, stale detection."""
import asyncio
import logging
import random
import time
from collections import deque

import httpx
from pydantic import ValidationError

from . import config
from . import metrics as M
from .models import SCHEMAS

log = logging.getLogger("sim")


class SimError(Exception):
    def __init__(self, code, message="", status=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


class SimRejected(SimError):
    """4xx domain rejection. Never retried, doesn't trip the breaker."""


class SimUnavailable(SimError):
    """Timeouts / 5xx / breaker open after retries."""


class SimInvalid(SimError):
    """Response failed schema or semantic validation."""


class Breaker:
    """Rate-based: opens when >= FAIL_RATIO of the last WINDOW requests failed *after retries*.
    A flaky simulator (retries win) never trips it; a dead one trips it within ~1 refresh."""
    CLOSED, HALF_OPEN, OPEN = 0, 1, 2

    def __init__(self, window=config.BREAKER_WINDOW, min_calls=config.BREAKER_MIN_CALLS,
                 ratio=config.BREAKER_FAIL_RATIO, cooldown=config.BREAKER_COOLDOWN_S):
        self.min_calls, self.ratio, self.cooldown = min_calls, ratio, cooldown
        self.results = deque(maxlen=window)
        self.state, self.opened_at, self.trial = self.CLOSED, 0.0, False

    def allow(self):
        if self.state == self.OPEN and time.monotonic() - self.opened_at >= self.cooldown:
            self._set(self.HALF_OPEN)
            self.trial = False
        if self.state == self.HALF_OPEN:
            if self.trial:  # one probe at a time
                return False
            self.trial = True
            return True
        return self.state == self.CLOSED

    def success(self):
        if self.state != self.CLOSED:
            self.results.clear()
        self.results.append(True)
        self._set(self.CLOSED)

    def failure(self):
        self.results.append(False)
        fails = self.results.count(False)
        if self.state == self.HALF_OPEN or (len(self.results) >= self.min_calls and fails / len(self.results) >= self.ratio):
            self.opened_at = time.monotonic()
            self._set(self.OPEN)
            log.warning("breaker.open", extra={"event": "breaker.open", "failures": fails})

    def _set(self, s):
        if s != self.state and s == self.CLOSED:
            log.info("breaker.closed", extra={"event": "breaker.close"})
        self.state = s
        M.SIM_BREAKER.set(s)

    @property
    def name(self):
        return ("closed", "half_open", "open")[self.state]


def _error_code(resp):
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP_{resp.status_code}", resp.text[:200]
    err = (body.get("error") or body.get("detail")) if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code", f"HTTP_{resp.status_code}"), err.get("message", "")
    if isinstance(err, list):
        return "VALIDATION_ERROR", str(err)[:300]
    return f"HTTP_{resp.status_code}", str(body)[:200]


class SimClient:
    def __init__(self, base_url=config.SIM_BASE_URL, transport=None):
        timeout = httpx.Timeout(config.SIM_READ_TIMEOUT, connect=config.SIM_CONNECT_TIMEOUT)
        self.http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self.breaker = Breaker()
        self.stale = False

    async def close(self):
        await self.http.aclose()

    async def _request(self, method, path, *, params=None, json=None, schema_key=None):
        schema = SCHEMAS.get(schema_key or path)
        label = path if path.startswith("/v1/") and path.count("/") <= 2 else path.rsplit("/", 1)[0] + "/*"
        if not self.breaker.allow():
            M.SIM_CALLS.labels(method, label, "breaker_open").inc()
            raise SimUnavailable("BREAKER_OPEN", "simulator circuit breaker is open")
        last = None
        for attempt in range(config.SIM_RETRIES):
            if attempt:
                M.SIM_RETRIES.labels(label).inc()
                await asyncio.sleep(random.uniform(0, min(1.0, 0.1 * 2 ** attempt)))  # full jitter, <=1.4s total
            t0 = time.perf_counter()
            try:
                resp = await self.http.request(method, path, params=params, json=json)
            except httpx.HTTPError as e:
                last = SimUnavailable("NETWORK", f"{type(e).__name__}: {e}")
                M.SIM_CALLS.labels(method, label, "network_error").inc()
                continue
            finally:
                M.SIM_LAT.labels(method, label).observe(time.perf_counter() - t0)
            if resp.status_code >= 500:
                code, msg = _error_code(resp)
                last = SimUnavailable(code, msg, resp.status_code)
                M.SIM_CALLS.labels(method, label, "5xx").inc()
                continue
            self.breaker.success()
            if resp.status_code >= 400:
                code, msg = _error_code(resp)
                M.SIM_CALLS.labels(method, label, "rejected").inc()
                raise SimRejected(code, msg, resp.status_code)
            M.SIM_CALLS.labels(method, label, "ok").inc()
            if method == "GET":
                self.stale = resp.headers.get("X-Simulator-Stale", "").lower() == "true"
                M.SIM_STALE.set(int(self.stale))
            try:
                data = resp.json()
            except ValueError:
                M.SIM_INVALID.labels(label).inc()
                raise SimInvalid("NOT_JSON", f"{path} returned non-JSON")
            if schema is None:
                return data
            try:
                parsed = schema.validate_python(data)
            except ValidationError as e:
                M.SIM_INVALID.labels(label).inc()
                raise SimInvalid("SCHEMA", f"{path}: {e.error_count()} errors, first: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}")
            return [p.model_dump() for p in parsed] if isinstance(parsed, list) else parsed.model_dump()
        self.breaker.failure()  # counted once per request, after retries
        raise last

    async def get(self, path, **params):
        return await self._request("GET", path, params=params or None)

    async def create_allocation(self, body):
        # Safe to retry: idempotency_key is fixed per shipment, so a replay returns the original allocation.
        return await self._request("POST", "/v1/allocations", json=body, schema_key="POST /v1/allocations")

    async def cancel_allocation(self, alloc_id):
        return await self._request("POST", f"/v1/allocations/{int(alloc_id)}/cancel", schema_key="POST /v1/allocations")

    async def health(self):
        """/v1/health bypasses faults; single attempt, no breaker."""
        try:
            r = await self.http.get("/v1/health", timeout=2.0)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def admin(self, method, path, json=None):
        """/admin/* bypasses fault injection: single attempt, no breaker."""
        try:
            r = await self.http.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise SimUnavailable("NETWORK", str(e))
        if r.status_code >= 400:
            code, msg = _error_code(r)
            raise SimRejected(code, msg, r.status_code)
        return r.json()
