"""Structured logging setup — structlog + stdlib unified JSONL output."""

import logging
import logging.handlers
import sys
import structlog


def drop_color_message_key(_, __, event_dict):
    """Uvicorn duplicates the message in a color_message key. Drop it."""
    event_dict.pop("color_message", None)
    return event_dict


def setup_logging(
    json_logs: bool = True,
    log_level: str = "INFO",
    log_file: str | None = None,
):
    """Configure structlog as the unified formatter for all Python logging.

    Call at module level in main.py, BEFORE app = FastAPI().
    Every existing logging.getLogger(__name__) call automatically
    produces JSONL — zero call-site rewrites needed.
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.stdlib.ExtraAdder(),
        drop_color_message_key,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # Handler 1: stdout (for docker logs)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stdout_handler]

    # Handler 2: optional JSONL file with rotation
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_file,
            maxBytes=50 * 1024 * 1024,  # 50 MB
            backupCount=3,
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(log_level.upper())

    # Tame uvicorn: clear its handlers, let root handle them
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    # Suppress uvicorn access logs (middleware handles it)
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False
