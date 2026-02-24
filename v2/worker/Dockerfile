FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg wget gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /worker

# Increase pip timeout to avoid download failures on slow connections.
ENV PIP_DEFAULT_TIMEOUT=300

# Install shared package with ML extras (librosa, sentence-transformers)
# so the worker can extract features and embed lyrics.
COPY shared/ /shared/
RUN pip install --no-cache-dir "/shared/[ml]"

# Install CPU-only PyTorch + torchvision first — audio-separator pulls
# these as deps, and by default pip installs the CUDA variant (~3 GB).
# The worker runs ONNX inference on CPU, so CUDA is unnecessary.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install worker package.  Use --extra-index-url for PyTorch CPU so pip
# does not re-download the CUDA variant of torchvision.
COPY worker/pyproject.toml /worker/pyproject.toml
COPY worker/app/ /worker/app/
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu /worker/

COPY worker/entrypoint.sh /worker/entrypoint.sh
RUN chmod +x /worker/entrypoint.sh

WORKDIR /worker

ENTRYPOINT ["/worker/entrypoint.sh"]
