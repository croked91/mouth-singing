"""Bootstrap runner for mass-processing karaoke MP3 files.

Uses ``multiprocessing.Pool`` with ``imap_unordered`` so that all CPU cores
work in parallel and progress can be displayed incrementally via tqdm.

Each worker process runs the full pipeline for a single track:
  1. UVR separation (vocals + instrumental)
  2. Lyrics retrieval: LRC dump lookup → WhisperX force-align, or full ASR
  3. Syllabification of word-level timestamps
  4. Audio feature extraction (45-d vector)
  5. Lyric embedding (384-d vector)
  6. Persist to SQLite and batch-upsert to QDrant

QDrant upserts are accumulated in the main process and flushed every 100 tracks
to avoid per-track round-trips to the vector database.
"""

from __future__ import annotations

import dataclasses
import multiprocessing
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import structlog
from tqdm import tqdm

logger = structlog.get_logger(__name__)

# Collection names used in QDrant.
_AUDIO_COLLECTION = "audio_features"
_LYRIC_COLLECTION = "lyrics_embeddings"

# Number of QDrant points to upsert in a single call.
_QDRANT_BATCH_SIZE = 100


# ------------------------------------------------------------------
# Configuration dataclass
# ------------------------------------------------------------------


@dataclasses.dataclass
class BootstrapConfig:
    """All configuration needed to run the bootstrap pipeline.

    Attributes:
        input_dir: Directory containing source MP3 files.
        output_dir: Root directory for all generated output (audio, video, DB).
        workers: Number of parallel worker processes.
        lrclib_dump_path: Optional path to an lrc-lib JSON dump file.
        language: Language code used for WhisperX transcription (e.g. "ru").
        db_path: Path to the SQLite database file.
        qdrant_host: Hostname of the QDrant server.
        qdrant_port: Port of the QDrant server.
        skip_existing: If True, tracks already in the DB are not reprocessed.
    """

    input_dir: Path
    output_dir: Path
    workers: int
    lrclib_dump_path: Path | None
    lrclib_sqlite_path: Path | None
    lrclib_url: str | None
    language: str
    device: str
    whisper_model: str
    db_path: Path
    qdrant_host: str
    qdrant_port: int
    skip_existing: bool


# ------------------------------------------------------------------
# Token adapter for Syllabifier
# ------------------------------------------------------------------


@dataclasses.dataclass
class _WordToken:
    """Adapter so WhisperX word dicts can be fed into Syllabifier.

    Syllabifier expects objects with ``.text``, ``.start_ms``, ``.end_ms``,
    and ``.language`` attributes.
    """

    text: str
    start_ms: int
    end_ms: int
    language: str


def _words_to_tokens(words: list[dict], language: str) -> list[_WordToken]:
    """Convert WhisperX word dicts to _WordToken objects.

    Args:
        words: Output of ``WhisperXTranscriber.transcribe()`` or
               ``WhisperXTranscriber.force_align()``.
        language: Language code to attach to each token.

    Returns:
        List of _WordToken objects suitable for ``Syllabifier.syllabify()``.
    """
    tokens: list[_WordToken] = []
    for word_info in words:
        tokens.append(
            _WordToken(
                text=word_info["word"],
                start_ms=int(word_info["start"] * 1000),
                end_ms=int(word_info["end"] * 1000),
                language=language,
            )
        )
    return tokens


# ------------------------------------------------------------------
# Track ID generation
# ------------------------------------------------------------------


def _track_id_from_path(mp3_path: Path) -> str:
    """Derive a deterministic UUID from the MP3 filename.

    Using uuid5 with NAMESPACE_URL means the same file always gets the same
    ID regardless of where the bootstrap is run from, enabling idempotent
    reprocessing.

    Args:
        mp3_path: Path to the source MP3 file.

    Returns:
        A UUID string derived from the filename stem.
    """
    return str(uuid5(NAMESPACE_URL, mp3_path.name))


# ------------------------------------------------------------------
# SQLite helpers (synchronous — runs in worker processes)
# ------------------------------------------------------------------


