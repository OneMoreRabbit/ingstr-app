import logging
import sys

import structlog

from .config import LoggingConfig


def configure_logging(cfg: LoggingConfig) -> None:
    """Wire up structlog to emit either JSON or human-readable text to stdout.

    Call once at process start. File contents must never be logged at INFO level —
    only at DEBUG, and only when cfg.log_full_query is True.
    """
    level = getattr(logging, cfg.level)
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if cfg.format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
