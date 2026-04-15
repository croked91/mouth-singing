"""Shared fixtures for worker tests.

The project root (parent of the ``worker/`` and ``shared/`` packages) and the
``shared/`` source tree must both be on sys.path so that:

  from worker.gpu.gpu_pipeline import GpuPipeline
  from karaoke_shared.models.track import Track

both resolve correctly.

This module also installs the ``--regen-fixtures`` pytest option, which
re-runs the production GPU pipeline (UVR → BackVocal → Silero → MMS)
inside the worker container for each TorchCTCAligner alignment fixture
defined under ``tests/worker/fixtures/alignment/<name>/source.json``.
The flag is opt-in — without it, tests just read the committed
fixtures and never touch Docker or any ML model.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

# _RC1_ROOT is the project root: /home/croked/karaoke/
_RC1_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# The project root lets Python find the ``worker`` package (worker/__init__.py).
# The shared/ subdirectory lets Python find ``karaoke_shared`` (shared/karaoke_shared/).
_SHARED = _RC1_ROOT / "shared"

for p in (_RC1_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# --regen-fixtures: re-run the production pipeline for every alignment
# fixture before the test session starts. Off by default.
# ---------------------------------------------------------------------------

_ALIGNMENT_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "alignment"


def pytest_addoption(parser):
    parser.addoption(
        "--regen-fixtures",
        action="store_true",
        default=False,
        help=(
            "Regenerate alignment fixtures by re-running UVR/BackVocal/Silero/MMS "
            "inside the worker GPU container before the test session starts. "
            "Requires `make up-gpu` to be running and source MP3s available "
            "at the path specified in each fixture's source.json."
        ),
    )


def pytest_configure(config):
    if not config.getoption("--regen-fixtures"):
        return
    _regenerate_alignment_fixtures()


def _docker_compose_ps_worker() -> str:
    """Return the running worker container ID (or raise if absent)."""
    proc = subprocess.run(
        [
            "docker", "compose",
            "-f", str(_RC1_ROOT / "docker-compose.yml"),
            "-f", str(_RC1_ROOT / "docker-compose.gpu.yml"),
            "ps", "-q", "worker",
        ],
        check=True, capture_output=True, text=True,
    )
    ids = [line for line in proc.stdout.splitlines() if line.strip()]
    if not ids:
        raise RuntimeError(
            "--regen-fixtures: no worker container is running. "
            "Start it with `make up-gpu` first."
        )
    return ids[0]


def _docker(*args, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True)


def _regenerate_alignment_fixtures() -> None:
    if not shutil.which("docker"):
        raise RuntimeError("--regen-fixtures: docker CLI not found in PATH.")

    fixture_dirs = sorted(p for p in _ALIGNMENT_FIXTURES.iterdir() if p.is_dir())
    candidates = []
    for d in fixture_dirs:
        src = d / "source.json"
        if src.exists():
            candidates.append((d, json.loads(src.read_text(encoding="utf-8"))))
        else:
            print(f"[regen] {d.name}: no source.json — skipping.")

    if not candidates:
        print("[regen] no fixtures with source.json found — nothing to do.")
        return

    worker = _docker_compose_ps_worker()
    print(f"[regen] worker container = {worker[:12]}")

    # Make sure the latest generator script is in the container even
    # if the image was built before it was added.
    script_local = _RC1_ROOT / "scripts" / "generate_alignment_fixtures.py"
    if not script_local.exists():
        raise FileNotFoundError(
            f"--regen-fixtures: missing {script_local}; cannot regenerate."
        )
    _docker("cp", str(script_local), f"{worker}:/project/scripts/generate_alignment_fixtures.py")

    for fixture_dir, source in candidates:
        name = fixture_dir.name
        mp3 = (_RC1_ROOT / source["mp3_relative"]).resolve()
        lyrics = (fixture_dir / source["lyrics_file"]).resolve()
        language = source.get("language", "ru")

        if not mp3.exists():
            raise FileNotFoundError(
                f"--regen-fixtures: source MP3 missing for '{name}': {mp3}"
            )
        if not lyrics.exists():
            raise FileNotFoundError(
                f"--regen-fixtures: lyrics file missing for '{name}': {lyrics}"
            )

        in_mp3 = f"/tmp/regen_{name}.mp3"
        in_txt = f"/tmp/regen_{name}.txt"
        out_dir = f"/tmp/regen_fixtures/{name}"

        print(f"[regen] {name}: copying inputs → {worker[:12]}")
        _docker("cp", str(mp3), f"{worker}:{in_mp3}")
        _docker("cp", str(lyrics), f"{worker}:{in_txt}")
        _docker(
            "exec", worker, "rm", "-rf", out_dir,
        )

        print(f"[regen] {name}: running pipeline (UVR + BackVocal + Silero + MMS)")
        subprocess.run(
            [
                "docker", "exec", worker,
                "python", "/project/scripts/generate_alignment_fixtures.py",
                "--mp3", in_mp3,
                "--lyrics", in_txt,
                "--output", out_dir,
                "--language", language,
            ],
            check=True,
        )

        print(f"[regen] {name}: copying generated artifacts back")
        for fname in ("vocals.wav", "alignment.json"):
            _docker(
                "cp",
                f"{worker}:{out_dir}/{fname}",
                str(fixture_dir / fname),
            )

    print("[regen] done — all fixtures up to date.")

# Test data from m3 experiments
TEST_DATA_DIR = _RC1_ROOT.parent / "m3_test" / "test_data"


@pytest.fixture
def test_data_dir():
    """Path to m3_test/test_data/."""
    return TEST_DATA_DIR


@pytest.fixture
def track1_vocals(test_data_dir):
    """Path to track 1 vocals.wav."""
    return str(test_data_dir / "1" / "vocals.wav")


@pytest.fixture
def track1_lyrics(test_data_dir):
    """Track 1 lyrics text."""
    return (test_data_dir / "1" / "lyrics.txt").read_text(encoding="utf-8")


@pytest.fixture
def track1_meta(test_data_dir):
    """Track 1 metadata dict."""
    import json
    return json.loads((test_data_dir / "1" / "meta.json").read_text())