def _track_exists_in_db(db_path: Path, track_id: str) -> bool:
    """Return True if a track with *track_id* is already in the DB.

    Uses a direct sqlite3 connection (not aiosqlite) because this runs in
    worker processes that do not have an event loop.

    Args:
        db_path: Path to the SQLite database file.
        track_id: The UUID string to check.

    Returns:
        True if the track already exists, False otherwise.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "SELECT 1 FROM tracks WHERE id = ? LIMIT 1", (track_id,)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def _insert_track_sync(db_path: Path, track_data: dict) -> None:
    """Insert a single track row using a synchronous sqlite3 connection.

    Args:
        db_path: Path to the SQLite database file.
        track_data: Dict matching the tracks table schema.
    """
    import json

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO tracks (
                id, artist, title, duration_sec, mp3_path, instrumental_path,
                clip_path, lyrics_text, syllable_timings, language, source,
                status, error_message, play_count, qdrant_synced,
                created_at, updated_at
            ) VALUES (
                :id, :artist, :title, :duration_sec, :mp3_path,
                :instrumental_path, :clip_path, :lyrics_text,
                :syllable_timings, :language, :source, :status,
                :error_message, :play_count, :qdrant_synced,
                :created_at, :updated_at
            )
            """,
            track_data,
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------
# Syllable timestamp mapping
# ------------------------------------------------------------------


def _map_syllable_timestamps(
    whisperx_words: list[dict],
    expected_syllables: list[str],
    is_word_start: list[bool],
    is_line_start: list[bool] | None = None,
) -> list:
    """Map WhisperX force-align output to ``SyllableTiming`` objects.

    WhisperX may drop some "words" (syllables) that it cannot align.
    We match by position and add prefixes for word/line boundaries so
    that rendered karaoke text has proper spacing and line breaks.

    Prefix conventions:
    - ``" "`` (space) marks the first syllable of a new word.
    - ``"\\n"`` marks the first syllable of a new LRC line (implies
      word boundary too).  The frontend ``groupIntoLines()`` splits
      on this marker instead of using gap/punctuation heuristics.

    Args:
        whisperx_words: Output of ``WhisperXTranscriber.force_align()``,
            each with ``"word"``, ``"start"``, ``"end"`` keys.
        expected_syllables: The syllable strings sent to WhisperX.
        is_word_start: Boolean flags — ``True`` marks the first syllable
            of a new word.
        is_line_start: Optional boolean flags — ``True`` marks the first
            syllable of a new LRC line.

    Returns:
        List of ``SyllableTiming`` instances.
    """
    from karaoke_shared.models.track import SyllableTiming  # noqa: PLC0415

    timings: list[SyllableTiming] = []

    for i, word_info in enumerate(whisperx_words):
        if i >= len(expected_syllables):
            break

        syllable_text = word_info["word"]

        if i > 0:
            # Line break marker takes priority over word boundary space.
            if is_line_start and i < len(is_line_start) and is_line_start[i]:
                syllable_text = "\n" + syllable_text
            elif i < len(is_word_start) and is_word_start[i]:
                syllable_text = " " + syllable_text

        timings.append(
            SyllableTiming(
                syllable=syllable_text,
                start=float(word_info["start"]),
                end=float(word_info["end"]),
            )
        )

    return timings


# ------------------------------------------------------------------
# Per-track worker function
# ------------------------------------------------------------------


