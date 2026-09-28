"""Logging setup with request correlation.

The log format advertises ``request_id=...``, so that value has to actually be
populated. It is carried in a :mod:`contextvars` variable rather than passed
around, because a request fans out across providers, services and repositories:
threading the id through every signature would touch the whole codebase, and a
context variable is automatically correct per-task in asyncio.

The middleware in ``travel_api.main`` sets the id (from the ``x-request-id``
request header, or a fresh UUID) and echoes it back in the ``x-request-id``
response header, so a log line and a response can be matched up.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar, Token

#: The single source of truth for the log format. ``request_id`` is populated by
#: :class:`RequestIdFilter`, so the two must stay together.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"

#: The request id for the work currently being executed. ``"-"`` outside of a
#: request, e.g. during startup or in a CLI command.
_current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")


def set_request_id(request_id: str) -> Token:
    """Bind ``request_id`` to the current context and return a reset token."""
    return _current_request_id.set(request_id)


def get_request_id() -> str:
    """Return the request id bound to the current context."""
    return _current_request_id.get()


def reset_request_id(token: Token) -> None:
    """Restore the request id that was bound before ``token`` was created."""
    _current_request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every log record.

    An explicitly supplied ``extra={"request_id": ...}`` always wins, so
    background work can label itself (e.g. ``"system"``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = _current_request_id.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    # force=True matters: logging.basicConfig() is a silent no-op when the root
    # logger already has a handler (pytest installs one, and so does anything
    # else imported first). Without it the documented format — the one that
    # carries request_id — could be dropped without any error.
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stdout,
        format=LOG_FORMAT,
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdFilter())
