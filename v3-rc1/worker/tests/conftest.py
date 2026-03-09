"""Shared fixtures for v3-rc1 worker tests.

PYTHONPATH must include v3-rc1/shared and v3-rc1/worker before running.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# Ensure shared and worker packages are importable.
_RC1_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_SHARED = _RC1_ROOT / "shared"
_WORKER = _RC1_ROOT / "worker"

for p in (_SHARED, _WORKER):
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
