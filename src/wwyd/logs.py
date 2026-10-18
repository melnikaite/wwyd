# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging that never carries tool arguments, results or request bodies (SPEC P1).

Two layers:

* A global LogRecord factory sanitises every record at creation time, so the guarantee
  holds for any handler (stderr, pytest's caplog, a future error reporter):
  - records from loggers outside ``wwyd`` lose their format arguments (the SDK formats
    request params, tool names with values, etc. into them), and their DEBUG/INFO messages
    are replaced by a fixed text because some libraries build messages with f-strings;
  - exceptions are rendered as frames plus exception *type names* only: exception messages
    (e.g. pydantic or jsonschema validation errors) quote the offending input.
* :func:`configure_logging` sends everything to stderr; stdout is the MCP channel on stdio.

wwyd's own code logs only IDs, counts and fixed texts.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Mapping
from types import TracebackType

_OWN = "wwyd"
_installed = False


def _is_own(name: str) -> bool:
    return name == _OWN or name.startswith(_OWN + ".")


def safe_traceback(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> str:
    """Frames and exception type names, without messages or locals."""
    lines: list[str] = []
    exc: BaseException | None = exc_info[1]
    seen: set[int] = set()
    chain: list[BaseException] = []
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    for i, item in enumerate(reversed(chain)):
        if i:
            lines.append("The above exception led to the following exception:")
        lines.append("Traceback (most recent call last):")
        for frame in traceback.extract_tb(item.__traceback__):
            lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
        lines.append(f"{type(item).__module__}.{type(item).__qualname__}: <message redacted>")
    return "\n".join(lines)


def sanitize_record(record: logging.LogRecord) -> logging.LogRecord:
    if record.exc_info and record.exc_info[1] is not None:
        record.exc_text = safe_traceback(record.exc_info)  # type: ignore[arg-type]
        record.exc_info = None
    record.stack_info = None
    if not _is_own(record.name):
        if record.levelno < logging.WARNING:
            record.msg = "(library debug/info message suppressed)"
        elif record.args:
            record.msg = f"{record.msg} [arguments redacted]"
        record.args = {} if isinstance(record.args, Mapping) else ()
    return record


def install_privacy_filter() -> None:
    """Install the sanitising LogRecord factory (idempotent)."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs) -> logging.LogRecord:  # type: ignore[no-untyped-def]
        return sanitize_record(previous(*args, **kwargs))

    logging.setLogRecordFactory(factory)
    _installed = True


def configure_logging(level: str | None = None) -> None:
    """Log to stderr at ``level`` (default ``WWYD_LOG_LEVEL`` or INFO)."""
    install_privacy_filter()
    level = (level or os.environ.get("WWYD_LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_wwyd", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._wwyd = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
