"""The object a module is passed as `ctx`, built from a tool's `client` in the setup (specification 2.6).

The kinds of client, and the keys of each:

    {kind: web}                                        the page (chaperone.web)
    {kind: scripted, answers: FILE}                    answers from a file: made-up data
    {endpoint: FILE, prompts: FILE}                    an LLM server: its settings file, the project's prompts file
    {kind: imap, days: N, max_messages: N, authenticated_only: true|false, timezone: ZONE}
                                                       a mail server, read only; the login is in the environment
    {kind: calendar_file, file: NAME, name: TEXT}      a calendar file (.ics) in the state folder
    {kind: google_calendar, client_secret: FILE, token: FILE, calendar_name: TEXT, timezone: ZONE, mark: TEXT, file: NAME}
                                                       one Google calendar that this system creates, by its name
    {kind: text_file, file: NAME}                      the lines of a text file in the state folder
    {kind: clock, timezone: ZONE}                      today's date in a time zone
    {kind: documents, folder: FOLDER, extensions: [.txt, .md], file: NAME, block_chars: N, tokenizer: porter|unicode61, overview: TEXT}
                                                       the text files of a folder, anywhere on this machine, searched by their words

A FILE is a path relative to the project's folder. A NAME is a file in the state folder, where a run keeps what it
writes.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chaperone.yaml_lines import load_yaml
from chaperone.connect.access_log import AccessLog
from chaperone.connect.calendar_file import FileCalendar
from chaperone.connect.google_calendar import CalendarSignIn, CalendarUnavailable, EntryGone, GoogleAuth, GoogleCalendar, _q
from chaperone.connect.google_cleanup import _forget
from chaperone.connect.mail_reader import MailReader, MailUnavailable, MailWindowError, imap_login
from chaperone.connect.llm import ChatModel, ModelError, load_llm

from .engine import ModuleError
from .load import Project
from .values import Problem

PLACE = re.compile(r"\{([A-Za-z_]\w*)\}")                   # `{name}` in a prompt: the argument of that name
ANSWERS = ("text", "json")


class Scripted:
    """A tool that answers from a file: `client: {kind: scripted, answers: demo/mailbox.yaml}`. The file maps each
    method a module calls to its answer, in one of three forms:

        search: [...]                                  the same answer at every call
        fetch_next: {each_call: [first, second, null]} one answer per call; the last is given again after that
        url_of: {by_argument: {10: "https://..."}}     the answer for the first argument of the call

    Each run of a workflow on the page starts from the first answer (`fresh`), whatever ran before it.
    """

    def __init__(self, tool: str, answers: dict[str, Any]) -> None:
        self._tool, self._answers, self._calls = tool, answers, {}

    def fresh(self) -> Scripted:
        """The same answers, counted again from the first call."""
        return Scripted(self._tool, self._answers)

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_") or method not in self._answers:
            raise AttributeError(f"the scripted tool {self._tool} has no answer for `{method}`")
        answer = self._answers[method]

        def call(*arguments: Any, **named: Any) -> Any:
            arguments = arguments + tuple(named.values())
            if isinstance(answer, dict) and set(answer) == {"each_call"}:
                count = self._calls.get(method, 0)
                self._calls[method] = count + 1
                return _plain(answer["each_call"][min(count, len(answer["each_call"]) - 1)])
            if isinstance(answer, dict) and set(answer) == {"by_argument"}:
                if not arguments or arguments[0] not in answer["by_argument"]:
                    raise Problem("no_answer", f"the scripted tool {self._tool} has no answer of `{method}` for its argument")
                return _plain(answer["by_argument"][arguments[0]])
            return _plain(answer)
        return call


class Endpoint:
    """An LLM server: `client: {endpoint: <the model's settings file>, prompts: <the project's prompts file>}`.

    The prompts file maps each method a module calls to a prompt:

        next_step:
          answer: json            # or text: what the method returns
          system: "..."
          user: "The user wrote: {reply}"

    A module calls `ctx.next_step(reply=..., sizes=...)`, its arguments by name. `{name}` in the prompt is replaced by
    the argument of that name: a text as written, anything else as JSON. The method returns the model's answer: for
    `text`, the text without the spaces at its ends; for `json`, the object read from it. An answer read generously
    (JSON with other text around it, such as a code fence) is taken and counted in the log; one that cannot be read
    as JSON is returned as its text, for the module to refuse. A server that gives no answer raises the error
    `llm_unavailable`. The log (`llm.jsonl` in the state folder) has the model, the time and the tokens of each call,
    never a prompt or an answer.
    """

    def __init__(self, tool: str, settings_file: Path, prompts_file: Path, log_file: Path | None = None) -> None:
        self._tool = tool
        for file, what in ((settings_file, "the model's settings file"), (prompts_file, "the prompts file")):
            if not file.is_file():
                raise Problem("no_client", f"tool {tool}: {what} {file} does not exist")
        try:
            settings = load_llm(settings_file)
        except ValueError as problem:
            raise Problem("no_client", f"tool {tool}: {problem}") from None
        self._log = AccessLog(log_file)
        self._model = ChatModel(settings, self._log)
        self._prompts = _plain(load_yaml(prompts_file))
        for method, prompt in self._prompts.items():
            if (not isinstance(prompt, dict) or set(prompt) != {"answer", "system", "user"}
                    or prompt["answer"] not in ANSWERS or not all(isinstance(prompt[k], str) for k in ("system", "user"))):
                raise Problem("no_client", f"{prompts_file}, {method}: a prompt has `answer` ({' or '.join(ANSWERS)}), "
                                           f"`system` and `user`, both texts, and no other key")

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_") or method not in self._prompts:
            raise AttributeError(f"the prompts file of the tool {self._tool} has no prompt for `{method}`")
        prompt = self._prompts[method]

        def call(*arguments: Any, **named: Any) -> Any:
            if arguments:
                raise Problem("bad_call", f"`{method}` of the tool {self._tool}: the arguments are passed by name")

            def fill(text: str) -> str:
                missing = sorted(set(PLACE.findall(text)) - set(named))
                if missing:
                    raise Problem("bad_call", f"the prompt for `{method}` names {{{missing[0]}}}, and the module "
                                              f"passes no argument of that name")
                return PLACE.sub(lambda m: named[m.group(1)] if isinstance(named[m.group(1)], str)
                                 else json.dumps(named[m.group(1)], ensure_ascii=False), text)
            try:
                text = self._model.ask(method, fill(prompt["system"]), fill(prompt["user"])).strip()
            except ModelError as problem:
                raise ModuleError("llm_unavailable", str(problem)) from None
            if prompt["answer"] == "text":
                return text
            found, generous = read_json(text)
            if generous:
                self._log.record("model", "llm_generous", step=method, reason="JSON taken from within other text")
            if found is None:
                self._log.record("model", "llm_bad_response", step=method, reason="the answer is not JSON")
                return text
            return found
        return call


MAIL_VARIABLES = ("CHAPERONE_MAIL_HOST", "CHAPERONE_MAIL_PORT", "CHAPERONE_MAIL_USER", "CHAPERONE_MAIL_PASSWORD")


def imap_from_environment() -> tuple[Callable[[], Any], str]:
    """A function that logs in to the mail server the environment names (the template private.example/env.sh lists the variables),
    and where that is, for the client's own log. The password is read here and kept nowhere else."""
    missing = [name for name in MAIL_VARIABLES if not os.environ.get(name)]
    if missing:
        raise Problem("no_client", f"the mail login is not in the environment: {', '.join(missing)} not set. Fill in "
                                   f"private/env.sh (template: private.example/env.sh) and run `source private/env.sh`.")
    env = os.environ
    connect = imap_login(env["CHAPERONE_MAIL_HOST"], int(env["CHAPERONE_MAIL_PORT"]), env["CHAPERONE_MAIL_USER"], env["CHAPERONE_MAIL_PASSWORD"],
                         ssl=env.get("CHAPERONE_MAIL_SSL") == "1", starttls=env.get("CHAPERONE_MAIL_STARTTLS", "1") == "1",
                         certificate=env.get("CHAPERONE_MAIL_CERT") or None, trust_local=env.get("CHAPERONE_MAIL_TRUST_LOCAL") == "1")
    return connect, f"{env['CHAPERONE_MAIL_HOST']}:{env['CHAPERONE_MAIL_PORT']}"


