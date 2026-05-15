# GPU worker: CUDA 12.8 + BS-Roformer + Whisper local.
# CUDA 12.8 is required for Blackwell consumer GPUs (RTX 50-series, sm_120);
# torch <2.7 + CUDA 12.1 wheels do NOT include sm_120 kernels and fail at
# runtime with "no kernel image is available for execution on the device".
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

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

# Layer 1: PyTorch + torchvision + torchaudio with CUDA 12.8 (Blackwell).
# Heaviest layer (~3 GB) — kept first so it stays cached when the rest of
# the lockfile changes. The +cu128 wheels live on the PyTorch index, not
# default PyPI, so we pass --extra-index-url here directly.
RUN pip install --no-cache-dir --no-deps \
    --extra-index-url https://download.pytorch.org/whl/cu128 \
    torch==2.7.1+cu128 torchvision==0.22.1+cu128 torchaudio==2.7.1+cu128

# Layer 2: every other Python dep, fully pinned.
# Generated via:
#   docker exec mouth-singing-worker-1 pip freeze \
#     | grep -vE '^# Editable|^-e |^karaoke-shared' \
#     > docker/requirements-worker.lock
# `--no-deps` guarantees that pip won't accidentally pull a transitive
# upgrade and break ABI (torch/transformers/numpy/etc.). To upgrade ANY
# package: edit the lockfile by hand or re-run pip freeze from a fresh
# container.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
COPY docker/requirements-worker.lock /tmp/requirements-worker.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements-worker.lock

# Layer 3: shared package in editable mode (karaoke-shared is excluded
# from the lockfile because pip freeze records it as a non-portable
# `# Editable install ...` comment). The [ml] extras (librosa,
# sentence-transformers, numpy) are already satisfied by the lockfile.
COPY shared/pyproject.toml /shared/pyproject.toml
RUN mkdir -p /shared/karaoke_shared && touch /shared/karaoke_shared/__init__.py \
    && pip install --no-cache-dir --no-deps -e /shared

# Layer 4a: pre-download UVR + back-vocal checkpoints into image.
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

# Layer 4b: pre-cache Silero VAD (used by MMS pre-trim) so the first job
# doesn't pay the torch.hub download on cold start.
RUN python -c "import torch; torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True)"

# Layer 5: docker CLI (client only) — used by entrypoint.sh to look up
# this container's name via the host docker.sock and derive a stable
# WORKER_ID per replica. ~70 MB. Placed AFTER the heavy ML layers so that
# adding/removing this binary does NOT invalidate the torch / transformers
# cache; rebuild here is cheap.
RUN wget -qO- https://download.docker.com/linux/static/stable/x86_64/docker-24.0.7.tgz \
    | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker

# Layer 6: project code (changes here don't rebuild deps)
COPY shared/karaoke_shared/ /shared/karaoke_shared/
COPY worker/ /project/worker/
COPY scripts/ /project/scripts/

COPY worker/entrypoint.sh /project/entrypoint.sh
RUN chmod +x /project/entrypoint.sh

ENV WORKER_MODE=gpu
ENTRYPOINT ["/project/entrypoint.sh"]
