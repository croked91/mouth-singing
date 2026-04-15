"""Mel-Band RoFormer back-vocal separator — lead vs backing vocals.

Splits UVR vocals output into lead-only and backing-only stems via
``mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956``. Runs on the
16kHz mono vocals produced by :class:`UVRSeparator` (upsampled to
44.1kHz stereo internally for the model, then downsampled back).

Downstream VAD/Whisper/CTC steps consume the lead_vocals output so
backing harmonies no longer confuse ASR or alignment.
"""

from __future__ import annotations

import pathlib
import time

import structlog

logger = structlog.get_logger(__name__)

# Architecture config for mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt
_MODEL_CONFIG = {
    "dim": 384,
    "depth": 6,
    "stereo": True,
    "num_stems": 1,
    "time_transformer_depth": 1,
    "freq_transformer_depth": 1,
    "num_bands": 60,
    "dim_head": 64,
    "heads": 8,
    "attn_dropout": 0.0,
    "ff_dropout": 0.0,
    "flash_attn": True,
    "dim_freqs_in": 1025,
    "sample_rate": 44100,
    "stft_n_fft": 2048,
    "stft_hop_length": 441,
    "stft_win_length": 2048,
    "stft_normalized": False,
    "mask_estimator_depth": 2,
    "multi_stft_resolution_loss_weight": 1.0,
    "multi_stft_resolutions_window_sizes": (4096, 2048, 1024, 512, 256),
    "multi_stft_hop_size": 147,
    "multi_stft_normalized": False,
}

_SAMPLE_RATE = 44100
_STFT_HOP = 441
_DIM_T = 801
_CHUNK_SIZE = _STFT_HOP * (_DIM_T - 1)  # 352_800 samples (~8 sec at 44.1kHz)