def _process_track(args: tuple) -> dict | None:
    """Process a single MP3 file through the full karaoke pipeline.

    This function is designed to run in a worker process. It is a plain
    function (not a method) because ``multiprocessing.Pool`` requires
    picklable callables, and bound methods can cause issues.

    Args:
        args: A tuple of ``(mp3_path, config)`` where ``mp3_path`` is a
              ``Path`` object and ``config`` is a ``BootstrapConfig``.

    Returns:
        A dict with vector data for QDrant batch-upsert on success, or
        ``None`` on failure (the error is logged but does not propagate).
    """
    mp3_path, config = args

    # Import heavy dependencies lazily — they are only installed in the
    # Docker image, not necessarily in the host environment.
    from karaoke_shared.ml.feature_extractor import FeatureExtractor
    from karaoke_shared.ml.lyric_embedder import LyricEmbedder
    from karaoke_shared.utils.syllabifier import Syllabifier

    track_id = _track_id_from_path(mp3_path)
    track_log = logger.bind(track_id=track_id, mp3_path=str(mp3_path))

    try:
        if config.skip_existing and _track_exists_in_db(config.db_path, track_id):
            track_log.debug("bootstrap.skipping_existing")
            return None

        # Derive artist and title from the filename.
        stem = mp3_path.stem
        if " - " in stem:
            artist, title = stem.split(" - ", maxsplit=1)
        else:
            artist = "Unknown"
            title = stem

        track_log.info("bootstrap.processing_track", artist=artist, title=title)

        # ----------------------------------------------------------
        # Step 1: UVR separation
        # ----------------------------------------------------------
        from app.pipeline.uvr_separator import UVRSeparator  # noqa: PLC0415

        model_cache_dir = str(config.output_dir / "models")
        Path(model_cache_dir).mkdir(parents=True, exist_ok=True)

        separator = UVRSeparator(
            model_cache_dir=model_cache_dir,
            media_root=str(config.output_dir),
        )
        vocals_path, instrumental_path = separator.separate(str(mp3_path))
        separator.cleanup()
        track_log.info(
            "bootstrap.uvr_done",
            vocals_path=vocals_path,
            instrumental_path=instrumental_path,
        )

        # ----------------------------------------------------------
        # Steps 2+3: Lyrics retrieval, syllabification, alignment
        # ----------------------------------------------------------
        from app.pipeline.lrclib_dump import LRCLibDump  # noqa: PLC0415

        lyrics_text: str = ""
        syllable_timings: list = []
        used_lrc = False

        # Select LRC adapter: HTTP server, SQLite, or JSON-lines dump.
        lrc_raw: str | None = None
        if config.lrclib_url is not None:
            from app.pipeline.lrclib_http_adapter import (  # noqa: PLC0415
                LRCLibHTTPAdapter,
            )

            adapter = LRCLibHTTPAdapter(config.lrclib_url)
            try:
                lrc_raw = adapter.search(artist, title)
            finally:
                adapter.close()
        elif config.lrclib_sqlite_path is not None:
            from app.pipeline.lrclib_sqlite_adapter import (  # noqa: PLC0415
                LRCLibSQLiteAdapter,
            )

            adapter = LRCLibSQLiteAdapter(config.lrclib_sqlite_path)
            try:
                lrc_raw = adapter.search(artist, title)
            finally:
                adapter.close()
        elif config.lrclib_dump_path is not None:
            dump = LRCLibDump(config.lrclib_dump_path)
            try:
                lrc_raw = dump.search(artist, title)
            finally:
                dump.close()

        if lrc_raw:
            track_log.info("bootstrap.lrc_found")
            lrc_lines = LRCLibDump.parse_lrc(lrc_raw)
            lyrics_text = "\n".join(line["text"] for line in lrc_lines)

            # Build per-line segments: syllabify each LRC line and use
            # the LRC timestamps as segment boundaries for WhisperX.
            syllabifier = Syllabifier()
            segments: list[dict] = []
            all_syl_strings: list[str] = []
            all_is_word_start: list[bool] = []
            all_is_line_start: list[bool] = []

            for line in lrc_lines:
                text = line["text"].strip()
                if not text:
                    continue
                syl_strings, is_word_start = syllabifier.split_text_to_syllables(
                    text, config.language
                )
                if not syl_strings:
                    continue
                syl_text = " ".join(syl_strings)
                segments.append({
                    "text": syl_text,
                    "start": line["start_ms"] / 1000.0,
                    "end": line["end_ms"] / 1000.0,
                })
                # Track line boundaries: first syllable of each LRC line
                line_flags = [False] * len(syl_strings)
                line_flags[0] = True
                all_syl_strings.extend(syl_strings)
                all_is_word_start.extend(is_word_start)
                all_is_line_start.extend(line_flags)

            if segments:
                from app.pipeline.whisperx_transcriber import (  # noqa: PLC0415
                    WhisperXTranscriber,
                )

                try:
                    transcriber = WhisperXTranscriber(model_name=config.whisper_model, language=config.language, device=config.device)
                    syl_timestamps = transcriber.force_align(
                        Path(vocals_path), segments
                    )
                    transcriber.cleanup()
                    syllable_timings = _map_syllable_timestamps(
                        syl_timestamps, all_syl_strings, all_is_word_start,
                        all_is_line_start,
                    )
                    used_lrc = True
                except Exception:
                    track_log.warning(
                        "bootstrap.force_align_failed_falling_back_to_asr",
                        exc_info=True,
                    )
        else:
            track_log.info("bootstrap.lrc_not_found_falling_back_to_asr")

        if not used_lrc:
            # Fallback: full ASR + proportional syllabification.
            from app.pipeline.whisperx_transcriber import (  # noqa: PLC0415
                WhisperXTranscriber,
            )

            transcriber = WhisperXTranscriber(language=config.language, device=config.device)
            word_timestamps = transcriber.transcribe(Path(vocals_path))
            transcriber.cleanup()
            lyrics_text = " ".join(w["word"] for w in word_timestamps)

            syllabifier = Syllabifier()
            tokens = _words_to_tokens(word_timestamps, config.language)
            syllable_timings = syllabifier.syllabify(tokens)

        track_log.info(
            "bootstrap.lyrics_done",
            syllable_count=len(syllable_timings),
            used_lrc=used_lrc,
        )

        # ----------------------------------------------------------
        # Step 4: Audio feature extraction
        # ----------------------------------------------------------
        feature_extractor = FeatureExtractor()
        audio_vector = feature_extractor.extract(str(mp3_path))
        track_log.info("bootstrap.features_extracted")

        # ----------------------------------------------------------
        # Step 5: Lyric embedding
        # ----------------------------------------------------------
        lyric_embedder = LyricEmbedder()
        lyric_vector = lyric_embedder.embed(lyrics_text)
        track_log.info("bootstrap.lyrics_embedded")

        # ----------------------------------------------------------
        # Step 6: Persist to SQLite
        # ----------------------------------------------------------
        now_iso = datetime.now(timezone.utc).isoformat()
        syllable_timings_json = _serialise_syllable_timings(syllable_timings)

        _insert_track_sync(
            config.db_path,
            {
                "id": track_id,
                "artist": artist,
                "title": title,
                "duration_sec": None,
                "mp3_path": str(mp3_path),
                "instrumental_path": instrumental_path,
                "clip_path": None,
                "lyrics_text": lyrics_text or None,
                "syllable_timings": syllable_timings_json,
                "language": config.language,
                "source": "catalog",
                "status": "ready",
                "error_message": None,
                "play_count": 0,
                "qdrant_synced": 0,
                "created_at": now_iso,
                "updated_at": now_iso,
            },
        )
        track_log.info("bootstrap.sqlite_saved")

        # Return vector data for batch QDrant upsert in the main process.
        return {
            "track_id": track_id,
            "artist": artist,
            "title": title,
            "audio_vector": audio_vector,
            "lyric_vector": lyric_vector,
        }

    except Exception:
        track_log.exception("bootstrap.track_failed")
        return None


