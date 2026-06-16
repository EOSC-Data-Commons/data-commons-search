"""Unified logging for the app, uvicorn and libraries.

Two output formats, selected by `setup_logging(json_logs=...)`:

* dev: a single `CompactRichHandler` (dimmed timestamp, compact colored level,
  message, source `file:line` on the right).
* prod/staging: JSON Lines on stdout (one object per line) for ELK ingestion.

Logging is configured in code (see `main.py`) rather than via uvicorn's
`--log-config`, so the format is consistent no matter how the app is launched.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from rich.console import Console, ConsoleRenderable
from rich.logging import RichHandler
from rich.text import Text
from rich.theme import Theme

# ANSI constants kept for direct use in startup banner (see main.py).
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
BLUE = "\x1b[34m"
# GREY = "\x1b[90m"
# YELLOW = "\x1b[33m"

# Per-level abbreviation (<= 4 chars, so the level column stays tight) and style.
LEVEL_STYLES: dict[str, tuple[str, str]] = {
    "DEBUG": ("DBUG", "bold blue"),
    "INFO": ("INFO", "bold green"),
    "WARNING": ("WARN", "bold yellow"),
    "ERROR": ("ERR", "bold red"),
    "CRITICAL": ("CRIT", "bold magenta"),
}

TIME_FORMAT = "[%m/%d/%y %H:%M:%S]"

# Dim the timestamp column; the message keeps rich's default highlighting.
THEME = Theme({"log.time": "dim"})

# Standard LogRecord attributes plus framework-internal extras
_RESERVED_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName", "color_message"}

# Noisy third-party loggers kept at WARNING unless debugging.
_LIBRARY_LOGGERS = ("httpx", "mcp", "opensearch")

# Strip ANSI color codes (e.g. from the startup banner) out of JSON messages.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class CompactRichHandler(RichHandler):
    """RichHandler with a dimmed time and a tight, abbreviated level column."""

    def __init__(self, console: Console | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("show_time", True)
        kwargs.setdefault("show_level", True)
        kwargs.setdefault("show_path", True)
        kwargs.setdefault("markup", False)
        kwargs.setdefault("rich_tracebacks", True)
        kwargs.setdefault("log_time_format", TIME_FORMAT)
        super().__init__(console=console or Console(theme=THEME), **kwargs)
        # Shrink the level column so there is a single space after "INFO".
        self._log_render.level_width = 4

    def get_level_text(self, record: logging.LogRecord) -> Text:
        abbr, style = LEVEL_STYLES.get(record.levelname, (record.levelname[:4], "white"))
        return Text(abbr.ljust(4), style=style)

    def render_message(self, record: logging.LogRecord, message: str) -> ConsoleRenderable:
        # Parse raw ANSI codes (e.g. the startup banner's BOLD/BLUE/YELLOW) into
        # rich styles; RichHandler otherwise renders them as literal "\x1b[1m" text.
        if getattr(record, "markup", self.markup) or "\x1b" not in message:
            return super().render_message(record, message)
        return Text.from_ansi(message)


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON line for ELK ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds")
        data: dict[str, Any] = {
            "timestamp": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _ANSI_RE.sub("", record.getMessage()),
            "module": record.module,
            "func": record.funcName,
            # "line": record.lineno,
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            data["stack"] = self.formatStack(record.stack_info)
        # Include any custom fields passed via `logger.info(..., extra={...})`.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                data[key] = value
        return json.dumps(data, default=str, ensure_ascii=False)


def setup_logging(json_logs: bool = False, level: str = "INFO", debug: bool = False) -> None:
    """Configure the root, uvicorn and library loggers with a single handler.

    Args:
        json_logs: emit JSON Lines (prod/staging) instead of rich console output (dev).
        level: root/app log level.
        debug: when True, raise uvicorn-access and library loggers to INFO/DEBUG.
    """
    handler: logging.Handler
    if json_logs:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
    else:
        handler = CompactRichHandler()

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Route uvicorn through our handler instead of its own default handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False

    logging.getLogger("uvicorn.access").setLevel("INFO" if debug else "WARNING")
    for name in _LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel("DEBUG" if debug else "WARNING")
    logging.getLogger("data_commons_search").setLevel("DEBUG" if debug else level)


__all__ = ["BLUE", "BOLD", "RESET", "CompactRichHandler", "JsonFormatter", "setup_logging"]
