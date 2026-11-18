"""The web page (specification 2.13), on the demo setups."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from chaperone.errors import ConfigError
from chaperone import load_project, prerun
from chaperone.values import Problem
from chaperone.clients import Scripted, build_tools
from chaperone.web import Page, Session, WebClient, render, render_report

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
UI = {"labels": {"UNTRUSTED": {"words": "untrusted", "colour": "#b3261e"}, "CHECKED": {"words": "checked", "colour": "#1e7a1e"}},
      "styles": {"fields": {"layout": "one_part_per_line"}, "rows": {"layout": "table"}}, "default_style": "fields", "panes": []}
HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>&'


class Visitor:
    """A browser: it keeps the session's cookie and sends forms."""
    def __init__(self, session: Session) -> None:
        self.session, self.cookie = session, ""
        self.base = session.address.split("/?")[0]

    def request(self, path: str, form: dict[str, Any] | None = None, token: bool = True) -> tuple[int, str, Any]:
        data = None
        if form is not None:
            data = urllib.parse.urlencode({**({"token": self.session.token} if token else {}), **form}, doseq=True).encode()
        request = urllib.request.Request(self.base + path, data=data, headers={"Cookie": self.cookie} if self.cookie else {})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=5) as response:
                status, body, headers = response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as error:
            status, body, headers = error.code, error.read().decode(), error.headers
        if headers.get("Set-Cookie"):
            self.cookie = headers["Set-Cookie"].split(";")[0]
        if status == 303:
            return self.request(headers["Location"])
        return status, body, headers

    def enter(self) -> None:
        assert self.request("/?token=" + self.session.token)[0] == 200

    def wait_for(self, text: str, path: str = "/") -> str:
        for _ in range(100):
            body = self.request(path)[1]
            if text in body:
                return body
            time.sleep(0.05)
        raise AssertionError(f"{text!r} did not appear")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments: Any, **more: Any) -> None:
        return None


@pytest.fixture
def open_session(tmp_path: Path):
    sessions = []

    def make(name: str, *edits: tuple[str, str, str], files: dict[str, str] | None = None) -> tuple[Session, Visitor]:
        """The project copied, edited, and given `files` (path -> text), such as a setup `demo` of scripted tools."""
        shutil.copytree(EXAMPLES / "rules", tmp_path / "rules", dirs_exist_ok=True)
        folder = tmp_path / name
        shutil.copytree(EXAMPLES / name, folder, ignore=shutil.ignore_patterns("__pycache__"))
        for file, text in (files or {}).items():
            (folder / file).parent.mkdir(parents=True, exist_ok=True)
            (folder / file).write_text(text)
        for file, old, new in edits:
            text = (folder / file).read_text()
            assert old in text
            (folder / file).write_text(text.replace(old, new, 1))
        session = Session(load_project(folder, "demo"), tmp_path / "state")
        threading.Thread(target=session.serve_forever, daemon=True).start()
        sessions.append(session)
        return session, Visitor(session)
    yield make
    for session in sessions:
        session.close()


def element_id(body: str) -> str:
    return body.split('name="element" value="')[1].split('"')[0]


# ---- who may open the page -------------------------------------------------------------------------------------------

def test_the_page_opens_only_with_the_token(open_session) -> None:
    session, visitor = open_session("ask_my_mail")
    assert visitor.request("/")[0] == 403 and visitor.request("/?token=wrong")[0] == 403
    assert session.address.startswith("http://127.0.0.1:")
    visitor.enter()
    status, body, headers = visitor.request("/check")
    assert status == 200 and "possible violations" in body and "2. Exceptions approved at a start, not in the configuration" in body and "None: every exception in force is in the configuration." in body
    assert "script-src 'nonce-" in headers["Content-Security-Policy"] and "default-src 'none'" in headers["Content-Security-Policy"]
    assert visitor.request("/start", {"workflow": "ask_my_mail"}, token=False)[0] == 403           # a form without the token
    assert Visitor(session).request("/start", {"workflow": "ask_my_mail"})[0] == 403               # a form without the cookie
    assert visitor.request("/nowhere")[0] == 404


