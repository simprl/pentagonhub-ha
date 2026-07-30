"""Minimal Server-Sent Events parser."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """One parsed SSE product event."""

    event: str
    data: str
    event_id: str | None


class ServerSentEventParser:
    """Incrementally parse UTF-8 decoded SSE text chunks."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event_name = "message"
        self._event_id: str | None = None
        self._data_lines: list[str] = []

    def feed(self, chunk: str) -> list[ServerSentEvent]:
        """Consume a text chunk and return all complete events."""

        self._buffer += chunk
        events: list[ServerSentEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        return events

    def _consume_line(self, line: str) -> ServerSentEvent | None:
        if not line:
            if not self._data_lines:
                self._event_name = "message"
                return None
            event = ServerSentEvent(
                event=self._event_name,
                data="\n".join(self._data_lines),
                event_id=self._event_id,
            )
            self._event_name = "message"
            self._data_lines = []
            return event
        if line.startswith(":"):
            return None

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event_name = value
        elif field == "data":
            self._data_lines.append(value)
        elif field == "id" and "\x00" not in value:
            self._event_id = value
        return None
