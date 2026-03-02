"""Bootstrap runner for mass-processing karaoke MP3 files.

**Local mode** (default): uses ``multiprocessing.Pool`` with
``imap_unordered`` so that all CPU cores work in parallel and progress can be
displayed incrementally via tqdm.

**Remote mode** (when ``BootstrapConfig.remote_host`` is set): processes
tracks sequentially, pulling each MP3 from the remote server, running the
full pipeline locally (GPU), pushing the instrumental back, writing the track
record into the remote SQLite database, and then deleting the source MP3 from
the server.  Sequential processing is correct here because the GPU is the
bottleneck — parallelism would not help.

Each track goes through:
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
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
        input_dir: Directory containing source MP3 files (local mode), or the
            local working directory where files are temporarily staged (remote
            mode).
        output_dir: Root directory for all generated output (audio, DB, models).
        workers: Number of parallel worker processes (local mode only).
        lrclib_dump_path: Optional path to an lrc-lib JSON dump file.
        lrclib_sqlite_path: Optional path to an lrclib SQLite database.
        lrclib_url: Optional URL of a remote lrclib HTTP server.
        language: Language code used for WhisperX transcription (e.g. "ru").
        device: PyTorch device for WhisperX ("cpu" or "cuda").
        whisper_model: Whisper model size (e.g. "medium", "large-v3").
        db_path: Path to the local SQLite database file.
        qdrant_host: Hostname of the QDrant server.
        qdrant_port: Port of the QDrant server.
        skip_existing: If True, tracks already in the DB are not reprocessed.
        remote_host: SSH host for remote mode, e.g. "root@130.49.170.186".
            When set, the runner operates in remote mode.
        remote_mp3_dir: Directory on the remote server containing source MP3s.
        remote_output_dir: Directory on the remote server for output files.
        remote_db_path: Path to the SQLite database on the remote server.
        delete_remote_source: If True, delete the source MP3 from the remote
            server after successfully processing and pushing results.
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
    limit: int = 0  # 0 = process all tracks, >0 = process at most N tracks.
    # Remote mode settings. All optional — local mode uses none of these.
    remote_host: str | None = None
    remote_mp3_dir: str = "/root/mp3_library"
    remote_output_dir: str = "/root/bootstrap_output"
    remote_db_path: str = "/root/bootstrap_output/karaoke.db"
    remote_container_media_prefix: str = "/data/media"
    delete_remote_source: bool = True
    uvr_model: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    mvsep_api_key: str | None = None
    gpu_id: int | None = None
    container_media_prefix: str | None = None


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

    The UUID is derived from the full filename (including extension), not the
    stem, so that the remote server's filename and a locally staged copy of
    that same file produce identical IDs.

    Args:
        mp3_path: Path to the source MP3 file.

    Returns:
        A UUID string derived from the filename.
    """
    from uuid import NAMESPACE_URL, uuid5  # noqa: PLC0415

    return str(uuid5(NAMESPACE_URL, mp3_path.name))


# ------------------------------------------------------------------
# SQLite helpers (synchronous — runs in worker processes)
# ------------------------------------------------------------------


def _track_exists_in_db(db_path: Path, track_id: str) -> bool:
    """Return True if a track with *track_id* is already in the local DB.

    Uses a direct sqlite3 connection (not aiosqlite) because this runs in
    worker processes that do not have an event loop.

    Args:
        db_path: Path to the SQLite database file.
        track_id: The UUID string to check.

    Returns:
        True if the track already exists, False otherwise.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        cursor = conn.execute(
            "SELECT 1 FROM tracks WHERE id = ? LIMIT 1", (track_id,)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def _read_track_from_db(db_path: Path, track_id: str) -> dict:
    """Read a full track row from the local SQLite database as a plain dict.

    Used in remote mode to retrieve the locally-written track record so it
    can be forwarded (with modified paths) to the remote database.

    Args:
        db_path: Path to the local SQLite database file.
        track_id: The UUID string of the track to retrieve.

    Returns:
        A dict with all column names as keys.

    Raises:
        ValueError: If no track with the given ID exists in the database.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT * FROM tracks WHERE id = ? LIMIT 1", (track_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"Track {track_id!r} not found in local DB {db_path}")
        return dict(row)
    finally:
        conn.close()


