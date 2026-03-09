# v3-rc2/worker/Dockerfile
# CPU-only image: no CUDA, no audio-separator, no faster-whisper
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

ENV PIP_DEFAULT_TIMEOUT=300
ENV PYTHONUNBUFFERED=1

# Shared package (from v3-rc1, without ML extras by default)
COPY shared/ /shared/
RUN pip install --no-cache-dir "/shared/"

# PyTorch CPU (needed by ctc-forced-aligner and sentence-transformers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Worker package with all dependencies
COPY worker/pyproject.toml /worker/pyproject.toml
COPY worker/app/ /worker/app/
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu /worker/

COPY worker/entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

ENTRYPOINT ["/worker/entrypoint.sh"]
