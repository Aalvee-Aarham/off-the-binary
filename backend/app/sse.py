"""SSE is a hint, REST is truth: every event just wakes the orchestrator, which re-GETs state.
Polling (orchestrator timeout) covers us whenever the stream is down."""
import asyncio
import json
import logging
import random

import httpx

from . import metrics as M

log = logging.getLogger("sse")


async def listen(base_url, on_event, transport=None, silent=lambda: False):
    """`silent()` is the tick watchdog: the server drops slow subscribers without closing the stream (guide 6.1),
    so a connection that only sends keepalives while the simulator runs is torn down and re-established."""
    backoff = 1.0
    async with httpx.AsyncClient(base_url=base_url, transport=transport,
                                 timeout=httpx.Timeout(5.0, read=45.0)) as http:  # keepalive every 15s
        while True:
            try:
                async with http.stream("GET", "/v1/stream") as r:
                    if r.status_code != 200:
                        raise httpx.HTTPStatusError(f"stream {r.status_code}", request=r.request, response=r)
                    M.SSE_CONNECTED.set(1)
                    log.info("sse.connected", extra={"event": "sse.connected"})
                    on_event("sse.connected", {})  # no replay: refetch state
                    backoff = 1.0
                    name, data = None, []
                    async for line in r.aiter_lines():
                        if silent():
                            raise RuntimeError("no simulation.tick while RUNNING: stream silently dropped")
                        if line.startswith("event:"):
                            name = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif line == "" and name:
                            try:
                                payload = json.loads("\n".join(data)) if data else {}
                            except ValueError:
                                payload = {}
                            on_event(name, payload)
                            name, data = None, []
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("sse.disconnected", extra={"event": "sse.disconnected", "error": str(e)[:200]})
            M.SSE_CONNECTED.set(0)
            M.SSE_RECONNECTS.inc()
            await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
            backoff = min(30.0, backoff * 2)
