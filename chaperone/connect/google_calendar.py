"""A calendar this system owns on Google Calendar: one secondary calendar it created, and nothing else.

What this client can do, in full: create an entry on that calendar, or change an entry it created earlier.
It reads no other calendar, never lists the owner's calendars or events, and has no way to delete, invite, or
touch an entry it did not make. The one-time removal of everything it created is a separate, purpose-built
tool (`chaperone.connect.google_cleanup`), never a module a workflow can call.

Nothing is installed for this: sign-in and every call use the standard library (urllib, json, http.server).

Files, all under the owner's private folder or the state folder, never printed:

    client_secret.json   the OAuth client Google gave the owner (type "Desktop app"); read as written
    token.json           the refresh token from the one-time sign-in; written here, read on every run
    NAME.entries.json    this system's own entries, by key, each with its Google event id
    NAME.history.jsonl   every write in the order it happened, one per line, never rewritten

The permission asked of Google is the narrowest that fits what the system does: `calendar.app.created`, which
covers secondary calendars this application creates and the events on them. The owner's own calendars are
outside it, by Google's enforcement as well as by this code.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
import http.server
import http.client
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from chaperone.connect.access_log import AccessLog

SCOPE_APP_CREATED = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
MARK = "chaperone_calendar"                     # the private property every entry this system writes carries; its value names the system
BATCH = "chaperone_batch"                       # a second one: which pass of that system created the entry
TIMEOUT = 30                                    # seconds, every request


class CalendarUnavailable(OSError):
    """Google could not be reached, refused the request, or the sign-in is missing. Never carries a token. `kind` says which
    sort it is, so nobody has to read the message to tell."""
    kind = "service"


class CalendarSignIn(CalendarUnavailable):
    """The sign-in is missing or lapsed, or Google will not take it (401, 403)."""
    kind = "signin"


class CalendarRefused(CalendarUnavailable):
    """Google answered and refused this one request (a 4xx that trying again will not change, such as a 400). A sign-in that
    lapsed (401, 403) and being asked to slow down (429) stay CalendarUnavailable: those are about the service, not the entry."""


class EntryExists(CalendarUnavailable):
    """Google already holds an entry with the id asked for (409)."""


class EntryGone(CalendarUnavailable):
    """Google says what was asked for is not there (404, 410), or holds an entry only as cancelled: deleted on Google."""


# ---------------------------------------------------------------------------------------------------- sign-in


class GoogleAuth:
    """Holds the owner's OAuth client and refresh token, and gives a fresh access token on request."""

    def __init__(self, client_secret: Path | str, token: Path | str, *, scope: str = SCOPE_APP_CREATED,
                 fetch: Callable[[urllib.request.Request], bytes] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.client_secret_path = Path(client_secret)
        self.token_path = Path(token)
        self.scope = scope
        self._fetch = fetch or _fetch
        self._clock = clock
        self._access: str | None = None
        self._expires_at = 0.0

    # ---- the client Google issued

    def client(self) -> dict[str, str]:
        if not self.client_secret_path.exists():
            raise CalendarSignIn(f"no OAuth client file at {self.client_secret_path}; see README.md, section 'Google Calendar'")
        raw = json.loads(self.client_secret_path.read_text(encoding="utf-8"))
        inner = raw.get("installed") or raw.get("web") or {}
        if not inner.get("client_id") or not inner.get("client_secret"):
            raise CalendarSignIn(f"{self.client_secret_path} is not an OAuth client file (no client_id and client_secret in it)")
        return {"client_id": inner["client_id"], "client_secret": inner["client_secret"],
                "auth_uri": inner.get("auth_uri", AUTH_URL), "token_uri": inner.get("token_uri", TOKEN_URL)}

    # ---- the one-time sign-in

    def begin(self, port: int) -> tuple[str, str, str]:
        """The address to open in a browser, plus the PKCE verifier and the state this attempt must answer with."""
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        query = {"client_id": self.client()["client_id"], "redirect_uri": f"http://127.0.0.1:{port}", "response_type": "code",
                 "scope": self.scope, "access_type": "offline", "prompt": "consent", "state": state,
                 "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.client()['auth_uri']}?{urllib.parse.urlencode(query)}", verifier, state

    @staticmethod
    def code_from(redirected: str, state: str) -> str:
        """The code Google put in the address it sent the browser back to. Refuses an answer to another attempt."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(redirected.strip()).query)
        if query.get("error"):
            raise CalendarSignIn(f"Google refused the sign-in: {query['error'][0]}")
        if query.get("state", [None])[0] != state:
            raise CalendarSignIn("that address answers an earlier sign-in attempt, not this one: each run prints a fresh address; "
                                      "open the one printed above, allow, and paste where the browser lands this time")
        if not query.get("code"):
            raise CalendarSignIn("no code in that address; paste the whole address from the browser's address bar")
        return query["code"][0]

    def finish(self, code: str, verifier: str, port: int) -> None:
        """Trade the code for a refresh token and keep it. The token file is readable by the owner only."""
        c = self.client()
        answer = self._token_request(c, {"code": code, "code_verifier": verifier, "grant_type": "authorization_code",
                                         "redirect_uri": f"http://127.0.0.1:{port}"})
        if not answer.get("refresh_token"):
            raise CalendarSignIn("Google gave no refresh token; remove this application under myaccount.google.com/permissions and sign in again")
        self._save({"refresh_token": answer["refresh_token"], "scope": answer.get("scope", self.scope),
                    "obtained": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self._access, self._expires_at = answer.get("access_token"), self._clock() + float(answer.get("expires_in", 0)) - 60

    def signed_in(self) -> bool:
        return self.token_path.exists()

    # ---- on every run

    def access_token(self) -> str:
        """A current access token, refreshed from the stored refresh token when the last one is about to expire."""
        if self._access and self._clock() < self._expires_at:
            return self._access
        if not self.token_path.exists():
            raise CalendarSignIn(f"not signed in to Google Calendar yet (no {self.token_path}); run the google-auth command first")
        stored = json.loads(self.token_path.read_text(encoding="utf-8"))
        answer = self._token_request(self.client(), {"refresh_token": stored["refresh_token"], "grant_type": "refresh_token"})
        if not answer.get("access_token"):
            raise CalendarSignIn("Google did not renew the sign-in; run the google-auth command again")
        self._access, self._expires_at = answer["access_token"], self._clock() + float(answer.get("expires_in", 3600)) - 60
        return self._access

    def _token_request(self, c: dict[str, str], fields: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode({**fields, "client_id": c["client_id"], "client_secret": c["client_secret"]}).encode()
        req = urllib.request.Request(c["token_uri"], data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            return json.loads(self._fetch(req) or b"{}")
        except urllib.error.HTTPError as exc:
            raise CalendarSignIn(f"Google's sign-in service answered {exc.code} to the token request") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise CalendarSignIn(f"could not reach Google's sign-in service: {type(exc).__name__}") from None

    def _save(self, token: dict[str, Any]) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        tmp.write_text(json.dumps(token, indent=1, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_path)


def sign_in(auth: GoogleAuth, *, port: int = 8765, say: Callable[[str], None] = print, ask: Callable[[str], str] = input) -> None:
    """Walk the owner through the one-time sign-in at the terminal. Two ways back from the browser work: the browser
    reaches 127.0.0.1:PORT on this machine (same machine, or an SSH tunnel), or the owner pastes the address the
    browser landed on. Nothing is printed but the address to open."""
    url, verifier, state = auth.begin(port)
    caught: dict[str, str] = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                      # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if query.get("state") != [state] or not (query.get("code") or query.get("error")) or "url" in caught:
                self.send_response(404)                        # a favicon request, a stale redirect from an earlier attempt, or a repeat
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not this sign-in attempt's answer. If access was just allowed: this page's address, copied from the "
                                 b"address bar, goes into the terminal.")
                return
            caught["url"] = f"http://127.0.0.1:{port}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. This tab can be closed; the terminal goes on.")

        def log_message(self, *_):                              # nothing to the terminal
            pass

    server = None
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Catch)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        pass                                                    # the port is busy: pasting still works
    say("\nOpen this address in a browser, choose the Google account whose calendar this is, and allow it:\n")
    say(url)
    say(f"\nThe browser is then sent to http://127.0.0.1:{port}/ ...")
    say("  If this terminal is on the same machine as the browser, or that port is tunnelled here, the page says 'Signed in'.")
    say("  Otherwise the browser shows an error page; that is fine: copy the whole address from its address bar.")
    pasted = ask("\nPaste that address here (or press Enter if the page said 'Signed in'): ")
    if server is not None:
        server.shutdown()
        server.server_close()
    landed = pasted.strip() or caught.get("url", "")
    if not landed:
        raise CalendarSignIn("nothing came back from the browser; run the google-auth command again")
    auth.finish(GoogleAuth.code_from(landed, state), verifier, port)
    say(f"Signed in. The refresh token is in {auth.token_path} (readable by this user only). Nothing else was kept.")


# ---------------------------------------------------------------------------------------------------- the calendar


class GoogleCalendar:
    """Same contract as FileCalendar: entries() by key, upsert(key, ...) -> "created" | "updated"; a history file."""

    def __init__(self, auth: GoogleAuth, state_path: Path | str, *, name: str, timezone_name: str, system: str,
                 cleanup_hint: str, record: Path | str | None = None, log: AccessLog | None = None,
                 fetch: Callable[[urllib.request.Request], bytes] | None = None) -> None:
        """`system` names this run of the system; every entry it writes carries that name as its mark, so several
        systems (one per carrier, say) can share a calendar and a cleanup of one never touches the others'. `name` is
        the calendar's name; `cleanup_hint` is the project's command that removes marked entries, written on every entry
        (the project states all three: the framework knows no project's names or commands)."""
        self.auth = auth
        self.system = str(system)
        self.cleanup_hint = str(cleanup_hint)
        self.batch = new_batch()                                  # the command sets a new one per pass
        base = Path(state_path)
        self.path = Path(record) if record else base.with_suffix(".json")   # the id of the calendar this system created
        self._state = base.with_suffix(".entries.json")           # its entries, by key, each with its Google event id
        self.history = base.with_suffix(".history.jsonl")
        self.name = name
        self.timezone = timezone_name
        self.log = log or AccessLog(None)
        self._fetch = fetch or _fetch

    # ---- what the workflows use

    def entries(self) -> dict[str, dict[str, Any]]:
        """The entries this system created, by key. Read from this system's own record, never from Google."""
        if not self._state.exists():
            return {}
        return json.loads(self._state.read_text(encoding="utf-8"))

    def upsert(self, key: str, *, title: str, start: str, end: str, description: str = "") -> str:
        """Create the entry for `key` on this system's own calendar, or update the one made earlier for the same key."""
        for label, value in (("key", key), ("title", title), ("start", start), ("end", end), ("description", description)):
            if not isinstance(value, str):
                raise TypeError(f"{label} must be text")
        if not datetime.fromisoformat(start) < datetime.fromisoformat(end):
            raise ValueError("an entry must end after it starts")
        entries = self.entries()
        created_in = entries[key]["batch"] if key in entries and entries[key].get("batch") else self.batch
        body = {"summary": title, "description": description + mark_line(self.system, created_in, self.batch, self.cleanup_hint),
                "start": {"dateTime": _seconds(start), "timeZone": self.timezone},
                "end": {"dateTime": _seconds(end), "timeZone": self.timezone},
                "extendedProperties": {"private": {MARK: self.system, BATCH: created_in, "key": key}}}
        calendar_id = self.calendar_id()
        outcome = None
        if entries.get(key, {}).get("event_id"):
            event_id = entries[key]["event_id"]
            written_on = entries[key].get("calendar_id") or calendar_id     # where it is, whatever `calendar_name` says now
            try:
                if self._call("PATCH", f"/calendars/{_q(written_on)}/events/{_q(event_id)}", body).get("status") == "cancelled":
                    raise EntryGone("the entry is cancelled on Google")          # an update there would not show
                outcome, calendar_id = "updated", written_on
            except EntryGone:
                self.log.record("calendar", "gone", key=key)      # deleted by hand on Google: the record is stale; make it anew
        if outcome is None:                                       # new, made by the file calendar in this folder, or gone from Google
            # The id is ours, one per entry (an idempotency key): a write whose reply was lost cannot make a second entry. Google
            # keeps the id of a deleted entry too, so an id it already holds is taken over and shown again.
            event_id = hashlib.sha256(f"{self.system}/{key}".encode()).hexdigest()[:40]
            try:
                self._call("POST", f"/calendars/{_q(calendar_id)}/events", {**body, "id": event_id})
            except EntryGone:
                raise CalendarUnavailable(f"the calendar '{self.name}' is not on Google: deleted, or another account is signed in") from None
            except EntryExists:
                self._call("PATCH", f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}", {**body, "status": "confirmed"})
                self.log.record("calendar", "taken_over", key=key)
            outcome = "created"
        entries[key] = {"title": title, "start": start, "end": end, "description": description, "event_id": event_id,
                        "calendar_id": calendar_id, "batch": created_in, "last_batch": self.batch}
        self._write(entries)
        with self.history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "what": outcome, "key": key,
                                 **entries[key]}, sort_keys=True) + "\n")
        self.log.record("calendar", "upsert", key=key, outcome=outcome)
        return outcome

    # ---- the calendar itself, made once

    def calendar_id(self) -> str:
        """The id of the secondary calendar this system created; created on first use and remembered."""
        if self.path.exists():
            kept = json.loads(self.path.read_text(encoding="utf-8"))
            if kept.get("calendar_id"):
                if kept.get("timezone") != self.timezone:            # the owner changed the zone: the calendar's own follows
                    self._call("PATCH", f"/calendars/{_q(kept['calendar_id'])}", {"timeZone": self.timezone})
                    kept["timezone"] = self.timezone
                    self.path.write_text(json.dumps(kept, indent=1), encoding="utf-8")
                    self.log.record("calendar", "update_calendar", timezone=self.timezone)
                return kept["calendar_id"]
        made = self._call("POST", "/calendars", {"summary": self.name, "timeZone": self.timezone})
        calendar_id = made.get("id")
        if not isinstance(calendar_id, str) or not calendar_id:
            raise CalendarUnavailable("Google created the calendar but gave it no id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"calendar_id": calendar_id, "name": self.name, "timezone": self.timezone,
                                         "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=1), encoding="utf-8")
        self.log.record("calendar", "create_calendar", name=self.name)
        return calendar_id

    # ---- plumbing

    def _call(self, method: str, where: str, body: dict | None = None) -> dict[str, Any]:
        return google_call(self._fetch, self.auth, method, where, body)

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state.with_suffix(self._state.suffix + ".tmp")
        tmp.write_text(json.dumps(entries, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._state)


def google_call(fetch: Callable[[urllib.request.Request], bytes], auth: GoogleAuth, method: str, where: str,
                body: dict | None = None) -> dict[str, Any]:
    """One request to the Calendar API, as JSON. A failure becomes CalendarUnavailable with the status, never the body."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {auth.access_token()}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + where, data=data, method=method, headers=headers)
    try:
        raw = fetch(req)
    except urllib.error.HTTPError as exc:
        raise (EntryGone if exc.code in (404, 410) else EntryExists if exc.code == 409 else
               CalendarSignIn if exc.code in (401, 403) else CalendarRefused if 400 <= exc.code < 500 and exc.code not in (401, 403, 408, 429) else CalendarUnavailable)(
            f"Google Calendar answered {exc.code} to {method} {where.split('?')[0]}") from None
    except (OSError, http.client.HTTPException) as exc:           # URLError and TimeoutError are OSErrors; a cut answer is neither
        raise CalendarUnavailable(f"could not reach Google Calendar: {type(exc).__name__}") from None
    if not raw:
        return {}
    try:
        answer = json.loads(raw)
    except ValueError:
        raise CalendarUnavailable("Google Calendar answered with something that is not JSON") from None
    return answer if isinstance(answer, dict) else {}


def new_batch() -> str:
    """A short id for one pass of the system: the local date and time, then four random characters."""
    return datetime.now().strftime("%Y%m%d-%H%M") + "-" + _secrets.token_hex(2)


def mark_line(system: str, created_in: str, last: str, cleanup_hint: str) -> str:
    """The lines at the end of every entry's description that let a person, with no records at all, remove every
    entry this system or this pass wrote: the mark to quote to the project's cleanup command (`cleanup_hint`)."""
    if not cleanup_hint:                                      # a project with no such command: the mark, and no command
        return f"\n\nAgent mark: {system}/{created_in}" + (f" (last written by {system}/{last})" if last != created_in else "")
    text = (f"\n\nAgent mark: {system}/{created_in}" + (f" (last written by {system}/{last})" if last != created_in else "")
            + f"\nTo remove every entry from that pass: {cleanup_hint} {system}/{created_in}"
            + f"\nTo remove every entry this system wrote: {cleanup_hint} {system}")
    return text


def parse_mark(mark: str) -> tuple[str, str | None]:
    """`system` or `system/batch`, as printed on an entry."""
    system, _, batch = mark.strip().partition("/")
    if not system or "/" in batch:
        raise ValueError("a mark is SYSTEM or SYSTEM/BATCH, as printed at the end of an entry's description")
    return system, batch or None


def _fetch(req: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _seconds(iso: str) -> str:
    """Google wants seconds in a dateTime; the entries carry minutes."""
    return datetime.fromisoformat(iso).strftime("%Y-%m-%dT%H:%M:%S")


def _q(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def local_timezone_name() -> str | None:
    """The IANA name of this machine's zone, if it can be read (TZ, or the /etc/localtime link); else None."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None
