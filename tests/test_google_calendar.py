"""Google Calendar through a stand-in for Google: the sign-in, the narrow client, the cleanup tool, the routed outage.

No network. `FakeGoogle` answers the handful of requests the client and the cleanup make, records every one, and
can be told to fail. The client never lists calendars or events, never deletes; the cleanup deletes by exact id
after checking the mark, and nothing else.
"""

from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chaperone.connect.access_log import AccessLog
from chaperone.connect.google_calendar import (BATCH, MARK, SCOPE_APP_CREATED, CalendarUnavailable, GoogleAuth,
                                              GoogleCalendar, mark_line, parse_mark, sign_in)
from chaperone.connect import google_cleanup

HINT = "python -m some_project cleanup --marked"         # the project states its cleanup command; the client writes it


class FakeGoogle:
    """Just enough of Google's token service and Calendar API for these tests, as a `fetch(request) -> bytes`."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.calendars: dict[str, dict] = {}
        self.events: dict[tuple[str, str], dict] = {}
        self.refresh_tokens = {"rt-1"}
        self.codes = {"code-1"}
        self.fail_with: int | None = None
        self.next_id = 100
        self.deleted = {}                    # what DELETE took away: Google keeps the id

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        method = req.get_method()
        body = json.loads(req.data) if req.data and req.get_header("Content-type") == "application/json" else None
        form = urllib.parse.parse_qs(req.data.decode()) if req.data and req.get_header("Content-type", "").startswith("application/x-www") else None
        self.calls.append((method, url.path + ("?" + url.query if url.query else ""), body or form))
        if url.path == "/token":
            if form.get("grant_type") == ["authorization_code"]:
                assert form["code"][0] in self.codes and form.get("code_verifier"), "a code exchange carries the verifier"
                return json.dumps({"access_token": "at-fresh", "refresh_token": "rt-1", "expires_in": 3600, "scope": SCOPE_APP_CREATED}).encode()
            assert form["refresh_token"][0] in self.refresh_tokens
            return json.dumps({"access_token": "at-2", "expires_in": 3600}).encode()
        assert req.get_header("Authorization", "").startswith("Bearer at-"), "every API call carries the access token"
        if self.fail_with:
            raise urllib.error.HTTPError(req.full_url, self.fail_with, "boom", {}, io.BytesIO(b"{}"))
        parts = url.path.split("/")                                   # /calendar/v3/calendars[/{cal}[/events[/{id}]]]
        if parts[3] == "calendars" and len(parts) == 4 and method == "POST":
            cid = f"cal-{self.next_id}@group.calendar.google.com"
            self.next_id += 1
            self.calendars[cid] = body
            return json.dumps({"id": cid, **body}).encode()
        cal = urllib.parse.unquote(parts[4])
        if len(parts) == 5 and method == "PATCH":
            self.calendars[cal].update(body)
            return json.dumps({"id": cal, **self.calendars[cal]}).encode()
        if parts[5] == "events" and len(parts) == 6 and method == "POST":
            eid = body.get("id") or f"ev{self.next_id}"
            self.next_id += 1
            if not re.fullmatch(r"[a-v0-9]{5,1024}", eid):            # base32hex, as Google's documentation says
                raise urllib.error.HTTPError(req.full_url, 400, "invalid id", {}, io.BytesIO(b"{}"))
            if (cal, eid) in self.events or (cal, eid) in self.deleted:   # Google keeps a deleted entry's id too
                raise urllib.error.HTTPError(req.full_url, 409, "duplicate", {}, io.BytesIO(b"{}"))
            self.events[(cal, eid)] = {"id": eid, **body}
            return json.dumps(self.events[(cal, eid)]).encode()
        if parts[5] == "events" and len(parts) == 6 and method == "GET":
            query = urllib.parse.parse_qs(url.query)
            wants = [w.split("=", 1) for w in query["privateExtendedProperty"]]
            items = [e for (c, _), e in self.events.items() if c == cal and e.get("status") != "cancelled"
                     and all(e.get("extendedProperties", {}).get("private", {}).get(k) == v for k, v in wants)]
            return json.dumps({"items": items}).encode()
        eid = urllib.parse.unquote(parts[6])
        if method == "PATCH" and (cal, eid) in self.deleted:
            self.events[(cal, eid)] = {"id": eid, **self.deleted.pop((cal, eid))}
        if (cal, eid) not in self.events:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b"{}"))
        if method == "GET":
            return json.dumps(self.events[(cal, eid)]).encode()
        if method == "PATCH":
            self.events[(cal, eid)].update(body)
            return json.dumps(self.events[(cal, eid)]).encode()
        if method == "DELETE":
            if self.events[(cal, eid)].get("status") == "cancelled":  # deleted already: Google still answers a GET, 200,
                raise urllib.error.HTTPError(req.full_url, 410, "gone", {}, io.BytesIO(b"{}"))   # and refuses the delete
            self.deleted[(cal, eid)] = self.events.pop((cal, eid))
            return b""
        raise AssertionError(f"unexpected {method} {url.path}")


def signed_in(tmp_path, google: FakeGoogle, clock=None) -> GoogleAuth:
    (tmp_path / "client.json").write_text(json.dumps({"installed": {"client_id": "id-1", "client_secret": "secret-1",
                                                                     "auth_uri": "https://accounts.example/auth",
                                                                     "token_uri": "https://oauth.example/token"}}))
    auth = GoogleAuth(tmp_path / "client.json", tmp_path / "token.json", fetch=google, **({"clock": clock} if clock else {}))
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "rt-1"}))
    return auth


def _calendar(tmp_path, google: FakeGoogle, fetch, system="ups") -> GoogleCalendar:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return GoogleCalendar(signed_in(tmp_path, google), tmp_path / "state" / "deliveries", name="Deliveries (agent)", system=system,
                          timezone_name="America/New_York", cleanup_hint="cleanup", fetch=fetch)


def calendar_for(tmp_path, google: FakeGoogle, log=None) -> GoogleCalendar:
    return GoogleCalendar(signed_in(tmp_path, google), tmp_path / "state" / "deliveries", name="Deliveries (agent)", system="ups",
                          timezone_name="America/New_York", record=tmp_path / "google_calendar.json", log=log, fetch=google,
                          cleanup_hint=HINT)


def test_the_sign_in_uses_pkce_asks_for_the_narrow_scope_and_keeps_only_the_refresh_token(tmp_path):
    google = FakeGoogle()
    auth = signed_in(tmp_path, google)
    (tmp_path / "token.json").unlink()
    url, verifier, state = auth.begin(8765)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["scope"] == [SCOPE_APP_CREATED] and q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://127.0.0.1:8765"]
    assert q["access_type"] == ["offline"] and q["state"] == [state] and "secret-1" not in url
    with pytest.raises(CalendarUnavailable):
        GoogleAuth.code_from(f"http://127.0.0.1:8765/?state=other&code=code-1", state)       # an answer to another attempt
    with pytest.raises(CalendarUnavailable):
        GoogleAuth.code_from(f"http://127.0.0.1:8765/?state={state}&error=access_denied", state)
    code = GoogleAuth.code_from(f"http://127.0.0.1:8765/?state={state}&code=code-1&scope=x", state)
    auth.finish(code, verifier, 8765)
    kept = json.loads((tmp_path / "token.json").read_text())
    assert kept["refresh_token"] == "rt-1" and "access_token" not in kept and "secret-1" not in (tmp_path / "token.json").read_text()
    assert oct(os.stat(tmp_path / "token.json").st_mode & 0o777) == "0o600"


def test_the_terminal_sign_in_accepts_a_pasted_address(tmp_path):
    google = FakeGoogle()
    auth = signed_in(tmp_path, google)
    (tmp_path / "token.json").unlink()
    said = []
    answers = iter([])

    def ask(prompt):
        url = next(u for u in said if u.startswith("https://accounts.example/auth"))
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        return f"http://127.0.0.1:0/?state={state}&code=code-1"

    sign_in(auth, port=0, say=said.append, ask=ask)                     # port 0: no listener can bind; pasting is the way back
    assert auth.signed_in() and any("Signed in" in s for s in said) and not any("secret-1" in s for s in said)


def test_the_listener_keeps_the_first_real_answer_and_ignores_a_favicon_request(tmp_path):
    import threading
    import urllib.request as ur
    google = FakeGoogle()
    auth = signed_in(tmp_path, google)
    (tmp_path / "token.json").unlink()
    said = []
    entered = threading.Event()

    def ask(prompt):
        url = next(u for u in said if u.startswith("https://accounts.example/auth"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        port, state = int(q["redirect_uri"][0].rsplit(":", 1)[1]), q["state"][0]
        try:
            ur.urlopen(f"http://127.0.0.1:{port}/?state=stale-from-last-time&code=code-0", timeout=5)   # the browser retries an old one
        except urllib.error.HTTPError as exc:
            assert exc.code == 404 and b"goes into the terminal" in exc.read()
        with ur.urlopen(f"http://127.0.0.1:{port}/?state={state}&code=code-1&scope=x", timeout=5) as r:
            assert b"Signed in" in r.read()
        try:
            ur.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)           # the browser asks for this next
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        return ""                                                                  # the person just presses Enter

    sign_in(auth, port=18765, say=said.append, ask=ask)
    assert auth.signed_in()


def test_an_access_token_is_refreshed_once_and_reused_until_it_is_about_to_expire(tmp_path):
    google = FakeGoogle()
    now = [1000.0]
    auth = signed_in(tmp_path, google, clock=lambda: now[0])
    assert auth.access_token() == "at-2" and auth.access_token() == "at-2"
    assert len(google.calls) == 1
    now[0] += 3600
    auth.access_token()
    assert len(google.calls) == 2
    google.refresh_tokens.clear()
    now[0] += 3600
    with pytest.raises(AssertionError):                                   # the fake insists on a known refresh token
        auth.access_token()


def test_the_client_creates_its_calendar_once_then_creates_and_updates_marked_entries_and_nothing_else(tmp_path):
    google = FakeGoogle()
    log = AccessLog(tmp_path / "access.jsonl")
    cal = calendar_for(tmp_path, google, log)
    assert cal.upsert("1Z1", title="[agent] Delivery", start="2026-10-15T13:15", end="2026-10-15T17:15", description="d") == "created"
    assert cal.upsert("1Z1", title="[agent] Delivery", start="2026-10-16T09:00", end="2026-10-16T12:00", description="moved") == "updated"
    assert cal.upsert("1Z2", title="[agent] Other", start="2026-10-16T09:00", end="2026-10-16T12:00") == "created"
    methods = [(m, p.split("?")[0]) for m, p, _ in google.calls if p != "/token"]
    assert methods[0] == ("POST", "/calendar/v3/calendars") and methods[1][0] == "POST" and methods[2][0] == "PATCH" and methods[3][0] == "POST"
    assert len(google.calendars) == 1 and list(google.calendars.values())[0] == {"summary": "Deliveries (agent)", "timeZone": "America/New_York"}
    (cid, eid), event = next(iter(google.events.items()))
    assert event["extendedProperties"]["private"] == {MARK: "ups", BATCH: cal.batch, "key": "1Z1"} and event["start"] == {"dateTime": "2026-10-16T09:00:00", "timeZone": "America/New_York"}
    assert event["description"].startswith("moved\n\nAgent mark: ups/" + cal.batch) and f"{HINT} ups/{cal.batch}" in event["description"]
    assert event["description"].rstrip().endswith("--marked ups")                      # and the whole system's, in one line
    later = GoogleCalendar(cal.auth, tmp_path / "state" / "deliveries", name="Deliveries (agent)", system="ups",
                           timezone_name="America/New_York", cleanup_hint=HINT,
                           record=tmp_path / "google_calendar.json", fetch=google)         # a later pass updates it
    later.upsert("1Z1", title="[agent] Delivery", start="2026-10-16T09:00", end="2026-10-16T12:00", description="again")
    event = google.events[(cid, eid)]
    assert event["extendedProperties"]["private"][BATCH] == cal.batch and f"(last written by ups/{later.batch})" in event["description"]
    assert cal.entries()["1Z1"]["event_id"] == eid and json.loads((tmp_path / "google_calendar.json").read_text())["calendar_id"] == cid
    history = [json.loads(l) for l in cal.history.read_text().splitlines()]
    assert [h["what"] for h in history] == ["created", "updated", "created", "updated"] and history[0]["event_id"] == eid and history[0]["batch"] == cal.batch
    outline = (tmp_path / "access.jsonl").read_text()
    assert "Delivery" not in outline and "moved" not in outline and outline.count('"upsert"') == 3 and '"create_calendar"' in outline
    assert not any(hasattr(cal, name) for name in ("delete", "remove", "list", "events", "calendars"))   # the client cannot
    with pytest.raises(ValueError):
        cal.upsert("1Z3", title="t", start="2026-10-16T09:00", end="2026-10-16T09:00")


def test_a_state_folder_that_held_file_calendar_entries_can_switch_to_google(tmp_path):
    """A state folder used first with the calendar file, then with Google Calendar. The entries file is shared; an entry without a Google id is created on Google, not patched."""
    from chaperone.connect.calendar_file import FileCalendar
    google = FakeGoogle()
    FileCalendar(tmp_path / "state" / "deliveries.ics", system="ups").upsert("1Z1", title="old", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cal = calendar_for(tmp_path, google)
    assert "event_id" not in cal.entries()["1Z1"]
    assert cal.upsert("1Z1", title="new", start="2026-10-16T09:00", end="2026-10-16T10:00") == "created"
    assert cal.entries()["1Z1"]["event_id"] and [m for m, p, _ in google.calls if p.endswith("/events")] == ["POST"]
    assert cal.upsert("1Z1", title="newer", start="2026-10-16T09:00", end="2026-10-16T10:00") == "updated"


def test_a_second_client_reuses_the_recorded_calendar_and_a_lost_sign_in_or_a_refusal_is_an_outage_not_a_crash(tmp_path):
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google)
    cal.upsert("1Z1", title="t", start="2026-10-16T09:00", end="2026-10-16T10:00")
    again = calendar_for(tmp_path, google)
    again.upsert("1Z9", title="t", start="2026-10-16T09:00", end="2026-10-16T10:00")
    assert len(google.calendars) == 1
    google.fail_with = 403
    with pytest.raises(CalendarUnavailable, match="403"):
        again.upsert("1Z8", title="t", start="2026-10-16T09:00", end="2026-10-16T10:00")
    assert "1Z8" not in again.entries()
    (tmp_path / "token.json").unlink()
    google.fail_with = None
    unsigned = GoogleCalendar(GoogleAuth(tmp_path / "client.json", tmp_path / "token.json", fetch=google), tmp_path / "state" / "deliveries",
                              name="Deliveries (agent)", system="ups", cleanup_hint=HINT,
                              timezone_name="America/New_York", record=tmp_path / "google_calendar.json", fetch=google)
    with pytest.raises(CalendarUnavailable, match="google-auth"):
        unsigned.upsert("1Z7", title="t", start="2026-10-16T09:00", end="2026-10-16T10:00")


def test_cleanup_removes_only_marked_entries_by_exact_id_and_leaves_anything_else_alone(tmp_path):
    google = FakeGoogle()
    log = AccessLog(tmp_path / "access.jsonl")
    cal = calendar_for(tmp_path, google, log)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cal.upsert("1Z2", title="b", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cal.upsert("1Z3", title="c", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cid = cal.calendar_id()
    google.events[(cid, "stranger")] = {"id": "stranger", "summary": "the owner's own entry on this calendar"}
    google.events[(cid, "usps-1")] = {"id": "usps-1", "summary": "another run's entry", "extendedProperties": {"private": {MARK: "usps", "key": "9400"}}}
    e2 = cal.entries()["1Z2"]["event_id"]
    google.events[(cid, e2)]["extendedProperties"]["private"]["key"] = "1Z2-renamed"      # the mark no longer fits the key
    e3 = cal.entries()["1Z3"]["event_id"]
    del google.events[(cid, e3)]                                                               # already gone from Google
    plan = google_cleanup.planned(cal)
    assert [i["key"] for i in plan] == ["1Z1", "1Z2", "1Z3"]
    said = []
    result = google_cleanup.remove_all(cal, log=log, fetch=google, say=said.append)
    assert result["removed"] == ["1Z1", "1Z3"] and len(result["kept"]) == 1 and "mark" in result["kept"][0]
    assert (cid, "stranger") in google.events and (cid, e2) in google.events and (cid, "usps-1") in google.events and len(google.events) == 3
    deletes = [(m, p) for m, p, _ in google.calls if m == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith(urllib.parse.quote(cal.entries().get("1Z1", {}).get("event_id", "x"), safe="")) is False
    assert set(cal.entries()) == {"1Z2"}
    history = [json.loads(l) for l in cal.history.read_text().splitlines()]
    assert [h["what"] for h in history[-2:]] == ["deleted", "deleted"] and history[-1]["key"] == "1Z3"
    assert google_cleanup.planned(cal) and [i["key"] for i in google_cleanup.planned(cal)] == ["1Z2"]   # a second pass skips the removed
    assert '"delete"' in (tmp_path / "access.jsonl").read_text()


def test_cleanup_by_a_mark_read_off_an_entry_needs_no_records_and_can_take_one_pass_or_the_whole_system(tmp_path):
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    first = cal.batch
    cal.batch = "20261020-0900-beef"                                     # the next pass
    cal.upsert("1Z2", title="b", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cid = cal.calendar_id()
    google.events[(cid, "orphan")] = {"id": "orphan", "summary": "written before the state was wiped",
                                      "extendedProperties": {"private": {MARK: "ups", BATCH: first, "key": "1Z0"}}}
    google.events[(cid, "theirs")] = {"id": "theirs", "summary": "not ours"}
    google.events[(cid, "usps-1")] = {"id": "usps-1", "summary": "another run's entry", "extendedProperties": {"private": {MARK: "usps", "key": "9400"}}}
    cal._state.unlink()                                                   # the entries file is gone; the history remains
    cal.history.unlink()                                                  # and so is the history: nothing on record at all
    assert google_cleanup.planned(cal) == []
    assert parse_mark(f"ups/{first}") == ("ups", first) and parse_mark("ups") == ("ups", None)
    with pytest.raises(ValueError):
        parse_mark("ups/a/b")
    assert [i["key"] for i in google_cleanup.marked_on_google(cal, f"ups/{first}", fetch=google)] == ["1Z0", "1Z1"]
    result = google_cleanup.remove_all(cal, marked=f"ups/{first}", fetch=google, say=lambda s: None)      # one pass only
    assert result["removed"] == ["1Z0", "1Z1"] and result["kept"] == []
    assert sorted(k for _, k in google.events) == ["ev103", "theirs", "usps-1"] or len(google.events) == 3
    result = google_cleanup.remove_all(cal, marked="ups", fetch=google, say=lambda s: None)              # the whole system
    assert result["removed"] == ["1Z2"]
    assert sorted(google.events) == [(cid, "theirs"), (cid, "usps-1")]                   # the other run's entry and the person's stay
    assert google_cleanup.remove_all(cal, marked="ups", fetch=google, say=lambda s: None)["removed"] == []


def test_cleanup_empties_the_calendar_file_too_and_can_take_one_pass_of_it(tmp_path):
    from chaperone.connect.calendar_file import FileCalendar
    cal = FileCalendar(tmp_path / "deliveries.ics", log=AccessLog(tmp_path / "access.jsonl"), system="demo")
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    first = cal.batch
    cal.batch = "20261020-0900-beef"
    cal.upsert("1Z2", title="b", start="2026-10-16T09:00", end="2026-10-16T10:00")
    assert google_cleanup.planned(cal, marked="other") == [] and [i["key"] for i in google_cleanup.planned(cal, marked=f"demo/{first}")] == ["1Z1"]
    assert google_cleanup.remove_all(cal, marked=f"demo/{first}", say=lambda s: None)["removed"] == ["1Z1"]
    result = google_cleanup.remove_all(cal, say=lambda s: None)
    assert result["removed"] == ["1Z2"] and cal.entries() == {} and "BEGIN:VEVENT" not in (tmp_path / "deliveries.ics").read_text()
    assert [json.loads(l)["what"] for l in cal.history.read_text().splitlines()] == ["created", "created", "deleted", "deleted"]


def test_a_changed_zone_in_the_settings_updates_the_calendar_the_system_created(tmp_path):
    """A calendar created while the server's zone (UTC) was in use; once the settings said America/New_York
    the calendar itself should follow, without deleting anything."""
    google = FakeGoogle()
    utc = GoogleCalendar(signed_in(tmp_path, google), tmp_path / "state" / "deliveries", name="Deliveries (agent)", system="ups",
                         timezone_name="Etc/UTC", cleanup_hint=HINT, record=tmp_path / "google_calendar.json", fetch=google)
    cid = utc.calendar_id()
    assert google.calendars[cid]["timeZone"] == "Etc/UTC"
    cal = calendar_for(tmp_path, google)                                   # America/New_York in the settings now
    assert cal.calendar_id() == cid and google.calendars[cid]["timeZone"] == "America/New_York"
    assert json.loads((tmp_path / "google_calendar.json").read_text())["timezone"] == "America/New_York"
    patches = [p for m, p, _ in google.calls if m == "PATCH"]
    assert patches == [f"/calendar/v3/calendars/{urllib.parse.quote(cid, safe='')}"]
    cal.calendar_id()
    assert len([p for m, p, _ in google.calls if m == "PATCH"]) == 1               # once; the record now agrees


def test_an_entry_deleted_by_hand_on_google_is_made_anew_not_patched(tmp_path):
    """An entry deleted by hand in Google Calendar: the next write once tried to update the dead event."""
    google = FakeGoogle()
    log = AccessLog(tmp_path / "access.jsonl")
    cal = calendar_for(tmp_path, google, log)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cid, eid = cal.calendar_id(), cal.entries()["1Z1"]["event_id"]
    del google.events[(cid, eid)]                                            # deleted by hand
    assert cal.upsert("1Z1", title="b", start="2026-10-17T09:00", end="2026-10-17T10:00") == "created"
    assert (cid, eid) in google.events and google.events[(cid, eid)]["summary"] == "b" and '"gone"' in (tmp_path / "access.jsonl").read_text()
    google.fail_with = 500                                                   # any other failure is still an outage
    with pytest.raises(CalendarUnavailable, match="500"):
        cal.upsert("1Z1", title="c", start="2026-10-17T09:00", end="2026-10-17T10:00")


def test_an_entry_google_keeps_as_cancelled_is_gone_to_a_write_and_to_a_cleanup(tmp_path):
    """An entry deleted on Google still answers a GET, marked `cancelled`, and a DELETE answers
    410. A reset once stopped on that one, every time it ran."""
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cal.upsert("1Z2", title="b", start="2026-10-16T09:00", end="2026-10-16T10:00")
    cid = cal.calendar_id()
    first, second = cal.entries()["1Z1"]["event_id"], cal.entries()["1Z2"]["event_id"]
    google.events[(cid, first)]["status"] = google.events[(cid, second)]["status"] = "cancelled"
    assert cal.upsert("1Z1", title="c", start="2026-10-17T09:00", end="2026-10-17T10:00") == "created"    # never "updated": it would not show
    assert google.events[(cid, first)]["status"] == "confirmed" and google.events[(cid, first)]["summary"] == "c"     # shown again
    result = google_cleanup.remove_all(cal, fetch=google, say=lambda _: None)
    assert sorted(result["removed"]) == ["1Z1", "1Z2"] and result["kept"] == [] and cal.entries() == {}


def test_an_outage_is_not_read_as_gone_from_digits_in_an_id(tmp_path):
    """"404" in the text of the error matched the ids in the path: a 500 on an event whose id holds 404 made the cleanup drop
    its record, leaving the entry on Google with nothing to find it by, and made a write create a second entry."""
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    entries, cid = cal.entries(), cal.calendar_id()                          # an entry whose id Google chose, as an older one's was
    google.events[(cid, "ev404")] = google.events.pop((cid, entries["1Z1"]["event_id"]))
    entries["1Z1"]["event_id"] = "ev404"
    cal._write(entries)
    google.fail_with = 500
    with pytest.raises(CalendarUnavailable, match="500"):
        cal.upsert("1Z1", title="b", start="2026-10-17T09:00", end="2026-10-17T10:00")
    result = google_cleanup.remove_all(cal, fetch=google, say=lambda _: None)
    assert result["removed"] == [] and len(result["kept"]) == 1 and "1Z1" in cal.entries()


def test_a_write_goes_to_the_calendar_the_entry_is_on_whatever_calendar_name_says_now(tmp_path):
    """After a `calendar_name` change the update once went to the new calendar, answered 404, and a second
    entry was made; the old one was forgotten and stayed."""
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    first_calendar, event = cal.calendar_id(), cal.entries()["1Z1"]["event_id"]
    other = GoogleCalendar(signed_in(tmp_path, google), tmp_path / "state" / "deliveries", name="Another", system="ups",
                           timezone_name="America/New_York", record=tmp_path / "another.json", fetch=google, cleanup_hint=HINT)
    assert other.upsert("1Z1", title="b", start="2026-10-17T09:00", end="2026-10-17T10:00") == "updated"
    assert other.entries()["1Z1"]["calendar_id"] == first_calendar and other.entries()["1Z1"]["event_id"] == event
    assert len(google.events) == 1


def test_a_cancelled_entry_under_another_mark_is_gone_too(tmp_path):
    """It was once "left alone" as not carrying the mark, and a reset could never finish."""
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google)
    cal.upsert("1Z1", title="a", start="2026-10-16T09:00", end="2026-10-16T10:00")
    event = google.events[(cal.calendar_id(), cal.entries()["1Z1"]["event_id"])]
    event["status"] = "cancelled"
    event["extendedProperties"]["private"]["key"] = "other"
    result = google_cleanup.remove_all(cal, fetch=google, say=lambda _: None)
    assert result["removed"] == ["1Z1"] and result["kept"] == [] and cal.entries() == {}



def test_a_write_whose_reply_was_lost_makes_no_second_entry(tmp_path):
    """D2: Google made the entry and the answer never came. The next try, in this run or the next, updates that entry."""
    google = FakeGoogle()
    lost = []

    def fetch(req):
        answer = google(req)
        if req.get_method() == "POST" and req.full_url.endswith("/events") and not lost:
            lost.append(1)
            raise urllib.error.URLError("the reply was lost")
        return answer
    cal = _calendar(tmp_path, google, fetch)
    with pytest.raises(CalendarUnavailable):
        cal.upsert("1Z1", title="t", start="2026-11-01T09:00:00", end="2026-11-01T10:00:00")
    assert cal.upsert("1Z1", title="t2", start="2026-11-01T09:00:00", end="2026-11-01T10:00:00") == "created"
    assert [e["summary"] for e in google.events.values()] == ["t2"]


def test_an_entry_deleted_on_google_comes_back_under_its_own_id(tmp_path):
    """Google keeps a deleted entry's id, so a new entry of that id is refused (409): it is shown again instead, once."""
    google = FakeGoogle()
    cal = calendar_for(tmp_path, google, log=AccessLog(tmp_path / "access.jsonl"))
    cal.upsert("1Z1", title="t", start="2026-11-01T09:00:00", end="2026-11-01T10:00:00")
    ((where, event),) = google.events.items()
    event["status"] = "cancelled"
    assert cal.upsert("1Z1", title="t2", start="2026-11-01T09:00:00", end="2026-11-01T10:00:00") == "created"
    assert len(google.events) == 1 and event["status"] == "confirmed" and event["summary"] == "t2"
    calls = [json.loads(line)["call"] for line in (tmp_path / "access.jsonl").read_text().splitlines()]
    assert calls.count("taken_over") == 1            # said in the log: "created" alone does not tell this from a new id


def test_two_systems_on_one_calendar_choose_different_ids(tmp_path):
    google = FakeGoogle()
    one, other = _calendar(tmp_path / "a", google, google, system="one"), _calendar(tmp_path / "b", google, google, system="other")
    one.upsert("1Z1", title="t", start="2026-11-01T09:00:00", end="2026-11-01T10:00:00")
    other.upsert("1Z1", title="t", start="2026-11-01T09:00:00", end="2026-11-01T10:00:00")
    assert len({eid for _, eid in google.events}) == 2 and len({c["event_id"] for c in (one.entries()["1Z1"], other.entries()["1Z1"])}) == 2
