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

# WhisperX first — it pins torch to its required version (~2.8.0).
# Default PyPI torch includes CUDA support, which we keep for bootstrap
# GPU inference (UVR separation + WhisperX).
RUN pip install --no-cache-dir whisperx

# Replace CPU-only onnxruntime with GPU variant so audio_separator
# can use CUDA for UVR separation.
RUN pip install --no-cache-dir onnxruntime-gpu

ENTRYPOINT ["python", "-m", "app.cli"]