def test_nothing_starts_until_the_result_is_accepted_and_the_page_accepts_nothing(open_session) -> None:
    session, visitor = open_session("ask_my_mail")
    visitor.enter()
    assert "Nothing runs until the result of the pre-run check is accepted" in visitor.request("/start", {"workflow": "ask_my_mail"})[1]
    assert session.runs == {}
    assert "To accept: stop the program and start it again." in visitor.request("/check")[1]
    assert visitor.request("/accept", {"by": "user"})[0] == 404 and session.result()[1] is None      # the page accepts nothing
    prerun.accept(session.result()[0], session.state, "user")
    assert "Accepted by user" in visitor.request("/check")[1]
    visitor.request("/start", {"workflow": "ask_my_mail"})
    assert "ask_my_mail" in session.runs


def test_a_risk_acknowledged_at_the_start_lets_a_run_start_and_the_tab_lists_the_call_it_refused(tmp_path: Path) -> None:
    """share_notes ships with two possible violations, both the send by e-mail. Unaccepted, nothing runs. Acknowledged
    as risks at the start, a run starts, the send is refused, and the tab "Rules and exceptions" lists the risks and
    the refused call with the labels each argument carried."""
    project, state = load_project(EXAMPLES / "share_notes", "demo"), tmp_path / "state"
    session = Session(project, state)
    try:
        assert "Nothing runs until" in session.start("share_summary")
        answers = ["1", "4", "4", "user"]
        assert prerun.at_start(project, prerun.check(project), state, lambda prompt: answers.pop(0), lambda line: None, True)
        assert session.start("share_summary") == ""
        session.runs["share_summary"]["thread"].join(timeout=10)
        assert session.runs["share_summary"]["outcome"].state == "FAILED"                 # the refusal took the error route
        page = render(session, "check")
        assert "calls acknowledged as a risk: refused when made" in page and "acknowledged_risks.yaml" in page
        assert "Acknowledged risk: a run refuses this call when it is made." in page
        at = page.index("Calls refused in the runs since the program started")
        assert "None." not in page[at:at + 400]
        row = page[page.index("share_summary.MAIN", at):]
        assert "mail.send.message" in row[:200] and "body: LLM_WRITTEN, PERSONAL, UNTRUSTED" in row[:600] and "<td>yes</td>" in row[:700]
    finally:
        session.server.server_close()                                                         # never served: no loop to shut down


def test_the_page_is_served_on_this_machine_only_unless_another_address_is_named(tmp_path: Path) -> None:
    project = load_project(EXAMPLES / "ask_my_mail", "demo")
    local = Session(project, tmp_path / "a")
    everywhere = Session(project, tmp_path / "b", host="0.0.0.0")
    try:
        assert local.local_only and local.server.server_address[0] == "127.0.0.1"
        assert not everywhere.local_only and everywhere.server.server_address[0] == "0.0.0.0"
        assert everywhere.address.startswith("http://<this machine's address>:")
    finally:
        local.server.server_close()
        everywhere.server.server_close()


# ---- the three projects on the page ----------------------------------------------------------------------------------

def test_ask_my_mail_on_the_page(open_session) -> None:
    session, visitor = open_session("ask_my_mail")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    visitor.request("/start", {"workflow": "ask_my_mail"})
    body = visitor.wait_for("Keyword to search for?")
    assert "enter · fixed format" in body
    visitor.request("/answer", {"element": element_id(body), "field_keyword": "parcel", "act": "send"})
    body = visitor.wait_for("Your question?")
    assert "describe · free text" in body
    visitor.request("/answer", {"element": element_id(body), "text": "When does it arrive?", "act": "send"})
    body = visitor.wait_for("scheduled for Tuesday")
    assert 'class="chip lab0">untrusted</span>' in body and "personal" in body          # the answer with its labels
    state = json.loads(visitor.wait_for("ended in MAIN", "/state"))
    assert "mail.search" in state["side"] and session.runs["ask_my_mail"]["outcome"].ended == "end"


RAG_CHUNKS = [{"document_id": "federalist_49.txt", "title": "FEDERALIST No. 49", "source": "federalist_49.txt", "chunk_id": "federalist_49.txt#2",
               "location": {"first_paragraph": 2, "last_paragraph": 2}, "text": "the people are the only legitimate fountain of power", "score": 5.0},
              {"document_id": "federalist_51.txt", "title": "FEDERALIST No. 51", "source": "federalist_51.txt", "chunk_id": "federalist_51.txt#4",
               "location": {"first_paragraph": 4, "last_paragraph": 4}, "text": "Ambition must be made to counteract ambition.", "score": 4.0}]
