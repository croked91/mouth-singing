"""Bootstrap CLI entry point.

Provides a single ``bootstrap`` command that mass-processes a directory of MP3
files into karaoke tracks.

**Local mode** (default)::

    karaoke-bootstrap /path/to/mp3s --lrclib-url http://localhost:9876 --workers 4

**Remote mode** (pull MP3s from a server, process locally with GPU, push back)::

    karaoke-bootstrap /tmp/work \\
        --remote-host root@130.49.170.186 \\
        --remote-mp3-dir /root/mp3_library \\
        --remote-output-dir /root/bootstrap_output \\
        --remote-db-path /root/bootstrap_output/karaoke.db \\
        --device cuda

Each track goes through: UVR separation → lyrics retrieval → syllabification →
WhisperX alignment → feature extraction → SQLite + QDrant persistence.

Run ``karaoke-bootstrap --help`` for all options.
"""

from __future__ import annotations

import multiprocessing
import os
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
        help=(
            "Local MP3 directory (local mode) or local working directory where "
            "remote MP3s are staged during processing (remote mode). Created "
            "automatically in remote mode if it does not exist."
        ),
        file_okay=False,
        dir_okay=True,
    ),
    output_dir: Path = typer.Option(
        "./data",
        help="Root output directory for processed audio and the local database.",
    ),
    workers: int = typer.Option(
        0,
        help=(
            "Number of parallel worker processes (local mode only). "
            "0 = CPU count minus 1. Ignored in remote mode."
        ),
        min=0,
    ),
    lrclib_dump: Path | None = typer.Option(
        None,
        help="Path to an lrc-lib JSON-lines dump file for lyrics lookup.",
    ),
    lrclib_sqlite: Path | None = typer.Option(
        None,
        help="Path to an lrclib SQLite database for lyrics lookup.",
    ),
    lrclib_url: str | None = typer.Option(
        None,
        help="URL of a remote lrclib HTTP server for lyrics lookup.",
    ),
    language: str = typer.Option(
        "ru",
        help="Language code for WhisperX transcription (e.g. 'ru', 'en').",
    ),
    db_path: Path = typer.Option(
        "./data/karaoke.db",
        help="Path to the local SQLite database file.",
    ),
    qdrant_host: str = typer.Option(
        "localhost",
        help="QDrant server hostname.",
    ),
    qdrant_port: int = typer.Option(
        6333,
        help="QDrant server port.",
    ),
    device: str = typer.Option(
        "cpu",
        help="PyTorch device for WhisperX ('cpu' or 'cuda').",
    ),
    whisper_model: str = typer.Option(
        "medium",
        help="Whisper model size ('tiny', 'base', 'small', 'medium', 'large-v3').",
    ),
    skip_existing: bool = typer.Option(
        True,
        help="Skip tracks whose ID is already present in the database.",
    ),
    limit: int = typer.Option(
        0,
        help="Process at most N tracks (0 = no limit). Useful for test runs.",
        min=0,
    ),
    # Remote mode options — all optional.
    remote_host: str | None = typer.Option(
        None,
        help=(
            "SSH host for remote mode, e.g. 'root@130.49.170.186'. "
            "When set, MP3s are pulled from the server, processed locally, "
            "and results are pushed back. Workers is forced to 1."
        ),
    ),
    remote_mp3_dir: str = typer.Option(
        "/root/mp3_library",
        help="Directory on the remote server containing source MP3 files.",
    ),
    remote_output_dir: str = typer.Option(
        "/root/bootstrap_output",
        help="Directory on the remote server where processed files are written.",
    ),
    remote_db_path: str = typer.Option(
        "/root/bootstrap_output/karaoke.db",
        help="Path to the SQLite database on the remote server.",
    ),
    uvr_model: str = typer.Option(
        "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        help="UVR model name for vocal separation (audio-separator model identifier).",
    ),
    no_delete_remote_source: bool = typer.Option(
        False,
        "--no-delete-remote-source",
        help=(
            "Do not delete the source MP3 from the remote server after "
            "successful processing. Useful for dry-run testing."
        ),
    ),
) -> None:
    """Mass-process MP3 files into karaoke tracks with lyrics and features.

    **Local mode** (default): processes all ``*.mp3`` files found in INPUT_DIR
    using a parallel worker pool.

    **Remote mode** (``--remote-host``): pulls MP3s from the remote server one
    at a time, processes them locally (GPU recommended), pushes the
    instrumental back, writes to the remote database, and deletes the source.

    For each track the pipeline runs:
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
    lrc_opts = sum(x is not None for x in (lrclib_dump, lrclib_sqlite, lrclib_url))
    if lrc_opts > 1:
        typer.echo(
            "ERROR: --lrclib-dump, --lrclib-sqlite, and --lrclib-url are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=1)

    has_lrc_source = lrc_opts > 0

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

    # Validate and prepare input_dir differently depending on the mode.
    if remote_host is not None:
        # Remote mode: input_dir is a local staging area, not a source of MP3s.
        # Create it if it does not exist.
        if workers > 1:
            typer.echo(
                f"WARNING: --workers={workers} is ignored in remote mode. "
                "Processing is always sequential (1 track at a time).",
                err=True,
            )
        effective_workers = 1
        input_dir.mkdir(parents=True, exist_ok=True)
    else:
        # Local mode: input_dir must already exist and be a readable directory.
        if not input_dir.exists():
            typer.echo(
                f"ERROR: Input directory does not exist: {input_dir}",
                err=True,
            )
            raise typer.Exit(code=1)
        if not input_dir.is_dir():
            typer.echo(
                f"ERROR: Input path is not a directory: {input_dir}",
                err=True,
            )
            raise typer.Exit(code=1)
        effective_workers = _resolve_worker_count(workers)

    # Ensure output directories exist before workers start writing.
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "cli.bootstrap_starting",
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        workers=effective_workers,
        language=language,
        lrclib_dump=str(lrclib_dump) if lrclib_dump else None,
        lrclib_sqlite=str(lrclib_sqlite) if lrclib_sqlite else None,
        lrclib_url=lrclib_url,
        device=device,
        whisper_model=whisper_model,
        db_path=str(db_path),
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        skip_existing=skip_existing,
        has_whisperx=HAS_WHISPERX,
        remote_host=remote_host,
        remote_mp3_dir=remote_mp3_dir if remote_host else None,
        remote_output_dir=remote_output_dir if remote_host else None,
        remote_db_path=remote_db_path if remote_host else None,
        delete_remote_source=not no_delete_remote_source if remote_host else None,
        uvr_model=uvr_model,
    )

    config = BootstrapConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        workers=effective_workers,
        lrclib_dump_path=lrclib_dump,
        lrclib_sqlite_path=lrclib_sqlite,
        lrclib_url=lrclib_url,
        language=language,
        device=device,
        whisper_model=whisper_model,
        db_path=db_path,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        skip_existing=skip_existing,
        limit=limit,
        remote_host=remote_host,
        remote_mp3_dir=remote_mp3_dir,
        remote_output_dir=remote_output_dir,
        remote_db_path=remote_db_path,
        delete_remote_source=not no_delete_remote_source,
        uvr_model=uvr_model,
    )

    runner = BootstrapRunner(config)
    runner.run()


if __name__ == "__main__":
    # Required on Windows and some macOS configurations so that spawned
    # worker processes do not recursively invoke the CLI.
    multiprocessing.freeze_support()
    app()
