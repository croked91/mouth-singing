"""Worker configuration loaded from environment variables.

Only GPU mode is supported. API mode (MVSEP + OpenAI Whisper) has been removed.
"""

from __future__ import annotations

import os
import socket

from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    """Runtime configuration for the karaoke audio worker (GPU mode only)."""

    # ------------------------------------------------------------------
    # Common: infrastructure
    # ------------------------------------------------------------------

    pg_dsn: str = "postgresql://karaoke:karaoke@postgres:5432/karaoke"
    media_root: str = "/data/media"

    # S3-compatible storage
    s3_bucket: str = "karaoke"
    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"

    # RabbitMQ
    rabbitmq_url: str = "amqp://karaoke:karaoke@rabbitmq:5672/"
    model_cache_dir: str = "/data/models"
    worker_id: str = f"{socket.gethostname()}-{os.getpid()}"
    poll_interval_sec: float = 2.0
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Common: lyrics search (providers + algorithmic matcher + agent fallback)
    # ------------------------------------------------------------------

    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    searxng_url: str = "http://searxng:8080"
    yandex_search_api_key: str = ""
    yandex_search_folder_id: str = ""
    lyrics_agent_max_iterations: int = 15
    lyrics_agent_timeout: float = 15.0

    # Lyrics providers
    genius_token: str = ""
    lyrics_provider_timeout: float = 10.0
    lyrics_search_fragments: int = 2

    # ------------------------------------------------------------------
    # Common: CTC aligner
    # ------------------------------------------------------------------

    ctc_min_frames_for_char: int = 10
    ctc_device: str = "cpu"
    """ONNX execution provider for CTC alignment: 'cuda' or 'cpu'.
    CPU is the only viable option — wav2vec2 ONNX graph has 24 ops
    unsupported by CUDA EP, causing constant CPU<->GPU memcpy that
    makes GPU slower than pure CPU."""
    ctc_batch_size: int = 16
    """Batch size for generate_emissions (CPU has plenty of RAM)."""

    # MMS (TorchCTCAligner) pre-trim via Silero VAD: skip intro ad-libs /
    # inhales that would otherwise anchor the first word too early.
    mms_pre_trim_enabled: bool = True
    mms_pre_trim_threshold: float = 0.7
    mms_pre_trim_min_speech_ms: int = 300
    mms_pre_trim_lead_in_ms: int = 100
    """Deprecated — no longer applied. Silero onset is refined via RMS
    back-tracking (``_refine_silero_onset``) which adapts per-track
    instead of using a fixed lead-in. Kept for .env backward compat."""

    # Per-line RMS-dip adjustment: for every first-in-line word, search
    # the natural window [prev_word_end, this_word_end] for a sandwich'ed
    # RMS dip (local minimum bordered by louder peaks on both sides) and
    # shift the word's start to the end of that dip. Fixes MMS anchoring
    # to ad-libs/backing leakage.
    mms_line_start_rms_adjust: bool = True

    # Word-end drift adjustment: detect last phoneme spans that drift
    # into silence (emission drift), validate via RMS back-track so
    # legitimate vocal sustains are preserved.
    mms_word_end_drift_adjust: bool = True

    # Word-end sustain extension: opposite failure mode of drift. MMS
    # emission fires once per phoneme at attack, so a sustained final
    # vowel (common at line-end) closes the word at its attack frame.
    # Forward RMS walk extends the word end to the natural silence
    # boundary, capped by the next word's onset.
    mms_word_end_sustain_extend: bool = True

    # ------------------------------------------------------------------
    # Common: VAD
    # ------------------------------------------------------------------

    vad_top_db: int = 16

    # ------------------------------------------------------------------
    # GPU mode: UVR local separator (BS-Roformer ViperX ep_317 — vocals/instrumental)
    # Revive 2 was evaluated but rejected: it cleans vocals too aggressively
    # for Whisper (out-of-distribution) → transcription degrades and lyrics
    # matcher picks the wrong song version.
    # ------------------------------------------------------------------

    uvr_model_name: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    uvr_torch_device: str = "cuda"
    uvr_chunk_batch_size: int = 2
    uvr_use_autocast: bool = True
    uvr_overlap: float = 8.0

    # ------------------------------------------------------------------
    # GPU mode: Back-vocal separator (Mel-Band RoFormer aufr33 — lead/backing)
    # ------------------------------------------------------------------

    back_vocal_enabled: bool = True
    back_vocal_model_name: str = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
    back_vocal_torch_device: str = "cuda"
    back_vocal_chunk_batch_size: int = 2
    back_vocal_use_autocast: bool = True
    back_vocal_overlap: float = 4.0

    # ------------------------------------------------------------------
    # GPU mode: faster-whisper local ASR
    # ------------------------------------------------------------------

    whisper_model_size: str = "tiny"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    auto_repair_whisper_model_size: str = "large-v3"
    auto_repair_whisper_device: str | None = None
    auto_repair_whisper_compute_type: str | None = None

    model_config = {"env_prefix": ""}


settings = WorkerSettings()
