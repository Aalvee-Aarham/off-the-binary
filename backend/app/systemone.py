"""One client for both System-One models: Laya (`laya-serve`, local) and TypeSafe Jev (OpenRouter).
Same wire protocol (POST /v1/systemone), different base URL / key / model."""
import time
from collections import deque

import httpx

from . import metrics as M
from .sim_client import Breaker


class SystemOneError(Exception):
    pass


class SystemOneClient:
    def __init__(self, name, base_url, api_key="", model=None, timeout=2.0, transport=None, max_calls_per_hour=0):
        self.name, self.model = name, model
        self.max_calls, self.calls = max_calls_per_hour, deque()  # 0 = unlimited (local Laya); paid APIs get a budget
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
        self.breaker = Breaker(window=10, min_calls=3, ratio=0.5, cooldown=15.0)
        self.disabled = None  # set on 401/403: a bad key never gets better by retrying

    async def ask(self, state, questions):
        if self.disabled:
            raise SystemOneError(f"{self.name}: disabled ({self.disabled})")
        if self.max_calls:
            now = time.time()
            while self.calls and now - self.calls[0] > 3600:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                M.SYSTEMONE_CALLS.labels(self.name, "budget").inc()
                raise SystemOneError(f"{self.name}: hourly call budget ({self.max_calls}) spent")
            self.calls.append(now)
        if not self.breaker.allow():
            raise SystemOneError(f"{self.name}: breaker open")
        body = {"state": state, "questions": questions}
        if self.model:
            body["model"] = self.model
        t0 = time.perf_counter()
        try:
            r = await self.http.post("/v1/systemone", json=body)
            r.raise_for_status()
            data = r.json()
            answers = data["answers"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                self.disabled = f"auth rejected ({e.response.status_code}): check the API key"
            self.breaker.failure()
            M.SYSTEMONE_CALLS.labels(self.name, "error").inc()
            raise SystemOneError(f"{self.name}: {type(e).__name__}: {str(e)[:200]}")
        finally:
            M.SYSTEMONE_LAT.labels(self.name).observe(time.perf_counter() - t0)
        self.breaker.success()
        M.SYSTEMONE_CALLS.labels(self.name, "ok").inc()
        return answers, round((time.perf_counter() - t0) * 1000, 1), data.get("usage")

    async def health(self):
        try:
            r = await self.http.get("/health", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False
