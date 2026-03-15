"""Vector math utilities shared across backend and worker."""

from __future__ import annotations

import math


def normalize_l2(vector: list[float]) -> list[float]:
    """L2-normalise a vector in-place. Returns zero vector if norm is near zero."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm < 1e-8:
        return vector
    return [x / norm for x in vector]
