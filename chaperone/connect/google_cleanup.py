"""Remove every entry this system wrote, and nothing else. A purpose-built tool the owner runs by hand.

This is not a module: no workflow can call it, no model is involved, and it is not registered with the framework.
It exists so the owner can recover if a run goes wrong. What it deletes is decided by exact id, from this
system's own record of what it created (the entries file, and the history file behind it), and each entry is
fetched and checked for this system's own mark before it is removed. The mark is a private property on the
event, `chaperone_calendar=<system name>` and `key=<the entry's key>`, that the client writes on every entry it
creates; nothing else writes it. An entry without this system's mark for this key is left alone and reported,
so another system's entries on the same calendar, or anything a person put there, are never touched.

Works for both calendars: the file calendar (entries are dropped from the file) and the Google calendar
(each event is deleted by id on the secondary calendar this system created; the calendar itself stays).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from chaperone.connect.access_log import AccessLog
from chaperone.connect.calendar_file import FileCalendar
from chaperone.connect.google_calendar import BATCH, MARK, CalendarUnavailable, EntryGone, GoogleCalendar, _fetch, _q, google_call, parse_mark


def planned(calendar: FileCalendar | GoogleCalendar, *, include_history: bool = True, marked: str | None = None) -> list[dict[str, Any]]:
    """What a cleanup would remove, from this run's own records: one record per key, from the entries file, plus any
    key the history file names that the entries file no longer has (a reset in between, for example). With `marked`
    (SYSTEM or SYSTEM/BATCH, as printed on an entry) only records carrying that mark. Nothing is contacted."""
    want_system, want_batch = parse_mark(marked) if marked else (None, None)
    found: dict[str, dict[str, Any]] = {}
    for key, e in calendar.entries().items():
        found[key] = {"key": key, "title": e.get("title", ""), "start": e.get("start", ""), "event_id": e.get("event_id"),
                      "calendar_id": e.get("calendar_id"), "batch": e.get("batch"), "from": "entries"}
    if include_history and Path(calendar.history).exists():
        for number, line in enumerate(Path(calendar.history).read_text(encoding="utf-8").splitlines(), 1):
            try:
                rec = json.loads(line)
            except ValueError:
                print(f"{calendar.history}, line {number} cannot be read: an entry named only there stays on the calendar; "
                      "`cleanup --marked` finds it")
                continue
            if rec.get("what") in ("created", "updated") and rec.get("key") in found and not found[rec["key"]]["calendar_id"] \
                    and found[rec["key"]]["from"] == "entries" and rec.get("event_id") == found[rec["key"]]["event_id"]:
                found[rec["key"]]["calendar_id"] = rec.get("calendar_id")     # the calendar it was written on (review)
            if rec.get("what") in ("created", "updated") and rec.get("key") and rec["key"] not in found:
                found[rec["key"]] = {"key": rec["key"], "title": rec.get("title", ""), "start": rec.get("start", ""),
                                     "event_id": rec.get("event_id"), "calendar_id": rec.get("calendar_id"), "batch": rec.get("batch"),
                                     "from": "history"}
            elif rec.get("what") == "deleted" and rec.get("key") in found and found[rec["key"]]["from"] == "history":
                del found[rec["key"]]                         # already removed by an earlier cleanup
    if want_system:
        found = {k: v for k, v in found.items()
                 if getattr(calendar, "system", None) == want_system and (want_batch is None or v.get("batch") == want_batch)}
    return [found[k] for k in sorted(found)]


def marked_on_google(calendar: GoogleCalendar, marked: str, *, fetch: Callable[[urllib.request.Request], bytes] | None = None) -> list[dict[str, Any]]:
    """Every entry on the calendar this system created that carries the given mark (SYSTEM or SYSTEM/BATCH, as
    printed at the end of an entry's description), asked of Google directly. For the case where the records are
    gone, or a person found an errant entry and read the mark off it. This looks at that one calendar, for that one
    mark; another system's entries there are not returned, and the owner's calendars are outside the permission the
    sign-in asked for."""
    want_system, want_batch = parse_mark(marked)
    if not Path(calendar.path).exists():
        return []                                                 # no calendar of ours on record: nothing to look at
    fetch = fetch or getattr(calendar, "_fetch", None) or _fetch  # the calendar's own connection (a test's fake, say)
    cal_id = calendar.calendar_id()
    found: list[dict[str, Any]] = []
    page = None
    while True:
        query = [("privateExtendedProperty", f"{MARK}={want_system}"), ("maxResults", "250"), ("showDeleted", "false")]
        if want_batch:
            query.append(("privateExtendedProperty", f"{BATCH}={want_batch}"))
        if page:
            query.append(("pageToken", page))
        answer = google_call(fetch, calendar.auth, "GET", f"/calendars/{_q(cal_id)}/events?{urllib.parse.urlencode(query)}")
        for event in answer.get("items") or []:
            private = ((event.get("extendedProperties") or {}).get("private") or {})
            if private.get(MARK) == want_system and (want_batch is None or private.get(BATCH) == want_batch) \
                    and isinstance(event.get("id"), str) and private.get("key"):
                found.append({"key": private["key"], "title": event.get("summary", ""), "start": (event.get("start") or {}).get("dateTime", ""),
                              "event_id": event["id"], "calendar_id": cal_id, "batch": private.get(BATCH), "mark": want_system, "from": "google"})
        page = answer.get("nextPageToken")
        if not page:
            return sorted(found, key=lambda i: i["key"])


def remove_all(calendar: FileCalendar | GoogleCalendar, *, include_history: bool = True, marked: str | None = None,
               log: AccessLog | None = None, fetch: Callable[[urllib.request.Request], bytes] | None = None,
               say: Callable[[str], None] = print) -> dict[str, list[str]]:
    """Remove every entry this run's records name; or, with `marked`, every entry carrying that mark (on Google, asked
    of Google for the one calendar this system created; for the file, from the records). Each is still checked for
    the mark before it goes. Returns {"removed": keys, "kept": keys with why}."""
    log = log or getattr(calendar, "log", None) or AccessLog(None)
    want_system = parse_mark(marked)[0] if marked else calendar.system
    want_batch = parse_mark(marked)[1] if marked else None
    removed: list[str] = []
    kept: list[str] = []
    if isinstance(calendar, GoogleCalendar):
        fetch = fetch or getattr(calendar, "_fetch", None) or _fetch
        plan = marked_on_google(calendar, marked, fetch=fetch) if marked else planned(calendar, include_history=include_history)
        default_calendar = calendar.calendar_id() if Path(calendar.path).exists() else None
        for item in plan:
            cal_id = item["calendar_id"] or default_calendar
            guessed = not item["calendar_id"]                     # no record of which calendar it was written on
            if not item["event_id"] or not cal_id:
                kept.append(f"{item['key']}: no event id or calendar id on record")
                continue
            where = f"/calendars/{_q(cal_id)}/events/{_q(item['event_id'])}"
            try:
                event = google_call(fetch, calendar.auth, "GET", where)
                guessed = False                                   # it is on this calendar, deleted or not
                if event.get("status") == "cancelled":            # deleted on Google: whatever marks it still shows
                    raise EntryGone("the entry is cancelled on Google")
                private = ((event.get("extendedProperties") or {}).get("private") or {})
                if private.get(MARK) != want_system or private.get("key") != item["key"] or (want_batch and private.get(BATCH) != want_batch):
                    kept.append(f"{item['key']}: the entry on Google does not carry the mark '{marked or want_system}' for this key; left alone")
                    continue
                google_call(fetch, calendar.auth, "DELETE", where)
            except EntryGone:
                if guessed:
                    # not on the calendar it was guessed to be on: it may be on another one this system made (after
                    # a change of calendar_name), so it is not counted as removed and its record is kept
                    kept.append(f"{item['key']}: not found on the calendar '{calendar.name}'; it may be on another "
                                "calendar this system made")
                    continue
                _forget(calendar, item["key"], cal_id, "gone already")
                removed.append(item["key"])
                continue
            except CalendarUnavailable as exc:
                kept.append(f"{item['key']}: {exc}")
                continue
            _forget(calendar, item["key"], cal_id, "deleted")
            log.record("calendar", "delete", key=item["key"], outcome="deleted")
            removed.append(item["key"])
    else:
        plan = planned(calendar, include_history=include_history, marked=marked)
        entries = calendar.entries()
        for item in plan:
            if item["key"] in entries:
                del entries[item["key"]]
            _forget(calendar, item["key"], None, "deleted")
            log.record("calendar", "delete", key=item["key"], outcome="deleted")
            removed.append(item["key"])
        calendar._write(entries)                              # the file holds what remains: nothing of ours
    for key in removed:
        say(f"  removed {key}")
    for why in kept:
        say(f"  kept    {why}")
    return {"removed": removed, "kept": kept}


def _forget(calendar: FileCalendar | GoogleCalendar, key: str, calendar_id: str | None, what: str) -> None:
    """Drop the key from the entries file and append the removal to the history, so a second cleanup skips it."""
    entries = calendar.entries()
    if key in entries:
        del entries[key]
        if isinstance(calendar, GoogleCalendar):
            calendar._write(entries)
    Path(calendar.history).parent.mkdir(parents=True, exist_ok=True)   # the folder may be gone
    with Path(calendar.history).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "what": "deleted", "key": key,
                             "calendar_id": calendar_id, "note": what}, sort_keys=True) + "\n")