class Mailbox:
    """A mail server, read only:
    `client: {kind: imap, days: 14, max_messages: 200, authenticated_only: true, timezone: America/New_York}`.

    `days` is how far back it reads; a window holding more than `max_messages` messages is the error `too_much_mail`.
    With `authenticated_only`, a message whose headers do not say its sender was verified is never returned, and is
    counted in the log. A server that cannot be reached is the error `mail_unavailable`.

    search_senders(terms, subjects)
                             [{address, messages, subjects}]: the senders of the messages that mention a term, each
                             with a count and the subjects of its newest messages, at most `subjects` of them
    count_messages(addresses) [{address, messages}]: a count for each address
    fetch_next(addresses, handled)
                             the oldest message from these addresses whose id is not in `handled`, or None:
                             {id, from, received, received_day, received_time, subject, body, links}

    `timezone` is the user's time zone, by its name in the tz database; `received_day` and `received_time` are the day and the time of day ("HH:MM") the message was
    received there, whatever the clock of the machine this runs on. Which messages were dealt with is the caller's
    to keep: the client writes no file of its own but its log. In memory it keeps the messages of its last search of
    the server, and asks the server again only when none of them is left for the caller: one search for a run of
    messages, not one for each. The log, `mail.jsonl`, has ids and counts, no text.
    """

    def __init__(self, tool: str, connect: Callable[[], Any], where: str, state: Path, days: int, max_messages: int,
                 authenticated_only: bool, timezone: str, keep_open: bool = False) -> None:
        self._tool, self._only_verified = tool, authenticated_only
        self._log = AccessLog(state / "mail.jsonl")
        try:
            self._reader = MailReader(connect, window_days=days, max_messages=max_messages, log=self._log, where=where, keep_open=keep_open)
        except MailWindowError as problem:
            raise Problem("no_client", f"tool {tool}: {problem}") from None
        self._waiting: list[dict[str, Any]] = []            # the messages of the last search, oldest first; in memory only
        try:
            self._zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise Problem("no_client", f"tool {tool}: `timezone` is the name of a time zone in the tz database, such as "
                                       f"America/New_York or Europe/Berlin; `{timezone}` is none") from None

    def _recent(self, **conditions: Any) -> list[dict[str, Any]]:
        try:
            found = self._reader.recent(**conditions)
        except MailUnavailable as problem:
            raise ModuleError("mail_unavailable", f"the mail server gave no answer ({problem.kind})") from None
        except MailWindowError as problem:
            raise ModuleError("too_much_mail", str(problem)) from None
        if not self._only_verified:
            return found
        verified = [message for message in found if message["authenticated"]]
        if len(verified) < len(found):
            self._log.record("mail", "not_verified", ids=[m["id"] for m in found if not m["authenticated"]])
        return verified

    def _counts(self, messages: list[dict[str, Any]], addresses: list[str]) -> list[dict[str, Any]]:
        return [{"address": address, "messages": sum(1 for m in messages if m["sender"] == address)} for address in addresses]

    def search_senders(self, terms: list[str], subjects: int = 0) -> list[dict[str, Any]]:
        messages = self._recent(mention=list(terms))
        found = self._counts(messages, sorted({m["sender"] for m in messages}))
        for sender in found:                                 # the subjects of its newest messages, each once
            newest = [m["subject"] for m in reversed(messages) if m["sender"] == sender["address"]]
            sender["subjects"] = list(dict.fromkeys(newest))[:max(0, subjects)]
        return found

    def count_messages(self, addresses: list[str]) -> list[dict[str, Any]]:
        return self._counts(self._recent(from_any=[a.lower() for a in addresses]), [a.lower() for a in addresses])

    def fetch_next(self, addresses: list[str], handled: list[Any]) -> dict[str, Any] | None:
        wanted, done = {a.lower() for a in addresses}, {str(i) for i in handled}
        left = [m for m in self._waiting if m["id"] not in done and m["sender"] in wanted]
        if not left:                                         # nothing left of the last search: ask the server
            self._waiting = left = self._recent(from_any=sorted(wanted), except_ids=sorted(done))
        if not left:
            return None
        message = left[0]
        there = datetime.fromisoformat(message["received"]).astimezone(self._zone)
        return {"id": message["id"], "from": message["sender"], "received": message["received"],
                "received_day": there.date().isoformat(), "received_time": there.strftime("%H:%M"),
                "subject": message["subject"], "body": message["body"], "links": message["links"]}


