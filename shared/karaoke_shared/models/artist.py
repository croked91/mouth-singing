"""Pydantic models for artists."""

from __future__ import annotations

from pydantic import BaseModel


class Artist(BaseModel):
    """Artist record with optional image."""

    name: str
    image_path: str | None = None
    source: str | None = None
    created_at: str
    updated_at: str
