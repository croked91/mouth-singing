"""BS-Roformer vocal separator — direct PyTorch inference.

Bypasses the ``audio-separator`` wrapper for lower overhead:
  - torch.inference_mode() instead of torch.no_grad()
  - Batched chunk processing (multiple chunks per forward pass)
  - Overlap-add on GPU (no CPU↔GPU transfers per chunk)
  - Native autocast FP16
"""

from __future__ import annotations

import pathlib
import time

import structlog

logger = structlog.get_logger(__name__)

# Architecture config for model_bs_roformer_ep_317_sdr_12.9755.ckpt
_MODEL_CONFIG = {
    "dim": 512,
    "depth": 12,
    "stereo": True,
    "num_stems": 1,
    "time_transformer_depth": 1,
    "freq_transformer_depth": 1,
    "dim_head": 64,
    "heads": 8,
    "attn_dropout": 0.1,
    "ff_dropout": 0.1,
    "flash_attn": True,
    "mask_estimator_depth": 2,
    "stft_n_fft": 2048,
    "stft_hop_length": 441,
    "stft_win_length": 2048,
    "stft_normalized": False,
    "freqs_per_bands": (
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
        2, 2, 2, 2, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 12, 12, 12, 12,
        12, 12, 12, 12, 24, 24, 24, 24, 24, 24, 24, 24, 48, 48, 48, 48,
        48, 48, 48, 48, 128, 129,
    ),
}

# Audio params matching the model's training config.
_SAMPLE_RATE = 44100
_STFT_HOP = 441
_DIM_T = 801  # model's inference.dim_t
_CHUNK_SIZE = _STFT_HOP * (_DIM_T - 1)  # 352_800 samples (~8 sec at 44.1kHz)


