# Laya System-One decision model, served over the Jev-compatible /v1/systemone protocol (laya-serve).
FROM python:3.12-slim
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "laya[serve]==0.3.21"
ENV LAYA_PORT=8000 LAYA_PRELOAD=1 LAYA_MODELS=typed-decisions LAYA_MAX_LOADED=1 LAYA_THREADS=4 \
    LAYA_MAX_CONCURRENT=8 HF_HOME=/models
RUN useradd -m laya && mkdir -p /models && chown laya /models
USER laya
EXPOSE 8000
# first start downloads the checkpoint (~800 MB) into the /models volume
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1
CMD ["laya-serve"]
