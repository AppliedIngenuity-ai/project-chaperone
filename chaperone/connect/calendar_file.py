"""A calendar this system owns: one .ics file on this machine.

What this client can do, in full: create an entry, or change an entry it
created earlier. It reads no other calendar and has no way to delete, invite,
or touch an entry it did not make. Three files sit side by side:

    NAME.ics             the calendar, for a calendar program to open or subscribe to
    NAME.entries.json    the same entries as plain data
    NAME.history.jsonl   every write in the order it happened, one per line, never rewritten The file can be subscribed to or imported
by any calendar program; nothing here connects to a calendar service.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chaperone.connect.access_log import AccessLog


class FileCalendar:
    def __init__(self, path: Path | str, *, log: AccessLog | None = None, name: str = "Calendar", system: str = "file") -> None:
        from chaperone.connect.google_calendar import new_batch
        self.system = system                                      # which run of the system this is, for cleanup by mark
        self.batch = new_batch()                                  # which pass; the command sets a new one per pass
        self.path = Path(path)
        self._state = self.path.with_suffix(".entries.json")     # this system's own entries, and nothing else
        self.history = self.path.with_suffix(".history.jsonl")   # every write, in order, one per line; only ever added to
        self.log = log or AccessLog(None)
        self.name = name

    def entries(self) -> dict[str, dict[str, Any]]:
        """The entries this system created, by key."""
        if not self._state.exists():
            return {}
        return json.loads(self._state.read_text(encoding="utf-8"))

    def upsert(self, key: str, *, title: str, start: str, end: str, description: str = "") -> str:
        """Create the entry for `key`, or update the one made earlier for the same key. Returns "created" or "updated"."""
        for label, value in (("key", key), ("title", title), ("start", start), ("end", end), ("description", description)):
            if not isinstance(value, str):
                raise TypeError(f"{label} must be text")
        begins, ends = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if not begins < ends:
            raise ValueError("an entry must end after it starts")
        entries = self.entries()
        outcome = "updated" if key in entries else "created"
        created_in = entries[key]["batch"] if key in entries and entries[key].get("batch") else self.batch
        entries[key] = {"title": title, "start": start, "end": end, "description": description, "batch": created_in, "last_batch": self.batch}
        self._write(entries)
        with self.history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "what": outcome, "key": key,
                                 **entries[key]}, sort_keys=True) + "\n")
        self.log.record("calendar", "upsert", key=key, outcome=outcome)
        return outcome

    def remove(self, key: str) -> str:
        """Remove the entry made for `key`. Returns "removed", or "absent" when there is none."""
        entries = self.entries()
        if key not in entries:
            return "absent"
        gone = entries.pop(key)
        self._write(entries)
        with self.history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "what": "removed", "key": key,
                                 **gone}, sort_keys=True) + "\n")
        self.log.record("calendar", "remove", key=key, outcome="removed")
        return "removed"

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//chaperone//calendar file//EN", f"X-WR-CALNAME:{_esc(self.name)}"]
        for key in sorted(entries):
            e = entries[key]
            lines += ["BEGIN:VEVENT", f"UID:{_esc(key)}@chaperone.local", f"DTSTAMP:{stamp}",
                      f"DTSTART:{_when(e['start'])}", f"DTEND:{_when(e['end'])}",
                      f"SUMMARY:{_esc(e['title'])}", f"DESCRIPTION:{_esc(e['description'])}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        for target, text in ((self._state, json.dumps(entries, indent=1, sort_keys=True)), (self.path, "\r\n".join(lines) + "\r\n")):
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)                                # whole file or nothing


def _when(iso: str) -> str:
    """Local wall-clock time, as the notice gave it (no time zone is claimed)."""
    return datetime.fromisoformat(iso).strftime("%Y%m%dT%H%M%S")


def _esc(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "\ufffd", text.replace("\r\n", "\n").replace("\r", "\n"))   # marked, never dropped
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
