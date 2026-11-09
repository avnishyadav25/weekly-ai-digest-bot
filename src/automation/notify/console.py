"""Console notifier: prints the message to standard output. Always available; the default channel."""

from __future__ import annotations

import sys
from typing import TextIO

from automation.notify.base import Message, Notifier

_MARK = {"info": "INFO", "warning": "WARNING", "error": "ALERT"}


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def configured(self) -> tuple[bool, str]:
        return True, "prints to standard output"

    def render(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> str:
        lines = [f"[{_MARK[message.level]}] {message.title}", message.text.rstrip()]
        lines += [f"{key}: {value}" for key, value in message.fields.items()]
        if message.link:
            lines.append(message.link)
        source = " · ".join(p for p in (job, f"run {run_id}" if run_id else None) if p)
        if source:
            lines.append(f"-- {source}")
        return "\n".join(line for line in lines if line) + "\n"

    def send(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> None:
        stream = self._stream or sys.stdout
        stream.write(self.render(message, job=job, run_id=run_id))
        stream.flush()