class BackVocalSeparator:
    """Mel-Band RoFormer back-vocal separator (lead vs backing vocals).

    Args:
        model_cache_dir: Directory where the back-vocal checkpoint is stored.
        media_root: Root directory for audio output (writes to ``media_root/instrumental``).
        model_name: Checkpoint filename (defaults to aufr33 viperx).
        torch_device: 'cuda' or 'cpu'.
        chunk_batch_size: Number of audio chunks per forward pass.
        use_autocast: Enable FP16 autocast on GPU.
        overlap: Chunk step in seconds (smaller = more overlap, slower, better quality).
    """

    MODEL_NAME = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"

    def __init__(
        self,
        model_cache_dir: str,
        media_root: str,
        model_name: str | None = None,
        torch_device: str = "cuda",
        chunk_batch_size: int = 2,
        use_autocast: bool = True,
        overlap: float = 4.0,
    ) -> None:
        self.model_cache_dir = model_cache_dir
        self.media_root = media_root
        self._model_name = model_name or self.MODEL_NAME
        self.torch_device = torch_device
        self._chunk_batch_size = chunk_batch_size
        self._use_autocast = use_autocast
        self._overlap = overlap
        self._model = None
        self._output_dir: str | None = None

    def _ensure_model(self):
        """Load the Mel-Band RoFormer back-vocal model on first use."""
        if self._model is not None:
            return

        import torch
        from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import (
            MelBandRoformer,
        )

        output_dir = pathlib.Path(self.media_root) / "instrumental"
        output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = str(output_dir)

        model_path = str(pathlib.Path(self.model_cache_dir) / self._model_name)

        self._model = MelBandRoformer(**_MODEL_CONFIG)
        state_dict = torch.load(model_path, map_location="cpu")
        if isinstance(state_dict, dict) and "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
        self._model.load_state_dict(state_dict)
        self._model.to(self.torch_device).half().eval()

        logger.info(
            "back_vocal_model_loaded",
            model=self._model_name,
            device=self.torch_device,
            params_m=round(
                sum(p.numel() for p in self._model.parameters()) / 1e6, 1
            ),
        )

    def separate(self, vocals_path: str) -> tuple[str, str]:
        """Separate lead vocals from backing vocals.

        Runs synchronously — use asyncio.to_thread from async context.

        Args:
            vocals_path: Path to vocals WAV from UVRSeparator (16kHz mono).

        Returns:
            (lead_vocals_path, backing_vocals_path) — both WAV at 16kHz mono.
        """
        import soundfile as sf
        import torch
        import torchaudio.functional as F
        from scipy.signal import windows as scipy_windows

        self._ensure_model()

        logger.info("back_vocal_starting", vocals_path=vocals_path)
        t0 = time.monotonic()

        # --- Load audio (upsample 16kHz mono → 44.1kHz stereo for model) ---
        data, sr = sf.read(vocals_path, dtype="float32")
        if data.ndim == 1:
            data = data[:, None]
        mix = torch.from_numpy(data.T)  # (channels, samples)
        if sr != _SAMPLE_RATE:
            mix = F.resample(mix, sr, _SAMPLE_RATE)
        if mix.shape[0] == 1:
            mix = mix.repeat(2, 1)
        elif mix.shape[0] > 2:
            mix = mix[:2]

        peak = mix.abs().max()
        if peak > 0:
            mix = mix * (0.9 / peak)

        # --- Chunk and process ---
        chunk_size = _CHUNK_SIZE
        desired_step = int(self._overlap * _SAMPLE_RATE)
        step = min(desired_step, chunk_size) if desired_step > 0 else chunk_size
        num_samples = mix.shape[1]

        window = torch.tensor(
            scipy_windows.hamming(chunk_size), dtype=torch.float32,
            device=self.torch_device,
        )
        result = torch.zeros(
            2, num_samples, dtype=torch.float32, device=self.torch_device,
        )
        weight = torch.zeros(
            num_samples, dtype=torch.float32, device=self.torch_device,
        )

        starts = list(range(0, num_samples, step))
        if starts and starts[-1] + chunk_size < num_samples:
            starts.append(num_samples - chunk_size)

        logger.debug(
            "back_vocal_chunking",
            num_samples=num_samples,
            chunk_size=chunk_size,
            step=step,
            total_chunks=len(starts),
            batch_size=self._chunk_batch_size,
        )

        with torch.inference_mode():
            for batch_start in range(0, len(starts), self._chunk_batch_size):
                batch_indices = starts[
                    batch_start : batch_start + self._chunk_batch_size
                ]
                chunks = []
                for idx in batch_indices:
                    end = idx + chunk_size
                    if end <= num_samples:
                        chunk = mix[:, idx:end]
                    else:
                        chunk = torch.zeros(2, chunk_size)
                        remaining = num_samples - idx
                        chunk[:, :remaining] = mix[:, idx:]
                    chunks.append(chunk)

                batch = torch.stack(chunks).to(self.torch_device)

                if self._use_autocast and self.torch_device == "cuda":
                    with torch.amp.autocast("cuda"):
                        lead_batch = self._model(batch)
                else:
                    lead_batch = self._model(batch)

                for i, idx in enumerate(batch_indices):
                    length = min(chunk_size, num_samples - idx)
                    windowed = lead_batch[i, :, :length] * window[:length]
                    result[:, idx : idx + length] += windowed
                    weight[idx : idx + length] += window[:length]

        weight = weight.clamp(min=1e-8)
        lead = result / weight.unsqueeze(0)

        # Backing = input mix - lead
        mix_gpu = mix.to(self.torch_device)
        backing = mix_gpu - lead

        if peak > 0:
            scale = peak / 0.9
            lead = lead * scale
            backing = backing * scale

        # --- Downsample to 16kHz mono for VAD/Whisper/CTC ---
        lead_16k = F.resample(lead, _SAMPLE_RATE, 16000).mean(dim=0, keepdim=True).cpu()
        backing_16k = F.resample(backing, _SAMPLE_RATE, 16000).mean(dim=0, keepdim=True).cpu()

        stem = pathlib.Path(vocals_path).stem  # e.g. "{job_id}_(Vocals)"
        base_id = stem.replace("_(Vocals)", "")
        lead_path = str(
            pathlib.Path(self._output_dir) / f"{base_id}_(Lead).wav"
        )
        backing_path = str(
            pathlib.Path(self._output_dir) / f"{base_id}_(Backing).wav"
        )

        sf.write(lead_path, lead_16k.numpy().T, 16000, subtype="PCM_16")
        sf.write(backing_path, backing_16k.numpy().T, 16000, subtype="PCM_16")

        logger.info(
            "back_vocal_completed",
            lead_path=lead_path,
            backing_path=backing_path,
            duration_sec=round(time.monotonic() - t0, 2),
        )
        return lead_path, backing_path

    def cleanup(self) -> None:
        """Release GPU memory held by the model."""
        import gc

        if self._model is not None:
            del self._model
            self._model = None

        gc.collect()

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        logger.info("back_vocal_cleanup_done")
