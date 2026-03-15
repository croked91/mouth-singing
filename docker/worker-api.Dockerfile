# API worker: CPU-only, offloads UVR and ASR to external APIs
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

ENV PIP_DEFAULT_TIMEOUT=300
ENV PYTHONUNBUFFERED=1

# Shared package
COPY shared/pyproject.toml /shared/pyproject.toml
COPY shared/karaoke_shared/ /shared/karaoke_shared/
RUN pip install --no-cache-dir /shared/

# PyTorch CPU (needed by ctc-forced-aligner and sentence-transformers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Worker code + dependencies
COPY worker/ /worker/
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu /worker/ 2>/dev/null || true

# CTC aligner + lyric embedding + scraping
RUN pip install --no-cache-dir \
    ctc-forced-aligner==1.0.2 soundfile>=0.12.1 unidecode>=1.3 \
    "sentence-transformers>=2.2,<3" \
    beautifulsoup4>=4.12 lxml>=5.0 \
    aiosqlite>=0.20 structlog>=24.0 httpx>=0.27 "qdrant-client>=1.8" \
    pyphen>=0.16 pydantic-settings>=2.0

COPY worker/entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

ENV WORKER_MODE=api
ENTRYPOINT ["/worker/entrypoint.sh"]
