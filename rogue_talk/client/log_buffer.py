"""In-memory log buffer for TUI display."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass
class LogEntry:
    """A single log entry."""

    timestamp: datetime
    level: str
    name: str
    message: str


class LogBuffer(logging.Handler):
    """Logging handler that stores log entries in a circular buffer.

    This allows logs to be displayed in the TUI without writing to stderr.
    """

    def __init__(self, maxlen: int = 100) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        """Store a log record in the buffer."""
        entry = LogEntry(
            timestamp=datetime.fromtimestamp(record.created),
            level=record.levelname,
            name=record.name,
            message=record.getMessage(),
        )
        self._entries.append(entry)

    def get_entries(self, count: int | None = None) -> list[LogEntry]:
        """Get the most recent log entries.

        Args:
            count: Maximum number of entries to return. None for all.

        Returns:
            List of LogEntry objects, oldest first.
        """
        if count is None:
            return list(self._entries)
        return list(self._entries)[-count:]

    def clear(self) -> None:
        """Clear all log entries."""
        self._entries.clear()
