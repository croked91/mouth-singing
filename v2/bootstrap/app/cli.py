"""Bootstrap CLI entry point.

Provides a single ``bootstrap`` command that mass-processes a directory of MP3
files into karaoke tracks:

    karaoke-bootstrap /path/to/mp3s --lrclib-dump lrclib.json --workers 4

Each track goes through: UVR separation → lyrics retrieval → syllabification →
WhisperX alignment → feature extraction → SQLite + QDrant persistence.

Run ``karaoke-bootstrap --help`` for all options.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path

import structlog
import typer

from app.bootstrap_runner import BootstrapConfig, BootstrapRunner
from app.pipeline.whisperx_transcriber import HAS_WHISPERX

logger = structlog.get_logger(__name__)

app = typer.Typer(
    name="karaoke-bootstrap",
    help="Mass-process MP3 files into karaoke tracks with lyrics, video, and search vectors.",
    add_completion=False,
)


def _configure_logging() -> None:
    """Set up structlog with a human-readable console renderer."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def _resolve_worker_count(requested: int) -> int:
    """Resolve the effective worker count.

    Args:
        requested: Value from ``--workers`` CLI option. 0 means auto.

    Returns:
        Number of worker processes to use. Minimum 1.
    """
    if requested > 0:
        return requested
    cpu_count = os.cpu_count() or 1
    return max(1, cpu_count - 1)


@app.command()
def bootstrap(
    input_dir: Path = typer.Argument(
        ...,
        help="Directory containing MP3 files to process.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
    ),
    output_dir: Path = typer.Option(
        "./data",
        help="Root output directory for processed audio, video, and the database.",
    ),
    workers: int = typer.Option(
        0,
        help="Number of parallel worker processes. 0 = CPU count minus 1.",
        min=0,
    ),
    lrclib_dump: Path | None = typer.Option(
        None,
        help="Path to an lrc-lib JSON-lines dump file for lyrics lookup.",
    ),
    lrclib_sqlite: Path | None = typer.Option(
        None,
        help="Path to an lrclib SQLite database for lyrics lookup (alternative to --lrclib-dump).",
    ),
    language: str = typer.Option(
        "ru",
        help="Language code for WhisperX transcription (e.g. 'ru', 'en').",
    ),
    db_path: Path = typer.Option(
        "./data/karaoke.db",
        help="Path to the SQLite database file.",
    ),
    qdrant_host: str = typer.Option(
        "localhost",
        help="QDrant server hostname.",
    ),
    qdrant_port: int = typer.Option(
        6333,
        help="QDrant server port.",
    ),
    skip_existing: bool = typer.Option(
        True,
        help="Skip tracks whose ID is already present in the database.",
    ),
) -> None:
    """Mass-process MP3 files into karaoke tracks with lyrics and features.

    For each MP3 found in INPUT_DIR the pipeline runs:
    \b
      1. UVR separation (vocals / instrumental)
      2. Lyrics: LRC lookup → syllabify → WhisperX force-align, or full ASR
      3. Audio feature extraction (45-d vector)
      4. Lyric embedding (384-d vector)
      5. Persist to SQLite and QDrant

    Errors on individual tracks are logged but do not stop the run.
    """
    _configure_logging()

    # Validate mutually exclusive LRC options.
    if lrclib_dump is not None and lrclib_sqlite is not None:
        typer.echo(
            "ERROR: --lrclib-dump and --lrclib-sqlite are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=1)

    has_lrc_source = lrclib_dump is not None or lrclib_sqlite is not None

    # Warn if WhisperX is not available — the run will fail at the first
    # track that needs transcription, so it is better to surface this early.
    if not HAS_WHISPERX:
        typer.echo(
            "WARNING: WhisperX is not installed. Transcription will fail for any track "
            "that does not have lyrics in the LRC source.\n"
            "Install it with: pip install karaoke-bootstrap[whisperx]",
            err=True,
        )
        if not has_lrc_source:
            typer.echo(
                "ERROR: WhisperX is not installed and no LRC source was provided. "
                "Cannot transcribe any tracks. Aborting.",
                err=True,
            )
            raise typer.Exit(code=1)

    if lrclib_dump is not None and not lrclib_dump.exists():
        typer.echo(
            f"ERROR: --lrclib-dump path does not exist: {lrclib_dump}",
            err=True,
        )
        raise typer.Exit(code=1)

    if lrclib_sqlite is not None and not lrclib_sqlite.exists():
        typer.echo(
            f"ERROR: --lrclib-sqlite path does not exist: {lrclib_sqlite}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Ensure output directories exist before workers start writing.
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    effective_workers = _resolve_worker_count(workers)

    logger.info(
        "cli.bootstrap_starting",
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        workers=effective_workers,
        language=language,
        lrclib_dump=str(lrclib_dump) if lrclib_dump else None,
        lrclib_sqlite=str(lrclib_sqlite) if lrclib_sqlite else None,
        db_path=str(db_path),
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        skip_existing=skip_existing,
        has_whisperx=HAS_WHISPERX,
    )

    config = BootstrapConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        workers=effective_workers,
        lrclib_dump_path=lrclib_dump,
        lrclib_sqlite_path=lrclib_sqlite,
        language=language,
        db_path=db_path,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        skip_existing=skip_existing,
    )

    runner = BootstrapRunner(config)
    runner.run()


if __name__ == "__main__":
    # Required on Windows and some macOS configurations so that spawned
    # worker processes do not recursively invoke the CLI.
    multiprocessing.freeze_support()
    app()
