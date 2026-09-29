"""Settings (env vars) and the model registry (models.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class ModelSpec:
    id: str
    file: str
    repo: str
    port: int
    ctx_size: int = 4096
    parallel: int = 2
    threads: int = 0
    aliases: tuple[str, ...] = ()
    description: str = ""
    extra_args: tuple[str, ...] = ()
    # If set, the gateway talks to this llama-server instead of spawning one.
    url: str | None = None


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...]      # accepted client keys (empty = auth off)
    admin_api_key: str             # for /api/admin/*; empty = any client key
    public_tunnel: bool            # expose this server via a Cloudflare quick tunnel
    cloudflared_bin: str
    port: int                      # our own listening port (tunnel target)
    models_config: Path
    models_dir: Path
    log_dir: Path
    llama_server_bin: str
    llama_host: str
    threads_override: int
    enabled_models: tuple[str, ...]
    request_timeout_s: float
    startup_timeout_s: float
    cache_size: int
    cache_ttl_s: float
    default_model: str
    fallback_order: tuple[str, ...]
    models: tuple[ModelSpec, ...] = field(default_factory=tuple)


def load_settings() -> Settings:
    config_path = Path(os.getenv("MODELS_CONFIG", ROOT / "models.toml"))
    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)

    enabled = tuple(m.strip() for m in os.getenv("ENABLED_MODELS", "").split(",") if m.strip())
    models = []
    for m in raw.get("models", []):
        if enabled and m["id"] not in enabled:
            continue
        # Per-model external URL override, e.g. LLAMA_URL_GEMMA3_1B=http://gemma:8080
        env_url = os.getenv("LLAMA_URL_" + m["id"].upper().replace("-", "_").replace(".", "_"))
        models.append(
            ModelSpec(
                id=m["id"],
                file=m["file"],
                repo=m["repo"],
                port=int(m["port"]),
                ctx_size=int(m.get("ctx_size", 4096)),
                parallel=int(m.get("parallel", 2)),
                threads=int(m.get("threads", 0)),
                aliases=tuple(m.get("aliases", [])),
                description=m.get("description", ""),
                extra_args=tuple(m.get("extra_args", [])),
                url=env_url or m.get("url"),
            )
        )
    if not models:
        raise RuntimeError(f"No models enabled (config={config_path}, ENABLED_MODELS={enabled})")

    server = raw.get("server", {})
    ids = [m.id for m in models]
    default_model = os.getenv("DEFAULT_MODEL", server.get("default_model", ids[0]))
    if default_model not in ids:
        default_model = ids[0]
    fallback = [m for m in server.get("fallback_order", ids) if m in ids]
    fallback += [m for m in ids if m not in fallback]

    return Settings(
        api_keys=tuple(k.strip() for k in os.getenv("API_KEY", "").split(",") if k.strip()),
        admin_api_key=os.getenv("ADMIN_API_KEY", "").strip(),
        public_tunnel=os.getenv("PUBLIC_TUNNEL", "").strip().lower() in ("1", "true", "yes"),
        cloudflared_bin=os.getenv("CLOUDFLARED_BIN", "cloudflared"),
        port=_env_int("PORT", 8000),
        models_config=config_path,
        models_dir=Path(os.getenv("MODELS_DIR", ROOT / "models")),
        log_dir=Path(os.getenv("LOG_DIR", ROOT / "logs")),
        llama_server_bin=os.getenv("LLAMA_SERVER_BIN", "llama-server"),
        llama_host=os.getenv("LLAMA_HOST", "127.0.0.1"),
        threads_override=_env_int("LLAMA_THREADS", 0),
        enabled_models=enabled,
        request_timeout_s=float(os.getenv("REQUEST_TIMEOUT_S", "120")),
        startup_timeout_s=float(os.getenv("STARTUP_TIMEOUT_S", "300")),
        cache_size=_env_int("CACHE_SIZE", 1024),
        cache_ttl_s=float(os.getenv("CACHE_TTL_S", "3600")),
        default_model=default_model,
        fallback_order=tuple(fallback),
        models=tuple(models),
    )
