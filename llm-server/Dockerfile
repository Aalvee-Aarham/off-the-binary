# One self-contained image: llama-server (llama.cpp) + FastAPI gateway + all GGUF models.
# The gateway starts one llama-server per model inside the container.
ARG LLAMA_CPP_TAG=server-v0.5.0
FROM ghcr.io/ggml-org/llama.cpp:${LLAMA_CPP_TAG}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

# Base image is Ubuntu 24.04 -> python3.12 (has tomllib).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

WORKDIR /srv

# 1) Models: largest layer, re-downloaded only when models.toml changes.
COPY models.toml ./
COPY scripts/download_models.py scripts/
RUN python scripts/download_models.py --dest /models

# 2) Python dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 3) Application code (small, changes often).
COPY app ./app
RUN python -c "import app.main"

ENV MODELS_DIR=/models \
    LLAMA_SERVER_BIN=/app/llama-server \
    LD_LIBRARY_PATH=/app \
    LOG_DIR=/tmp/llm-logs \
    PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fs http://localhost:${PORT}/health || exit 1

ENTRYPOINT []
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --timeout-graceful-shutdown 10"]
