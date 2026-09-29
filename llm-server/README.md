# BUP LLM Server

A self-hosted inference server for three small open models, built on **llama.cpp** with a **FastAPI** gateway in front of it.
Everything ships in **one Docker image**, including the models, so you can deploy it to Azure with a single command.

| Model id | Model | Quant | Size | Good for |
|---|---|---|---|---|
| `smollm2-360m` (default) | SmolLM2-360M-Instruct | Q8_0 | ~0.39 GB | Fastest. Routing, classification, short answers |
| `gemma3-1b` | Gemma-3-1B-IT | Q4_K_M | ~0.81 GB | Best quality for its speed. Explanations, JSON extraction |
| `smollm2-1.7b` | SmolLM2-1.7B-Instruct | Q4_K_M | ~1.06 GB | Largest. Summaries, longer answers |

All three stay loaded at once and use about 3–4 GB of RAM in total, including the KV caches.

---

## Contents

1. [Architecture](#1-architecture)
2. [Why llama.cpp](#2-why-llamacpp)
3. [Deploy to Azure (recommended)](#3-deploy-to-azure)
4. [Run locally](#4-run-locally)
5. [API reference](#5-api-reference)
6. [Using it from code](#6-using-it-from-code)
7. [Configuration](#7-configuration)
8. [Performance and tuning](#8-performance-and-tuning)
9. [Operations: health, metrics, fallback, chaos demo](#9-operations)
10. [Troubleshooting](#10-troubleshooting)
11. [Project layout](#11-project-layout)

---

## 1. Architecture

```
                      ┌──────────────────── Docker container ─────────────────────┐
 client / backend     │                                                             │
 ───── HTTP :8000 ───►│  FastAPI gateway (uvicorn)                                  │
  Bearer API key      │   • auth, validation, caching, metrics, model fallback      │
                      │   • supervises the llama-server processes (auto-restart)    │
                      │        │                  │                  │              │
                      │        ▼ :8081            ▼ :8082            ▼ :8083        │
                      │   llama-server       llama-server       llama-server        │
                      │   SmolLM2-360M       Gemma-3-1B         SmolLM2-1.7B        │
                      │   (4 slots)          (4 slots)          (2 slots)           │
                      │        └──────── /models/*.gguf (baked into image) ──┘      │
                      └─────────────────────────────────────────────────────────────┘
```

* The **gateway** is the only public port. It exposes task-level endpoints (`/api/generate`, `/api/structured`, `/api/classify`, …) and an **OpenAI-compatible** endpoint (`/v1/chat/completions`), so the official `openai` SDK works without changes.
* Each model runs in its **own llama-server process**. One model crashing does not affect the others. The gateway restarts a crashed process with backoff, and in the meantime sends requests to another ready model. See [fallback](#fallback).
* Inside the container the llama-server processes listen only on `127.0.0.1`.

## 2. Why llama.cpp

| | llama.cpp (chosen) | PyTorch / transformers | vLLM |
|---|---|---|---|
| CPU speed (Azure CPU VMs) | **Best**: quantized GGUF, AVX2/AVX-512 kernels | 3–5× slower, fp32/bf16 | GPU-first |
| RAM for all 3 models | **~3–4 GB** | ~10+ GB | n/a on CPU |
| Structured output | **Built-in grammar / JSON-schema constrained decoding** | extra libs | yes |
| Image size / cold start | ~3 GB, seconds to load | 5+ GB (torch), slow | large, needs GPU |
| OpenAI-compatible API | built-in | DIY | built-in |

vLLM becomes the better choice only when you serve high concurrency on a GPU. For short, typed decisions from small models on CPU, llama.cpp is the fastest and cheapest option.

---

## 3. Deploy to Azure

`deploy/azure-deploy.sh` handles the whole deployment. There are two targets:

| Target | Command | Speed | URL | Cost (approx.) |
|---|---|---|---|---|
| **VM (default, fastest)** | `./deploy/azure-deploy.sh vm` | 8 vCPU compute-optimised, no cold starts | `http://bupllm-xxxx.<region>.cloudapp.azure.com` | Standard_F8s_v2 ≈ $0.35–0.50/h (≈ $8–12 per day) |
| Container Apps | `./deploy/azure-deploy.sh containerapp` | max 4 vCPU / 8 GiB | `https://…azurecontainerapps.io` (HTTPS) | ≈ $10/day always-on, before the free grant |

> Prices vary by region. Check the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/). **Delete everything when you're done** with `./deploy/azure-deploy.sh destroy`.

### 3.1 Easiest route: Azure Cloud Shell (nothing to install, and downloads use Azure's network)

1. Zip the `llm-server` folder. It's about 30 KB because the models are **not** included.
2. Open <https://shell.azure.com> and choose **Bash**.
3. Click **Manage files → Upload**, select the zip, then run:

```bash
unzip llm-server.zip -d llm-server && cd llm-server
chmod +x deploy/azure-deploy.sh
./deploy/azure-deploy.sh vm
```

On the **VM target**, the script does the following:

1. Creates the resource group `rg-bup-llm` in `centralindia`.
2. Uploads the source code to the VM through cloud-init. Only ~21 KB leaves your machine.
3. Has the VM install Docker, **build the image on the VM**, and download the 2.3 GB of models inside Azure's network.
4. Opens port 80, waits for `/health`, and prints the URL and the generated **API key**.

The first boot takes about 6–12 minutes. The URL and API key are saved in `deploy/.azure-state.env`.

### 3.2 From your own machine

You need the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) and a bash shell (Git Bash works on Windows):

```bash
az login
cd llm-server
./deploy/azure-deploy.sh vm
```

### 3.3 Options (environment variables)

```bash
LOCATION=southeastasia VM_SIZE=Standard_F16s_v2 ./deploy/azure-deploy.sh vm
API_KEY=my-secret ./deploy/azure-deploy.sh vm
CA_CPU=2 CA_MEMORY=4Gi ./deploy/azure-deploy.sh containerapp
```

| Var | Default | Notes |
|---|---|---|
| `RG` | `rg-bup-llm` | resource group; `destroy` deletes it |
| `LOCATION` | `centralindia` | nearest region to Bangladesh; `southeastasia` is the alternative |
| `VM_SIZE` | `Standard_F8s_v2` | Faster: `Standard_F16s_v2`, `Standard_F8as_v6` (AMD Genoa, if available). Cheaper: `Standard_D4s_v5` |
| `API_KEY` | random 48 hex chars | clients must send it |
| `CA_CPU` / `CA_MEMORY` | `4` / `8Gi` | Container Apps consumption plan maximum |

### 3.4 Day-2 commands

```bash
./deploy/azure-deploy.sh status    # URL, API key, /health
./deploy/azure-deploy.sh logs      # VM: setup log + container logs
./deploy/azure-deploy.sh vm        # re-deploy changed code to the existing VM (rebuilds image there)
./deploy/azure-deploy.sh destroy   # delete the resource group (stops billing)
```

SSH access: `ssh azureuser@<vm-fqdn>`. The key is in `~/.ssh/id_rsa` of the machine that ran the script. Once connected, `sudo docker logs -f llm-server` follows the logs.

### 3.5 Common Azure errors

| Error | Fix |
|---|---|
| `QuotaExceeded` / `SkuNotAvailable` | Student and free subscriptions often allow only 4–6 vCPUs per region. Use `VM_SIZE=Standard_D4s_v5` (4 vCPU) or `Standard_F4s_v2`, or try `LOCATION=southeastasia` |
| `TasksOperationsNotAllowed` (containerapp) | Some subscriptions block ACR cloud builds. Use the `vm` target, which builds on the VM itself |
| Health still 503 after 15 min | Run `./deploy/azure-deploy.sh logs`. The Docker build or model download is usually still running |

---

## 4. Run locally

### 4.1 Docker (any OS)

```bash
cp .env.example .env          # set API_KEY
docker compose up --build     # first build downloads ~2.3 GB of models
python scripts/smoke_test.py --url http://localhost:8000 --api-key <key>
# or open http://localhost:8000/ui in a browser
```

### 4.2 Windows without Docker

This uses the native llama.cpp build plus `uv`. The first run downloads llama.cpp, Python and the models (~2.3 GB). Later starts take a few seconds.

**Easiest:** double-click **`start-server.bat`**, then open **http://127.0.0.1:8100/chat** (chatbot) or **http://127.0.0.1:8100/ui** (endpoint tester). To stop the server, press Ctrl+C or close the window.

From a terminal:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1                     # CPU, port 8100
powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1 -ApiKey secret123   # require a key
powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1 -Backend vulkan     # use the Radeon iGPU
```

Test it:
```powershell
.venv\Scripts\python.exe scripts\smoke_test.py            # defaults to http://127.0.0.1:8100
```

Notes:
* **Use `127.0.0.1`, not `localhost`.** The fuel simulator listens on `[::]:8000`, and Windows resolves `localhost` to IPv6 `::1` first. That is why the LLM server defaults to port 8100 locally.
* Running `python app/main.py` (VS Code ▶ Run) also works. It listens on `0.0.0.0:8000` by default, and you can change that with the `PORT` env var. Open `http://127.0.0.1:8000/ui`, not `localhost`.
* **Run only one copy at a time.** Each model uses a fixed internal port (8081–8083). A second copy refuses to start its models ("port … already in use") instead of sharing them.
* The `llama-server` child processes are tied to the server, so they exit even if the window is closed.
* When no `API_KEY` is set, auth is **disabled**. That's fine locally, but always set a key for deployments.

---

## 5. API reference

### Chatbot: `/chat`
Open **`<server-url>/chat`** for a simple chat interface. It has:
* streaming replies (text appears as it's generated) through `/v1/chat/completions`, with a **Stop** button
* a model picker, an editable system prompt, **New chat**, and multi-turn memory
* per-reply stats: model, time to first token, tokens/s, total time, and a note when a fallback model answered

Enter sends; Shift+Enter adds a new line. If `API_KEY` is set, paste it into the header field.

### Test UI: `/ui`
Open **`<server-url>/ui`** in a browser, paste the API key and click **Connect**. The page lets you:
* use one tab each for **Generate**, **Chat**, **Structured JSON** and **Classify**, with a model picker, max tokens, temperature and a "bypass cache" option
* see the answer, latency, tokens/s, token counts, and whether the response was cached or used a fallback, with the raw JSON underneath
* view live model status, with **Stop/Start** buttons for the fallback demo

The page is a single file, [app/static/index.html](app/static/index.html), with no external dependencies. You can also open it straight from disk and type the server URL into the header, which works because CORS is enabled.

Interactive docs are served at **`/docs`** (Swagger UI) and **`/redoc`**.

**Auth.** When `API_KEY` is set, every `/api/*` and `/v1/*` call needs one of these headers:
```
Authorization: Bearer <API_KEY>
X-API-Key: <API_KEY>
```
`/`, `/health`, `/health/live` and `/metrics` are open.

**Model names.** Any id or alias works:
* `smollm2-360m`: aliases `small`, `fast`
* `gemma3-1b`: aliases `gemma`, `gemma3`, `gemma-3-1b-it`
* `smollm2-1.7b`: alias `medium`

If you omit `model`, the server uses `DEFAULT_MODEL`.

**Common generation fields.** These apply to `/api/generate`, `/api/chat` and `/api/structured`:

| Field | Default | Notes |
|---|---|---|
| `model` | default model | id or alias |
| `max_tokens` | 256 (`structured`: 512) | 1–4096 |
| `temperature` | 0.7 (`structured`: 0) | `0` = greedy/deterministic, which enables the cache |
| `top_p`, `stop`, `seed` | – | optional |
| `allow_fallback` | `true` | if the model is down, another ready model answers (`fallback_used: true`) |
| `use_cache` | `true` | only applies when `temperature == 0` |

**Common response fields:** `model` (the model that actually answered), `usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`), `latency_ms`, `tokens_per_second`, `finish_reason` (`stop` or `length`), `fallback_used`, `cached`.

### `GET /health`
Returns `200` when at least one model is ready, and `503` otherwise. Use it as the readiness probe.
```json
{"status":"ok","models":{"smollm2-360m":"ready","gemma3-1b":"ready","smollm2-1.7b":"ready"},"cache_items":3}
```
`status` is `ok` (all models ready), `degraded` (some ready) or `unavailable` (none ready). The per-model state is one of `starting`, `ready`, `crashed` or `stopped`.

### `GET /health/live`
Always returns `200` while the process is up. Use it as the liveness probe.

### `GET /api/models`
Returns the registry plus live state for each model: load time, uptime, restarts and last error.

### `POST /api/generate`: prompt in, text out
```bash
curl -s $URL/api/generate -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{
  "model": "gemma3-1b",
  "system": "You are a fuel logistics assistant. Be concise.",
  "prompt": "Explain in 2 sentences why station S-2 should be refilled before S-4.",
  "max_tokens": 120, "temperature": 0.3
}'
```
```json
{
  "model": "gemma3-1b",
  "text": "Station S-2 has lower stock and higher hourly demand...",
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 41, "completion_tokens": 37, "total_tokens": 78},
  "latency_ms": 912.4, "tokens_per_second": 41.8,
  "fallback_used": false, "cached": false
}
```

### `POST /api/chat`: multi-turn
```json
{
  "model": "smollm2-1.7b",
  "messages": [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "What does MPC stand for in control?"}
  ],
  "max_tokens": 60
}
```
The response has the same shape as `/api/generate`.

### `POST /api/structured`: guaranteed-valid JSON
The output is **constrained by the schema during decoding** (llama.cpp grammar), so the model cannot produce malformed JSON. The only exception is truncation by `max_tokens`, which returns `502` with a hint.
```json
{
  "model": "gemma3-1b",
  "prompt": "Station S-3 reports it needs 4500 litres of diesel by tonight.",
  "schema": {
    "type": "object",
    "properties": {
      "station": {"type": "string"},
      "fuel":    {"type": "string", "enum": ["diesel", "petrol", "octane"]},
      "litres":  {"type": "number"},
      "urgent":  {"type": "boolean"}
    },
    "required": ["station", "fuel", "litres", "urgent"]
  }
}
```
```json
{"model":"gemma3-1b","data":{"station":"S-3","fuel":"diesel","litres":4500,"urgent":true},
 "finish_reason":"stop","usage":{...},"latency_ms":1210.7,"tokens_per_second":39.5,"fallback_used":false,"cached":false}
```

### `POST /api/classify`: pick one label, with confidence
The model can **only** output one of the labels because a grammar enforces it, and it outputs only the label, so the call is very fast. `confidence` is estimated from the model's token probabilities: the first-token probability of each label, normalised across the labels.
```json
{
  "model": "smollm2-360m",
  "text": "Depot D1 diesel inventory dropped 40% in 2 hours; three stations report empty tanks.",
  "labels": ["normal", "shortage", "demand_surge", "route_disruption"],
  "instruction": "Classify the fuel supply situation."
}
```
```json
{"label":"shortage","confidence":0.87,
 "scores":{"normal":0.02,"shortage":0.87,"demand_surge":0.08,"route_disruption":0.03},
 "model":"smollm2-360m","finish_reason":"stop","usage":{...},"latency_ms":180.3,
 "tokens_per_second":null,"fallback_used":false,"cached":false}
```
A common routing rule is **`confidence < 0.6` → send to the human review queue**.
Classify results are cached, because they are deterministic.

### `POST /v1/chat/completions`: OpenAI-compatible
This is the standard OpenAI Chat Completions request and response, including `stream: true` (SSE), `response_format`, `logprobs` and `tools`. The `X-Served-Model` response header shows which model answered.

### `GET /v1/models`
Lists the models in OpenAI format.

### Admin endpoints (require the API key)
| Method | Path | Effect |
|---|---|---|
| POST | `/api/admin/models/{model}/stop` | stop a model (stays down until started) |
| POST | `/api/admin/models/{model}/start` | start it again |
| POST | `/api/admin/models/{model}/restart` | restart the process |
| GET | `/api/admin/models/{model}/logs?lines=100` | tail that model's llama-server log |
| POST | `/api/admin/cache/clear` | empty the response cache |

### Errors
| Code | Meaning |
|---|---|
| 401 | missing or wrong API key |
| 404 | unknown model name (the error lists valid names) |
| 422 | invalid request body |
| 502 | model error, or invalid JSON from `/api/structured` (usually truncation) |
| 503 | model not ready and no fallback available (e.g. during startup) |
| 504 | model timed out (`REQUEST_TIMEOUT_S`) |

---

## 6. Using it from code

### Python (httpx)
```python
import httpx

LLM = httpx.Client(base_url="http://<vm-fqdn>", headers={"Authorization": "Bearer <KEY>"}, timeout=30)

r = LLM.post("/api/classify", json={
    "text": state_summary,
    "labels": ["greedy", "lp", "mpc", "robust_lp", "rationing"],
    "instruction": "Pick the best allocation algorithm for this fuel network state.",
}).json()

if r["confidence"] is None or r["confidence"] < 0.6:
    send_to_human_queue(r)
else:
    run_algorithm(r["label"])
```

### OpenAI SDK
```python
from openai import OpenAI
client = OpenAI(base_url="http://<vm-fqdn>/v1", api_key="<KEY>")
resp = client.chat.completions.create(
    model="gemma3-1b",
    messages=[{"role": "user", "content": "Summarise this incident in one line: ..."}],
    max_tokens=80,
)
print(resp.choices[0].message.content)
```

### Suggested fit with the Fuel Ops platform (PLAN.md)
| Task | Endpoint | Model |
|---|---|---|
| Regime classification / algorithm routing (T1) | `/api/classify` | `smollm2-360m` (fast), with `gemma3-1b` in shadow for comparison |
| "Needs human?" gate | `/api/classify` with `["auto","human_review"]` | `smollm2-360m` |
| Parse operator free-text into actions | `/api/structured` | `gemma3-1b` |
| Incident summaries / explanations (T3 fallback when Groq/Gemini are down) | `/api/generate` | `gemma3-1b` or `smollm2-1.7b` |

---

## 7. Configuration

### Environment variables
| Var | Default | Description |
|---|---|---|
| `API_KEY` | *(empty = auth off)* | shared secret for `/api/*` and `/v1/*` |
| `DEFAULT_MODEL` | `smollm2-360m` | used when a request has no `model` |
| `ENABLED_MODELS` | *(all)* | comma list, e.g. `smollm2-360m,gemma3-1b`, to save RAM/CPU |
| `LLAMA_THREADS` | `0` (auto = physical cores) | CPU threads per llama-server |
| `CACHE_SIZE` / `CACHE_TTL_S` | `1024` / `3600` | response cache (temperature 0 only) |
| `REQUEST_TIMEOUT_S` | `120` | per upstream request |
| `STARTUP_TIMEOUT_S` | `300` | max model load time before restart |
| `LOG_LEVEL` | `INFO` | gateway log level |
| `CORS_ORIGINS` | `*` | comma list of browser origins allowed to call the API |
| `PORT` | `8000` | gateway port (Docker) |
| `MODELS_DIR`, `LLAMA_SERVER_BIN`, `LOG_DIR`, `MODELS_CONFIG` | set in the image | paths |
| `LLAMA_URL_<MODEL_ID>` | – | use an external llama-server instead of spawning one, e.g. `LLAMA_URL_GEMMA3_1B=http://gpu-box:8080` |

### `models.toml`
Each `[[models]]` entry defines one backend:

| Field | Meaning |
|---|---|
| `id`, `aliases` | names clients can use |
| `repo`, `file` | Hugging Face repo and GGUF filename (downloaded at build time) |
| `port` | internal llama-server port |
| `ctx_size` | total context tokens, shared by all slots |
| `parallel` | concurrent request slots (continuous batching) |
| `threads` | CPU threads (0 = auto) |
| `extra_args` | extra llama-server flags, e.g. `["--cache-type-k","q8_0"]` |

To **swap a quantization**, change `file` (e.g. `SmolLM2-1.7B-Instruct-Q8_0.gguf` for better quality) and rebuild. To **add a model**, add another `[[models]]` block with a new port.

---

## 8. Performance and tuning

**Ballpark numbers** for decode speed on an 8 vCPU Azure F-series VM, with one request at a time. Measure your own with `python scripts/smoke_test.py --url … --api-key … --bench 10`.

| Model | Tokens/s (approx.) | `/api/classify` latency |
|---|---|---|
| smollm2-360m Q8_0 | 50–90 | ~0.1–0.3 s |
| gemma3-1b Q4_K_M | 30–50 | ~0.2–0.5 s |
| smollm2-1.7b Q4_K_M | 15–30 | ~0.4–0.9 s |

Tips:
* **Short prompts matter most on CPU.** Prompt processing (prefill) usually costs more than generation. Send a compact state summary, not raw JSON dumps.
* **Use `/api/classify` for decisions.** It generates only a few tokens, so it is 5–20× faster than asking for an explanation.
* **Use `temperature: 0` for repeatable calls** so the cache can answer repeats in under 1 ms.
* **More cores** help prefill and concurrency. `Standard_F16s_v2` roughly doubles throughput under load.
* **Contention.** All three models share the CPU. If you only need one or two, set `ENABLED_MODELS` so the others don't compete.
* **Concurrency.** Each model has `parallel` slots with continuous batching. Extra requests queue inside llama-server.

---

## 9. Operations

* **Probes:** readiness uses `/health` (200 once at least one model is ready); liveness uses `/health/live`.
* **Prometheus** metrics at `/metrics`:
  * `llm_requests_total{endpoint,model,status}`, `llm_request_latency_seconds` (histogram, gives p50/p95/p99)
  * `llm_tokens_total{model,kind}`, `llm_generation_tokens_per_second`
  * `llm_cache_hits_total`, `llm_fallback_total{requested,served}`
  * `llm_backend_up{model}`, `llm_backend_restarts{model}`
* **Logs:** container stdout has the gateway logs. Each model's llama-server log is available at `GET /api/admin/models/{id}/logs`.

### Fallback
When the requested model is not ready, or returns a 5xx or connection error, the gateway tries the other **ready** models in `fallback_order` from `models.toml`. The response then has `"fallback_used": true` and `model` set to the model that answered. Send `"allow_fallback": false` to disable this behaviour.

### Chaos demo ("ML model unavailable")
```bash
curl -X POST $URL/api/admin/models/smollm2-360m/stop -H "Authorization: Bearer $KEY"
curl -s $URL/health                       # -> "degraded"
curl -s $URL/api/classify -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"text":"...","labels":["a","b"],"model":"smollm2-360m","use_cache":false}'   # -> served by gemma3-1b, fallback_used=true
curl -X POST $URL/api/admin/models/smollm2-360m/start -H "Authorization: Bearer $KEY"
```
If a llama-server process crashes by itself, the gateway restarts it automatically with backoff. The `llm_backend_restarts` metric counts those restarts.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/health` returns 503 right after start | models are loading (a few seconds each); wait |
| Model state `crashed` repeatedly | check `GET /api/admin/models/{id}/logs`; usually out of memory, so reduce `ctx_size`/`parallel` or disable a model |
| `502 invalid JSON` from `/api/structured` | output was truncated; raise `max_tokens` |
| Slow responses under load | too many concurrent requests for the cores; use a bigger VM or fewer models |
| `401` | missing `Authorization: Bearer <key>` header |
| Docker build fails downloading models | Hugging Face hiccup; re-run (the downloader retries and resumes). All configured repos are ungated; if you switch to a gated repo, run `scripts/download_models.py` with `HF_TOKEN` set |

---

## 11. Project layout

```
llm-server/
├── app/
│   ├── main.py            FastAPI app: endpoints, auth, cache, fallback, OpenAI proxy
│   ├── manager.py         spawns/supervises one llama-server per model
│   ├── config.py          env settings + models.toml loader
│   ├── cache.py           LRU+TTL response cache
│   ├── metrics.py         Prometheus metrics
│   └── static/
│       ├── chat.html      streaming chatbot (served at /chat)
│       └── index.html     endpoint tester (served at /ui)
├── scripts/
│   ├── download_models.py GGUF downloader (stdlib, resumable)
│   ├── smoke_test.py      end-to-end test + latency benchmark (stdlib)
│   └── run-local.ps1      native Windows runner (no Docker)
├── deploy/
│   └── azure-deploy.sh    Azure VM / Container Apps deploy, status, logs, destroy
├── models.toml            model registry
├── Dockerfile             llama.cpp server image + Python gateway + models
├── docker-compose.yml     local run
├── requirements.txt
└── .env.example
```

**Versions:** llama.cpp `v0.5.0` Docker image (pinned through the `LLAMA_CPP_TAG` build arg in the Dockerfile). Base OS is Ubuntu 24.04 with Python 3.12.

**Security notes:** use a strong `API_KEY`. The VM target serves plain HTTP on port 80. Put it behind Azure Front Door or Caddy, or use the Container Apps target, if you need HTTPS. On the VM the API key is part of the cloud-init custom data, so anyone who can read the VM's configuration can see it.
