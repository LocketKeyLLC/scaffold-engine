"""Structured logging setup — structlog as the single app-wide formatter.

Stack choice (documented):
    - structlog is the sole formatter. All stdlib ``logging.getLogger(...)``
      records are routed through ``structlog.stdlib.ProcessorFormatter``
      via ``foreign_pre_chain``, so library code that uses stdlib logging
      produces identical JSONL output without any call-site changes.
    - ``structlog.contextvars.merge_contextvars`` is in the shared chain,
      so fields bound via ``structlog.contextvars.bind_contextvars`` (e.g.
      ``request_id`` set by RequestIdMiddleware) appear on every record
      regardless of which logger emitted it.

Idempotency:
    - ``configure_logging_once`` is fixture-safe. pytest will import and
      re-import modules across a session; calling ``setup_logging`` more
      than once was previously appending handlers and re-wrapping
      structlog, which could surface as duplicate log lines or stale
      cached loggers. The guard short-circuits repeat invocations.
"""
import logging
import logging.handlers
import sys

import structlog

_CONFIGURED = False


def drop_color_message_key(_, __, event_dict):
    """Uvicorn duplicates the message in a color_message key. Drop it."""
    event_dict.pop("color_message", None)
    return event_dict


def _resolve_level(log_level: str | int) -> int:
    """Validate log_level via stdlib; fall back to INFO on garbage input."""
    if isinstance(log_level, int):
        return log_level
    name = (log_level or "INFO").upper().strip()
    # getLevelName returns the level int for a known name, else the string
    # "Level <name>". Guard by round-tripping: only accept names that map
    # to an int in the standard set.
    level = logging.getLevelName(name)
    if isinstance(level, int):
        return level
    # Unknown name — emit a warning via root and fall back.
    logging.getLogger(__name__).warning(
        "invalid_log_level: got=%r falling_back_to=INFO", log_level,
    )
    return logging.INFO


def setup_logging(
    json_logs: bool = True,
    log_level: str = "INFO",
    log_file: str | None = None,
):
    """Configure structlog as the unified formatter for all Python logging.

    Call at module level in main.py, BEFORE ``app = FastAPI()``.
    """
    configure_logging_once(
        json_logs=json_logs, log_level=log_level, log_file=log_file,
    )


def configure_logging_once(
    json_logs: bool = True,
    log_level: str = "INFO",
    log_file: str | None = None,
):
    """Fixture-safe idempotent variant — no-op on repeat calls."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = _resolve_level(log_level)

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

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stdout_handler]

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
    root.setLevel(level)

    # Tame uvicorn: clear its handlers, let root handle them
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False

    _CONFIGURED = True
