"""Read mail over IMAP. Read only, and only a short window of days.

What this client can do, in full:
    log in, open one mailbox read-only, search by date and by plain terms in the
    sender or subject, fetch the matching messages without marking them read,
    log out. Only what the search matched is ever fetched.

What it cannot do, because the code is not here: send, delete, move, flag,
mark as read, create or remove folders, or read mail older than the window.

The window is fixed when the reader is made, from the owner's settings (default
4 days, never more than MAX_WINDOW_DAYS). It normally ends now; for testing on
older mail it can be made to end on a past date. A caller can ask for fewer
days, never for more, and never for mail outside the window.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
import ssl as ssl_module
import warnings
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, NavigableString

from chaperone.connect.access_log import AccessLog

MAX_WINDOW_DAYS = 92
MAX_MESSAGES = 500
MAX_BODY_CHARS = 200_000                                  # a call's default limits on one message (`recent`, `limits`):
MAX_HTML_CHARS = 400_000                                  # a message over one is skipped and recorded, never cut and
MAX_LINKS = 200                                           # never returned
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailWindowError(ValueError):
    """Asked for more days, or more messages, than this reader is allowed to give."""


class MailUnavailable(OSError):
    """The mail source could not be reached or opened: nothing was read. The message never carries a password. `kind` says what
    happened, so nobody has to read the message to tell: `network`, `login` (the server said no; trying again cannot help, and
    can lock the account), or what the source's own error says. `where`: the reader's `where`."""

    def __init__(self, message: str, kind: str = "network", where: str | None = None) -> None:
        super().__init__(message)
        self.kind, self.where = kind, where


class MailReader:
    def __init__(self, connect: Callable[[], Any], *, window_days: int, mailbox: str = "INBOX",
                 max_messages: int = 200, until: date | None = None, log: AccessLog | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), where: str | None = None,
                 keep_open: bool = False) -> None:
        """`connect` returns a logged-in IMAP connection (imaplib.IMAP4 or IMAP4_SSL, or a stand-in for tests).
        `where` says in plain words what the source is (a host and port, a folder) for failure messages and the
        access log, so a person can see what could not be reached; it is never a login.
        `keep_open`: keep one logged-in session between calls instead of logging in and out for each (a loop
        every 15 minutes would otherwise log in each time, and once per search). Each use first checks the session with
        NOOP and opens the mailbox afresh, read-only, so new mail is seen; a session that does not answer, or fails
        during a call, is given up and the next use logs in once more. What the reader can do is unchanged. `close()`
        logs out."""
        if not 1 <= window_days <= MAX_WINDOW_DAYS:
            raise MailWindowError(f"the window must be between 1 and {MAX_WINDOW_DAYS} days, not {window_days}")
        if not 1 <= max_messages <= MAX_MESSAGES:
            raise MailWindowError(f"the limit must be between 1 and {MAX_MESSAGES} messages, not {max_messages}")
        self._connect, self.mailbox = connect, mailbox
        self.window_days, self.max_messages = window_days, max_messages
        self.log = log or AccessLog(None)
        self.until = until                                 # if set, the window ends at the end of this day
        self._now = now
        self.where = where
        self.keep_open = keep_open
        self._session: Any = None                          # the session kept between calls, with keep_open

    def _open(self) -> Any:
        """A logged-in session: the one kept, if it still answers NOOP, else a new one (recorded as a login when kept,
        so the access log shows how often the reader really logs in)."""
        if self._session is not None:
            try:
                status, _ = self._session.noop()
                if status == "OK":
                    return self._session
            except (OSError, imaplib.IMAP4.error):
                pass
            self._drop()
        conn = self._connect()
        if self.keep_open:
            self.log.record("mail", "login", **({"where": self.where} if self.where else {}))
            self._session = conn
        return conn

    def _drop(self, wait: float | None = None) -> None:
        """Give up the kept session, logging out. `wait`: at most this many seconds for the server's goodbye (at exit,
        a half-dead session would otherwise hold the process for the whole socket timeout)."""
        session, self._session = self._session, None
        if session is not None:
            try:
                sock = session.sock if wait is not None else None
            except Exception:                                      # a stand-in, or a session with no socket
                sock = None
            try:
                if sock is not None:
                    sock.settimeout(wait)
                session.logout()
            except BaseException:                                  # a session that is gone cannot say goodbye; nor is
                pass                                               # a second Ctrl-C at exit a reason for a traceback

    def close(self) -> None:
        """Log out of the kept session, if any, waiting at most a few seconds. Safe to call more than once."""
        self._drop(wait=CLOSE_WAIT)

    def recent(self, days: int | None = None, *, mention: list[str] | None = None,
               from_any: list[str] | None = None, except_ids: Iterable[str] | None = None,
               limits: Mapping[str, int] | None = None, skipped: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Messages received in the last `days` days (default and maximum: the reader's window), oldest first.

        `mention`: only messages whose sender or subject contains one of these terms.
        `from_any`: only messages from one of these addresses. An empty list means nobody: nothing is fetched.
        `except_ids`: the server's ids of messages the caller has dealt with already. They are dropped after the
        server's search and before any fetch, so a message dealt with is never fetched again; one not yet dealt with
        is fetched on each call that lists it; the limit on messages then counts only what is left. Efficiency only: the window and the
        conditions are unchanged, and the access log says how many were skipped as known.
        `limits`: the most one message may hold, by part (`body` and `html` in characters, `links` in links); a part
        not named keeps its default (DEFAULT_LIMITS). A message over a limit is skipped: not returned, never cut, and
        recorded in the access log (a `fetch` line: its id, `skipped: too large`, the part, its size and the limit) and
        in `skipped`, when the caller passes a list. The other messages are returned, so one large message cannot stop
        the call. Only a message the call would return is checked: one the conditions leave out is left out as any is.
        The server does the searching, so nothing else is ever fetched; the reader then checks each message
        against the same conditions, whatever the server returned."""
        days = self.window_days if days is None else days
        largest = _limits(limits)
        if not isinstance(days, int) or not 1 <= days <= self.window_days:
            raise MailWindowError(f"this reader gives at most {self.window_days} day(s) of mail; {days!r} was asked for")
        for term in list(mention or []) + list(from_any or []):
            if not isinstance(term, str) or not re.fullmatch(r"[\w@.+' =!#$%&*/?^`{|}~-]{1,80}", term):
                raise ValueError("a search term must be plain text of up to 80 characters, with no quote mark, backslash or line break")
        if from_any is not None and not from_any:
            self.log.record("mail", "recent", days=days, senders=[], returned=0, ids=[])
            return []
        end = self._now() if self.until is None else min(
            self._now(), datetime.combine(self.until + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
        since = (end - timedelta(days=days)).date()
        search = ["SINCE", _imap_date(since)]
        if self.until is not None:
            search += ["BEFORE", _imap_date(self.until + timedelta(days=1))]
        if mention:
            search += _any_of([(key, t) for t in mention for key in ("FROM", "SUBJECT")])
        if from_any:
            search += _any_of([("FROM", a) for a in from_any])

        try:
            conn = self._open()
        except (OSError, imaplib.IMAP4.error) as exc:
            at = f" at {self.where}" if self.where else ""
            self.log.record("mail", "connect", failed=type(exc).__name__, **({"where": self.where} if self.where else {}))
            refused = isinstance(exc, imaplib.IMAP4.error) and not isinstance(exc, imaplib.IMAP4.abort)
            raise MailUnavailable(f"could not reach or log in to the mail source{at}: {type(exc).__name__}: {str(exc)[:120]}",
                                  kind="login" if refused else getattr(exc, "kind", "network"), where=self.where) from exc
        try:
            try:
                status, _ = conn.select(self.mailbox, readonly=True)      # read-only: nothing can be changed
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailUnavailable(f"could not open the mailbox '{self.mailbox}': {type(exc).__name__}") from exc
            if status != "OK":
                raise MailUnavailable(f"could not open the mailbox '{self.mailbox}'")
            status, data = conn.uid("SEARCH", None, *search)
            if status != "OK":
                raise MailUnavailable("the mail server refused the search")
            uids = (data[0] or b"").split()
            known = {str(i) for i in (except_ids or ())}
            skipped_known = sum(1 for u in uids if u.decode() in known)
            uids = [u for u in uids if u.decode() not in known]               # dealt with before: never fetched again
            if len(uids) > self.max_messages:
                self.log.record("mail", "recent", days=days, since=str(since), found=len(uids), skipped_known=skipped_known, refused="too many")
                raise MailWindowError(f"{len(uids)} messages in the window; this reader gives at most {self.max_messages}. "
                                      "Nothing was fetched.")
            messages = []
            missing = []
            for uid in uids:
                # BODY.PEEK fetches without marking the message as read
                status, parts = conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    # the server listed it but did not give it (a bridge or a mailbox in flux): say so in the log and go
                    # on; nothing is marked done for it, so the next pass asks again
                    missing.append(uid.decode())
                    self.log.record("mail", "fetch", id=uid.decode(), returned="nothing", skipped="not returned")   # the reason, as "too large" below
                    continue
                try:
                    messages.append(_to_record(uid.decode(), parts[0][0], parts[0][1]))
                except Exception as exc:                                   # this one message cannot be read; the others can
                    self.log.record("mail", "fetch", id=uid.decode(), returned="nothing", skipped="unreadable", problem=type(exc).__name__)
            if len(uids) > 1 and len(missing) == len(uids):                # one stuck message is no outage: it is named, and the rest are read
                raise MailUnavailable(f"the mail server listed {len(uids)} message(s) but returned none of them")
        except (OSError, imaplib.IMAP4.error, KeyboardInterrupt) as exc:
            if self.keep_open:                                             # a session that failed mid-call is not reused;
                self._drop()                                               # a refusal (too much mail) leaves it healthy
            if isinstance(exc, (MailUnavailable, KeyboardInterrupt)):
                raise
            raise MailUnavailable(f"the mail server stopped answering: {type(exc).__name__}") from exc
        finally:
            if not self.keep_open:
                try:
                    conn.logout()
                except Exception:                                          # the answer, or the error above, matters more
                    pass
        cutoff = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
        messages = [m for m in messages if cutoff <= _parse_iso(m["received"]) <= end]   # hold the line here too, whatever the server returned
        if from_any is not None:
            allowed = {a.lower() for a in from_any}
            messages = [m for m in messages if m["sender"] in allowed]
        if mention:
            terms = [t.lower() for t in mention]
            messages = [m for m in messages if any(t in (m["sender"] + " " + m["subject"]).lower() for t in terms)]
        kept, too_large = [], []
        for m in messages:                       # only what this call would return: a message the conditions leave out
            over = _too_large(m, largest)        # is not checked
            if over is None:
                kept.append(m)
                continue
            too_large.append(m["id"])
            self.log.record("mail", "fetch", id=m["id"], skipped="too large", **over)   # skipped, never cut
            if skipped is not None:
                skipped.append({"id": m["id"], **over})
        messages = kept
        messages.sort(key=lambda m: m["received"])
        narrowed = {k: v for k, v in (("mention", mention), ("senders", from_any)) if v is not None}
        if too_large:
            narrowed["skipped_too_large"] = too_large
        self.log.record("mail", "recent", days=days, since=str(since), until=str(self.until) if self.until else None,
                        **narrowed, returned=len(messages), skipped_known=skipped_known, ids=[m["id"] for m in messages])
        return messages


def _imap_date(day: date) -> str:
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def _any_of(keys: list[tuple[str, str]]) -> list[str]:
    """IMAP search tokens for "any of these": OR takes two keys, so a chain is OR k1 OR k2 ... kn."""
    out: list[str] = []
    for key, term in keys[:-1]:
        out += ["OR", key, f'"{term}"']
    key, term = keys[-1]
    return out + [key, f'"{term}"']


IMAP_TIMEOUT = 60                                         # seconds a socket operation may wait (finding F3: a half-dead
                                                          # port forward hung the loop with nothing printed)
CLOSE_WAIT = 5                                            # seconds to wait for the server's goodbye when a kept session is closed


def imap_login(host: str, port: int, user: str, password: str, *, ssl: bool = False, starttls: bool = True,
               certificate: str | None = None, trust_local: bool = False, timeout: float = IMAP_TIMEOUT) -> Callable[[], Any]:
    """A `connect` function for MailReader. The password stays inside the closure; it is never logged or returned.

    A mail bridge running on this machine usually presents a certificate it made itself. Give its certificate
    file as `certificate`. `trust_local` skips the check instead, and is refused unless the host is this machine."""
    if trust_local and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the certificate check can be skipped only for a mail bridge on this machine")
    context = ssl_module.create_default_context(cafile=certificate) if certificate else ssl_module.create_default_context()
    if trust_local:
        context.check_hostname = False
        context.verify_mode = ssl_module.CERT_NONE

    def connect() -> Any:
        conn = (imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout) if ssl
                else imaplib.IMAP4(host, port, timeout=timeout))
        if not ssl and starttls:
            conn.starttls(ssl_context=context)
        conn.login(user, password)
        return conn
    return connect


# ------------------------------------------------------------------ turning a raw message into a plain record

DEFAULT_LIMITS = {"body": MAX_BODY_CHARS, "links": MAX_LINKS, "html": MAX_HTML_CHARS}


def _limits(limits: Mapping[str, int] | None) -> dict[str, int]:
    """The defaults, with the caller's limits over them; an unknown part, or a limit that is not a whole number of at
    least 1, is refused."""
    out = dict(DEFAULT_LIMITS)
    for part, limit in (limits or {}).items():
        if part not in DEFAULT_LIMITS:
            raise ValueError(f"no limit on '{part}': the parts are {', '.join(DEFAULT_LIMITS)}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"the limit on '{part}' must be a whole number of at least 1, not {limit!r}")
        out[part] = limit
    return out


def _too_large(record: dict[str, Any], limits: Mapping[str, int]) -> dict[str, Any] | None:
    """The first part of a record over its limit (the part, its size and the limit); None when it fits."""
    for part, limit in limits.items():
        if len(record[part]) > limit:
            return {"part": part, "size": len(record[part]), "limit": limit}
    return None


def _to_record(uid: str, envelope: bytes, raw: bytes) -> dict[str, Any]:
    """The message as a plain record, whole; `recent` then checks it against the call's limits (`_too_large`)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body, links, html = _body(msg)
    record = {
        "id": uid,                                                         # the mail server's own id, not a header a sender controls
        "sender": (email.utils.parseaddr(str(msg.get("From", "")))[1] or "").lower(),
        "authenticated": _authenticated(msg),
        "received": _internal_date(envelope),
        "subject": str(msg.get("Subject", "")),
        "to": _recipient(msg),                                             # which of the owner's addresses it came to
        "reply_to": _address(msg.get("Reply-To")),                         # where a reply would go: set by the sender; empty if none
        "body": body,                                                      # what a reader sees
        "links": links,                                                    # every link, in order, found by code
        "html": html,                                                      # the source, for a rule that reads it; empty for plain text
    }
    return record


def _address(header: Any) -> str:
    value = str(header or "")
    return (email.utils.parseaddr(value)[1] or value).lower() if value else ""


def _recipient(msg: Any) -> str:
    """The address the message was delivered to: what the receiving mail system recorded if it did, else the To header.
    Owners with several addresses (name+shop@domain) see which one a sender used."""
    for header in ("Delivered-To", "X-Original-To", "To"):
        value = str(msg.get(header, "") or "")
        if value:
            return (email.utils.parseaddr(value)[1] or value).lower()
    return ""


def _authenticated(msg: Any) -> bool:
    """True if any Authentication-Results header in the message says dkim=pass or dmarc=pass.

    KNOWN LIMIT. This is not proof of who sent the message; two ways past it:
    1. A sender writes the header into the message itself. It is plain text, and a receiving server adds its own above it without
       removing theirs. `Authentication-Results: x; dmarc=pass` below the server's own `dmarc=fail` passes here.
    2. No forging at all: a message signed by the sender's own domain, with a From address at another. The server truly reports
       `dkim=pass header.d=evil.test; dmarc=fail header.from=ups.com`, and any dkim=pass passes here.
    So a "verified" sender in the cards and on the screens means this header test. What still stands: mail must also be from an
    address the owner approved; the calendar takes only values that pass the checks; the owner sees "NOT VERIFIED" only for mail
    with no such header. A real check would trust only the receiving server's own header and read dmarc from it. A message
    with no such header is left out: tests/test_real_clients.py. No test shows the two ways past it."""
    results = " ".join(str(v) for v in msg.get_all("Authentication-Results", []))
    return bool(re.search(r"\bdmarc=pass\b", results, re.I) or re.search(r"\bdkim=pass\b", results, re.I))


def _internal_date(envelope: bytes) -> str:
    """When the mail server received the message, as ISO text in UTC. Set by the server, not the sender."""
    match = re.search(rb'INTERNALDATE "([^"]+)"', envelope or b"")
    if not match:
        raise OSError("the mail server did not say when the message was received")
    when = datetime.strptime(match.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


BLOCK_TAGS = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote",
              "section", "article", "header", "footer", "hr", "pre", "address", "dd", "dt", "title"}


# What a reader never sees, by the plain ways of hiding: these parts are taken out before anything is read. Other ways, by a
# style sheet's class, by colour or by a tiny size, are not found: such text is read like any other.
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
SIZE_ZERO = re.compile(r"font-size\s*:\s*0(?![\d.])", re.I)
SETS_A_SIZE = re.compile(r"font-size", re.I)
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)    # a body that is only a link is still a body


def _visible(html: str) -> tuple[str, list[str]]:
    """The text a reader sees of an HTML part, and its links: whitespace in a run of text collapsed to one space, one line
    per block, table cells a space apart."""
    soup = BeautifulSoup(html, "html.parser")
    # size 0 hides nothing when what is inside sets its own size: the usual way mail templates lay out their tables
    for tag in [*soup(["script", "style"]), *soup(hidden=True), *soup(style=HIDDEN_STYLE),
                *(t for t in soup(style=SIZE_ZERO) if not t.find(style=SETS_A_SIZE))]:
        if not tag.decomposed:
            tag.decompose()
    links = list(dict.fromkeys(a["href"].strip() for a in soup("a", href=True) if a["href"].strip()))
    for piece in soup.find_all(string=lambda s: type(s) is NavigableString):
        piece.replace_with(re.sub(r"\s+", " ", piece))
    for tag in soup([*BLOCK_TAGS, "br"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup(["td", "th"]):
        cell.insert_before(" ")
    lines = (re.sub(r" {2,}", " ", line).strip() for line in soup.get_text().split("\n"))
    return "\n".join(line for line in lines if line), links


def _content(part: Any) -> str:
    """A part's text. A character set Python does not know is read as UTF-8, with what does not fit marked; nothing is dropped."""
    try:
        return str(part.get_content())
    except LookupError:
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def _body(msg: Any) -> tuple[str, list[str], str]:
    """The text a reader sees, the links in the message, and the HTML source if there is one.
    The plain-text part is used as it is when there is one; otherwise the HTML part is rendered."""
    html_part = msg.get_body(preferencelist=("html",))
    html = _content(html_part) if html_part is not None else ""
    visible, html_links = _visible(html) if html else ("", [])
    part = msg.get_body(preferencelist=("plain",))
    if part is None or not _content(part).strip():          # a blank plain part: the HTML holds the text
        return visible, html_links, html
    text = _content(part).replace("\r\n", "\n")
    return text, [u for u in re.findall(r"https?://[^\s<>\"']+", text) if u not in html_links] + html_links, html
