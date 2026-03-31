# GPU worker: CUDA 12.1 + BS-Roformer + Whisper local
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip \
    ffmpeg wget gcc g++ libc6-dev libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

ENV PIP_DEFAULT_TIMEOUT=300
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/data/models/hf
ENV XDG_CACHE_HOME=/data/models/xdg
ENV PYTHONPATH=/project

WORKDIR /project

# Layer 1: PyTorch + torchaudio with CUDA (heaviest, cached first)
# torchaudio provides forced_align() with native CUDA kernels for CTC alignment.
RUN pip install --no-cache-dir \
    torch torchaudio \
    --index-url https://download.pytorch.org/whl/cu130

# Layer 2: shared package with ML extras
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
COPY shared/pyproject.toml /shared/pyproject.toml
COPY shared/karaoke_shared/ /shared/karaoke_shared/
RUN pip install --no-cache-dir "/shared/[ml]" \
    && pip install --no-cache-dir "sentence-transformers>=2.2,<3" "transformers>=4.36,<5"

# Layer 3: CTranslate2 + faster-whisper
RUN pip install --no-cache-dir ctranslate2==4.4.0 faster-whisper==1.0.3

# Layer 4: CTC aligner
RUN pip install --no-cache-dir ctc-forced-aligner==1.0.2 soundfile>=0.12.1 unidecode>=1.3

# Layer 5: audio-separator + GPU ONNX
RUN pip install --no-cache-dir audio-separator>=0.24 onnxruntime-gpu>=1.18

# Layer 6: runtime deps
RUN pip install --no-cache-dir \
    aiosqlite>=0.20 structlog>=24.0 httpx>=0.27 "qdrant-client>=1.8" \
    pyphen>=0.16 beautifulsoup4>=4.12 lxml>=5.0 pydantic-settings>=2.0 \
    "openai>=1.0"

# Layer 7: project code
COPY worker/ /project/worker/
COPY scripts/ /project/scripts/

COPY worker/entrypoint.sh /project/entrypoint.sh
RUN chmod +x /project/entrypoint.sh

ENV WORKER_MODE=gpu
ENTRYPOINT ["/project/entrypoint.sh"]