class OwnGoogleCalendar(GoogleCalendar):
    """One Google calendar that this system creates, and only the entries this system writes on it:
    `client: {kind: google_calendar, client_secret: ..., token: ..., calendar_name: "Deliveries", timezone:
    America/New_York, mark: deliveries, file: google_deliveries}`.

    `client_secret` is the OAuth client file downloaded from Google; `token` is the file the one-time sign-in wrote.
    The permission asked of Google covers calendars this application created, and no other. `calendar_name` is the
    calendar's name: it is created at the first write, and a changed name makes a second calendar. `mark` is written
    at the end of every entry's description. `file` names the records kept in the state folder: the calendar's id,
    the entries by key, the history. Read from those records, never from Google.

    entries()                the entries this system wrote, by key
    upsert(key, title=, start=, end=, description=)   "created" or "updated"
    remove(key)              "removed" or "absent": deletes on Google the entry this system wrote for the key

    Google not accepting the sign-in is the error `calendar_sign_in`, whose message gives the command that signs in
    again; Google not answering, or refusing a request, is the error `calendar_unavailable`.
    """
    sign_in_command = ""                                    # set when the tool is built: the project's command that signs in

    def _failed(self, problem: CalendarUnavailable) -> ModuleError:
        if isinstance(problem, CalendarSignIn):
            return ModuleError("calendar_sign_in", f"Google does not accept the stored sign-in ({problem}). Sign in again: "
                                                   f"`{self.sign_in_command}`, then start the workflow again.")
        return ModuleError("calendar_unavailable", str(problem))

    def upsert(self, key: str, **entry: Any) -> str:
        try:
            return super().upsert(key, **entry)
        except CalendarUnavailable as problem:
            raise self._failed(problem) from None

    def remove(self, key: str) -> str:
        made = self.entries().get(key)
        if made is None:
            return "absent"
        try:
            calendar_id = made.get("calendar_id") or self.calendar_id()
            try:
                self._call("DELETE", f"/calendars/{_q(calendar_id)}/events/{_q(made['event_id'])}")
            except EntryGone:
                pass                                         # deleted by hand on Google: the record is dropped all the same
        except CalendarUnavailable as problem:
            raise self._failed(problem) from None
        _forget(self, key, calendar_id, "deleted")
        self.log.record("calendar", "delete", key=key, outcome="deleted")
        return "removed"


