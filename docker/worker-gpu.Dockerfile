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

# Layer 2: shared package deps with ML extras (cached while pyproject.toml unchanged)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
COPY shared/pyproject.toml /shared/pyproject.toml
RUN mkdir -p /shared/karaoke_shared && touch /shared/karaoke_shared/__init__.py \
    && pip install --no-cache-dir -e "/shared/[ml]" \
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

# Layer 6a: lyrics matching deps — algorithmic ASR↔candidate scoring
# (pymorphy3: RU lemmas; snowball: EN stems; jellyfish: metaphone;
#  rapidfuzz: fast Levenshtein)
RUN pip install --no-cache-dir \
    pymorphy3==2.0.4 \
    pymorphy3-dicts-ru==2.4.417150.4580142 \
    snowballstemmer==2.2.0 \
    jellyfish==1.1.0 \
    rapidfuzz==3.10.1

# Layer 6b: pre-download UVR + back-vocal checkpoints into image
# Stored in /opt/models (read-only image layer); entrypoint symlinks them
# into /data/models so the volume mount doesn't hide them.
#   - UVR main (BS-Roformer ViperX ep_317): from TRvlvr GitHub release
#   - Back-vocal (Mel-Band RoFormer aufr33 karaoke): from HuggingFace
RUN mkdir -p /opt/models \
    && wget -q -O /opt/models/model_bs_roformer_ep_317_sdr_12.9755.ckpt \
        https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt \
    && python -c "\
import shutil; \
from huggingface_hub import hf_hub_download; \
p = hf_hub_download(repo_id='jarredou/aufr33-viperx-karaoke-melroformer-model', filename='mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt'); \
shutil.copy(p, '/opt/models/mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt'); \
print('UVR + back-vocal checkpoints baked into image')"

# Layer 6c: pre-cache Silero VAD (used by MMS pre-trim) so the first job
# doesn't pay the torch.hub download on cold start.
RUN python -c "import torch; torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True)"

# Layer 7: project code (changes here don't rebuild deps)
COPY shared/karaoke_shared/ /shared/karaoke_shared/
COPY worker/ /project/worker/
COPY scripts/ /project/scripts/

COPY worker/entrypoint.sh /project/entrypoint.sh
RUN chmod +x /project/entrypoint.sh

ENV WORKER_MODE=gpu
ENTRYPOINT ["/project/entrypoint.sh"]
