"""Shared fixtures for worker tests.

The project root (parent of the ``worker/`` and ``shared/`` packages) and the
``shared/`` source tree must both be on sys.path so that:

  from worker.gpu.gpu_pipeline import GpuPipeline
  from karaoke_shared.models.track import Track

both resolve correctly.
"""

from __future__ import annotations

import pathlib
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
