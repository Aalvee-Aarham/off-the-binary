import os


def _env(name, default, cast=str):
    v = (os.getenv(name) or "").split(" #")[0].strip()  # docker env_file keeps inline comments in the value
    return default if v == "" else cast(v)


SIM_BASE_URL = _env("SIM_BASE_URL", "http://localhost:8000")
ADMIN_TOKEN = _env("ADMIN_TOKEN", "")  # empty = admin endpoints disabled
DB_PATH = _env("DB_PATH", "data/fuelops.db")

MODE = _env("MODE", "AUTO_GATED")  # AUTO_GATED | MANUAL
POLL_SECONDS = _env("POLL_SECONDS", 2.0, float)
DECIDE_EVERY_TICKS = _env("DECIDE_EVERY_TICKS", 4, int)
HORIZON_TICKS = _env("HORIZON_TICKS", 40, int)  # must cover transit + COVER_TICKS
EVAL_TICKS = _env("EVAL_TICKS", 16, int)  # twin scoring window (+ same again as discounted tail)
COVER_TICKS = _env("COVER_TICKS", 32, int)
PLAN_TTL_TICKS = _env("PLAN_TTL_TICKS", 8, int)
REVIEW_WINDOW_TICKS = _env("REVIEW_WINDOW_TICKS", 2, int)  # AUTO_GATED: soft-flagged plans run after this unless touched
MIN_SHIPMENT = _env("MIN_SHIPMENT", 500.0, float)
CONSTRAINED_DISPATCH_FACTOR = _env("CONSTRAINED_DISPATCH_FACTOR", 0.5, float)
CYCLE_BUDGET_MS = _env("CYCLE_BUDGET_MS", 800, int)

GATE_MIN_CONFIDENCE = _env("GATE_MIN_CONFIDENCE", 0.6, float)
GATE_MAX_DEPOT_SHARE = _env("GATE_MAX_DEPOT_SHARE", 0.5, float)

SIM_CONNECT_TIMEOUT = _env("SIM_CONNECT_TIMEOUT", 2.0, float)
SIM_READ_TIMEOUT = _env("SIM_READ_TIMEOUT", 5.0, float)
SIM_RETRIES = _env("SIM_RETRIES", 4, int)
BREAKER_WINDOW = _env("BREAKER_WINDOW", 20, int)          # last N requests (post-retry outcomes)
BREAKER_MIN_CALLS = _env("BREAKER_MIN_CALLS", 10, int)
BREAKER_FAIL_RATIO = _env("BREAKER_FAIL_RATIO", 0.5, float)
BREAKER_COOLDOWN_S = _env("BREAKER_COOLDOWN_S", 5.0, float)

VERSION = _env("APP_VERSION", "0.2.0")

# System-One router (phase 4). Laya = local laya-serve; Jev = OpenRouter.
ROUTER_PRIMARY = _env("ROUTER_PRIMARY", "laya")          # laya | jev | rules
LAYA_URL = _env("LAYA_URL", "")                          # e.g. http://ml-service:8000 ; empty = disabled
LAYA_MODEL = _env("LAYA_MODEL", "typed-decisions")       # english | multilingual | typed-decisions
JEV_URL = _env("JEV_URL", "https://openrouter.ai/api")
JEV_MODEL = _env("JEV_MODEL", "jev-1.13")               # per OpenRouter System One docs
JEV_API_KEY = _env("JEV_API_KEY", "")                    # OpenRouter key for Jev; own name so a global OPENROUTER_API_KEY is never picked up
ROUTER_TIMEOUT_S = _env("ROUTER_TIMEOUT_S", 30.0, float)     # background call; the cycle never waits on it
JEV_MAX_CALLS_PER_HOUR = _env("JEV_MAX_CALLS_PER_HOUR", 60, int)  # protects OpenRouter credits (~$0.00004/call)
ROUTER_SHADOW = _env("ROUTER_SHADOW", 1, int)            # also ask the other model, for comparison

# LLM pool (phase 6): text only, templates are the default, LLM only where a human reads
GROQ_API_KEYS = [k.strip() for k in _env("GROQ_API_KEYS", "").split(",") if k.strip()]
GEMINI_API_KEYS = [k.strip() for k in _env("GEMINI_API_KEYS", "").split(",") if k.strip()]
GROQ_MODEL = _env("GROQ_MODEL", "qwen/qwen3.8-27b")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash-lite")
LLM_TIMEOUT_S = _env("LLM_TIMEOUT_S", 8.0, float)
LLM_MAX_CONCURRENT = _env("LLM_MAX_CONCURRENT", 8, int)
LLM_MAX_ATTEMPTS = _env("LLM_MAX_ATTEMPTS", 4, int)          # keys tried per call before template fallback