class UVRSeparator:
    """Direct PyTorch BS-Roformer separator.

    Args:
        model_cache_dir: Directory where model checkpoint is stored.
        media_root: Root directory for media output.
        model_name: Checkpoint filename.
        torch_device: 'cuda' or 'cpu'.
        chunk_batch_size: Number of audio chunks per forward pass.
        use_autocast: Enable FP16 autocast on GPU.
        overlap: Overlap factor between chunks (higher = better quality, slower).
    """

    MODEL_NAME = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"

    def __init__(
        self,
        model_cache_dir: str,
        media_root: str,
        model_name: str | None = None,
        torch_device: str = "cuda",
        chunk_batch_size: int = 4,
        use_autocast: bool = True,
        overlap: int = 4,
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

    def fallback_to_cpu(self) -> "UVRSeparator":
        """Build a CPU-mode separator with the same configuration.

        Used by the GPU pipeline to recover from CUDA OOM without reaching
        into private attributes.
        """
        return UVRSeparator(
            model_cache_dir=self.model_cache_dir,
            media_root=self.media_root,
            model_name=self._model_name,
            torch_device="cpu",
            chunk_batch_size=1,
            use_autocast=False,
            overlap=self._overlap,
        )

    def _ensure_model(self):
        """Load the BS-Roformer model on first use."""
        if self._model is not None:
            return

        import torch
        from audio_separator.separator.uvr_lib_v5.roformer.bs_roformer import (
            BSRoformer,
        )

        output_dir = pathlib.Path(self.media_root) / "instrumental"
        output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = str(output_dir)

        model_path = str(pathlib.Path(self.model_cache_dir) / self._model_name)

        self._model = BSRoformer(**_MODEL_CONFIG)
        state_dict = torch.load(model_path, map_location="cpu")
        if isinstance(state_dict, dict) and "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
        self._model.load_state_dict(state_dict)
        # FP16 only on GPU (paired with autocast). CPU has no autocast and
        # would hit `mat1/mat2 dtype mismatch` because input chunks are
        # built as float32 in `separate()`.
        if self.torch_device == "cpu":
            self._model.to(self.torch_device).float().eval()
        else:
            self._model.to(self.torch_device).half().eval()

        logger.info(
            "uvr_model_loaded",
            model=self._model_name,
            device=self.torch_device,
            params_m=round(
                sum(p.numel() for p in self._model.parameters()) / 1e6, 1
            ),
        )

    def separate(self, mp3_path: str) -> tuple[str, str]:
        """Separate vocals from instrumental.

        Runs synchronously — use asyncio.to_thread from async context.

        Returns:
            (vocals_path, instrumental_path) tuple of WAV files.
        """
        import soundfile as sf
        import torch
        import torchaudio.functional as F
        from scipy.signal import windows as scipy_windows

        self._ensure_model()

        logger.info("uvr_starting", mp3_path=mp3_path)
        t0 = time.monotonic()

        # --- Load audio ---
        data, sr = sf.read(mp3_path, dtype="float32")  # (samples, channels)
        if data.ndim == 1:
            data = data[:, None]
        mix = torch.from_numpy(data.T)  # (channels, samples)
        if sr != _SAMPLE_RATE:
            mix = F.resample(mix, sr, _SAMPLE_RATE)

        # Ensure stereo.
        if mix.shape[0] == 1:
            mix = mix.repeat(2, 1)
        elif mix.shape[0] > 2:
            mix = mix[:2]

        # Normalize amplitude to 0.9 peak.
        peak = mix.abs().max()
        if peak > 0:
            mix = mix * (0.9 / peak)

        # --- Chunk and process ---
        chunk_size = _CHUNK_SIZE
        # overlap is in seconds — convert to samples, clamp to chunk_size.
        desired_step = int(self._overlap * _SAMPLE_RATE)
        step = min(desired_step, chunk_size) if desired_step > 0 else chunk_size
        num_samples = mix.shape[1]

        # Hamming window for overlap-add (on target device).
        window = torch.tensor(
            scipy_windows.hamming(chunk_size), dtype=torch.float32,
            device=self.torch_device,
        )

        # Prepare result accumulator on GPU.
        result = torch.zeros(
            2, num_samples, dtype=torch.float32, device=self.torch_device,
        )
        weight = torch.zeros(
            num_samples, dtype=torch.float32, device=self.torch_device,
        )

        # Collect chunk start positions.
        starts = list(range(0, num_samples, step))
        # Ensure last chunk covers the end.
        if starts and starts[-1] + chunk_size < num_samples:
            starts.append(num_samples - chunk_size)

        logger.debug(
            "uvr_chunking",
            num_samples=num_samples,
            chunk_size=chunk_size,
            step=step,
            total_chunks=len(starts),
            batch_size=self._chunk_batch_size,
        )

        # Process in batches.
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
                        # Pad short last chunk with zeros.
                        chunk = torch.zeros(2, chunk_size)
                        remaining = num_samples - idx
                        chunk[:, :remaining] = mix[:, idx:]
                    chunks.append(chunk)

                batch = torch.stack(chunks).to(self.torch_device)

                if self._use_autocast and self.torch_device == "cuda":
                    with torch.amp.autocast("cuda"):
                        vocals_batch = self._model(batch)
                else:
                    vocals_batch = self._model(batch)

                # Overlap-add on GPU.
                for i, idx in enumerate(batch_indices):
                    length = min(chunk_size, num_samples - idx)
                    windowed = vocals_batch[i, :, :length] * window[:length]
                    result[:, idx : idx + length] += windowed
                    weight[idx : idx + length] += window[:length]

        # Normalize by overlap weights.
        weight = weight.clamp(min=1e-8)
        vocals = result / weight.unsqueeze(0)

        # Instrumental = original mix minus vocals.
        mix_gpu = mix.to(self.torch_device)
        instrumental = mix_gpu - vocals

        # De-normalize back to original amplitude.
        if peak > 0:
            scale = peak / 0.9
            vocals = vocals * scale
            instrumental = instrumental * scale

        # --- Save WAV files ---
        instrumental_cpu = instrumental.cpu()

        # Vocals: downsample to 16kHz mono on GPU (required by VAD/Whisper).
        vocals_16k = F.resample(vocals, _SAMPLE_RATE, 16000).mean(dim=0, keepdim=True).cpu()

        job_id = pathlib.Path(mp3_path).stem
        vocals_path = str(
            pathlib.Path(self._output_dir) / f"{job_id}_(Vocals).wav"
        )
        instrumental_path = str(
            pathlib.Path(self._output_dir) / f"{job_id}_(Instrumental).wav"
        )

        sf.write(vocals_path, vocals_16k.numpy().T, 16000, subtype="PCM_16")
        sf.write(instrumental_path, instrumental_cpu.numpy().T, _SAMPLE_RATE)

        logger.info(
            "uvr_completed",
            vocals_path=vocals_path,
            instrumental_path=instrumental_path,
            duration_sec=round(time.monotonic() - t0, 2),
        )
        return vocals_path, instrumental_path

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

        logger.info("uvr_cleanup_done")
