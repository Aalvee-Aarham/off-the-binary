"""Optional public HTTPS URL for this server via a Cloudflare quick tunnel.

Enabled with PUBLIC_TUNNEL=1. Needs only the `cloudflared` binary (no account):
it opens an outbound connection to Cloudflare and gets a random
https://<words>.trycloudflare.com URL that forwards to http://127.0.0.1:<PORT>.
The URL changes every time the tunnel restarts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time

from .config import Settings
from .manager import _attach_to_job

log = logging.getLogger("llm.tunnel")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class Tunnel:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.url: str | None = None
        self.state = "starting"
        self.restarts = 0
        self.proc: subprocess.Popen | None = None
        self.log_path = settings.log_dir / "cloudflared.log"
        self.url_file = settings.log_dir / "public-url.txt"
        self._task: asyncio.Task | None = None

    def run(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="tunnel")

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
        self._kill()

    def info(self) -> dict:
        return {"enabled": True, "state": self.state, "url": self.url, "restarts": self.restarts}

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _spawn(self) -> None:
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        target = f"http://127.0.0.1:{self.settings.port}"
        cmd = [self.settings.cloudflared_bin, "tunnel", "--no-autoupdate", "--url", target]
        log.info("starting public tunnel -> %s", target)
        logfile = open(self.log_path, "wb")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, creationflags=flags)
        _attach_to_job(self.proc)
        logfile.close()

    def _find_url(self) -> str | None:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        found = URL_RE.findall(text)
        return found[-1] if found else None

    async def _supervise(self) -> None:
        backoff = 5.0
        while True:
            try:
                self.state, self.url = "starting", None
                self._spawn()
                deadline = time.time() + 60
                while self.proc and self.proc.poll() is None and self.url is None and time.time() < deadline:
                    await asyncio.sleep(0.5)
                    self.url = self._find_url()
                if self.url:
                    self.state = "online"
                    backoff = 5.0
                    self.url_file.write_text(self.url + "\n", encoding="utf-8")
                    log.warning("PUBLIC URL: %s   (chat: %s/chat, docs: %s/docs)", self.url, self.url, self.url)
                    while self.proc and self.proc.poll() is None:
                        await asyncio.sleep(2)
                self.state = "down"
                log.error("tunnel stopped (code %s); restarting in %.0fs — the URL will change",
                          self.proc.returncode if self.proc else "?", backoff)
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                self.state = "down"
                log.error("cloudflared not found (%s); set CLOUDFLARED_BIN", self.settings.cloudflared_bin)
                backoff = 60.0
            except Exception:
                self.state = "down"
                log.exception("tunnel error")
            self._kill()
            self.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
