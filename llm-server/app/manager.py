"""Starts, health-checks and restarts one llama-server process per model."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import httpx

from .config import ModelSpec, Settings

log = logging.getLogger("llm.manager")

# Backend states
STOPPED, STARTING, READY, CRASHED, EXTERNAL_DOWN = "stopped", "starting", "ready", "crashed", "unreachable"


class StartupError(Exception):
    """A backend can't be started for a reason that retrying soon won't fix."""


def _windows_kill_on_close_job():
    """Windows: a job object that kills every assigned child when this process dies (even if killed hard)."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadB", "WriteB", "OtherB")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation

    def assign(proc: subprocess.Popen) -> None:
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            log.warning("could not attach pid %s to job object (err %s)", proc.pid, ctypes.get_last_error())

    return assign


_attach_to_job = _windows_kill_on_close_job() if sys.platform == "win32" else (lambda proc: None)


class Backend:
    def __init__(self, spec: ModelSpec, settings: Settings, http: httpx.AsyncClient):
        self.spec = spec
        self.settings = settings
        self.http = http
        self.external = spec.url is not None
        self.url = (spec.url or f"http://{settings.llama_host}:{spec.port}").rstrip("/")
        self.proc: subprocess.Popen | None = None
        self.state = STARTING
        self.admin_stopped = False
        self.restarts = 0
        self.started_at: float | None = None
        self.ready_at: float | None = None
        self.last_error: str | None = None
        self.log_path = settings.log_dir / f"{spec.id}.log"
        self._task: asyncio.Task | None = None

    # ---------- public ----------

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def model_path(self) -> Path:
        return self.settings.models_dir / self.spec.file

    def run(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"supervise-{self.spec.id}")

    async def stop(self, admin: bool = False) -> None:
        self.admin_stopped = admin or self.admin_stopped
        self._kill()
        self.state = STOPPED

    async def start(self) -> None:
        self.admin_stopped = False
        if self.state == STOPPED:
            self.state = STARTING

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        self._kill()

    def info(self) -> dict:
        return {
            "id": self.spec.id,
            "aliases": list(self.spec.aliases),
            "description": self.spec.description,
            "file": self.spec.file,
            "repo": self.spec.repo,
            "state": self.state,
            "ready": self.ready,
            "external": self.external,
            "ctx_size": self.spec.ctx_size,
            "parallel": self.spec.parallel,
            "restarts": self.restarts,
            "uptime_s": round(time.time() - self.ready_at, 1) if self.ready and self.ready_at else None,
            "load_time_s": round(self.ready_at - self.started_at, 2) if self.ready_at and self.started_at else None,
            "last_error": self.last_error,
        }

    def tail_log(self, lines: int = 100) -> str:
        if not self.log_path.exists():
            return ""
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(deque(fh, maxlen=lines))

    # ---------- internals ----------

    def _binary(self) -> str:
        """Absolute path of llama-server (Windows CreateProcess won't resolve relative paths with '/')."""
        raw = self.settings.llama_server_bin
        if os.sep in raw or "/" in raw:
            path = Path(raw).resolve()
            if not path.exists():
                raise FileNotFoundError(f"LLAMA_SERVER_BIN not found: {path}")
            return str(path)
        found = shutil.which(raw)
        if not found:
            raise FileNotFoundError(f"'{raw}' not on PATH; set LLAMA_SERVER_BIN")
        return found

    def _command(self) -> list[str]:
        s, spec = self.settings, self.spec
        cmd = [
            self._binary(),
            "-m", str(self.model_path.resolve()),
            "--host", s.llama_host,
            "--port", str(spec.port),
            "-c", str(spec.ctx_size),
            "-np", str(spec.parallel),
            "--alias", spec.id,
            "--jinja",
            "--metrics",
            "--no-webui",
        ]
        threads = s.threads_override or spec.threads
        if threads > 0:
            cmd += ["-t", str(threads)]
        cmd += list(spec.extra_args)
        return cmd

    def _port_busy(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((self.settings.llama_host, self.spec.port)) == 0

    def _spawn(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"model file missing: {self.model_path}")
        if self._port_busy():
            # Otherwise a leftover llama-server from an earlier run would answer our health checks.
            raise StartupError(
                f"port {self.spec.port} is already in use (another llama-server/gateway still running?)"
            )
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        logfile = open(self.log_path, "ab")
        cmd = self._command()
        log.info("starting %s: %s", self.spec.id, " ".join(cmd))
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, creationflags=flags)
        _attach_to_job(self.proc)
        logfile.close()  # child keeps its own handle
        self.started_at = time.time()
        self.ready_at = None

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    async def _healthy(self) -> bool:
        try:
            r = await self.http.get(f"{self.url}/health", timeout=3)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _supervise(self) -> None:
        backoff = 2.0
        while True:
            try:
                if self.external:
                    await self._watch_external()
                elif self.admin_stopped:
                    await asyncio.sleep(1)
                elif self.proc is None or self.proc.poll() is not None:
                    await self._start_and_wait(backoff)
                    backoff = 2.0 if self.ready else min(backoff * 2, 30)
                else:
                    ok = await self._healthy()
                    if not ok and self.proc and self.proc.poll() is None and self.state == READY:
                        log.warning("%s health check failed (process alive)", self.spec.id)
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except (FileNotFoundError, StartupError) as exc:  # missing file / busy port: one line, slow retry
                if self.last_error != str(exc):
                    log.error("%s cannot start: %s", self.spec.id, exc)
                self.last_error = str(exc)
                self.state = CRASHED
                await asyncio.sleep(10)
            except Exception as exc:  # keep the supervisor alive no matter what
                self.last_error = str(exc)
                self.state = CRASHED
                log.exception("%s supervisor error", self.spec.id)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _start_and_wait(self, backoff: float) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            if self.admin_stopped:
                return
            self.restarts += 1
            self.state = CRASHED
            self.last_error = f"llama-server exited with code {code}"
            log.error("%s exited (code %s); restarting in %.0fs. Log tail:\n%s",
                      self.spec.id, code, backoff, self.tail_log(15))
            await asyncio.sleep(backoff)
            if self.admin_stopped:
                return

        self.state = STARTING
        self._spawn()
        deadline = time.time() + self.settings.startup_timeout_s
        while time.time() < deadline:
            if self.admin_stopped:
                return
            if self.proc is None or self.proc.poll() is not None:
                return  # died during load -> handled on next loop iteration
            if await self._healthy():
                self.state = READY
                self.ready_at = time.time()
                self.last_error = None
                log.info("%s ready in %.1fs", self.spec.id, self.ready_at - self.started_at)
                return
            await asyncio.sleep(0.5)
        self.last_error = f"not ready after {self.settings.startup_timeout_s:.0f}s"
        log.error("%s %s; killing", self.spec.id, self.last_error)
        self._kill()

    async def _watch_external(self) -> None:
        if self.admin_stopped:
            self.state = STOPPED
        elif await self._healthy():
            if self.state != READY:
                self.ready_at = time.time()
            self.state = READY
        else:
            self.state = EXTERNAL_DOWN
        await asyncio.sleep(2)


class ModelManager:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.backends: dict[str, Backend] = {m.id: Backend(m, settings, http) for m in settings.models}
        self._alias: dict[str, str] = {}
        for m in settings.models:
            for name in (m.id, *m.aliases):
                self._alias[name.lower()] = m.id

    def start_all(self) -> None:
        for b in self.backends.values():
            b.run()

    async def shutdown(self) -> None:
        for b in self.backends.values():
            await b.shutdown()

    def resolve(self, name: str | None) -> Backend | None:
        if not name:
            return self.backends[self.settings.default_model]
        model_id = self._alias.get(name.lower())
        return self.backends.get(model_id) if model_id else None

    def candidates(self, requested: Backend, allow_fallback: bool) -> list[Backend]:
        """Requested model first, then healthy fallbacks in configured order."""
        order = [requested]
        if allow_fallback:
            order += [self.backends[m] for m in self.settings.fallback_order
                      if m != requested.spec.id and self.backends[m].ready]
        return order