def google_auth(project: Project, client: dict[str, Any], fetch: Any = None) -> GoogleAuth:
    return GoogleAuth(project.folder / str(client["client_secret"]), project.folder / str(client["token"]), **({"fetch": fetch} if fetch else {}))


def sign_in_command(project: Project) -> str:
    return f"python -m chaperone signin {project.folder} --setup {getattr(project, 'setup', 'local')}"


def google_calendar(tool: str, project: Project, client: dict[str, Any], state: Path, fetch: Any = None) -> OwnGoogleCalendar:
    auth = google_auth(project, client, fetch)
    if not auth.client_secret_path.is_file():
        raise Problem("no_client", f"tool {tool}: {auth.client_secret_path} does not exist: the OAuth client file downloaded from "
                                   f"Google Cloud (a client of type Desktop app; README.md, section 'Google Calendar').")
    if not auth.signed_in():
        raise Problem("no_client", f"tool {tool}: not signed in to Google: {auth.token_path} does not exist. "
                                   f"Sign in once: `{sign_in_command(project)}`.")
    try:
        ZoneInfo(str(client["timezone"]))
    except (ZoneInfoNotFoundError, ValueError):
        raise Problem("no_client", f"tool {tool}: `timezone` is the name of a time zone in the tz database; `{client['timezone']}` is none") from None
    calendar = OwnGoogleCalendar(auth, state / _name(tool, client["file"]), name=str(client["calendar_name"]), timezone_name=str(client["timezone"]),
                                 system=str(client["mark"]), cleanup_hint="", log=AccessLog(state / "calendar.jsonl"), **({"fetch": fetch} if fetch else {}))
    calendar.sign_in_command = sign_in_command(project)
    return calendar


class Clock:
    """Today's date where the user is: `client: {kind: clock, timezone: America/New_York}`. A workflow that acts on
    the date asks this tool, so that a test or a made-up setup can give it another day.

    today()    the date in that time zone, written year-month-day
    """

    def __init__(self, tool: str, timezone: str) -> None:
        try:
            self._zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise Problem("no_client", f"tool {tool}: `timezone` is the name of a time zone in the tz database; `{timezone}` is none") from None

    def today(self) -> str:
        return datetime.now(self._zone).date().isoformat()


class TextFile:
    """The lines of a text file in the state folder: `client: {kind: text_file, file: senders.txt}`. Empty lines and
    lines that start with # are left out. A file that does not exist has no lines."""

    def __init__(self, file: Path) -> None:
        self._file = file

    def read_lines(self) -> list[str]:
        if not self._file.is_file():
            return []
        lines = (line.strip() for line in self._file.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line and not line.startswith("#")]


TOKENIZERS = {"porter": "porter unicode61", "unicode61": "unicode61"}     # the setup's word -> SQLite's tokenizer
INDEX_FORM = 2                                                            # the columns of the index; an index of another form is made again