def _serialise_syllable_timings(syllable_timings: list) -> str | None:
    """Serialise syllable timings to a JSON string for the DB.

    Args:
        syllable_timings: List of ``SyllableTiming`` Pydantic model instances.

    Returns:
        JSON string, or ``None`` if the list is empty.
    """
    import json

    if not syllable_timings:
        return None
    return json.dumps([st.model_dump() for st in syllable_timings])


# ------------------------------------------------------------------
# Main runner
# ------------------------------------------------------------------


class BootstrapRunner:
    """Orchestrates parallel processing of an MP3 directory.

    Uses ``multiprocessing.Pool`` with ``imap_unordered`` so that:
    - All available CPU cores are kept busy.
    - Progress can be shown incrementally with tqdm.
    - A failure in one track does not halt the whole batch.

    QDrant vectors are accumulated in the main process and batch-upserted
    every ``_QDRANT_BATCH_SIZE`` (100) tracks to reduce round-trips.

    Args:
        config: All runtime settings for the bootstrap run.
    """

    def __init__(self, config: BootstrapConfig) -> None:
        self._config = config

    @staticmethod
    def _ensure_db_schema(db_path: Path) -> None:
        """Create the tracks table if it does not exist."""
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY NOT NULL,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL,
                    duration_sec INTEGER,
                    mp3_path TEXT,
                    instrumental_path TEXT,
                    clip_path TEXT,
                    lyrics_text TEXT,
                    syllable_timings TEXT,
                    language TEXT,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    qdrant_synced INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
                CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);
                """
            )
        finally:
            conn.close()

    def run(self) -> None:
        """Discover MP3 files and process them in parallel.

        Steps:
        1. Scan ``input_dir`` for ``*.mp3`` files.
        2. Launch a ``multiprocessing.Pool`` with the configured worker count.
        3. Map each MP3 through the pipeline, showing a tqdm progress bar.
        4. Batch-upsert accumulated QDrant vectors.
        """
        self._ensure_db_schema(self._config.db_path)

        mp3_files = sorted(self._config.input_dir.glob("*.mp3"))
        if not mp3_files:
            logger.warning(
                "bootstrap.no_mp3_files_found",
                input_dir=str(self._config.input_dir),
            )
            return

        total = len(mp3_files)
        logger.info("bootstrap.starting", total=total, workers=self._config.workers)

        worker_args = [(mp3, self._config) for mp3 in mp3_files]

        audio_batch: list[tuple[str, list[float], dict]] = []
        lyric_batch: list[tuple[str, list[float], dict]] = []
        processed = 0
        failed = 0

        with multiprocessing.Pool(processes=self._config.workers) as pool:
            results = pool.imap_unordered(_process_track, worker_args)

            with tqdm(total=total, desc="Processing tracks", unit="track") as progress:
                for result in results:
                    progress.update(1)

                    if result is None:
                        failed += 1
                        continue

                    processed += 1
                    track_id = result["track_id"]
                    artist = result["artist"]
                    title = result["title"]

                    audio_batch.append((
                        track_id,
                        result["audio_vector"],
                        {"artist": artist, "title": title, "status": "ready"},
                    ))
                    lyric_batch.append((
                        track_id,
                        result["lyric_vector"],
                        {"artist": artist, "title": title, "status": "ready"},
                    ))

                    # Flush to QDrant when we reach the batch size.
                    if len(audio_batch) >= _QDRANT_BATCH_SIZE:
                        self._flush_qdrant(audio_batch, lyric_batch)
                        audio_batch = []
                        lyric_batch = []

        # Flush any remaining vectors.
        if audio_batch:
            self._flush_qdrant(audio_batch, lyric_batch)

        logger.info(
            "bootstrap.finished",
            total=total,
            processed=processed,
            failed=failed,
        )

    def _flush_qdrant(
        self,
        audio_batch: list[tuple[str, list[float], dict]],
        lyric_batch: list[tuple[str, list[float], dict]],
    ) -> None:
        """Batch-upsert accumulated vectors into QDrant.

        Errors are logged but do not raise so that the overall run can
        continue even if QDrant is temporarily unavailable.

        Args:
            audio_batch: List of ``(track_id, vector, payload)`` for audio features.
            lyric_batch: List of ``(track_id, vector, payload)`` for lyric embeddings.
        """
        from qdrant_client import QdrantClient  # noqa: PLC0415

        from karaoke_shared.repositories.qdrant_repository import (  # noqa: PLC0415
            QDrantRepository,
        )

        try:
            client = QdrantClient(
                host=self._config.qdrant_host,
                port=self._config.qdrant_port,
            )
            repo = QDrantRepository(client)
            repo.batch_upsert(_AUDIO_COLLECTION, audio_batch)
            repo.batch_upsert(_LYRIC_COLLECTION, lyric_batch)
            logger.info("bootstrap.qdrant_flushed", count=len(audio_batch))
        except Exception:
            logger.exception(
                "bootstrap.qdrant_flush_failed",
                count=len(audio_batch),
            )
