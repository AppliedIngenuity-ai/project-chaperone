"""The client of an LLM server (specification 2.6), the prompts file, the setup's note, and the assistant of
"deliveries" on such a client. The server here is a stand-in on this machine; one test asks a real one, and is
skipped unless CHAPERONE_LIVE_LLM names a model's settings file."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from chaperone.errors import ConfigError
from chaperone.connect.calendar_file import FileCalendar
from chaperone import ModuleError, load_project, prerun
from chaperone.values import Problem
from chaperone.clients import Endpoint, Scripted, build_tools, read_json
from chaperone.web import Session, render
from chaperone import yaml_lines
from chaperone.overrides import read_layer

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
PROMPTS = ('next_step:\n  answer: json\n  system: "Choose one."\n  user: "Offered: {options}. Sizes: {sizes}. The user wrote: {reply}"\n'
           'word:\n  answer: text\n  system: "One word."\n  user: "{text}"\n')


class Server:
    """A model server with the common chat interface: it gives the answers it was told to, in turn, and keeps
    what it was sent."""
    def __init__(self, *answers: str) -> None:
        self.answers, self.sent = list(answers), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments: Any) -> None:
                return

            def do_POST(self) -> None:
                outer.sent.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                answer = outer.answers.pop(0) if len(outer.answers) > 1 else outer.answers[0]
                data = json.dumps({"choices": [{"message": {"content": answer}, "finish_reason": "stop"}], "usage": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def settings(self, folder: Path, port: int | None = None) -> Path:
        file = folder / "model.yaml"
        file.write_text(f'name: "stand-in"\nprovider: openai\nmodel: "m"\napi_base: http://127.0.0.1:{port or self.server.server_address[1]}/v1\ntimeout: 5\n')
        return file


@pytest.fixture
def server():
    made = []

    def make(*answers: str) -> Server:
        made.append(Server(*answers))
        return made[-1]
    yield make
    for one in made:
        one.server.shutdown()


def endpoint(tmp_path: Path, served: Server, prompts: str = PROMPTS, port: int | None = None) -> Endpoint:
    (tmp_path / "prompts.yaml").write_text(prompts)
    return Endpoint("LLM", served.settings(tmp_path, port), tmp_path / "prompts.yaml", tmp_path / "llm.jsonl")


# ---- the client ------------------------------------------------------------------------------------------------------

def test_the_prompt_is_the_file_s_with_each_argument_in_its_place(tmp_path: Path, server) -> None:
    served = server('{"option": "done"}')
    tool = endpoint(tmp_path, served)
    assert tool.next_step(reply='we are {good} "now"', sizes={"found": 2}, options=["add", "done"]) == {"option": "done"}
    system, user = (m["content"] for m in served.sent[0]["messages"])
    assert system == "Choose one."
    assert user == 'Offered: ["add", "done"]. Sizes: {"found": 2}. The user wrote: we are {good} "now"'     # a text as written


def test_an_answer_with_text_around_its_json_is_taken_and_counted(tmp_path: Path, server) -> None:
    tool = endpoint(tmp_path, server('```json\n{"option": "add"}\n```', "I would search.", "  Blue. \n"))
    assert tool.next_step(reply="r", sizes={}, options=[]) == {"option": "add"}
    assert tool.next_step(reply="r", sizes={}, options=[]) == "I would search."          # no JSON: the text, for the module to refuse
    assert tool.word(text="a colour") == "Blue."
    calls = [json.loads(line)["call"] for line in (tmp_path / "llm.jsonl").read_text().splitlines()]
    assert calls == ["next_step", "llm_generous", "next_step", "llm_bad_response", "word"]


def test_the_log_of_the_client_holds_no_prompt_and_no_answer(tmp_path: Path, server) -> None:
    tool = endpoint(tmp_path, server('{"option": "search", "terms": ["SECRET-ANSWER"]}'))
    tool.next_step(reply="SECRET-REPLY", sizes={}, options=[])
    log = (tmp_path / "llm.jsonl").read_text()
    assert "SECRET" not in log and '"model": "stand-in"' in log


def test_a_server_that_gives_no_answer_is_the_error_llm_unavailable(tmp_path: Path, server) -> None:
    tool = endpoint(tmp_path, server("x"), port=9)                                       # nothing listens there
    with pytest.raises(ModuleError) as raised:
        tool.next_step(reply="r", sizes={}, options=[])
    assert raised.value.type == "llm_unavailable"


def test_what_the_client_refuses(tmp_path: Path, server) -> None:
    served = server("x")
    tool = endpoint(tmp_path, served)
    with pytest.raises(Problem, match="names {reply}, and the module passes no argument of that name"):
        tool.next_step(sizes={}, options=[])
    with pytest.raises(Problem, match="the arguments are passed by name"):
        tool.next_step("r", {}, [])
    with pytest.raises(AttributeError, match="has no prompt for `classify`"):
        tool.classify(message={})
    assert served.sent == []                                                             # nothing was sent
    for bad in ('next_step: {answer: xml, system: "s", user: "u"}\n', 'next_step: {answer: json, system: "s"}\n',
                'next_step: {answer: json, system: "s", user: "u", temperature: 1}\n'):
        with pytest.raises(Problem, match="a prompt has `answer`"):
            endpoint(tmp_path, served, bad)
    with pytest.raises(Problem, match="does not exist"):
        Endpoint("LLM", tmp_path / "none.yaml", tmp_path / "prompts.yaml")


def test_read_json() -> None:
    assert read_json('{"a": 1}') == ({"a": 1}, False)
    assert read_json('Here: {"a": [1, 2]} I hope it helps') == ({"a": [1, 2]}, True)
    assert read_json("nothing here {") == (None, False)


def test_the_scripted_tool_takes_arguments_by_name_too() -> None:
    tool = Scripted("T", {"next_step": {"each_call": ["a", "b"]}, "lookup": {"by_argument": {49: "x"}}})
    assert [tool.next_step(reply="r", sizes={}) for _ in range(3)] == ["a", "b", "b"]
    assert tool.lookup(id=49) == "x"


# ---- the module llm.next_step reads the answer generously ------------------------------------------------------------

def next_step(answer: Any, **options: Any) -> dict[str, Any]:
    project = load_project(EXAMPLES / "deliveries", "demo")
    tool = Scripted("T", {"next_step": answer})
    return project.declarations["llm.next_step"].function(tool, "a question", "a reply", {}, {"search": True, "clarify": True, "done": True, **options},
                                                          "Nothing to {option} now.")


def test_the_option_is_read_generously_and_one_not_offered_is_unusable() -> None:
    assert next_step({"option": ' "Done." '}) == {"option": "done"}
    assert next_step({"option": "search", "terms": " UPS "}) == {"option": "search", "terms": ["UPS"]}
    assert next_step({"option": "clarify", "message": " Which carrier? "}) == {"option": "clarify", "message": "Which carrier?"}
    assert next_step({"option": "remove"}, remove=0) == {"option": "clarify", "message": "Nothing to remove now."}   # exists, not offered: said
    assert next_step({"option": "remove"}) == {"option": "unusable"}                      # no such option in this conversation
    assert next_step({"option": "remove"}, remove=2) == {"option": "remove"}
    for unusable in ("done", ["done"], {"option": "delete_everything"}, {"option": None}, {"option": "search"},
                     {"option": "search", "terms": []}, {"option": "search", "terms": [1]}, {"option": "clarify", "message": " "}):
        assert next_step(unusable) == {"option": "unusable"}


# ---- the setups ------------------------------------------------------------------------------------------------------

def test_the_setup_demo_llm_differs_from_demo_by_the_client_of_the_llm_and_names_this_machine() -> None:
    demo, with_llm = load_project(EXAMPLES / "deliveries", "demo"), load_project(EXAMPLES / "deliveries", "demo_llm")
    assert {name for name in demo.tools if demo.tools[name] != with_llm.tools[name]} == {"LOCAL_LLM"}                    # the clock is made-up in both
    assert demo.tools["LOCAL_LLM"]["facts"] == with_llm.tools["LOCAL_LLM"]["facts"]
    assert prerun.check(with_llm).refused == [] and demo.lines == with_llm.lines
    client = with_llm.tools["LOCAL_LLM"]["client"]
    settings = (with_llm.folder / client["endpoint"]).read_text()
    assert "api_base: http://localhost:" in settings                                     # a shipped file names no other host
    assert (with_llm.folder / client["prompts"]).is_file()


def test_every_demo_setup_says_on_the_page_that_its_data_is_made_up(tmp_path: Path) -> None:
    for name, setup in (("ask_my_mail", "demo"), ("deliveries", "demo"), ("deliveries", "demo_llm"), ("share_notes", "demo")):
        project = load_project(EXAMPLES / name, setup)
        assert "ade-up data" in project.note, (name, setup)
        if setup == "demo":
            session = Session(project, tmp_path / name)
            assert f'<div class="setup-note">{project.note.replace(chr(34), "&quot;")[:40]}' in render(session, "talk")
            session.server.server_close()
    assert "The assistant is a script" in load_project(EXAMPLES / "deliveries", "demo").note
    assert "The LLM is the model server on this machine" in load_project(EXAMPLES / "deliveries", "demo_llm").note


def copy_of_deliveries(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules")
    shutil.copytree(EXAMPLES / "deliveries", tmp_path / "deliveries", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path / "deliveries"


def test_a_note_is_a_text_and_is_escaped_on_the_page(tmp_path: Path) -> None:
    folder = copy_of_deliveries(tmp_path)
    setup = folder / "setups" / "demo.yaml"
    lines = [line for line in setup.read_text().splitlines() if not line.startswith("note:")]
    setup.write_text("\n".join(lines + ["note: [a, b]"]) + "\n")
    with pytest.raises(ConfigError, match="`note` is a text"):
        load_project(folder, "demo")
    setup.write_text("\n".join(lines + ['note: "<script>alert(1)</script>"']) + "\n")
    session = Session(load_project(folder, "demo"), tmp_path / "state")
    page = render(session, "talk")
    session.server.server_close()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
    setup.write_text("\n".join(lines) + "\n")
    assert load_project(folder, "demo").note == ""                                       # no note: nothing is displayed


def test_a_client_of_an_llm_server_names_its_settings_and_its_prompts(tmp_path: Path) -> None:
    folder = copy_of_deliveries(tmp_path)
    setup = folder / "setups" / "demo_llm.yaml"
    setup.write_text(setup.read_text().replace(", prompts: prompts.yaml}", "}"))
    with pytest.raises(Problem, match="a client of kind `endpoint` has the key\\(s\\) prompts; this one has none"):
        build_tools(load_project(folder, "demo_llm"), lambda tool: None)


# ---- the assistant on the page, with an LLM server -------------------------------------------------------------------

def assistant(tmp_path: Path, served: Server, port: int | None = None) -> Session:
    folder = copy_of_deliveries(tmp_path)
    setup = folder / "setups" / "demo_llm.yaml"
    served.settings(tmp_path, port)
    setup.write_text(setup.read_text().replace("../../configs/llm/local.yaml", "../model.yaml"))
    project = load_project(folder, "demo_llm")
    session = Session(project, tmp_path / "state")
    prerun.accept(prerun.check(project), tmp_path / "state", "a test")
    assert session.start("assistant") == ""
    return session


def answer(session: Session, text: str) -> None:
    for _ in range(200):
        waiting = [e for e in session.page.pending if not e["done"]]
        if waiting:
            return session.page.answer(waiting[0]["id"], {"text": [text]}, closed=False)
        time.sleep(0.02)
    raise AssertionError("no element waits")


def ended(session: Session) -> Any:
    session.runs["assistant"]["thread"].join(10)
    return session.runs["assistant"]["outcome"]


def test_the_assistant_follows_what_the_llm_chooses(tmp_path: Path, server) -> None:
    served = server('{"option": "clarify", "message": "Which carrier?"}', 'Sure!\n{"option": "DONE"}')
    session = assistant(tmp_path, served)
    answer(session, "hmm")
    answer(session, "I think we are good!")
    outcome = ended(session)
    session.server.server_close()
    assert (outcome.ended, outcome.state) == ("end", "DONE")
    from chaperone.web import _side
    assert '<h2>LLM servers</h2>' in _side(session) and '<span class="mono">LOCAL_LLM</span><span>m</span>' in _side(session)   # the model's name
    assert "The user was asked: Which carrier?\nThe user wrote: I think we are good!" in served.sent[1]["messages"][1]["content"]
    assert any("Which carrier?" in json.dumps(entry, default=str) for entry in session.page.transcript)


def test_three_unusable_answers_end_the_conversation_and_say_so(tmp_path: Path, server) -> None:
    session = assistant(tmp_path, server("I cannot decide."))
    answer(session, "track my parcels")
    outcome = ended(session)
    session.server.server_close()
    assert (outcome.ended, outcome.state) == ("end", "FAILED_BAD_LLM")
    assert "could not be used 3 times" in json.dumps(session.page.transcript, default=str)


def test_a_server_that_is_down_ends_the_conversation_with_the_type_of_the_error(tmp_path: Path, server) -> None:
    session = assistant(tmp_path, server("x"), port=9)
    answer(session, "track my parcels")
    outcome = ended(session)
    session.server.server_close()
    assert (outcome.ended, outcome.state) == ("end", "FAILED")
    assert "llm_unavailable" in json.dumps(session.page.transcript, default=str)


@pytest.fixture
def users_changes():
    """Only in the live tests, which the user runs to check their own LLM: their changes in private/local.yaml (run from
    the folder project-chaperone), as the program reads them. The other tests never read it."""
    file = Path("private") / "local.yaml"
    yaml_lines.LAYER = read_layer(file) if file.is_file() else None
    yield
    yaml_lines.LAYER = None


@pytest.mark.skipif(not os.environ.get("CHAPERONE_LIVE_LLM"), reason="CHAPERONE_LIVE_LLM names no model's settings file")
def test_a_real_llm_chooses_the_step_for_plain_replies(tmp_path: Path, users_changes) -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    tool = Endpoint("LOCAL_LLM", ROOT / os.environ["CHAPERONE_LIVE_LLM"], EXAMPLES / "deliveries" / "prompts.yaml")
    choose = project.declarations["llm.next_step"].function
    options = {"search": True, "add": True, "clarify": True, "done": True, "choose": 2, "remove": 1, "review": 0}
    for reply, expected in (("I want to track my UPS packages", "search"), ("I think we are good!", "done"),
                            ("add United Parcel Service", "search"), ("lets add Fedex and UPS", "search"), ("add pkginfo@ups.example", "add"),
                            ("let me type an address myself", "add"), ("stop watching one of them", "remove")):
        assert choose(tool, "Anything else?", reply, {"found": 2, "monitored": 1, "attention": 0}, options, "no {option}")["option"] == expected, reply
    assert choose(tool, "Anything else?", "yeah", {"found": 2, "monitored": 1, "attention": 0}, options, "no {option}")["option"] == "clarify"


@pytest.mark.skipif(not os.environ.get("CHAPERONE_LIVE_LLM"), reason="CHAPERONE_LIVE_LLM names no model's settings file")
def test_a_real_llm_reads_the_made_up_mailbox(tmp_path: Path, users_changes) -> None:
    """One entry: written for the day and hours the first notice prints (11/12/2026, 1:15 PM to 5:15 PM), then marked
    delivered at the day and time the last notice prints (11/10/2026 at 2:14 PM). Nothing else."""
    project = load_project(EXAMPLES / "deliveries", "demo")
    tools = build_tools(project, lambda tool: None)
    tools["LOCAL_LLM"] = Endpoint("LOCAL_LLM", ROOT / os.environ["CHAPERONE_LIVE_LLM"], EXAMPLES / "deliveries" / "prompts.yaml")
    tools["GOOGLE_CALENDAR"] = calendar = FileCalendar(tmp_path / "calendar.ics")
    tools["SENDERS_FILE"] = Scripted("SENDERS_FILE", {"read_lines": ["pkginfo@ups.example"]})
    prerun.accept(prerun.check(project), tmp_path, "a test")
    run = prerun.open_run(project, tools, tmp_path)
    assert run.run("update_calendar").state == "DONE"
    made = calendar.entries()
    assert list(made) == ["1Z999AA10123456784"]
    assert (made["1Z999AA10123456784"]["start"], made["1Z999AA10123456784"]["end"]) == ("2026-11-10T14:14:00", "2026-11-10T14:44:00")
    history = [json.loads(line) for line in calendar.history.read_text().splitlines()]
    assert (history[0]["start"], history[0]["end"]) == ("2026-11-12T13:15:00", "2026-11-12T17:15:00")
    assert made["1Z999AA10123456784"]["title"] == "Delivered (ups.example) shipped from Example Outfitters, 1Z999AA10123456784"   # the shipper the first notice names, kept
