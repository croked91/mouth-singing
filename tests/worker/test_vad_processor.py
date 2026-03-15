"""Unit tests for VADProcessor."""

from __future__ import annotations

from worker.common.vad_processor import VADProcessor


class TestVADProcessor:
    """Tests for VADProcessor.process()."""

    def test_custom_top_db(self):
        """VADProcessor accepts custom top_db."""
        vad = VADProcessor(top_db=25)
        assert vad._top_db == 25