class Documents:
    """The text files of a folder, searched by their words: `client: {kind: documents, folder: corpus, extensions:
    [.txt, .md], file: documents.sqlite, block_chars: 1200, tokenizer: porter, overview: "..."}`. `overview` is one
    or two sentences on what the documents are, written by the user ("" for none); no check reads it, and a module
    may pass it to an LLM.

    `folder` is relative to the project's folder, or absolute, or begins with `~`. The files read are those under it
    whose name ends with one of `extensions`; a file or folder whose name begins with a dot (`.obsidian`, `.git`) is
    not read. Each file is read as paragraphs (a blank line ends one), and paragraphs that follow each other are
    joined into a chunk of at most `block_chars` characters; a longer paragraph is a chunk by itself, never cut. In a
    chunk's text a run of spaces or line breaks is one space. A file is read as UTF-8; a byte that is not is read
    as the character U+FFFD, and nothing else says so. `tokenizer` is how words are matched: `porter` reads
    English words by their stem ("factions" finds "faction"); `unicode61` matches words as written, in any language.
    The index is the file `file` in the state folder (SQLite full-text search), made at the first call and again when
    a file was added, removed or changed. Nothing leaves this machine.

    search(query, limit, exclude)   the `limit` chunks that fit the words of `query` best, best first, leaving out the
                                    chunks whose ids are in `exclude`. A word is a run of letters or digits; a chunk
                                    fits when it has any of them, and better with more of them.
    get(chunk_ids)                  the chunks with those ids, in that order; an id no chunk has is left out
    files()                         the number of files read
    stats(terms)                    for each term, how many chunks hold it and their share of all chunks, 0 to 1:
                                    {term: {count, share}}
    find(texts, terms)              for each text, where each term is in it: a list of [start, end, term], in the
                                    text's order
    search_terms(terms, limit, exclude, min_phrase_count)
                                    as search, for a list of terms: a chunk fits when it has any of them
    overview()                      the client's `overview`, a text of the user's: what the documents are

    A term is one word, or several words that follow each other ("supreme court"), matched as `tokenizer` matches
    words (with `porter`, "factions" is found where the text says "faction"). A term of several words is matched as a
    whole, its words next to each other; in `search_terms`, a term of several words that fewer than
    `min_phrase_count` chunks hold that way is matched where a chunk has all of its words, anywhere. The words of
    such a term are never searched one without the others. Anything that is not a term is left out.

    A chunk is {document_id, title, source, chunk_id, location, text, score}. `document_id` and `source` are the
    file's path in the folder ("federalist_10.txt"); `title` is the file's first line that is not empty;
    `chunk_id` is the document_id, # and the chunk's number in the file ("federalist_10.txt#3"); a block of front
    matter at the top of a file, between two lines `---`, is in the text and is not the title; `location` is
    {first_paragraph, last_paragraph}, counted in the file from 1; `score` is how well the chunk fits the query,
    higher is better (SQLite's bm25, negated, to three decimals), and 0 from `get`.
    """

    def __init__(self, tool: str, folder: Path, index: Path, extensions: Any, block_chars: Any, tokenizer: Any, overview: Any = "") -> None:
        if not isinstance(overview, str):
            raise Problem("no_client", f"tool {tool}: `overview` is a text; \"\" for none")
        self._overview = " ".join(overview.split())
        self._not_before, self._boost, self._title_indexed = "", 0.0, False       # a folder's documents have no date (see DocumentDb)
        if not (isinstance(block_chars, int) and block_chars > 0):
            raise Problem("no_client", f"tool {tool}: `block_chars` is a whole number above 0")
        if not (isinstance(extensions, list) and extensions and all(isinstance(e, str) and re.fullmatch(r"\.\w+", e) for e in extensions)):
            raise Problem("no_client", f"tool {tool}: `extensions` is a list of file endings, such as [.txt, .md]")
        if tokenizer not in TOKENIZERS:
            raise Problem("no_client", f"tool {tool}: `tokenizer` is {' or '.join(TOKENIZERS)}; `{tokenizer}` is neither")
        self._tool, self._folder, self._index, self._block_chars = tool, folder, index, block_chars
        self._extensions, self._tokenizer = tuple(e.lower() for e in extensions), tokenizer
        if not folder.is_dir() or not self._files():
            raise Problem("no_client", f"tool {tool}: the folder {folder} does not exist or has no file ending in {' or '.join(self._extensions)}")

    def _files(self) -> list[Path]:
        found = []
        for root, folders, names in os.walk(self._folder):
            folders[:] = [name for name in folders if not name.startswith(".")]          # never walked into
            found += [Path(root) / name for name in names if not name.startswith(".") and Path(name).suffix.lower() in self._extensions]
        return sorted(found)

    def _chunks(self, file: Path) -> tuple[str, list[tuple[int, int, str]]]:
        """The file's title, and its chunks: (first paragraph, last paragraph, text)."""
        text = file.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        after = 0                                            # a block of front matter between two lines `---` is not the title
        if lines and lines[0].strip() == "---":
            after = next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        title = next((" ".join(line.split()) for line in lines[after:] if line.strip()), "")
        return title, self._split(text)

    def _split(self, text: str) -> list[tuple[int, int, str]]:
        """A text as chunks, (first paragraph, last paragraph, text): paragraphs that follow each other, joined up to
        `block_chars`. The chunks of one document are linked by its id and their order."""
        chunks: list[tuple[int, int, str]] = []
        paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
        for number, paragraph in enumerate((p for p in paragraphs if p), 1):
            if chunks and len(chunks[-1][2]) + 1 + len(paragraph) <= self._block_chars:
                chunks[-1] = (chunks[-1][0], number, chunks[-1][2] + " " + paragraph)
            else:
                chunks.append((number, number, paragraph))
        return chunks

    def _made_from(self) -> str:
        """What the index was made from: when this text changes, the index is made again."""
        return json.dumps([INDEX_FORM, self._block_chars, self._tokenizer, self._extensions]
                          + [[f.relative_to(self._folder).as_posix(), f.stat().st_size, f.stat().st_mtime_ns] for f in self._files()])

    def _sources(self) -> Any:
        """Each document: (document_id, title, source, date, its chunks)."""
        for file in self._files():
            document_id = file.relative_to(self._folder).as_posix()
            title, chunks = self._chunks(file)
            yield document_id, title, document_id, "", chunks

    def _open(self) -> sqlite3.Connection:
        """The index, made again when the documents, or the settings it was made with, are not the ones now."""
        made_from = self._made_from()
        index = sqlite3.connect(self._index)
        index.execute("create table if not exists made_from (what text)")
        if index.execute("select what from made_from").fetchone() != (made_from,):
            index.execute("drop table if exists chunks")
            index.execute(f"create virtual table chunks using fts5(chunk_id unindexed, document_id unindexed, title{'' if self._title_indexed else ' unindexed'}, "
                          f"first unindexed, last unindexed, text, source unindexed, date unindexed, tokenize = '{TOKENIZERS[self._tokenizer]}')")
            for document_id, title, source, date, chunks in self._sources():
                index.executemany("insert into chunks values (?, ?, ?, ?, ?, ?, ?, ?)",
                                  [(f"{document_id}#{n}", document_id, title, first, last, text, source, date) for n, (first, last, text) in enumerate(chunks, 1)])
            index.execute("delete from made_from")
            index.execute("insert into made_from values (?)", (made_from,))
            index.commit()
        return index

    COLUMNS = "chunk_id, document_id, title, first, last, text, source, date"

    @staticmethod
    def _chunk(row: tuple[Any, ...]) -> dict[str, Any]:
        chunk_id, document_id, title, first, last, text, source, date, score = row
        chunk = {"document_id": document_id, "title": title, "source": source, "chunk_id": chunk_id,
                 "location": {"first_paragraph": first, "last_paragraph": last}, "text": text, "score": round(-score, 3) if score else 0}
        return chunk | {"date": date} if date else chunk

    def _best(self, index: sqlite3.Connection, match: str, limit: int, skip: set[str]) -> list[dict[str, Any]]:
        """The chunks that fit `match`, best first: not before `not_before`, and with the boost for a later date."""
        wanted = (int(limit) + len(skip)) * (4 if self._boost else 1)       # more than asked for, so that a later chunk can rise
        rows = index.execute(f"select {self.COLUMNS}, bm25(chunks) from chunks where chunks match ?" + (" and date >= ?" if self._not_before else "")
                             + " order by bm25(chunks) limit ?", (match, self._not_before, wanted) if self._not_before else (match, wanted)).fetchall()
        chunks = [self._chunk(row) for row in rows if row[0] not in skip]
        if self._boost:
            def day(chunk: dict[str, Any]) -> int | None:
                try:
                    return _datetime.date.fromisoformat(chunk.get("date", "")).toordinal()
                except ValueError:
                    return None
            days = [d for d in map(day, chunks) if d is not None]
            if days and max(days) > min(days):
                for chunk in chunks:
                    if day(chunk) is not None:
                        chunk["score"] = round(chunk["score"] * (1 + self._boost * (day(chunk) - min(days)) / (max(days) - min(days))), 3)
                chunks.sort(key=lambda chunk: -chunk["score"])
        return chunks[:int(limit)]

    def search(self, query: str, limit: int, exclude: list[str]) -> list[dict[str, Any]]:
        words = list(dict.fromkeys(re.findall(r"\w+", query.lower()))) if isinstance(query, str) else []
        if not words:
            return []
        index = self._open()
        try:                                                 # each word in quotes: a word is never read as an operator of the search
            return self._best(index, " OR ".join(f'"{word}"' for word in words), limit, set(exclude or []))
        finally:
            index.close()

    def get(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        wanted = [str(i) for i in chunk_ids or []]
        if not wanted:
            return []
        index = self._open()
        try:
            rows = {row[0]: row for row in index.execute(f"select {self.COLUMNS}, 0 from chunks "
                                                         f"where chunk_id in ({', '.join('?' * len(wanted))})", wanted).fetchall()}
        finally:
            index.close()
        return [self._chunk(rows[i]) for i in wanted if i in rows]

    def files(self) -> int:
        return len(self._files())

    def overview(self) -> str:
        return self._overview

    @staticmethod
    def _words(term: Any) -> list[str]:
        """A term is one word or several that follow each other ("supreme court"); anything else has no words."""
        return re.findall(r"\w+", term.lower()) if isinstance(term, str) and re.fullmatch(r"\s*\w+(\s+\w+)*\s*", term) else []

    def stats(self, terms: list[str]) -> dict[str, dict[str, Any]]:
        wanted = list(dict.fromkeys(t for t in terms or [] if self._words(t)))
        if not wanted:
            return {}
        index = self._open()
        try:
            chunks = index.execute("select count(*) from chunks").fetchone()[0]
            found = {}
            for term in wanted:
                count = index.execute("select count(*) from chunks where chunks match ?", (f'"{" ".join(self._words(term))}"',)).fetchone()[0]
                found[term] = {"count": count, "share": round(count / chunks, 6) if chunks else 0.0}
            return found
        finally:
            index.close()

    def search_terms(self, terms: list[str], limit: int, exclude: list[str], min_phrase_count: int) -> list[dict[str, Any]]:
        groups = [self._words(term) for term in terms or []]
        groups = [words for words in groups if words]
        if not groups:
            return []
        skip = set(exclude or [])
        index = self._open()
        try:
            parts = []
            for words in groups:
                phrase = '"' + " ".join(words) + '"'
                if len(words) > 1 and index.execute("select count(*) from chunks where chunks match ?", (phrase,)).fetchone()[0] < int(min_phrase_count):
                    phrase = "(" + " AND ".join(f'"{word}"' for word in words) + ")"      # too rare as a phrase: its words, all of them
                parts.append(phrase)
            return self._best(index, " OR ".join(parts), limit, skip)
        finally:
            index.close()

    def find(self, texts: list[str], terms: list[str]) -> list[list[list[Any]]]:
        """Each text is put in a table in memory with the index's tokenizer, and SQLite marks where a term matched."""
        wanted = list(dict.fromkeys(t for t in terms or [] if self._words(t)))
        texts = [text if isinstance(text, str) else "" for text in texts or []]
        found: list[list[list[Any]]] = [[] for _ in texts]
        table = sqlite3.connect(":memory:")
        try:
            table.execute(f"create virtual table texts using fts5(text, tokenize = '{TOKENIZERS[self._tokenizer]}')")
            table.executemany("insert into texts (rowid, text) values (?, ?)", list(enumerate(text.replace("\x01", " ").replace("\x02", " ") for text in texts)))
            for term in wanted:
                for row, marked in table.execute("select rowid, highlight(texts, 0, char(1), char(2)) from texts where texts match ?",
                                                 (f'"{" ".join(self._words(term))}"',)):
                    at, start = 0, 0
                    for piece in re.split("([\x01\x02])", marked):
                        if piece == "\x01":
                            start = at
                        elif piece == "\x02":
                            found[row].append([start, at, term])
                        else:
                            at += len(piece)
        finally:
            table.close()
        return [sorted(spans) for spans in found]


class DocumentDb(Documents):
    """Documents kept in a database that a loader script wrote, searched as the client of kind `documents` searches a
    folder: `client: {kind: document_db, database: ../../data/arxiv/cs.AI.sqlite, file: arxiv.sqlite, block_chars:
    2500, tokenizer: porter, overview: "...", not_before: "", recency_boost: 0.1}`.

    `database` is an SQLite file (a path relative to the project's folder, absolute, or beginning with `~`) with a
    table `documents(document_id, title, source, date, text, ...)`: one row for each document; `date` is
    year-month-day, or "" for none; `source` is where the document is found, a text. Nothing here knows who wrote
    the file (scripts/load_arxiv.py writes one). The index is the file `file` in the state folder, made at the first
    call and again when the database file changed: one file is looked at, not each document. A document's text is
    split into chunks as a file's is, and its title is searched as well as its text. The methods and a chunk are
    those of `documents`; a chunk also has `date` when its document has one.

    `not_before` (year-month-day, or "" for none): a search leaves out the documents dated before that day, and those
    with no date. `recency_boost` (0 for none): among the chunks a search finds, the score of the one dated last is
    raised by that share (0.1: by a tenth), the one dated first by nothing, the others in between; so of two chunks
    that fit about equally, the later comes first. Neither changes `get`, `stats` or `find`.
    """

    def __init__(self, tool: str, database: Path, index: Path, block_chars: Any, tokenizer: Any, overview: Any, not_before: Any,
                 recency_boost: Any) -> None:
        if not database.is_file():
            raise Problem("no_client", f"tool {tool}: the database {database} does not exist; a loader script writes it "
                                       f"(for arXiv abstracts: python scripts/load_arxiv.py --category cs.AI --since DATE)")
        if not (isinstance(block_chars, int) and block_chars > 0):
            raise Problem("no_client", f"tool {tool}: `block_chars` is a whole number above 0")
        if tokenizer not in TOKENIZERS:
            raise Problem("no_client", f"tool {tool}: `tokenizer` is {' or '.join(TOKENIZERS)}; `{tokenizer}` is neither")
        if not isinstance(overview, str):
            raise Problem("no_client", f"tool {tool}: `overview` is a text; \"\" for none")
        if not isinstance(not_before, str) or (not_before and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", not_before)):
            raise Problem("no_client", f"tool {tool}: `not_before` is a day written year-month-day, in quotes, or \"\" for none")
        if isinstance(recency_boost, bool) or not isinstance(recency_boost, (int, float)) or recency_boost < 0:
            raise Problem("no_client", f"tool {tool}: `recency_boost` is a number, 0 or above")
        self._tool, self._database, self._index, self._block_chars, self._tokenizer = tool, database, index, block_chars, tokenizer
        self._overview, self._not_before, self._boost, self._title_indexed = " ".join(overview.split()), not_before, float(recency_boost), True

    def _read(self, query: str) -> list[tuple[Any, ...]]:
        database = sqlite3.connect(f"file:{self._database}?mode=ro", uri=True)       # read only: this client never writes the loader's file
        try:
            return database.execute(query).fetchall()
        except sqlite3.Error as problem:
            raise Problem("no_client", f"tool {self._tool}: the database {self._database} has no table `documents` with the columns "
                                       f"document_id, title, source, date, text ({problem})") from None
        finally:
            database.close()

    def _made_from(self) -> str:
        made = self._database.stat()
        return json.dumps([INDEX_FORM, self._block_chars, self._tokenizer, "database", made.st_size, made.st_mtime_ns])

    def _sources(self) -> Any:
        for document_id, title, source, date, text in self._read("select document_id, title, source, date, text from documents"):
            yield str(document_id), " ".join(str(title or "").split()), str(source or document_id), str(date or ""), self._split(str(text or ""))

    def files(self) -> int:
        return int(self._read("select count(*) from documents")[0][0])


def read_json(text: str) -> tuple[Any, bool]:
    """(the JSON value in `text`, whether anything around it was taken away); (None, False) when there is none."""
    try:
        return json.loads(text), False
    except ValueError:
        pass
    for opening, closing in (("{", "}"), ("[", "]")):
        first, last = text.find(opening), text.rfind(closing)
        if 0 <= first < last:
            try:
                return json.loads(text[first:last + 1]), True
            except ValueError:
                pass
    return None, False


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _plain(inner) for key, inner in node.items()}
    if isinstance(node, list):
        return [_plain(inner) for inner in node]
    return node


KEYS = {"web": (), "scripted": ("answers",), "endpoint": ("prompts",), "imap": ("days", "max_messages", "authenticated_only", "timezone"),
        "calendar_file": ("file", "name"), "text_file": ("file",),
        "google_calendar": ("client_secret", "token", "calendar_name", "timezone", "mark", "file"), "clock": ("timezone",),
        "documents": ("folder", "extensions", "file", "block_chars", "tokenizer", "overview"),
        "document_db": ("database", "file", "block_chars", "tokenizer", "overview", "not_before", "recency_boost")}


def build_tools(project: Project, web: Callable[[str], Any], state: Path | str | None = None) -> dict[str, Any]:
    """One object for each tool of the setup in force. `web(tool name)` gives the object for a client of kind `web`.
    `state` is the state folder: a client's own log and files are there; a client that needs one is refused without."""
    tools: dict[str, Any] = {}
    state = Path(state) if state else None
    for name, spec in project.tools.items():
        client = spec["client"]
        kind = client.get("kind", "endpoint")
        if kind not in KEYS:
            raise Problem("no_client", f"NOT YET IMPLEMENTED: a client of kind `{kind}` (tool {name}). The kinds built "
                                       f"are {', '.join(k for k in KEYS if k != 'endpoint')} and an `endpoint`.")
        given = set(client) - {"kind", "endpoint"}
        if given != set(KEYS[kind]):
            raise Problem("no_client", f"tool {name}: a client of kind `{kind}` has the key(s) "
                                       f"{', '.join(KEYS[kind]) or 'none'}; this one has {', '.join(sorted(given)) or 'none'}")
        if kind in ("imap", "calendar_file", "text_file", "google_calendar", "documents", "document_db") and state is None:
            raise Problem("no_client", f"tool {name}: a client of kind `{kind}` keeps files in a state folder, and none was given")
        if kind == "endpoint":
            tools[name] = Endpoint(name, project.folder / str(client["endpoint"]), project.folder / str(client["prompts"]),
                                   state / "llm.jsonl" if state else None)
        elif kind == "web":
            tools[name] = web(name)
        elif kind == "scripted":
            tools[name] = Scripted(name, load_yaml(project.folder / str(client["answers"])))
        elif kind == "imap":
            if not (isinstance(client["days"], int) and isinstance(client["max_messages"], int) and isinstance(client["authenticated_only"], bool)):
                raise Problem("no_client", f"tool {name}: `days` and `max_messages` are whole numbers, `authenticated_only` is true or false")
            connect, where = imap_from_environment()
            tools[name] = Mailbox(name, connect, where, state, client["days"], client["max_messages"], client["authenticated_only"],
                                  client["timezone"], keep_open=True)                     # one login for a run, not one for each call
        elif kind == "clock":
            tools[name] = Clock(name, str(client["timezone"]))
        elif kind == "documents":
            tools[name] = Documents(name, project.folder / Path(str(client["folder"])).expanduser(), state / _name(name, client["file"]),
                                    client["extensions"], client["block_chars"], client["tokenizer"], client["overview"])
        elif kind == "document_db":
            tools[name] = DocumentDb(name, project.folder / Path(str(client["database"])).expanduser(), state / _name(name, client["file"]),
                                     client["block_chars"], client["tokenizer"], client["overview"], client["not_before"], client["recency_boost"])
        elif kind == "google_calendar":
            tools[name] = google_calendar(name, project, client, state)
        elif kind == "calendar_file":
            tools[name] = FileCalendar(state / _name(name, client["file"]), log=AccessLog(state / "calendar.jsonl"), name=str(client["name"]))
        else:
            tools[name] = TextFile(state / _name(name, client["file"]))
    return tools


def _name(tool: str, name: Any) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name.startswith("."):
        raise Problem("no_client", f"tool {tool}: `file` is the name of a file in the state folder, not a path")
    return name