def _insert_track_sync(db_path: Path, track_data: dict) -> None:
    """Insert a single track row using a synchronous sqlite3 connection.

    Args:
        db_path: Path to the SQLite database file.
        track_data: Dict matching the tracks table schema.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
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

    This function is designed to run in a worker process (local mode) or
    called directly in the main process (remote mode). It is a plain function
    (not a method) because ``multiprocessing.Pool`` requires picklable
    callables, and bound methods can cause issues.

    Args:
        args: A tuple of ``(mp3_path, config)`` where ``mp3_path`` is a
              ``Path`` object and ``config`` is a ``BootstrapConfig``.

    Returns:
        A dict with vector data and internal paths on success, or ``None`` on
        failure (the error is logged but does not propagate).  The returned
        dict contains:

        - ``track_id`` (str): The UUID of the processed track.
        - ``artist`` (str): Artist name derived from the filename.
        - ``title`` (str): Title derived from the filename.
        - ``audio_vector`` (list[float]): 45-dimensional audio feature vector.
        - ``lyric_vector`` (list[float]): 384-dimensional lyric embedding.
        - ``_instrumental_path`` (str): Local path to the instrumental stem.
        - ``_vocals_path`` (str): Local path to the vocals stem.

        The ``_``-prefixed keys are internal and used only by ``_run_remote()``
        to know which local files to push and then clean up.
    """
    mp3_path, config = args

    # Import heavy dependencies lazily — they are only installed in the
    # Docker image, not necessarily in the host environment.
    from karaoke_shared.ml.feature_extractor import FeatureExtractor  # noqa: PLC0415
    from karaoke_shared.ml.lyric_embedder import LyricEmbedder  # noqa: PLC0415
    from karaoke_shared.utils.syllabifier import Syllabifier  # noqa: PLC0415

    track_id = _track_id_from_path(mp3_path)
    track_log = logger.bind(track_id=track_id, mp3_path=str(mp3_path))

    # These are set during UVR separation and referenced in the return dict.
    # Declare them up front so the names are always defined even if we return
    # early via an exception path.
    vocals_path: str | None = None
    instrumental_path: str | None = None

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
        # Step 1: Vocal separation (MVSEP API or local UVR)
        # ----------------------------------------------------------
        if config.mvsep_api_key:
            from app.pipeline.mvsep_separator import MVSepSeparator  # noqa: PLC0415

            separator = MVSepSeparator(
                api_key=config.mvsep_api_key,
                media_root=str(config.output_dir),
            )
        else:
            from app.pipeline.uvr_separator import UVRSeparator  # noqa: PLC0415

            model_cache_dir = str(config.output_dir / "models")
            Path(model_cache_dir).mkdir(parents=True, exist_ok=True)

            separator = UVRSeparator(
                model_cache_dir=model_cache_dir,
                media_root=str(config.output_dir),
                model_name=config.uvr_model,
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
                    transcriber = WhisperXTranscriber(
                        model_name=config.whisper_model,
                        language=config.language,
                        device=config.device,
                    )
                    syl_timestamps = transcriber.force_align(
                        Path(vocals_path), segments
                    )
                    transcriber.cleanup()
                    syllable_timings = _map_syllable_timestamps(
                        syl_timestamps,
                        all_syl_strings,
                        all_is_word_start,
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

            transcriber = WhisperXTranscriber(
                language=config.language,
                device=config.device,
            )
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
        # Step 6: Persist to local SQLite
        # ----------------------------------------------------------
        now_iso = datetime.now(timezone.utc).isoformat()
        syllable_timings_json = _serialise_syllable_timings(syllable_timings)

        # Rewrite paths for container visibility when deploying with
        # different mount points (e.g. host /root/bootstrap_output →
        # container /data/media).
        if config.container_media_prefix:
            db_instrumental_path = (
                config.container_media_prefix
                + "/instrumental/"
                + Path(instrumental_path).name
            )
            db_mp3_path = None
        else:
            db_instrumental_path = instrumental_path
            db_mp3_path = str(mp3_path)

        _insert_track_sync(
            config.db_path,
            {
                "id": track_id,
                "artist": artist,
                "title": title,
                "duration_sec": None,
                "mp3_path": db_mp3_path,
                "instrumental_path": db_instrumental_path,
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

        # Delete vocals stem — only needed for WhisperX alignment above,
        # not used in production.  Saves ~50% of stem disk usage.
        if vocals_path:
            Path(vocals_path).unlink(missing_ok=True)
            track_log.info("bootstrap.vocals_deleted")

        # Return vector data for batch QDrant upsert in the main process.
        # The _instrumental_path key is used by _run_remote() to push files.
        return {
            "track_id": track_id,
            "artist": artist,
            "title": title,
            "audio_vector": audio_vector,
            "lyric_vector": lyric_vector,
            "_instrumental_path": instrumental_path,
            "_vocals_path": None,
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
    import json  # noqa: PLC0415

    if not syllable_timings:
        return None
    return json.dumps([st.model_dump() for st in syllable_timings])


# ------------------------------------------------------------------
# Main runner
# ------------------------------------------------------------------


class BootstrapRunner:
    """Orchestrates processing of an MP3 library into karaoke tracks.

    Supports two modes:

    **Local mode** (``config.remote_host is None``): scans a local directory
    for MP3 files and processes them in parallel using
    ``multiprocessing.Pool``.

    **Remote mode** (``config.remote_host`` is set): lists MP3 files on the
    remote server via SSH, pulls each one, processes it locally (GPU), pushes
    the instrumental back, writes the track record to the remote SQLite DB,
    and deletes the source MP3 from the server.  Processing is sequential
    because GPU is the bottleneck.

    QDrant vectors are accumulated in the main process and batch-upserted
    every ``_QDRANT_BATCH_SIZE`` (100) tracks to reduce round-trips.

    Args:
        config: All runtime settings for the bootstrap run.
    """

    def __init__(self, config: BootstrapConfig) -> None:
        self._config = config

    @staticmethod
    def _ensure_db_schema(db_path: Path) -> None:
        """Create the local tracks table and indexes if they do not exist.

        Args:
            db_path: Path to the local SQLite database file.
        """
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
        """Dispatch to remote or local processing based on configuration.

        If ``config.remote_host`` is set, runs in remote mode (sequential,
        pull-process-push-delete).  Otherwise runs in local mode (parallel
        pool over a local MP3 directory).
        """
        if self._config.remote_host:
            self._run_remote()
        elif self._config.gpu_id is not None:
            self._run_local_gpu()
        else:
            self._run_local()

    def _run_local(self) -> None:
        """Discover local MP3 files and process them in parallel.

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

    def _run_local_gpu(self) -> None:
        """Process local MP3s one-by-one with atomic file claiming.

        Designed for multi-GPU servers: each worker runs as a separate
        process with ``--gpu-id N``, claiming files via atomic rename so
        multiple workers can safely share the same input directory.

        Safe for preemptible (spot) instances: every track is fully
        persisted (SQLite + QDrant) before moving to the next one, and
        stale claims from killed processes are recovered on restart by
        the launch script.
        """
        import os  # noqa: PLC0415

        gpu_id = self._config.gpu_id
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)

        log = logger.bind(gpu_id=gpu_id)
        log.info("bootstrap.local_gpu_starting", gpu_id=gpu_id)

        self._ensure_db_schema(self._config.db_path)

        input_dir = self._config.input_dir
        processing_dir = input_dir / ".processing"
        processing_dir.mkdir(parents=True, exist_ok=True)

        # Ensure output directories exist.
        self._config.output_dir.mkdir(parents=True, exist_ok=True)

        audio_batch: list[tuple[str, list[float], dict]] = []
        lyric_batch: list[tuple[str, list[float], dict]] = []
        processed = 0
        failed = 0
        skipped = 0
        failed_ids: set[str] = set()  # track IDs that already failed — skip on rescan

        # Count total available MP3s for the progress bar.
        mp3_files = sorted(input_dir.glob("*.mp3"))
        total = len(mp3_files)
        if self._config.limit > 0:
            total = min(total, self._config.limit)

        log.info("bootstrap.local_gpu_found", total_available=len(mp3_files))

        with tqdm(
            total=total,
            desc=f"GPU {gpu_id}",
            unit="track",
        ) as progress:
            while True:
                if self._config.limit > 0 and processed >= self._config.limit:
                    break

                # Rescan directory each iteration — other workers may
                # have claimed files since last scan.
                mp3_files = sorted(input_dir.glob("*.mp3"))
                if not mp3_files:
                    break

                claimed = False
                for mp3_path in mp3_files:
                    # Skip hidden files / directories.
                    if mp3_path.name.startswith("."):
                        continue

                    claimed_path = processing_dir / mp3_path.name

                    # Atomic claim via rename.  On the same filesystem
                    # this is an atomic operation.  If another worker
                    # renamed it first, we get FileNotFoundError.
                    try:
                        mp3_path.rename(claimed_path)
                    except (FileNotFoundError, OSError):
                        continue

                    claimed = True
                    progress.set_postfix_str(mp3_path.name[:50])

                    # Check skip_existing after claiming — this way the
                    # file is "consumed" even if already in the DB, so
                    # other workers don't re-check it.
                    track_id = _track_id_from_path(claimed_path)

                    if track_id in failed_ids:
                        # Already failed in this run — unclaim and skip.
                        try:
                            claimed_path.rename(
                                input_dir / claimed_path.name
                            )
                        except OSError:
                            pass
                        continue

                    if self._config.skip_existing and _track_exists_in_db(
                        self._config.db_path, track_id
                    ):
                        # Already processed — delete claimed file.
                        claimed_path.unlink(missing_ok=True)
                        skipped += 1
                        progress.update(1)
                        break

                    # Run the full pipeline.
                    result = _process_track((claimed_path, self._config))

                    if result is None:
                        # Pipeline failed — unclaim and remember to skip.
                        failed_ids.add(track_id)
                        try:
                            claimed_path.rename(
                                input_dir / claimed_path.name
                            )
                        except OSError:
                            pass
                        failed += 1
                        progress.update(1)
                        break

                    # Success — remove claimed source file.
                    claimed_path.unlink(missing_ok=True)

                    # Flush QDrant immediately (preemptible safety).
                    audio_batch.append((
                        result["track_id"],
                        result["audio_vector"],
                        {
                            "artist": result["artist"],
                            "title": result["title"],
                            "status": "ready",
                        },
                    ))
                    lyric_batch.append((
                        result["track_id"],
                        result["lyric_vector"],
                        {
                            "artist": result["artist"],
                            "title": result["title"],
                            "status": "ready",
                        },
                    ))
                    self._flush_qdrant(audio_batch, lyric_batch)
                    audio_batch = []
                    lyric_batch = []

                    processed += 1
                    progress.update(1)
                    break

                if not claimed:
                    # No files left to claim — we're done.
                    break

        log.info(
            "bootstrap.local_gpu_finished",
            gpu_id=gpu_id,
            processed=processed,
            failed=failed,
            skipped=skipped,
        )

    def _run_remote(self) -> None:
        """Pull MP3s from the remote server, process locally, push results back.

        For each MP3 in the remote server's MP3 directory:
        1. Skip if already processed (check local DB, then remote DB).
        2. Pull the MP3 via SSH cat pipe.
        3. Run the full local pipeline (UVR → lyrics → features → embeddings).
        4. Push the instrumental stem to the remote output directory.
        5. Write the track record to the remote SQLite database.
        6. Flush accumulated QDrant vectors every 100 tracks.
        7. Delete the source MP3 from the remote server (only if push + insert
           both succeeded — preserving the invariant that a failed track can
           always be retried on the next run).
        8. Clean up all local staging files for this track.

        The SSH ControlMaster is established before the loop and torn down in
        a finally block so the connection is always closed cleanly.
        """
        from app.remote_syncer import RemoteSyncer  # noqa: PLC0415

        self._ensure_db_schema(self._config.db_path)

        work_dir = self._config.input_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        syncer = RemoteSyncer(
            host=self._config.remote_host,
            remote_mp3_dir=self._config.remote_mp3_dir,
            remote_output_dir=self._config.remote_output_dir,
            remote_db_path=self._config.remote_db_path,
        )
        syncer.start()

        try:
            syncer.ensure_remote_setup()

            filenames = syncer.list_remote_mp3s()
            if self._config.limit > 0:
                filenames = filenames[: self._config.limit]
            total = len(filenames)
            logger.info(
                "bootstrap.remote_starting",
                total=total,
                host=self._config.remote_host,
                remote_mp3_dir=self._config.remote_mp3_dir,
            )

            audio_batch: list[tuple[str, list[float], dict]] = []
            lyric_batch: list[tuple[str, list[float], dict]] = []
            processed = 0
            failed = 0
            skipped = 0

            with tqdm(total=total, desc="Remote tracks", unit="track") as progress:
                for filename in filenames:
                    progress.set_postfix_str(filename[:50])

                    # Compute the track_id from the filename alone. We
                    # construct a fake local path just to reuse the helper;
                    # only the filename (including extension) matters for the
                    # UUID derivation.
                    fake_path = work_dir / filename
                    track_id = _track_id_from_path(fake_path)

                    # --------------------------------------------------
                    # Step 1: Fast local skip + atomic claim
                    # --------------------------------------------------
                    if self._config.skip_existing and self._config.db_path.exists():
                        if _track_exists_in_db(self._config.db_path, track_id):
                            skipped += 1
                            progress.update(1)
                            continue

                    # Atomic claim: mv the MP3 into .processing/.
                    # If another worker already claimed it, skip.
                    if not syncer.claim_mp3(filename):
                        skipped += 1
                        progress.update(1)
                        continue

                    # --------------------------------------------------
                    # Step 2: Pull MP3 from .processing/ (claimed)
                    # --------------------------------------------------
                    local_mp3: Path | None = None
                    try:
                        local_mp3 = syncer.pull_mp3(
                            filename, work_dir, from_claim_dir=True,
                        )
                    except Exception:
                        logger.exception("bootstrap.pull_failed", filename=filename)
                        syncer.unclaim_mp3(filename)
                        failed += 1
                        progress.update(1)
                        continue

                    # --------------------------------------------------
                    # Step 3: Run the full local pipeline
                    # --------------------------------------------------
                    result = _process_track((local_mp3, self._config))

                    if result is None:
                        # Pipeline failed — clean up locally, unclaim so
                        # the file can be retried.
                        _unlink_if_exists(local_mp3)
                        syncer.unclaim_mp3(filename)
                        failed += 1
                        progress.update(1)
                        continue

                    instrumental_path = Path(result["_instrumental_path"])
                    vocals_path = Path(result["_vocals_path"]) if result["_vocals_path"] else None

                    # --------------------------------------------------
                    # Step 4: Push instrumental to the remote server
                    # --------------------------------------------------
                    remote_instrumental: str | None = None
                    try:
                        remote_instrumental = syncer.push_file(
                            instrumental_path,
                            self._config.remote_output_dir + "/instrumental",
                        )
                    except Exception:
                        logger.exception(
                            "bootstrap.push_failed", filename=filename
                        )
                        # Clean up locally, unclaim for retry.
                        _unlink_if_exists(local_mp3)
                        _unlink_if_exists(instrumental_path)
                        _unlink_if_exists(vocals_path)
                        syncer.unclaim_mp3(filename)
                        failed += 1
                        progress.update(1)
                        continue

                    # --------------------------------------------------
                    # Step 5: Insert track record into remote SQLite DB
                    # --------------------------------------------------
                    insert_ok = False
                    try:
                        track_data = _read_track_from_db(
                            self._config.db_path, result["track_id"]
                        )
                        # Overwrite paths to reflect the remote layout:
                        # mp3_path is gone (deleted after this), instrumental
                        # lives in the remote output directory.
                        track_data["mp3_path"] = None
                        # Store the container-visible path, not the host path.
                        track_data["instrumental_path"] = (
                            self._config.remote_container_media_prefix
                            + "/instrumental/"
                            + instrumental_path.name
                        )
                        syncer.insert_remote_track(track_data)
                        insert_ok = True
                    except Exception:
                        logger.exception(
                            "bootstrap.remote_insert_failed", filename=filename
                        )

                    if not insert_ok:
                        # Clean up locally, unclaim for retry.
                        _unlink_if_exists(local_mp3)
                        _unlink_if_exists(instrumental_path)
                        if vocals_path:
                            _unlink_if_exists(vocals_path)
                        syncer.unclaim_mp3(filename)
                        failed += 1
                        progress.update(1)
                        continue

                    # --------------------------------------------------
                    # Step 6: Accumulate QDrant vectors
                    # --------------------------------------------------
                    artist = result["artist"]
                    title = result["title"]
                    audio_batch.append((
                        result["track_id"],
                        result["audio_vector"],
                        {"artist": artist, "title": title, "status": "ready"},
                    ))
                    lyric_batch.append((
                        result["track_id"],
                        result["lyric_vector"],
                        {"artist": artist, "title": title, "status": "ready"},
                    ))

                    if len(audio_batch) >= _QDRANT_BATCH_SIZE:
                        self._flush_qdrant(audio_batch, lyric_batch)
                        audio_batch = []
                        lyric_batch = []

                    # --------------------------------------------------
                    # Step 7: Clean up the claimed MP3 on the remote server.
                    # Only reached if push (step 4) AND insert (step 5) both
                    # succeeded — this is the critical safety invariant.
                    # The file is in .processing/ (claimed), so either delete
                    # it or move it back to the main dir (dry-run mode).
                    # --------------------------------------------------
                    if self._config.delete_remote_source:
                        remote_mp3_path = (
                            self._config.remote_mp3_dir
                            + "/.processing/"
                            + filename
                        )
                        try:
                            syncer.delete_remote_file(remote_mp3_path)
                        except Exception:
                            # Non-fatal: the track is already fully processed.
                            logger.warning(
                                "bootstrap.delete_remote_failed",
                                filename=filename,
                                remote_path=remote_mp3_path,
                            )
                    else:
                        # Dry-run: move the file back from .processing/
                        # so it won't be orphaned.
                        syncer.unclaim_mp3(filename)

                    # --------------------------------------------------
                    # Step 8: Clean up all local staging files
                    # --------------------------------------------------
                    _unlink_if_exists(local_mp3)
                    _unlink_if_exists(instrumental_path)
                    if vocals_path:
                        _unlink_if_exists(vocals_path)

                    processed += 1
                    progress.update(1)

            # Flush any remaining vectors after the loop.
            if audio_batch:
                self._flush_qdrant(audio_batch, lyric_batch)

            logger.info(
                "bootstrap.remote_finished",
                total=total,
                processed=processed,
                failed=failed,
                skipped=skipped,
            )

        finally:
            syncer.stop()

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


# ------------------------------------------------------------------
# File cleanup helper
# ------------------------------------------------------------------


def _unlink_if_exists(path: Path | None) -> None:
    """Delete a file if it exists. Silently ignores None or missing files.

    Args:
        path: Path to delete, or None.
    """
    if path is not None and path.exists():
        path.unlink(missing_ok=True)
