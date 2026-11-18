"""A record of every call made to an outside service, written by code.

One JSON object per line. It holds what was asked and an outline of the answer:
ids and counts, never the content of a message or an entry. It is opened for
appending only; this class has no way to rewrite or delete a line.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable


class AccessLog:
    def __init__(self, path: Path | str | None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self.clock = clock
        self.entries: list[dict[str, Any]] = []          # kept in memory too, for tests and for a run's summary
        self.mirror: Callable[[dict[str, Any]], None] | None = None   # told of every entry, for a journal that holds everything

    def record(self, service: str, call: str, **outline: Any) -> None:
        entry = {"at": round(self.clock(), 3), "service": service, "call": call, **outline}
        if self.path:                                    # first: a line that cannot be written is said nowhere else
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        self.entries.append(entry)
        if self.mirror is not None:
            self.mirror(entry)
