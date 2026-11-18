"""The clients for a mail server, a calendar file and a text file (chaperone/clients.py), on saved messages in a folder:
no mail account and no network."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

from chaperone.connect.eml_folder import connect_to_folder
from chaperone import ModuleError, load_project
from chaperone.values import Problem
from chaperone.clients import MAIL_VARIABLES, Mailbox, TextFile, build_tools

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def save(folder: Path, name: str, sender: str, subject: str, body: str, hours_ago: int, verified: bool = True) -> None:
    folder.mkdir(exist_ok=True)
    when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    results = "Authentication-Results: mx.example; dkim=pass; dmarc=pass\n" if verified else ""
    (folder / f"{name}.eml").write_text(f"Received: from a by b; {when}\n{results}From: Carrier <{sender}>\nTo: me@home.example\n"
                                        f"Subject: {subject}\nDate: {when}\nContent-Type: text/plain; charset=utf-8\n\n{body}\n")


@pytest.fixture
def mailbox(tmp_path: Path):
    mail = tmp_path / "mail"
    save(mail, "m1", "pkginfo@ups.example", "UPS Update: scheduled", "Scheduled Delivery: 11/12/2026. 1Z999AA10123456784", 30)
    save(mail, "m2", "pkginfo@ups.example", "UPS Update: delivered", "Delivered. 1Z999AA10123456784", 20)
    save(mail, "m3", "deals@shop.example", "20% off, free UPS shipping", "Buy now", 10)
    save(mail, "m4", "pkginfo@ups.example", "UPS Update: forged", "Scheduled Delivery: 11/13/2026", 5, verified=False)

    def make(authenticated_only: bool = True, days: int = 14) -> Mailbox:
        return Mailbox("MAILBOX", connect_to_folder(mail), "a folder", tmp_path / "state", days, 200, authenticated_only, "America/New_York")
    return make


def test_senders_are_counted_and_a_message_that_is_not_verified_is_left_out(mailbox, tmp_path: Path) -> None:
    box = mailbox()
    assert box.search_senders(["UPS"], 1) == [{"address": "deals@shop.example", "messages": 1, "subjects": ["20% off, free UPS shipping"]},
                                              {"address": "pkginfo@ups.example", "messages": 2, "subjects": ["UPS Update: delivered"]}]   # the newest
    assert [s["subjects"] for s in box.search_senders(["UPS"], 5)] == [["20% off, free UPS shipping"], ["UPS Update: delivered", "UPS Update: scheduled"]]
    assert [s["subjects"] for s in box.search_senders(["UPS"])] == [[], []]
    assert box.count_messages(["PKGINFO@ups.example", "nobody@x.example"]) == [{"address": "pkginfo@ups.example", "messages": 2},
                                                                              {"address": "nobody@x.example", "messages": 0}]
    assert mailbox(authenticated_only=False).count_messages(["pkginfo@ups.example"])[0]["messages"] == 3
    log = (tmp_path / "state" / "mail.jsonl").read_text()
    assert '"call": "not_verified"' in log and "Scheduled Delivery" not in log and "UPS Update" not in log     # ids and counts, no text


def test_fetch_next_returns_the_oldest_message_not_handled_and_keeps_nothing_itself(mailbox, tmp_path: Path) -> None:
    box = mailbox()
    first = box.fetch_next(["pkginfo@ups.example"], [])
    assert set(first) == {"id", "from", "received", "received_day", "received_time", "subject", "body", "links"}
    assert first["id"] == "m1" and first["from"] == "pkginfo@ups.example" and "Scheduled Delivery: 11/12/2026" in first["body"]
    assert box.fetch_next(["pkginfo@ups.example"], [])["id"] == "m1"                    # nothing is remembered by the client
    assert box.fetch_next(["pkginfo@ups.example"], ["m1"])["id"] == "m2"
    assert box.fetch_next(["pkginfo@ups.example"], ["m1", "m2"]) is None
    assert box.fetch_next([], []) is None                                               # no sender: nothing is asked of the server
    assert sorted(f.name for f in (tmp_path / "state").iterdir()) == ["mail.jsonl"]
    searches = [json.loads(line) for line in (tmp_path / "state" / "mail.jsonl").read_text().splitlines() if '"call": "recent"' in line]
    assert [s["returned"] for s in searches] == [3, 1, 0]     # one search served m1 and m2 (the third is not verified); then nothing new; then no sender


def test_the_day_received_is_the_day_in_the_time_zone_the_setup_names(tmp_path: Path) -> None:
    mail = tmp_path / "late"
    mail.mkdir()
    (mail / "m.eml").write_text("Received: from a by b; Wed, 11 Nov 2026 02:30:00 +0000\nAuthentication-Results: mx; dkim=pass\n"
                                "From: c@d.example\nSubject: s\nContent-Type: text/plain\n\nbody\n")
    days = {}
    for zone in ("America/New_York", "Europe/Berlin"):
        box = Mailbox("MAILBOX", connect_to_folder(mail), "a folder", tmp_path / zone.replace("/", "_"), 92, 200, True, zone)
        box._reader._now = lambda: datetime(2026, 11, 12, tzinfo=timezone.utc)
        got = box.fetch_next(["c@d.example"], [])
        days[zone] = (got["received_day"], got["received_time"])
    assert days == {"America/New_York": ("2026-11-10", "21:30"), "Europe/Berlin": ("2026-11-11", "03:30")}    # 02:30 UTC is the evening before in New York
    with pytest.raises(Problem, match="`timezone` is the name of a time zone in the tz database"):
        Mailbox("MAILBOX", connect_to_folder(mail), "a folder", tmp_path / "x", 14, 200, True, "Eastern")


def test_a_mail_server_that_cannot_be_reached_and_a_window_too_full_are_errors_a_workflow_can_route(mailbox, tmp_path: Path) -> None:
    gone = Mailbox("MAILBOX", connect_to_folder(tmp_path / "none"), "a folder", tmp_path / "state", 14, 200, True, "UTC")
    with pytest.raises(ModuleError) as raised:
        gone.search_senders(["UPS"])
    assert raised.value.type == "mail_unavailable"
    small = Mailbox("MAILBOX", connect_to_folder(tmp_path / "mail"), "a folder", tmp_path / "state", 14, 2, False, "UTC")
    with pytest.raises(ModuleError) as raised:
        small.count_messages(["pkginfo@ups.example"])
    assert raised.value.type == "too_much_mail"


def test_a_text_file_gives_its_lines_and_a_missing_one_gives_none(tmp_path: Path) -> None:
    assert TextFile(tmp_path / "senders.txt").read_lines() == []
    (tmp_path / "senders.txt").write_text("# my senders\n\n a@x.example \nb@y.example\n")
    assert TextFile(tmp_path / "senders.txt").read_lines() == ["a@x.example", "b@y.example"]


def copy_of_deliveries(tmp_path: Path, old: str = "", new: str = "") -> Any:
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules")
    shutil.copytree(EXAMPLES / "deliveries", tmp_path / "deliveries", ignore=shutil.ignore_patterns("__pycache__"))
    setup = tmp_path / "deliveries" / "setups" / "real_mail.yaml"
    assert old in setup.read_text()
    (tmp_path / "model.yaml").write_text('name: "m"\nprovider: openai\nmodel: "m"\napi_base: http://127.0.0.1:9/v1\n')
    setup.write_text(setup.read_text().replace(old, new).replace("../../configs/llm/local.yaml", "../model.yaml"))
    return load_project(tmp_path / "deliveries", "real_mail")


def test_the_setup_real_mail_is_refused_without_the_login_and_names_no_host(tmp_path: Path, monkeypatch) -> None:
    for name in MAIL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    project = load_project(EXAMPLES / "deliveries", "real_mail")
    with pytest.raises(Problem, match="the mail login is not in the environment: CHAPERONE_MAIL_HOST, CHAPERONE_MAIL_PORT, CHAPERONE_MAIL_USER, CHAPERONE_MAIL_PASSWORD not set"):
        build_tools(project, lambda tool: None, tmp_path)
    assert "host" not in (EXAMPLES / "deliveries" / "setups" / "real_mail.yaml").read_text().split("tools:")[1]
    assert project.note.startswith("Real: the mailbox (read only")


REFUSED = [
    ("days: 21, ", "", "a client of kind `imap` has the key(s) days, max_messages, authenticated_only, timezone; this one has authenticated_only, max_messages, timezone"),
    ("authenticated_only: true", "authenticated_only: 1", "`authenticated_only` is true or false"),
    ("file: deliveries.ics", "file: ../deliveries.ics", "`file` is the name of a file in the state folder, not a path"),
    ("{kind: text_file, file: senders.txt}", "{kind: text_file, path: senders.txt}", "a client of kind `text_file` has the key(s) file; this one has path"),
    ("kind: calendar_file, file: deliveries.ics, name: \"Deliveries (test)\"", "kind: outlook_calendar", "NOT YET IMPLEMENTED: a client of kind `outlook_calendar`"),
]


@pytest.mark.parametrize("old, new, reason", REFUSED, ids=[r[2][:40] for r in REFUSED])
def test_a_client_written_wrongly_is_refused_with_its_keys(tmp_path: Path, monkeypatch, old: str, new: str, reason: str) -> None:
    for name, value in zip(MAIL_VARIABLES, ("127.0.0.1", "1", "u", "p")):
        monkeypatch.setenv(name, value)
    with pytest.raises(Problem) as refused:
        build_tools(copy_of_deliveries(tmp_path, old, new), lambda tool: None, tmp_path / "state")
    assert reason in str(refused.value)


def test_the_calendar_of_real_mail_is_a_file_in_the_state_folder_with_the_name_the_setup_gives(tmp_path: Path, monkeypatch) -> None:
    for name, value in zip(MAIL_VARIABLES, ("127.0.0.1", "1", "u", "p")):
        monkeypatch.setenv(name, value)
    tools = build_tools(copy_of_deliveries(tmp_path), lambda tool: None, tmp_path / "state")      # building logs in nowhere
    calendar = tools["GOOGLE_CALENDAR"]
    assert calendar.name == "Deliveries (test)" and calendar.path == tmp_path / "state" / "deliveries.ics"
    calendar.upsert("K", title="Delivery K", start="2026-11-12T08:00:00", end="2026-11-12T17:00:00")
    assert "Delivery K" in (tmp_path / "state" / "deliveries.ics").read_text()
    assert json.loads((tmp_path / "state" / "calendar.jsonl").read_text())["outcome"] == "created"


# ---- Google Calendar, on the stand-in for Google of tests/test_google_calendar.py: no network ---------------------------

def stand_in_for_google() -> Any:
    import importlib.util
    spec = importlib.util.spec_from_file_location("stand_in_google", Path(__file__).resolve().parents[1] / "tests" / "test_google_calendar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeGoogle()


def google_tool(tmp_path: Path, google: Any, signed_in: bool = True) -> Any:
    from chaperone.clients import google_calendar
    (tmp_path / "client.json").write_text(json.dumps({"installed": {"client_id": "id-1", "client_secret": "secret-1",
                                                                     "auth_uri": "https://accounts.example/auth", "token_uri": "https://oauth.example/token"}}))
    if signed_in:
        (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "rt-1"}))
    project = type("P", (), {"folder": tmp_path})()
    client = {"kind": "google_calendar", "client_secret": "client.json", "token": "token.json", "calendar_name": "Deliveries (test)",
              "timezone": "America/New_York", "mark": "deliveries_test", "file": "google_deliveries"}
    return google_calendar("GOOGLE_CALENDAR", project, client, tmp_path / "state", fetch=google)


def test_update_calendar_writes_to_one_google_calendar_of_its_own_and_the_reset_workflows_remove_its_entries(tmp_path: Path) -> None:
    from tests.test_engine import NUMBER, Page, Scripted, delivery_tools, letter, start
    google = stand_in_for_google()
    calendar = google_tool(tmp_path, google)
    mail = [letter(1, f"{NUMBER} comes Thursday 11/12/2026"), letter(2, f"{NUMBER} was delivered"), None]
    page = Page()
    tools = delivery_tools(page, [], calendar=calendar, file=["notices@carrier.example"], mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(classify=lambda subject, body: "delivered" if "was delivered" in body else "scheduled",
                                        copy_values=lambda subject, body: {"tracking_number": NUMBER, "date": "11/12/2026"}))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("update_calendar").state == "DONE"
    assert [c["summary"] for c in google.calendars.values()] == ["Deliveries (test)"]                  # one calendar, by the setup's name
    (event,) = google.events.values()
    assert event["summary"] == f"Delivered (carrier.example) {NUMBER}" and event["start"] == {"dateTime": "2026-11-10T08:45:00", "timeZone": "America/New_York"}
    assert re.search(r"From notices@carrier\.example, received 2026-11-10 \d\d:\d\d, message 2\.", event["description"])
    assert "Agent mark: deliveries_test/" in event["description"] and "To remove every entry" not in event["description"]   # no command is named
    page.answers.append(lambda items: items)
    assert run.run("reset_entries").state == "CLEAR"
    assert google.events == {} and calendar.entries() == {} and len(google.calendars) == 1             # the entries go; the calendar stays
    assert not [call for call in google.calls if call[0] == "GET"]                                     # it never lists or reads what is on Google


def test_google_not_signed_in_is_said_before_anything_runs_and_google_not_answering_is_an_error_a_workflow_routes(tmp_path: Path) -> None:
    google = stand_in_for_google()
    with pytest.raises(Problem, match="not signed in to Google: .*token.json does not exist. Sign in once: `python -m chaperone signin "):
        google_tool(tmp_path, google, signed_in=False)
    calendar = google_tool(tmp_path, google)
    google.refresh_tokens.clear()                                                        # Google no longer accepts the stored token
    real_call = google.__call__

    def refusing(request: Any) -> bytes:
        if request.full_url.endswith("/token"):
            import io
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, 400, "invalid_grant", {}, io.BytesIO(b"{}"))
        return real_call(request)
    calendar.auth._fetch = refusing
    with pytest.raises(ModuleError) as raised:
        calendar.upsert("K", title="t", start="2026-11-12T08:00:00", end="2026-11-12T17:00:00", description="")
    assert raised.value.type == "calendar_sign_in" and "Sign in again: `python -m chaperone signin " in raised.value.message
    calendar.auth._fetch = google
    google.refresh_tokens.add("rt-1")
    google.fail_with = 503
    with pytest.raises(ModuleError) as raised:
        calendar.upsert("K", title="t", start="2026-11-12T08:00:00", end="2026-11-12T17:00:00", description="")
    assert raised.value.type == "calendar_unavailable"


def test_the_setup_real_names_google_for_the_calendar_and_private_files_for_the_sign_in() -> None:
    real, with_file = load_project(EXAMPLES / "deliveries", "real"), load_project(EXAMPLES / "deliveries", "real_mail")
    assert {name for name in real.tools if real.tools[name] != with_file.tools[name]} == {"GOOGLE_CALENDAR"}
    client = real.tools["GOOGLE_CALENDAR"]["client"]
    assert client["kind"] == "google_calendar" and client["calendar_name"].strip()          # the name is the user's own
    assert client["client_secret"].startswith("../../private/") and client["token"].startswith("../../private/")


def test_the_signin_command_says_what_is_missing(tmp_path: Path, capsys) -> None:
    from chaperone.__main__ import main
    assert main(["signin", str(EXAMPLES / "deliveries"), "--setup", "demo"]) == 2
    assert "has no tool whose client is of kind google_calendar" in capsys.readouterr().out
    project = copy_of_deliveries(tmp_path)                                               # a copy: its private files do not exist
    setup = project.folder / "setups" / "real.yaml"
    setup.write_text(setup.read_text().replace("../../private/", "../none/").replace("../../configs/llm/local.yaml", "../model.yaml"))
    assert main(["signin", str(project.folder), "--setup", "real"]) == 2
    assert "google_client_secret.json does not exist: the OAuth client file" in capsys.readouterr().out


def test_each_setup_has_its_own_state_folder_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    """Two setups of one project do not share a store: what one handled is not handled for the other."""
    from chaperone.__main__ import main
    monkeypatch.chdir(tmp_path)
    for setup in ("demo", "demo_llm"):
        assert main(["accept", str(EXAMPLES / "deliveries"), "--setup", setup, "--by", "a test"]) == 0
    capsys.readouterr()
    assert sorted(str(f.relative_to(tmp_path)) for f in tmp_path.rglob("accepted.json")) == ["state/deliveries/demo/accepted.json",
                                                                                           "state/deliveries/demo_llm/accepted.json"]


# ---- the documents of a folder, searched by their words ---------------------------------------------------------------

def documents(tmp_path: Path, block_chars: int = 60, tokenizer: str = "porter", extensions: Any = (".txt", ".md")):
    from chaperone.clients import Documents
    folder = tmp_path / "notes"
    (folder / "deep").mkdir(parents=True, exist_ok=True)
    (folder / ".obsidian").mkdir(exist_ok=True)
    (folder / "paper_10.txt").write_text("Paper ten\n\nFactions are sown in the nature\nof man.\n\nExtend the sphere.\n\n" + "A long paragraph that is never cut, " * 4 + "\n")
    (folder / "deep" / "paper_51.md").write_text("# Paper 51\n\n\nAmbition must be made to counteract ambition.\n\n\nIf men were angels, no government would be necessary.")
    (folder / "notes.rtf").write_text("ambition ambition ambition")                         # not an extension named: not read
    (folder / ".obsidian" / "cache.md").write_text("ambition ambition")                     # a name beginning with a dot: not read
    return Documents("DOCS_DB", folder, tmp_path / "documents.sqlite", list(extensions), block_chars, tokenizer), folder


def test_documents_are_read_as_chunks_and_the_best_chunks_for_the_words_are_returned(tmp_path: Path) -> None:
    docs, folder = documents(tmp_path)
    assert docs.files() == 2
    found = docs.search("What must ambition counteract?", 5, [])
    assert [c["chunk_id"] for c in found] == ["deep/paper_51.md#1"]
    assert found[0] | {"score": 0} == {"document_id": "deep/paper_51.md", "title": "# Paper 51", "source": "deep/paper_51.md",
                                       "chunk_id": "deep/paper_51.md#1", "location": {"first_paragraph": 1, "last_paragraph": 2},
                                       "text": "# Paper 51 Ambition must be made to counteract ambition.", "score": 0}
    assert found[0]["score"] > 0                                                           # higher is better
    # paragraphs that follow each other are joined up to block_chars; line breaks are one space; a long paragraph is whole
    sphere, = docs.search("sphere", 5, [])
    assert sphere["text"] == "Extend the sphere." and sphere["location"] == {"first_paragraph": 3, "last_paragraph": 3}
    first, = docs.search("nature", 5, [])                                                  # the title is the file's first paragraph
    assert first["text"] == "Paper ten Factions are sown in the nature of man." and first["title"] == "Paper ten"
    long, = docs.search("paragraph", 5, [])
    assert len(long["text"]) > 60 and long["chunk_id"] == "paper_10.txt#3"
    assert docs.search("", 5, []) == [] and docs.search("zebra", 5, []) == [] and docs.search('"; drop table chunks; -- OR NEAR(', 5, []) == []


def test_the_best_chunk_comes_first_and_seen_chunks_are_left_out(tmp_path: Path) -> None:
    docs, folder = documents(tmp_path)
    found = docs.search("angels government ambition", 5, [])
    assert [c["chunk_id"] for c in found] == ["deep/paper_51.md#2", "deep/paper_51.md#1"] and found[0]["score"] > found[1]["score"]
    assert [c["chunk_id"] for c in docs.search("angels government ambition", 1, [])] == ["deep/paper_51.md#2"]
    assert [c["chunk_id"] for c in docs.search("angels government ambition", 5, ["deep/paper_51.md#2"])] == ["deep/paper_51.md#1"]


def test_a_chunk_is_read_back_by_its_id(tmp_path: Path) -> None:
    docs, folder = documents(tmp_path)
    assert [c["chunk_id"] for c in docs.get(["paper_10.txt#2", "missing#1", "deep/paper_51.md#2"])] == ["paper_10.txt#2", "deep/paper_51.md#2"]
    assert docs.get(["paper_10.txt#2"])[0]["text"] == "Extend the sphere." and docs.get([]) == []


def test_front_matter_is_not_the_title_and_dot_folders_are_not_walked(tmp_path: Path) -> None:
    docs, folder = documents(tmp_path)
    (folder / "meeting.md").write_text("---\ntags: [x]\n---\n\n# Meeting notes\n\nWe chose the zebra.")
    zebra, = docs.search("zebra", 5, [])
    assert zebra["title"] == "# Meeting notes" and zebra["text"].startswith("--- tags: [x] --- # Meeting notes")
    assert docs.files() == 3                                                               # .obsidian/cache.md is not among them


def test_the_tokenizer_porter_finds_a_word_by_its_stem(tmp_path: Path) -> None:
    porter, _ = documents(tmp_path / "a")
    plain, _ = documents(tmp_path / "b", tokenizer="unicode61")
    assert [c["chunk_id"] for c in porter.search("faction", 5, [])] == ["paper_10.txt#1"]
    assert plain.search("faction", 5, []) == [] and len(plain.search("factions", 5, [])) == 1


def test_the_index_is_made_again_when_a_file_of_the_folder_changes(tmp_path: Path) -> None:
    docs, folder = documents(tmp_path)
    assert docs.search("zebra", 5, []) == []
    (folder / "new.txt").write_text("A zebra crossing.")
    assert [c["chunk_id"] for c in docs.search("zebra", 5, [])] == ["new.txt#1"]
    (folder / "new.txt").unlink()
    assert docs.search("zebra", 5, []) == []


def test_a_folder_outside_the_project_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`folder` may be absolute or begin with ~; the project's setup names it."""
    shutil.copytree(EXAMPLES / "rules", tmp_path / "project" / "rules")
    shutil.copytree(EXAMPLES.parent / "configs" / "llm", tmp_path / "configs" / "llm")      # the setup names ../../configs/llm/...
    shutil.copytree(EXAMPLES / "safe_rag", tmp_path / "project" / "safe_rag", ignore=shutil.ignore_patterns("__pycache__", "corpus"))
    _, notes = documents(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    setup = tmp_path / "project" / "safe_rag" / "setups" / "local.yaml"
    for written in ("~/notes", str(notes)):
        setup.write_text((EXAMPLES / "safe_rag" / "setups" / "local.yaml").read_text().replace("folder: corpus", f"folder: {written}"))
        tools = build_tools(load_project(tmp_path / "project" / "safe_rag"), lambda tool: None, tmp_path / "state")
        assert tools["DOCS_DB"].files() == 2, written


def test_a_documents_client_needs_a_folder_with_a_file_it_reads_and_its_settings(tmp_path: Path) -> None:
    from chaperone.clients import Documents
    (tmp_path / "empty").mkdir()
    for folder in (tmp_path / "empty", tmp_path / "missing"):
        with pytest.raises(Problem, match="does not exist or has no file ending in .txt or .md"):
            Documents("DOCS_DB", folder, tmp_path / "d.sqlite", [".txt", ".md"], 1200, "porter")
    with pytest.raises(Problem, match="whole number above 0"):
        documents(tmp_path, block_chars=0)
    with pytest.raises(Problem, match="`tokenizer` is porter or unicode61"):
        documents(tmp_path, tokenizer="porter'); drop table chunks; --")
    with pytest.raises(Problem, match="a list of file endings"):
        documents(tmp_path, extensions=["md"])
