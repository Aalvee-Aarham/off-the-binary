#!/usr/bin/env python3
"""Download the GGUF files listed in models.toml from Hugging Face.

Stdlib only, so it runs before `pip install` (used by the Dockerfile).
Resumes partial downloads (*.part), verifies size + SHA-256 against Hugging Face,
and skips files that already exist (use --verify to re-check them).

    python scripts/download_models.py                 # -> ./models
    python scripts/download_models.py --dest /models  # Docker
    python scripts/download_models.py --only gemma3-1b
Set HF_TOKEN for gated repos.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _headers() -> dict[str, str]:
    h = {"User-Agent": "bup-llm-server/1.0"}
    if token := os.getenv("HF_TOKEN"):
        h["Authorization"] = f"Bearer {token}"
    return h


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def expected_meta(url: str) -> tuple[int | None, str | None]:
    """(size, sha256) from Hugging Face's pre-redirect headers, if available."""
    req = urllib.request.Request(url, method="HEAD", headers=_headers())
    try:
        headers = urllib.request.build_opener(_NoRedirect).open(req, timeout=30).headers
    except urllib.error.HTTPError as exc:  # the 302 lands here with the headers we want
        headers = exc.headers
    except Exception:
        return None, None
    size = headers.get("X-Linked-Size")
    sha = (headers.get("X-Linked-ETag") or "").strip('"')
    return (int(size) if size and size.isdigit() else None), (sha if len(sha) == 64 else None)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 24):
            h.update(block)
    return h.hexdigest()


def verify(path: Path, size: int | None, sha: str | None) -> bool:
    if size is not None and path.stat().st_size != size:
        print(f"  size mismatch: {path.stat().st_size} != expected {size}", file=sys.stderr)
        return False
    if sha and sha256_of(path) != sha:
        print("  sha256 mismatch", file=sys.stderr)
        return False
    return True


def download(url: str, dest: Path, retries: int = 5) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    size, sha = expected_meta(url)
    for attempt in range(1, retries + 1):
        if size is not None and part.exists() and part.stat().st_size > size:
            part.unlink()  # can't be a valid prefix
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers=_headers())
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            if size is None or have < size:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if have and resp.status != 206:  # server ignored Range -> restart
                        have = 0
                    total = size or have + int(resp.headers.get("Content-Length", 0))
                    t0, done, last_pct = time.time(), have, -1
                    with open(part, "ab" if have else "wb") as fh:
                        while chunk := resp.read(1 << 20):
                            if total and done + len(chunk) > total:  # never write past the file end
                                chunk = chunk[: total - done]
                            fh.write(chunk)
                            done += len(chunk)
                            pct = int(done * 100 / total) if total else 0
                            if pct // 10 != last_pct // 10:
                                last_pct = pct
                                rate = (done - have) / max(time.time() - t0, 1e-6) / 1e6
                                print(f"  {dest.name}: {pct:3d}%  {done / 1e6:,.0f} MB  {rate:.1f} MB/s", flush=True)
                            if total and done >= total:
                                break
            print(f"  verifying {dest.name} ...", flush=True)
            if verify(part, size, sha):
                part.replace(dest)
                return
            part.unlink()  # corrupt -> start over
            raise RuntimeError("integrity check failed; restarting download")
        except Exception as exc:  # network hiccup -> retry with resume
            print(f"  attempt {attempt}/{retries} failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(2 ** attempt, 30))
    raise SystemExit(f"failed to download {url}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "models.toml"))
    ap.add_argument("--dest", default=os.getenv("MODELS_DIR", str(ROOT / "models")))
    ap.add_argument("--only", action="append", help="model id(s) to download")
    ap.add_argument("--verify", action="store_true", help="check existing files against Hugging Face checksums")
    args = ap.parse_args()

    with open(args.config, "rb") as fh:
        models = tomllib.load(fh)["models"]
    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    for m in models:
        if args.only and m["id"] not in args.only:
            continue
        dest = dest_dir / m["file"]
        url = f"https://huggingface.co/{m['repo']}/resolve/main/{m['file']}"
        if dest.exists():
            if not args.verify:
                print(f"[skip] {m['id']}: {dest} exists")
                continue
            ok = verify(dest, *expected_meta(url))
            print(f"[{'ok  ' if ok else 'BAD '}] {m['id']}: {dest}", flush=True)
            if ok:
                continue
            dest.unlink()
        print(f"[get ] {m['id']}: {url}", flush=True)
        download(url, dest)
        print(f"[done] {m['id']}: {dest.stat().st_size / 1e6:,.0f} MB", flush=True)


if __name__ == "__main__":
    main()