RAG_REPLY = {"restated_query": "Where does power come from?", "answer_found": True, "relevant": [1],
             "snippets": [{"id": 1, "snippet": "the only legitimate fountain of power"}, {"id": 2, "snippet": "ambition must counteract ambition"}],
             "answer": "Power comes from the people."}


def scripted_rag(reply: dict[str, Any] = RAG_REPLY) -> dict[str, str]:
    """A setup `demo` for "safe retrieval", written by the test: the store and the LLM answer from files."""
    setup = (EXAMPLES / "safe_rag" / "setups" / "local.yaml").read_text()
    setup = re.sub(r"\{kind: documents, folder: corpus, [^}]*\}", "{kind: scripted, answers: demo/docs.yaml}", setup).replace("{endpoint: ../../configs/llm/local.yaml, prompts: prompts.yaml}",
                                                                               "{kind: scripted, answers: demo/llm.yaml}")
    assert setup.count("kind: scripted") == 2
    return {"setups/demo.yaml": setup, "demo/docs.yaml": json.dumps({"search": RAG_CHUNKS, "get": RAG_CHUNKS, "files": 2}),
            "demo/llm.yaml": json.dumps({"ask": json.dumps(reply)})}


def test_safe_retrieval_on_the_page_shows_the_llms_words_and_the_documents_text_by_their_labels(open_session) -> None:
    session, visitor = open_session("safe_rag", files=scripted_rag())
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    visitor.request("/start", {"workflow": "answer"})
    body = visitor.wait_for("Your question?")
    visitor.request("/answer", {"element": element_id(body), "text": "Where does power come from?", "act": "send"})
    body = visitor.wait_for("the only legitimate fountain of power")
    answer = body.split("Power comes from the people.")[1].split("</span></span>")[0]
    assert 'class="chip lab0">untrusted' in answer and "written by the LLM" in answer and "read by the check" in answer
    documents = body.split("the only legitimate fountain of power")[1].split("</span></span>")[0]
    assert 'class="chip lab3">from the documents' in documents and "untrusted" not in documents
    assert "ambition must counteract ambition" not in body and "Ambition must be made" not in body   # not verified, not named relevant
    assert "The marked text is in the documents, word for word." in body


