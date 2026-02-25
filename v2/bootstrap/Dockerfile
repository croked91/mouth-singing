FROM python:3.12-slim

# System dependencies:
# - ffmpeg: required by audio loading in WhisperX/librosa
# - gcc, libc6-dev: needed to compile C extensions in some Python packages
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared package first so worker and bootstrap can depend on it.
# The [ml] extra pulls in librosa, numpy, and sentence-transformers.
COPY shared/ /app/shared/
RUN pip install --no-cache-dir "/app/shared[ml]"

# Install the worker package so UVRSeparator is importable.
COPY worker/ /app/worker/
RUN pip install --no-cache-dir /app/worker/

# Install the bootstrap package.
COPY bootstrap/ /app/bootstrap/
RUN pip install --no-cache-dir /app/bootstrap/

# Install PyTorch CPU build first (large download — separate layer for better
# cache reuse when re-building after bootstrap code changes).
RUN pip install --no-cache-dir \
    torch \
    torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

# WhisperX after torch so it can detect the installed torch version.
RUN pip install --no-cache-dir whisperx

ENTRYPOINT ["python", "-m", "app.cli"]
