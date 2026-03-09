#!/usr/bin/env python3
"""Pre-download all ML models into MODEL_CACHE_DIR.

Run before starting the service:
    docker compose run --rm worker python /worker/tools/download_models.py
"""

import os
import pathlib

MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/data/models")


def main():
    print("1/4 Loading faster-whisper tiny...")
    from faster_whisper import WhisperModel
    WhisperModel("tiny", device="cpu", compute_type="int8",
                 download_root=f"{MODEL_CACHE_DIR}/hf")
    print("    OK")

    print("2/4 Loading sentence-transformers...")
    from sentence_transformers import SentenceTransformer
    SentenceTransformer(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        cache_folder=MODEL_CACHE_DIR,
    )
    print("    OK")

    print("3/4 Loading MMS-300m (ctc-forced-aligner)...")
    from ctc_forced_aligner import AlignmentSingleton
    AlignmentSingleton()
    print("    OK")

    print("4/4 Loading BS-Roformer (audio-separator)...")
    from audio_separator.separator import Separator
    pathlib.Path(f"{MODEL_CACHE_DIR}/uvr").mkdir(parents=True, exist_ok=True)
    sep = Separator(
        output_dir="/tmp",
        model_file_dir=f"{MODEL_CACHE_DIR}/uvr",
    )
    sep.load_model("model_bs_roformer_ep_317_sdr_12.9755.ckpt")
    print("    OK")

    print("\nAll models downloaded successfully.")


if __name__ == "__main__":
    main()