def test_deliveries_on_the_page_choose_and_enter(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    visitor.request("/start", {"workflow": "assistant"})
    body = visitor.wait_for("What do you want to do?")
    visitor.request("/answer", {"element": element_id(body), "text": "track my UPS packages", "act": "send"})
    body = visitor.wait_for("Monitor mail from which of these senders?")
    assert "pkginfo@ups.example" in body and 'type="checkbox"' in body and "United Parcel Service" in body
    assert '<div class="sub"><span class="key">subjects</span><div>UPS Update: Package Scheduled for Delivery Thursday</div>' in body   # to tell senders apart
    visitor.request("/answer", {"element": element_id(body), "item": ["0", "1"], "act": "send"})
    body = visitor.wait_for("Anything else?")
    visitor.request("/answer", {"element": element_id(body), "text": "also add an address", "act": "send"})
    body = visitor.wait_for("Addresses to add, one per line")
    visitor.request("/answer", {"element": element_id(body), "field_addresses": "tracking@fedex.example\nfedex", "act": "send"})
    body = visitor.wait_for("is not an address")
    assert "addresses, line 2: “fedex” is not an address" in body and "tracking@fedex.example\nfedex</textarea>" in body
    assert session.store.get("MONITORED_SENDERS").data == ["pkginfo@ups.example", "mcinfo@ups.example"]      # nothing saved yet
    visitor.request("/answer", {"element": element_id(body), "field_addresses": "tracking@fedex.example", "act": "send"})
    body = visitor.wait_for("Monitor mail from which of the addresses entered?")       # entered is not yet monitored: the count first, then a tick
    assert "tracking@fedex.example" in body and "messages 0" in body
    assert session.store.get("MONITORED_SENDERS").data == ["pkginfo@ups.example", "mcinfo@ups.example"]
    visitor.request("/answer", {"element": element_id(body), "item": ["0"], "act": "send"})
    body = visitor.wait_for("Anything else?")
    assert session.store.get("MONITORED_SENDERS").data == ["pkginfo@ups.example", "mcinfo@ups.example", "tracking@fedex.example"]
    assert '<span class="key">addresses</span> <span class="val">tracking@fedex.example</span>' in body          # what was entered, as text, not as JSON
    visitor.request("/answer", {"element": element_id(visitor.request("/")[1]), "text": "that's all", "act": "send"})
    visitor.wait_for("ended in DONE", "/state")
    visitor.request("/start", {"workflow": "update_calendar"})                        # the unattended workflow
    side = json.loads(visitor.wait_for("writes the calendar.</div><div class=\\\"state\\\">ended in DONE", "/state"))["side"]
    assert "NEEDS_ATTENTION</span><span>1</span>" in side and "calendar.write_entry" in side
    assert "HANDLED_MESSAGES</span><span>4</span>" in side and "Activity · update_calendar" in side       # what the unattended run did
    order = [side.index(f"<strong>{name}</strong>") for name in ("assistant", "update_calendar", "reset_entries", "reset_all")]
    assert order == sorted(order) and "Unattended: reads new mail from the monitored senders" in side      # ui.yaml's order; each with its description
    assert "COPY_SCHEDULED · check.delivery_values · bad_values" in side and "the date is not in a form this check reads" in side
    assert "the date is not in a form" not in (session.state / "access.jsonl").read_text()                 # the log keeps the type only


def test_each_run_reads_a_scripted_tool_from_the_start_whatever_ran_before(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    counts = []
    for _ in range(2):                                        # a second run in the same program; before, it found no mail
        visitor.request("/start", {"workflow": "update_calendar"})
        for _ in range(200):
            if session.runs["update_calendar"]["outcome"] is not None:
                break
            time.sleep(0.05)
        log = (session.state / "access.jsonl").read_text()
        counts.append(log.count('"llm.classify_message"'))
    assert counts[0] == 4 and counts[1] == 8                  # each run classified the four made-up notices


def test_a_closed_element_returns_an_empty_answer(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    visitor.request("/start", {"workflow": "assistant"})
    visitor.request("/answer", {"element": element_id(visitor.wait_for("What do you want to do?")), "text": "search", "act": "send"})
    visitor.request("/answer", {"element": element_id(visitor.wait_for("Monitor mail from which")), "act": "close"})
    body = visitor.wait_for("Anything else?")
    assert "Closed." in body and session.store.get("MONITORED_SENDERS").data == []


# ---- escaping --------------------------------------------------------------------------------------------------------

def test_a_hostile_value_is_text_wherever_it_is_displayed(open_session) -> None:
    session, visitor = open_session("ask_my_mail", ("demo/llm.yaml", 'ask: "The parcel 1Z999AA10123456784 is scheduled for Tuesday."', "ask: '" + HOSTILE + "'"))
    visitor.enter()
    prerun.accept(session.result()[0], session.state, HOSTILE)
    assert "<script>alert" not in visitor.request("/check")[1] and "<img" not in visitor.request("/check")[1]
    visitor.request("/start", {"workflow": "ask_my_mail"})
    visitor.request("/answer", {"element": element_id(visitor.wait_for("Keyword")), "field_keyword": HOSTILE, "act": "send"})
    visitor.request("/answer", {"element": element_id(visitor.wait_for("Your question?")), "text": HOSTILE, "act": "send"})
    body = visitor.wait_for("&lt;script&gt;alert(1)&lt;/script&gt;")
    assert body.count("<script") == 1 and "<img" not in body and "onerror=alert(2)&gt;" in body      # the page's one script
    side = json.loads(visitor.request("/state")[1])["side"]
    assert "<script" not in side and "<img" not in side


def test_a_shown_value_is_escaped_in_every_position() -> None:
    report = {HOSTILE: [HOSTILE, {"k": HOSTILE}], "n": 3}
    for style in (None, "rows"):
        drawn = render_report(report, {HOSTILE: [["UNTRUSTED"], {"k": [HOSTILE]}]}, UI, style)
        assert "<script" not in drawn and "<img" not in drawn and "&lt;script&gt;" in drawn


def test_a_colour_that_is_not_a_colour_is_refused_when_the_project_is_loaded(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules")
    shutil.copytree(EXAMPLES / "ask_my_mail", tmp_path / "p", ignore=shutil.ignore_patterns("__pycache__"))
    ui = (tmp_path / "p" / "ui.yaml").read_text().replace('"#b3261e"', '"red;background:url(x)"')
    (tmp_path / "p" / "ui.yaml").write_text(ui)
    with pytest.raises(ConfigError, match="a colour is written"):
        load_project(tmp_path / "p")


# ---- how a value is drawn --------------------------------------------------------------------------------------------

def test_a_labelled_value_is_one_outline_and_a_box_of_one_kind_has_one_bar() -> None:
    mixed = render_report({"calling": "mail.search", "terms": ["UPS"]}, {"calling": [], "terms": [["UNTRUSTED"]]}, UI)
    assert '<span class="lv lab0"><span class="val">UPS</span><span class="chip lab0">untrusted</span></span>' in mixed
    assert '<span class="val">mail.search</span>' in mixed and "whole" not in mixed
    same = render_report(["a@x.example", "b@x.example"], [["CHECKED", "UNTRUSTED"], ["UNTRUSTED", "CHECKED"]], UI)
    assert 'class="report whole lab0"' in same and same.count("chip") == 2 and '"lv' not in same       # the labels once
    plain = render_report({"note": "Stopped."}, {}, UI)
    assert "chip" not in plain and "lv" not in plain
    table = render_report([{"id": 1, "url": "u"}, {"id": 2}], [], UI, "rows")
    assert "<table>" in table and table.count("<tr>") == 3


# ---- the elements, and the scripted tool -----------------------------------------------------------------------------

def test_an_element_waits_for_its_answer(tmp_path: Path) -> None:
    page = Page()
    client, got = WebClient(page), {}
    worker = threading.Thread(target=lambda: got.update(answer=client.choose([{"a": 1}, {"a": 2}, {"a": 3}], "Which?")))
    worker.start()
    while not page.pending:
        time.sleep(0.01)
    assert worker.is_alive()
    page.answer(page.pending[0]["id"], {"item": ["2", "0", "9", "x"]}, closed=False)
    worker.join(2)
    assert got["answer"] == [{"a": 1}, {"a": 3}] and page.pending == []
    with pytest.raises(Problem, match="the types are text, email_list"):
        client.enter("?", {"when": "date"})


def test_an_item_chosen_before_is_displayed_ticked_and_fixed_and_is_not_returned_again() -> None:
    page = Page()
    client, got = WebClient(page), {}
    items = [{"address": "a@x.example"}, {"address": "b@x.example"}, {"address": "c@x.example"}]
    worker = threading.Thread(target=lambda: got.update(answer=client.choose(items, "Which?", already=[items[1]])))
    worker.start()
    while not page.pending:
        time.sleep(0.01)
    from chaperone.web import _element
    drawn = _element(page.pending[0], "t")
    assert drawn.count('name="item"') == 2 and drawn.count("checked disabled") == 1
    assert "already chosen" in drawn
    page.answer(page.pending[0]["id"], {"item": ["0", "1"]}, closed=False)                # a forged tick of the fixed one
    worker.join(2)
    assert got["answer"] == [{"address": "a@x.example"}]


def test_the_state_tab_shows_stored_and_saved_values_with_their_labels_and_ui_yaml_removes_it(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    visitor.request("/start", {"workflow": "assistant"})
    body = visitor.wait_for("What do you want to do?")
    assert '<a href="/values">State</a>' in body
    visitor.request("/answer", {"element": element_id(body), "text": "track <b>UPS</b>", "act": "send"})
    body = visitor.wait_for("Monitor mail from which of these senders?")
    visitor.request("/answer", {"element": element_id(body), "item": ["0"], "act": "send"})
    visitor.wait_for("Anything else?")
    status, page, _ = visitor.request("/values")
    assert status == 200 and "Stored values" in page and "Saved by the steps of the run of assistant" in page
    for name in ("MONITORED_SENDERS", "FOUND_SENDERS", "HANDLED_MESSAGES", "new_senders", "reply"):
        assert f'<span class="mono">{name}</span>' in page
    assert "pkginfo@ups.example" in page and "monitored" in page and "track &lt;b&gt;UPS&lt;/b&gt;" in page and "<b>UPS</b>" not in page


def test_the_log_tab_has_every_error_with_its_message_and_what_the_llm_gave(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    assert "No run since the page started" in visitor.request("/log")[1] and "Files of this setup" in visitor.request("/log")[1]
    visitor.request("/start", {"workflow": "update_calendar"})
    visitor.wait_for("ended in DONE", "/state")
    page = visitor.request("/log")[1]
    assert "<td>bad_values</td><td>the date is not in a form this check reads</td>" in page and "&quot;date&quot;: &quot;delayed&quot;" in page
    assert "update_calendar · every call" in page and "location.reload" not in page and "side.innerHTML" not in page   # it is not drawn again: its text can be copied
    assert f"{session.state / 'access.jsonl'}</code></td><td>every call of every run" in page             # which file holds what
    held = visitor.request("/values")[1]
    assert "the date is not in a form this check reads" in held                            # and with the message set aside, in the store


def test_stop_ends_a_run_that_waits_for_the_user(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    visitor.request("/start", {"workflow": "assistant"})
    visitor.wait_for("What do you want to do?")
    assert 'action="/stop"' in json.loads(visitor.request("/state")[1])["side"]
    visitor.request("/stop", {"workflow": "assistant"})
    side = json.loads(visitor.wait_for("stopped by the user", "/state"))["side"]
    assert 'action="/start"' in side and session.runs["assistant"]["outcome"].ended == "stopped"
    assert "is not running" in visitor.request("/stop", {"workflow": "assistant"})[1]


def test_start_from_nothing_deletes_what_the_system_keeps_and_is_refused_while_a_run_is_in_progress(open_session) -> None:
    session, visitor = open_session("deliveries")
    visitor.enter()
    prerun.accept(session.result()[0], session.state, "user")
    (session.state / "senders.txt").write_text("mine@x.example\n")                       # the user's own file
    visitor.request("/start", {"workflow": "update_calendar"})
    visitor.wait_for("ended in DONE", "/state")
    assert {"store.json", "accepted.json", "access.jsonl"} <= {f.name for f in session.kept_files()}
    assert visitor.request("/reset", {})[0] == 404                                       # the box was not ticked
    visitor.request("/start", {"workflow": "assistant"})
    visitor.wait_for("What do you want to do?")
    assert "Not reset: assistant is running. Stop it first." in visitor.request("/reset", {"sure": "yes"})[1]
    visitor.request("/stop", {"workflow": "assistant"})
    visitor.wait_for("stopped by the user", "/state")
    said = visitor.request("/reset", {"sure": "yes"})[1]
    assert "Reset: " in said and "Nothing is accepted, monitored or handled." in said
    assert sorted(f.name for f in session.state.iterdir()) == ["senders.txt"]
    assert session.store.get("HANDLED_MESSAGES").data == [] and session.runs == {} and session.result()[1] is None
    assert "Nothing runs until the result of the pre-run check is accepted" in visitor.request("/start", {"workflow": "assistant"})[1]


def test_without_state_tab_there_is_no_such_tab_and_no_such_page(open_session) -> None:
    session, visitor = open_session("deliveries", ("ui.yaml", "state_tab: true", "state_tab: false"), ("ui.yaml", "reset_control: true", "reset_control: false"))
    visitor.enter()
    assert "/values" not in visitor.request("/")[1] and visitor.request("/values")[0] == 404
    assert visitor.request("/reset", {"sure": "yes"})[0] == 404


def test_the_scripted_tool_answers_in_three_forms() -> None:
    tool = Scripted("T", {"same": [1, 2], "turns": {"each_call": ["a", "b", None]}, "lookup": {"by_argument": {49: "x"}}})
    assert tool.same("anything") == [1, 2] and tool.same() == [1, 2]
    assert [tool.turns() for _ in range(4)] == ["a", "b", None, None]
    assert tool.lookup(49) == "x"
    with pytest.raises(Problem):
        tool.lookup(50)
    with pytest.raises(AttributeError, match="has no answer for `missing`"):
        tool.missing()


def test_a_client_that_is_not_built_yet_is_said_so(tmp_path: Path) -> None:
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules")
    shutil.copytree(EXAMPLES / "safe_rag", tmp_path / "safe_rag", ignore=shutil.ignore_patterns("__pycache__"))
    setup = tmp_path / "safe_rag" / "setups" / "local.yaml"
    setup.write_text(setup.read_text().replace("kind: documents", "kind: postgres"))
    with pytest.raises(Problem, match="NOT YET IMPLEMENTED: a client of kind `postgres`"):
        build_tools(load_project(tmp_path / "safe_rag"), lambda tool: None, tmp_path)


def test_news_of_a_run_is_displayed_only_when_ui_yaml_says_show_updates() -> None:
    for switch, expected in ((True, 1), (False, 0)):
        page = Page()
        WebClient(page, show_updates=switch).show({"message_found": "x"}, {}, None, True)
        assert len(page.transcript) == expected
    page = Page()
    WebClient(page).show({"note": "x"}, {})                                              # a show that is no news is always displayed
    assert len(page.transcript) == 1
