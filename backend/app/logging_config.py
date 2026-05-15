"""Structlog configuration with JSON output.

Call `configure_logging()` once at application startup (inside the lifespan
function in main.py). After that, obtain loggers with:

    import structlog
    logger = structlog.get_logger(__name__)
"""

import logging
import sys

import structlog
import structlog.contextvars  # noqa: F401  — register submodule so .merge_contextvars resolves

from app.config import settings


def configure_logging() -> None:
    """Wire structlog to the standard-library logging system.

    Output is always JSON so that log collectors (Loki, Filebeat, etc.) can
    parse it without any extra configuration. The log level is driven by
    ``Settings.log_level`` so it can be overridden at runtime via the
    ``LOG_LEVEL`` environment variable.
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Configure the standard-library root logger so that third-party libraries
    # that use `logging.getLogger(...)` also get JSON output at the right level.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list[structlog.types.Processor] = [
        # Pull request_id (and any other contextvars set via
        # structlog.contextvars.bind_contextvars) into every event dict —
        # used to stitch backend↔worker logs by X-Request-ID.
        structlog.contextvars.merge_contextvars,
        # Add log level and logger name to every event dict.
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        # Add a UTC ISO-8601 timestamp.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Render exceptions as a structured dict rather than a raw traceback.
        structlog.processors.format_exc_info,
        # Make sure all values are JSON-serialisable.
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            # Allow filtering by level before any heavy processing.
            structlog.stdlib.filter_by_level,
            *shared_processors,
            # Hand off to the stdlib handler, which renders the final JSON line.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Attach the structlog JSON renderer to the root stdlib handler.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    root_handler = logging.root.handlers[0]
    root_handler.setFormatter(formatter)
