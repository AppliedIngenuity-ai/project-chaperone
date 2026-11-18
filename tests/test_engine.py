"""The call check (C1 to C4) and the workflow engine (2.12).

"Ask my mail" and "safe retrieval" run end to end with scripted tools; the rows of the specification's tables and
its "refused call" bullets are assertions. The mechanics of 2.12 are tested on a project of three modules.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from chaperone.errors import ConfigError
from chaperone.connect.calendar_file import FileCalendar
from chaperone import ModuleError, Outcome, Run, Store, Value, load_project

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


# ---- scripted tools ----------------------------------------------------------------------------------------------

class Browser:
    def __init__(self, *answers: Any) -> None:
        self.answers, self.shown, self.updates = list(answers), [], []

    def describe(self, prompt: str) -> Any:
        return self.answers.pop(0) if self.answers else ""           # a box closed returns an empty text

    def enter(self, prompt: str, fields: dict) -> Any:
        return self.answers.pop(0)

    def show(self, report: Any, marks: Any, style: Any, update: bool = False) -> None:
        (self.updates if update else self.shown).append((report, marks))


class Scripted:
    """A tool whose every method returns the next scripted answer and records that it was called."""
    def __init__(self, **answers: Any) -> None:
        self.answers, self.calls = answers, []

    def __getattr__(self, method: str) -> Any:
        def call(*arguments: Any, **named: Any) -> Any:
            self.calls.append((method, arguments + tuple(named.values())))
            answer = self.answers[method]
            return answer(*arguments, **named) if callable(answer) else answer
        return call


def copy_of(tmp_path: Path, name: str, *edits: tuple[str, str, str]) -> Path:
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules", dirs_exist_ok=True)
    project = tmp_path / name
    shutil.copytree(EXAMPLES / name, project, ignore=shutil.ignore_patterns("__pycache__"))
    for file, old, new in edits:
        text = (project / file).read_text()
        assert old in text, f"{file} does not contain {old!r}"
        (project / file).write_text(text.replace(old, new, 1))
    return project


def start(folder: Path, tmp_path: Path, tools: dict[str, Any]) -> Run:
    project = load_project(folder)
    return Run(project, tools, Store(tmp_path / "state" / "store.json", project.stores), tmp_path / "state" / "access.jsonl")


def entry(run: Run, call: str, nth: int = 0) -> dict[str, Any]:
    return [e for e in run.log if e["call"] == call][nth]


def permits(run: Run, call: str, nth: int = 0) -> dict[str, Any]:
    return {f["rule"]: f["permitted_by"] for f in entry(run, call, nth).get("forbidden_by", [])}


# ---- Example A: ask my mail ----------------------------------------------------------------------------

MESSAGES = [{"from": "pkginfo@ups.example", "body": "Your parcel 1Z999 arrives Tuesday"}]


def mail_tools() -> dict[str, Any]:
    return {"BROWSER": Browser({"keyword": "parcel"}, "When does it arrive?"),
            "MAILBOX": Scripted(search=MESSAGES), "LOCAL_LLM": Scripted(ask="Tuesday.")}


def test_ask_my_mail_runs_and_every_call_is_checked_and_labelled(tmp_path: Path) -> None:
    tools = mail_tools()
    run = start(EXAMPLES / "ask_my_mail", tmp_path, tools)
    outcome = run.run("ask_my_mail")
    assert outcome.ended == "end"
    assert entry(run, "user.enter")["result"] == [] and entry(run, "user.describe")["result"] == []
    assert permits(run, "mail.search") == {"NOTHING_LEAVES_NETWORK": "MAILBOX_APPROVED_EX"}
    assert entry(run, "mail.search")["result"] == ["PERSONAL", "UNTRUSTED"]                      # P1
    assert permits(run, "llm.answer") == {}                                    # an LLM is not OUTPUT: no rule forbids the call
    assert entry(run, "llm.answer")["result"] == ["PERSONAL", "UNTRUSTED"]                       # P1 and P2
    assert permits(run, "user.show") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "SHOW_TO_USER_EX", "NO_PERSONAL_DATA_OUTPUT": "PERSONAL_TO_USER_EX"}
    assert tools["BROWSER"].shown == [("Tuesday.", ["PERSONAL", "UNTRUSTED"])]                   # the answer with its marks
    assert tools["MAILBOX"].calls == [("search", ("parcel",))]


def test_the_access_log_holds_names_and_labels_and_never_a_value(tmp_path: Path) -> None:
    run = start(EXAMPLES / "ask_my_mail", tmp_path, mail_tools())
    run.run("ask_my_mail")
    written = (tmp_path / "state" / "access.jsonl").read_text()
    assert len(written.splitlines()) == len(run.log) == 6
    for secret in ("parcel", "1Z999", "Tuesday", "pkginfo", "When does"):
        assert secret not in written


def test_a_remote_llm_is_refused_before_it_is_called(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "ask_my_mail",
                     ("setups/demo.yaml", "modules:", "  REMOTE_LLM: {client: {endpoint: configs/llm/vendor.yaml}, facts: {OUTPUT: true, LEAVES_NETWORK: true, adds: [UNTRUSTED]}}\nmodules:"),
                     ("setups/demo.yaml", "llm.answer: {tool: LOCAL_LLM}", "llm.answer: {tool: REMOTE_LLM}"))
    tools = mail_tools() | {"REMOTE_LLM": Scripted(ask="never")}
    run = start(folder, tmp_path, tools)
    outcome = run.run("ask_my_mail")
    assert (outcome.ended, outcome.state) == ("end", "FAILED")                                    # the refusal took the error route
    assert "forbidden by NOTHING_LEAVES_NETWORK" in run.reasons[max(run.reasons)]["message"]
    assert tools["REMOTE_LLM"].calls == [] and tools["BROWSER"].shown == []
    assert entry(run, "llm.answer")["error"] == {"type": "refused", "labels": ["PERSONAL", "UNTRUSTED"]}      # P6


def test_sending_the_answer_by_mail_is_refused(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "ask_my_mail",
                     ("modules.py", "@chaperone.module(\"user.show\"", "@chaperone.module(\"mail.send\", properties={\"DESTRUCTIVE\": False, \"OUTPUT\": True})\ndef mail_send(ctx, text):\n    ctx.send(text)\n\n\n@chaperone.module(\"user.show\""),
                     ("workflows/ask_my_mail.yaml", "  - {call: labels_of,", "  - {call: mail.send, with: {text: \"{vars.answer}\"}}\n      - {call: labels_of,"))
    tools = mail_tools()
    run = start(folder, tmp_path, tools)
    assert run.run("ask_my_mail").state == "FAILED"
    message = run.reasons[max(run.reasons)]["message"]
    assert entry(run, "mail.send")["error"]["type"] == "refused" and "NO_UNTRUSTED_DATA_TO_OUTPUT" in message
    assert "the argument text, which carries UNTRUSTED" in message
    assert not any(method == "send" for method, _ in tools["MAILBOX"].calls)


def test_a_rule_on_personal_data_with_its_exception_refuses_nothing(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "ask_my_mail",
                     ("rules.yaml", "imports: [../rules/default.yaml]", "imports: [../rules/default.yaml]\nrules:\n  - {id: NO_PERSONAL_ANYWHERE, never: {call: any, carrying: {PERSONAL: true}}}"),
                     ("exceptions.yaml", "exceptions:", "exceptions:\n  - {id: PERSONAL_HERE, rule: NO_PERSONAL_ANYWHERE, except: {call: {where: [{BROWSER: true}, {LOCAL_LLM: true}]}}}"))
    run = start(folder, tmp_path, mail_tools())
    assert run.run("ask_my_mail").ended == "end"
    assert permits(run, "llm.answer")["NO_PERSONAL_ANYWHERE"] == "PERSONAL_HERE"


def test_an_error_of_a_module_continues_in_the_error_state_and_is_recorded(tmp_path: Path) -> None:
    def outage(keyword: str) -> Any:
        raise ConnectionError("mail.example refused the connection")
    tools = mail_tools() | {"MAILBOX": Scripted(search=outage)}
    run = start(EXAMPLES / "ask_my_mail", tmp_path, tools)
    outcome = run.run("ask_my_mail")
    assert (outcome.ended, outcome.state) == ("end", "FAILED")
    failed = entry(run, "mail.search")
    assert failed["error"]["type"] == "module_failed" and failed["state"] == "MAIN"
    assert run.reasons[max(run.reasons)]["detail"] == {"python": "ConnectionError", "text": "mail.example refused the connection"}
    assert set(failed["error"]["labels"]) == {"UNTRUSTED", "PERSONAL"}                            # the module's adds
    assert run.log[-1]["error"]["type"] == "module_failed"


# ---- Example C: retrieval with verified excerpts ----------------------

def chunk(document: int, number: int, text: str, score: float) -> dict[str, Any]:
    return {"document_id": f"federalist_{document}.txt", "title": f"FEDERALIST No. {document}", "source": f"federalist_{document}.txt",
            "chunk_id": f"federalist_{document}.txt#{number}", "location": {"first_paragraph": number, "last_paragraph": number},
            "text": text, "score": score}


CHUNKS = [chunk(49, 2, "the people are the only legitimate fountain of power", 5.0),
          chunk(51, 4, "ambition must be made to counteract ambition", 4.0)]
REPLY = json.dumps({"restated_query": "Where does power come from?", "answer_found": True, "relevant": [1, 2], "snippets": [
    {"id": 1, "snippet": "the only legitimate fountain of power"},
    {"id": "2", "snippet": "ambition must counteract ambition"},       # a number as a text is taken; this snippet is not in chunk 2
    {"id": 9, "snippet": "anything"}],                                  # the LLM was passed no chunk 9
    "answer": "From the people."})


def documents_tool(chunks: list[dict[str, Any]] = CHUNKS, stored: list[dict[str, Any]] | None = None) -> Scripted:
    """A store: `search` gives `chunks`; `get` gives the chunks as stored, `stored` when given."""
    kept = stored if stored is not None else chunks
    return Scripted(search=lambda query, limit, exclude: [c for c in chunks if c["chunk_id"] not in exclude][:limit],
                    get=lambda ids: [dict(c, score=0) for i in ids for c in kept if c["chunk_id"] == i], files=lambda: 2)


def rag_tools(reply: str = REPLY, docs: Scripted | None = None) -> dict[str, Any]:
    return {"BROWSER": Browser("Where does power come from?"), "LLM": Scripted(ask=reply), "DOCS_DB": docs or documents_tool()}


SHOWN = ["CHECKED", "LLM_WRITTEN", "UNTRUSTED"]


def test_safe_retrieval_the_labels_at_each_call(tmp_path: Path) -> None:
    tools = rag_tools()
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    assert run.run("answer").state == "DONE"
    assert entry(run, "user.describe")["result"] == [] and permits(run, "user.describe") == {}       # ASK
    assert entry(run, "rag.search")["result"] == [] and permits(run, "rag.search") == {}             # SEARCH: the store adds nothing
    assert entry(run, "llm.answer")["result"] == ["LLM_WRITTEN", "UNTRUSTED"]                        # ANSWER, P1
    verify = entry(run, "verify.response")                                                           # VERIFY
    assert verify["arguments"] == {"chunk_ids": [], "response": ["LLM_WRITTEN", "UNTRUSTED"]}
    assert permits(run, "verify.response") == {}
    assert verify["requests"] == [[field, "CHECKED", True] for field in ("restated_query", "answer", "found", "supported")] + [
        ["documents", "UNTRUSTED", False], ["documents", "LLM_WRITTEN", False], ["documents", "ORIGINAL_SOURCE", True],
        ["left_out", "CHECKED", True]]
    assert permits(run, "user.show") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "SHOW_CHECKED_EX"}              # SHOW, per part, C2
    (screen, marks), = tools["BROWSER"].shown
    assert screen["question_as_the_llm_read_it"] == "Where does power come from?" and screen["answer"] == "From the people."
    for field in ("question_as_the_llm_read_it", "answer", "the_llm_found_an_answer"):
        assert marks[field] == SHOWN, field                                                          # the LLM's: untrusted, and read by the check
    assert screen["from_the_documents"] == [
        {"title": "FEDERALIST No. 49", "source": "federalist_49.txt", "chunks": [{"chunk_id": "federalist_49.txt#2",
         "location": {"first_paragraph": 2, "last_paragraph": 2}, "verified": True,
         "pieces": [{"text": "the people are ", "marked": False}, {"text": "the only legitimate fountain of power", "marked": True}]}]},
        {"title": "FEDERALIST No. 51", "source": "federalist_51.txt", "chunks": [{"chunk_id": "federalist_51.txt#4",   # named relevant, nothing verified
         "location": {"first_paragraph": 4, "last_paragraph": 4}, "verified": False,
         "pieces": [{"text": "ambition must be made to counteract ambition", "marked": False}]}]}]
    assert marks["from_the_documents"][0]["chunks"][0]["pieces"][1] == {"text": ["ORIGINAL_SOURCE"], "marked": ["ORIGINAL_SOURCE"]}
    assert screen["left_out"] == {"numbers_that_name_no_chunk": 1, "snippets_not_in_their_chunk": 1}
    assert marks["left_out"] == {"numbers_that_name_no_chunk": SHOWN, "snippets_not_in_their_chunk": SHOWN} and marks["note"] == []   # counts of the LLM's reply
    assert (screen["files_searched"], screen["chunks_found"], screen["chunks_passed_to_the_llm"]) == (2, 2, 2)


def test_the_text_displayed_as_from_the_documents_is_read_from_the_store(tmp_path: Path) -> None:
    """The check is passed chunk ids only and reads each chunk's text from the store: a snippet that is in the text
    the search returned, and not in the stored text, is not verified, and the stored text is what is displayed."""
    stored = [dict(CHUNKS[0], text="the people decide"), CHUNKS[1]]
    reply = json.dumps({"restated_query": "q", "answer_found": True, "relevant": [1, 2], "answer": "a",
                        "snippets": [{"id": 1, "snippet": "the only legitimate fountain of power"}, {"id": 2, "snippet": "ambition must be made"}]})
    tools = rag_tools(reply=reply, docs=documents_tool(stored=stored))
    start(EXAMPLES / "safe_rag", tmp_path, tools).run("answer")
    (screen, _), = tools["BROWSER"].shown
    assert screen["from_the_documents"][0]["chunks"][0]["pieces"] == [{"text": "the people decide", "marked": False}]
    assert screen["left_out"]["snippets_not_in_their_chunk"] == 1
    assert ("get", (["federalist_49.txt#2", "federalist_51.txt#4"],)) in tools["DOCS_DB"].calls


def test_a_reply_that_found_no_answer_shows_nothing_from_the_documents(tmp_path: Path) -> None:
    tools = rag_tools(reply='```json\n{"restated_query": "What is the capital of France?", "answer_found": false}\n```')
    start(EXAMPLES / "safe_rag", tmp_path, tools).run("answer")
    (screen, _), = tools["BROWSER"].shown                                    # after two rounds
    assert screen["no_snippet_found_for"] == QUESTION and screen["chunks_the_llm_named_relevant"] == []
    assert "answer" not in screen


def verified(tmp_path: Path, reply: dict[str, Any]) -> dict[str, Any]:
    """The report of verify.response for `reply`, over CHUNKS."""
    run = start(EXAMPLES / "safe_rag", tmp_path, rag_tools(reply=json.dumps(reply)))
    return run.run("answer").saved["report"].data


def test_a_snippet_is_found_only_as_whole_words(tmp_path: Path) -> None:
    report = verified(tmp_path, {"restated_query": "q", "answer_found": True, "answer": "a",
                                 "snippets": [{"id": 2, "snippet": "ambit"}, {"id": 1, "snippet": "e"}, {"id": 1, "snippet": "the people"}]})
    assert report["left_out"]["snippets_not_in_their_chunk"] == 2 and report["supported"] is True
    assert [p for p in report["documents"][0]["chunks"][0]["pieces"] if p["marked"]] == [{"text": "the people", "marked": True}]


def test_the_keys_of_a_reply_are_read_whatever_answer_found_says_and_may_be_absent(tmp_path: Path) -> None:
    """Generous: a reply that says it found nothing and gives a snippet is read; a reply with no `relevant` is used."""
    report = verified(tmp_path / "a", {"restated_query": "q", "answer_found": False, "snippets": [{"id": 1, "snippet": "the people"}]})
    assert report["found"] is False and report["supported"] is True and report["answer"] == ""
    report = verified(tmp_path / "b", {"restated_query": "q", "answer_found": True, "answer": "a", "snippets": [{"id": 2, "snippet": "ambition"}]})
    assert report["supported"] is True and [d["source"] for d in report["documents"]] == ["federalist_51.txt"]


def test_documents_the_user_does_not_vouch_for_are_marked_and_nothing_is_refused(tmp_path: Path) -> None:
    from chaperone import prerun
    folder = copy_of(tmp_path, "safe_rag", ("setups/local.yaml", "LEAVES_NETWORK: false, adds: []}}\n  LLM:", "LEAVES_NETWORK: false, adds: [UNTRUSTED_SOURCE]}}\n  LLM:"))
    assert prerun.check(load_project(folder)).refused == []
    tools = rag_tools()
    start(folder, tmp_path, tools).run("answer")
    (screen, marks), = tools["BROWSER"].shown
    assert "UNTRUSTED_SOURCE" in marks["from_the_documents"][0]["chunks"][0]["pieces"][1]["text"] and "UNTRUSTED_SOURCE" in marks["answer"]


def test_the_chunks_passed_to_the_llm_are_chosen_by_document(tmp_path: Path) -> None:
    """rag.search: grouped by document, the documents ranked by the sum of their chunks' scores, at most N documents
    and M chunks of each, within a total of characters; in a document the chunks are in the file's order."""
    found = [chunk(1, 5, "a" * 50, 9.0), chunk(2, 1, "b" * 50, 6.0), chunk(2, 3, "c" * 50, 5.0), chunk(2, 2, "d" * 50, 4.0),
             chunk(3, 1, "e" * 50, 3.0), chunk(1, 1, "f" * 500, 1.0)]
    folder = copy_of(tmp_path, "safe_rag", ("workflows/answer.yaml", "max_documents: 8, max_chunks_per_document: 4,\n                                      max_chars: 12000",
                                            "max_documents: 2, max_chunks_per_document: 2, max_chars: 300"))
    tools = rag_tools(docs=documents_tool(found))
    run = start(folder, tmp_path, tools)
    run.run("answer")
    method, (question, chunks) = tools["LLM"].calls[0]                 # 2 has 15 by its sum, 1 has 10, 3 is left out; 1's long chunk does not fit
    assert chunks == f"Document: FEDERALIST No. 2\n[1] {'b' * 50}\n[2] {'c' * 50}\n\nDocument: FEDERALIST No. 1\n[3] {'a' * 50}"
    assert ("search", ("Where does power come from?", 20, [])) in tools["DOCS_DB"].calls


# ---- step 3: a second round when no snippet was found, the nothing-found screen, several questions -------------------

EXTRA = chunk(10, 3, "Extend the sphere, and you take in a greater variety of parties and interests", 3.0)
QUESTION = "Where does power come from?"


def reply(restated: str = "Where does power come from, for Publius?", snippets: Any = (), relevant: Any = (), answer: str = "From the people.") -> str:
    return json.dumps({"restated_query": restated, "answer_found": True, "relevant": list(relevant), "answer": answer,
                       "snippets": [{"id": i, "snippet": text} for i, text in snippets]})


def two_rounds(*replies: str, questions: tuple[str, ...] = (QUESTION,)) -> dict[str, Any]:
    """The user's question finds CHUNKS; any other query finds EXTRA first. The LLM gives `replies`, one per call."""
    answers = list(replies)
    def search(query: str, limit: int, exclude: list[str]) -> list[dict[str, Any]]:
        found = CHUNKS if query in questions else [EXTRA] + CHUNKS
        return [c for c in found if c["chunk_id"] not in exclude][:limit]
    docs = Scripted(search=search, get=lambda ids: [dict(c, score=0) for i in ids for c in CHUNKS + [EXTRA] if c["chunk_id"] == i], files=lambda: 3)
    return {"BROWSER": Browser(*questions), "LLM": Scripted(ask=lambda **named: answers.pop(0)), "DOCS_DB": docs}


def states(run: Run) -> list[str]:
    return [e["state"] for e in run.log if e["call"] in ("user.show", "rag.search")]


def test_round_one_supported_shows_round_one_and_asks_again(tmp_path: Path) -> None:
    tools = two_rounds(reply(snippets=[(1, "the only legitimate fountain of power")]))
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    assert run.run("answer").state == "DONE" and states(run) == ["SEARCH", "SHOW"]
    assert permits(run, "decide", 1) == {"NO_DECISIONS_FROM_UNTRUSTED": "SUPPORTED_DECIDES_EX"}         # the branch on `supported`
    assert len(tools["LLM"].calls) == 1


def test_no_snippet_found_searches_again_with_the_llms_words_leaving_out_what_was_seen(tmp_path: Path) -> None:
    tools = two_rounds(reply(snippets=[(1, "a sentence the papers do not have")]), reply(snippets=[(1, "Extend the sphere")]))
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    assert run.run("answer").state == "DONE" and states(run) == ["SEARCH", "SEARCH_2", "SHOW_2"]
    assert ("search", ("Where does power come from, for Publius?", 20, ["federalist_49.txt#2", "federalist_51.txt#4"])) in tools["DOCS_DB"].calls
    assert permits(run, "rag.search", 1) == {}
    assert [call[1][0] for call in tools["LLM"].calls] == [QUESTION, QUESTION]                        # round two: the user's own question
    assert permits(run, "llm.answer", 1) == {}
    (screen, marks), = tools["BROWSER"].shown
    assert [d["source"] for d in screen["from_the_documents"]] == ["federalist_10.txt"]                # round two's documents only
    assert screen["searched_again_for"] == "Where does power come from, for Publius?" and "chunks_found" not in screen
    assert "ORIGINAL_SOURCE" in marks["from_the_documents"][0]["chunks"][0]["pieces"][0]["text"]


def test_two_rounds_with_no_snippet_found_display_no_answer_and_the_chunks_named_relevant(tmp_path: Path) -> None:
    tools = two_rounds(reply(snippets=[(1, "nothing")]), reply(snippets=[(1, "nothing either")], relevant=[1], answer="A guess."))
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    assert run.run("answer").state == "DONE" and states(run) == ["SEARCH", "SEARCH_2", "NOTHING_FOUND"]
    (screen, marks), = tools["BROWSER"].shown
    assert screen["no_snippet_found_for"] == QUESTION and screen["searched_again_for"] == "Where does power come from, for Publius?"
    assert "answer" not in screen
    assert [(d["source"], d["chunks"][0]["verified"]) for d in screen["chunks_the_llm_named_relevant"]] == [("federalist_10.txt", False)]
    assert "A guess." not in json.dumps(screen)


@pytest.mark.parametrize("round_", [1, 2])
def test_a_reply_that_cannot_be_read_in_either_round_shows_a_fixed_sentence(tmp_path: Path, round_: int) -> None:
    replies = ["not json"] if round_ == 1 else [reply(snippets=[(1, "nothing")]), "not json"]
    tools = two_rounds(*replies)
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    assert run.run("answer").state == "DONE" and tools["BROWSER"].shown == [({"note": NOTE_BAD_REPLY}, {})]
    assert "not json" not in json.dumps(tools["BROWSER"].shown)


def test_several_questions_in_one_run_and_an_empty_box_ends_it(tmp_path: Path) -> None:
    second = "Who wrote it?"
    tools = two_rounds(reply(snippets=[(1, "the people are")]), reply(snippets=[(2, "ambition must be made")]), questions=(QUESTION, second))
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    outcome = run.run("answer")
    assert outcome.ended == "end" and outcome.state == "DONE" and states(run) == ["SEARCH", "SHOW", "SEARCH", "SHOW"]
    assert [call[1][0] for call in tools["LLM"].calls] == [QUESTION, second]
    assert entry(run, "decide")["arguments"] == {"value": []} and permits(run, "decide") == {}          # the empty text: a branch on the user's own words


NOTE_BAD_REPLY = "The LLM's reply could not be read, so no answer is displayed. Ask the question again, or in other words. The tab Log has the type of the error; an answer cut at the model's max_tokens is said in llm.jsonl, in the state folder."
NOTE_FAILED = "Stopped on an error. The tab Log has its type and message."


def test_a_reply_that_cannot_be_parsed_routes_to_its_state_and_shows_a_literal(tmp_path: Path) -> None:
    tools = rag_tools(reply="I think the answer is the people.")
    run = start(EXAMPLES / "safe_rag", tmp_path, tools)
    outcome = run.run("answer")
    assert outcome.ended == "end" and outcome.state == "DONE"
    assert entry(run, "verify.response")["error"] == {"type": "bad_reply", "labels": ["LLM_WRITTEN", "UNTRUSTED"]}
    assert tools["BROWSER"].shown == [({"note": NOTE_BAD_REPLY}, {})]
    assert permits(run, "user.show") == {}                                   # a literal carries no label


def test_an_llm_that_gives_no_answer_is_said_on_the_page(tmp_path: Path) -> None:
    def down(**named: Any) -> Any:
        raise ModuleError("llm_unavailable", "the environment variable ANTHROPIC_API_KEY is not set")
    tools = rag_tools() | {"LLM": Scripted(ask=down)}
    outcome = start(EXAMPLES / "safe_rag", tmp_path, tools).run("answer")
    assert outcome.state == "DONE" and "api_key_env" in tools["BROWSER"].shown[0][0]["note"]


def test_showing_the_error_itself_is_refused(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "safe_rag", ("workflows/answer.yaml", f'report: {{note: "{NOTE_BAD_REPLY}"}}', 'report: "{error}"'))
    tools = rag_tools(reply="not json")
    run = start(folder, tmp_path, tools)
    outcome = run.run("answer")
    assert tools["BROWSER"].shown == [({"note": NOTE_FAILED}, {})] and outcome.state == "FAILED"
    assert entry(run, "user.show")["error"]["type"] == "refused"


def test_showing_the_reply_without_the_verification_is_refused(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "safe_rag", ("workflows/answer.yaml", 'report: "{vars.screen}", marks', 'report: "{vars.response}", marks'))
    tools = rag_tools()
    run = start(folder, tmp_path, tools)
    run.run("answer")
    assert tools["BROWSER"].shown == [({"note": NOTE_FAILED}, {})] and permits(run, "user.show") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": None}


def test_a_request_outside_the_declaration_refuses_the_call_and_changes_no_label(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "safe_rag", ("modules.py", '    return {"text": ctx.ask(query=query, chunks=',
                                            '    return {"text": "{}"}, [("", "CHECKED", True)]\n    return {"text": ctx.ask(query=query, chunks='))
    run = start(folder, tmp_path, rag_tools())
    outcome = run.run("answer")
    failed = entry(run, "llm.answer")
    assert failed["error"]["type"] == "refused" and "result" not in failed and outcome.state == "DONE"      # by ANSWER's own error state, NO_LLM
    assert "response" not in outcome.saved                                    # nothing receives the result


# ---- the mechanics of 2.12, on a project of three modules -----------------------------------------------------------

def tiny(tmp_path: Path, workflow: str, exceptions: str = "exceptions: []\n") -> Path:
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules", dirs_exist_ok=True)
    folder = tmp_path / "tiny"
    (folder / "setups").mkdir(parents=True)
    (folder / "workflows").mkdir()
    (folder / "system.yaml").write_text("labels:\n  UNTRUSTED: {spreads: true}\n  PERSONAL: {spreads: true}\nstores:\n  NOTES: []\n  COUNT: 0\n")
    (folder / "setups" / "local.yaml").write_text(
        "tools:\n  SOURCE: {client: {kind: scripted}, facts: {OUTPUT: false, LEAVES_NETWORK: false, adds: [UNTRUSTED]}}\n"
        "log: {facts: {OUTPUT: false, DESTRUCTIVE: false}}\nmodules:\n  source.read: {tool: SOURCE}\n")
    (folder / "rules.yaml").write_text("imports: [../rules/default.yaml]\n")
    (folder / "exceptions.yaml").write_text(exceptions)
    (folder / "modules.py").write_text(
        'import chaperone\n\n'
        '@chaperone.module("make.value", properties={"DESTRUCTIVE": False, "OUTPUT": False})\ndef make(value):\n    return value\n\n'
        '@chaperone.module("make.fail", properties={"DESTRUCTIVE": False, "OUTPUT": False})\ndef fail(type):\n    raise chaperone.ModuleError(type, "asked to fail")\n\n'
        '@chaperone.module("source.read", properties={"DESTRUCTIVE": False})\ndef read(ctx):\n    return ctx.read()\n')
    (folder / "workflows" / "flow.yaml").write_text(workflow)
    return folder


def run_tiny(tmp_path: Path, workflow: str, exceptions: str = "exceptions: []\n", read: Any = "text") -> tuple[Run, Any]:
    run = start(tiny(tmp_path, workflow, exceptions), tmp_path, {"SOURCE": Scripted(read=read)})
    return run, run.run("flow")


BRANCH = """workflow: flow
start: A
on_error: FAILED
states:
  A:
    steps: [{call: make.value, with: {value: VALUE}, save_as: v}]
    next: {switch: "{vars.v}", goto: {true: TAKEN, 3: THREE, word: WORD}ELSE}
  TAKEN: {end: true}
  THREE: {end: true}
  WORD: {end: true}
  OTHER: {end: true}
  FAILED: {end: true}
"""


@pytest.mark.parametrize("value, other, state", [("true", "", "TAKEN"), ("3", "", "THREE"), ("word", "", "WORD"),
                                                 ("1", ", else: OTHER", "OTHER"),          # 1 is not true
                                                 ('"3"', ", else: OTHER", "OTHER"),        # the text 3 is not the number
                                                 ("nothing", "", "FAILED")])               # no key, no else: no_match
def test_a_branch_takes_the_key_equal_to_the_value_in_kind_and_content(tmp_path: Path, value: str, other: str, state: str) -> None:
    run, outcome = run_tiny(tmp_path, BRANCH.replace("VALUE", value).replace("ELSE", other))
    assert outcome.state == state
    if state == "FAILED":
        assert entry(run, "decide")["error"]["type"] == "no_match"


ROUTES = """workflow: flow
start: A
max_states: 5
on_error: FAILED
states:
  A:
    steps: [{call: make.fail, with: {type: KIND}}]
    on_error: LOCAL
    next: FAILED
  OWN: {end: true}
  LOCAL: {end: true}
  FAILED:
    steps: [{call: make.fail, with: {type: again}}]
    end: true
"""


@pytest.mark.parametrize("kind", ["own", "special", "anything"])
def test_the_error_route_of_a_state_is_taken_before_the_workflows_whatever_the_type_of_the_error(tmp_path: Path, kind: str) -> None:
    assert run_tiny(tmp_path, ROUTES.replace("KIND", kind))[1].state == "LOCAL"


@pytest.mark.parametrize("kind", ['"two words"', '"a/b"', '""'])
def test_an_error_whose_type_is_not_a_name_is_an_error_of_type_module_failed(tmp_path: Path, kind: str) -> None:
    run, outcome = run_tiny(tmp_path, ROUTES.replace("KIND", kind))
    assert outcome.state == "LOCAL" and entry(run, "make.fail")["error"]["type"] == "module_failed"


LOGGED = """workflow: flow
start: A
on_error: FAILED
states:
  A:
    steps: [{call: source.read, save_as: text}, {call: make.fail, with: {type: "{vars.text}"}}]
    next: FAILED
  FAILED: {end: true}
"""
LOG_IS_OUTPUT = ("log: {facts: {OUTPUT: false, DESTRUCTIVE: false}}", "log: {facts: {OUTPUT: true, DESTRUCTIVE: false}}")
LOG_PERMITTED = "exceptions:\n  - {id: ERRORS_IN_LOG_EX, rule: NO_UNTRUSTED_DATA_TO_OUTPUT, except: {call: [log]}}\n"


def logged(tmp_path: Path, exceptions: str = "exceptions: []\n", exit_on: str = "") -> tuple[Run, Outcome]:
    """A module fails while it is passed what the source returned; the setup states that `log` is OUTPUT. `exit_on`:
    the value of system.yaml's `exit_run_on_refusal`, or "" for none written."""
    folder = tiny(tmp_path, LOGGED, exceptions)
    setup = folder / "setups" / "local.yaml"
    assert LOG_IS_OUTPUT[0] in setup.read_text()
    setup.write_text(setup.read_text().replace(*LOG_IS_OUTPUT))
    if exit_on:
        (folder / "system.yaml").write_text((folder / "system.yaml").read_text() + f"exit_run_on_refusal: {exit_on}\n")
    run = start(folder, tmp_path, {"SOURCE": Scripted(read="text")})
    return run, run.run("flow")


@pytest.mark.parametrize("exit_on, ends", [("", False), ("never", False), ("[make.value]", False), ("[log]", True), ("always", True)])
def test_a_refused_call_of_log_is_an_error_that_ends_the_run_only_as_system_yaml_says(tmp_path: Path, exit_on: str, ends: bool) -> None:
    run, outcome = logged(tmp_path, exit_on=exit_on)
    if ends:
        assert (outcome.ended, outcome.state) == ("error", "A")                                # no error route is taken
        assert outcome.error.data["type"] == "refused" and outcome.error.data["call"] == "log"
        assert "NO_UNTRUSTED_DATA_TO_OUTPUT" in outcome.error.data["message"] and "asked to fail" not in outcome.error.data["message"]
    else:
        assert (outcome.ended, outcome.state) == ("end", "FAILED")                             # the refusal took the error route
    assert entry(run, "log") == {"workflow": "flow", "state": "A", "call": "log", "arguments": {"error": ["UNTRUSTED"]},
                                 "forbidden_by": [{"rule": "NO_UNTRUSTED_DATA_TO_OUTPUT", "permitted_by": None}],
                                 "error": {"type": "refused", "labels": ["UNTRUSTED"]}}
    index = next(i for i, record in enumerate(run.log) if record["call"] == "make.fail")
    assert run.reasons[index] == {"message": None, "detail": None, "withheld": {"rule": "NO_UNTRUSTED_DATA_TO_OUTPUT", "label": "UNTRUSTED"}}
    assert entry(run, "make.fail")["error"] == {"type": "text", "labels": ["UNTRUSTED"]}      # the type is a name: it is recorded


def test_exit_run_on_refusal_names_only_modules_decide_and_log(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as refused:
        logged(tmp_path, exit_on="[make.valeu]")
    assert "exit_run_on_refusal names make.valeu, which is not a module, nor `decide` or `log`" in str(refused.value)


def test_an_exception_for_log_keeps_what_the_module_wrote(tmp_path: Path) -> None:
    run, outcome = logged(tmp_path, LOG_PERMITTED)
    assert outcome.state == "FAILED"
    assert permits(run, "log") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "ERRORS_IN_LOG_EX"}
    index = next(i for i, record in enumerate(run.log) if record["call"] == "make.fail")
    assert run.reasons[index] == {"message": "asked to fail", "detail": None}


def test_an_error_the_framework_wrote_is_no_call_of_log(tmp_path: Path) -> None:
    """A refused branch: its message names the rule and the label, no value."""
    folder = tiny(tmp_path, UNTRUSTED_BRANCH.replace("WHICH", "same"))
    setup = folder / "setups" / "local.yaml"
    setup.write_text(setup.read_text().replace(*LOG_IS_OUTPUT))
    run = start(folder, tmp_path, {"SOURCE": Scripted(read="text")})
    run.run("flow")
    assert not any(record["call"] == "log" for record in run.log)
    assert "forbidden by NO_DECISIONS_FROM_UNTRUSTED" in run.reasons[len(run.log) - 1]["message"]


def test_an_error_in_the_state_its_route_names_ends_the_run(tmp_path: Path) -> None:
    workflow = ROUTES.replace("    on_error: LOCAL\n", "").replace("KIND", "x")
    run, outcome = run_tiny(tmp_path, workflow)
    assert outcome.ended == "error" and outcome.state == "FAILED" and outcome.error.data["type"] == "again"


LOOP = """workflow: flow
start: A
max_states: 4
on_error: FAILED
states:
  A:
    steps:
      - {call: store.get, with: {store: COUNT}, save_as: n}
      - {call: store.add, with: {store: NOTES, items: ["{vars.n}", again]}, save_as: added}
    next: A
  FAILED: {end: true}
"""


def test_max_states_ends_a_run_and_the_store_keeps_what_was_put(tmp_path: Path) -> None:
    run, outcome = run_tiny(tmp_path, LOOP)
    assert outcome.ended == "too_many_states" and run.log[-1]["error"]["type"] == "too_many_states"
    assert run.store.get("NOTES").data == [0, "again"] and outcome.saved["added"].data == {"added": [], "present": [0, "again"]}


UNTRUSTED_BRANCH = """workflow: flow
start: A
on_error: FAILED
states:
  A:
    steps:
      - {call: source.read, save_as: text}
      - {call: get, with: {value: {shown: "{vars.text}", note: fixed}}, save_as: both}
      - {call: pick, with: {value: "{vars.both}", fields: [note]}, save_as: note}
      - {call: equals, with: {a: "{vars.text}", b: text}, save_as: same}
    next: {switch: "{vars.WHICH}", goto: {true: TAKEN, fixed: TAKEN}, else: SKIPPED}
  TAKEN: {end: true}
  SKIPPED: {end: true}
  FAILED: {end: true}
"""
DECIDE = "exceptions:\n  - {id: BRANCHES, rule: NO_DECISIONS_FROM_UNTRUSTED, except: {call: [decide]}}\n"


def test_a_branch_on_an_untrusted_value_is_refused_and_the_refusal_is_routed_as_any_error(tmp_path: Path) -> None:
    run, outcome = run_tiny(tmp_path, UNTRUSTED_BRANCH.replace("WHICH", "same"))
    assert entry(run, "equals")["result"] == ["UNTRUSTED"]                                 # P3: computed, so it spreads
    assert entry(run, "decide")["error"]["type"] == "refused"
    assert outcome.ended == "end" and outcome.state == "FAILED"                            # no rule is checked on an error route


def test_a_refused_branch_ends_the_run_when_system_yaml_names_decide(tmp_path: Path) -> None:
    folder = tiny(tmp_path, UNTRUSTED_BRANCH.replace("WHICH", "same"))
    (folder / "system.yaml").write_text((folder / "system.yaml").read_text() + "exit_run_on_refusal: [decide]\n")
    outcome = start(folder, tmp_path, {"SOURCE": Scripted(read="text")}).run("flow")
    assert (outcome.ended, outcome.state, outcome.error.data["call"]) == ("error", "A", "decide")


def test_an_exception_for_branches_permits_the_branch(tmp_path: Path) -> None:
    run, outcome = run_tiny(tmp_path, UNTRUSTED_BRANCH.replace("WHICH", "same"), DECIDE)
    assert outcome.state == "TAKEN" and permits(run, "decide") == {"NO_DECISIONS_FROM_UNTRUSTED": "BRANCHES"}


def test_a_part_written_in_the_file_is_not_forbidden_beside_an_untrusted_one(tmp_path: Path) -> None:
    run, outcome = run_tiny(tmp_path, UNTRUSTED_BRANCH.replace("{vars.WHICH}", "{vars.note.note}"))
    assert outcome.state == "TAKEN" and "forbidden_by" not in entry(run, "decide")
    assert entry(run, "get")["result"] == ["UNTRUSTED"] and entry(run, "pick")["result"] == []


REFUSED = [
    ("a key the file does not know", "max_states: 5", "max_state: 5", "the key `max_state` is not known"),
    ("a state with the name of a key", "  OWN: {end: true}", "  Next: {end: true}", "may not have the name of a key"),
    ("a state with next and end", "    next: FAILED", "    next: FAILED\n    end: true", "one of the two"),
    ("a next that names no state", "    next: FAILED", "    next: NOWHERE", "NOWHERE, which is not a state"),
    ("an error route that names no state", "    on_error: LOCAL", "    on_error: NOWHERE", "on_error names NOWHERE"),
    ("an error route by error type", "on_error: FAILED\nstates", "on_error: {special: FAILED}\nstates", "a map by error type is not accepted"),
    ("a state's error route by error type", "    on_error: LOCAL", "    on_error: {own: OWN}", "a map by error type is not accepted"),
    ("a call that is no module", "call: make.fail, with: {type: KIND}", "call: make.fial, with: {type: KIND}", "not a module of the project"),
    ("decide written as a step", "call: make.fail, with: {type: KIND}", "call: decide, with: {type: KIND}", "not a module of the project"),
    ("an argument the module does not have", "with: {type: KIND}", "with: {type: KIND, extra: 1}", "it has no argument extra"),
    ("an argument that is not given", "with: {type: KIND}", "with: {}", "the argument type is not given"),
    ("a store that is not declared", "call: make.fail, with: {type: KIND}", "call: store.get, with: {store: MISSING}", "the store MISSING is not declared"),
    ("a reference mixed with other text", "with: {type: KIND}", 'with: {type: "kind {vars.x}"}', "mixes a reference with other text"),
    ("a reference to a name no step saves", "with: {type: KIND}", 'with: {type: "{vars.never}"}', "which no step of the workflow saves"),
    ("a loop without max_states", "max_states: 5\n", "", "max_states is not given"),
    ("a name that is not the file's", "workflow: flow", "workflow: other", "the two are the same"),
    ("a branch written with on", "    next: FAILED", '    next: {on: "{vars.x}", goto: {a: OWN}}', "quote it"),
    ("a branch without goto", "    next: FAILED", '    next: {switch: "{vars.x}"}', "`goto` is required"),
]


@pytest.mark.parametrize("what, old, new, reason", REFUSED, ids=[r[0] for r in REFUSED])
def test_a_workflow_the_specification_does_not_permit_is_refused(tmp_path: Path, what: str, old: str, new: str, reason: str) -> None:
    looping = ROUTES.replace("    next: FAILED", "    next: FAILED", 1)
    if what == "a loop without max_states":
        looping = looping.replace("  LOCAL: {end: true}", "  LOCAL: {next: A}")
    assert old in looping
    with pytest.raises(ConfigError) as refused:
        load_project(tiny(tmp_path, looping.replace(old, new, 1)))
    assert reason in str(refused.value)


# ---- Example B: deliveries ----------------------------------------------------------

FOUND = [{"address": "pkginfo@ups.example", "messages": 5}, {"address": "mcinfo@ups.example", "messages": 2},
         {"address": "deals@shop.example", "messages": 1}]


class Page(Browser):
    def __init__(self, *answers: Any) -> None:
        super().__init__(*answers)
        self.offered: list[Any] = []

    def choose(self, items: Any, prompt: str, already: Any = None) -> Any:
        self.offered.append(items)
        self.already = already
        return self.answers.pop(0)(items)


def delivery_tools(page: Page, steps: list[dict[str, Any]], **more: Any) -> dict[str, Any]:
    seen: list[Any] = []

    def next_step(asked: str, reply: str, sizes: dict, options: list) -> Any:
        seen.append((reply, sizes, options))
        return steps.pop(0)
    llm = more.get("llm", Scripted())                       # one LLM tool: it chooses the next step and reads each message
    llm.answers["next_step"], llm.seen = next_step, seen
    return {"BROWSER": page, "LOCAL_LLM": llm,
            "MAILBOX": more.get("mailbox", Scripted(search_senders=FOUND, count_messages=lambda a: [{"address": x, "messages": 0} for x in a])),
            "GOOGLE_CALENDAR": more.get("calendar", Scripted(upsert="created", entries={})),
            "SENDERS_FILE": Scripted(read_lines=more.get("file", [])), "CLOCK": Scripted(today=more.get("today", "2026-01-01"))}   # a day before every test's entries: none is past


def test_one_run_of_the_assistant_the_table_of_7_1(tmp_path: Path) -> None:
    page = Page("track my UPS packages", lambda items: items[:2], "also add an address",
                {"addresses": ["tracking@fedex.example"]}, lambda items: items, "that's all")
    tools = delivery_tools(page, [{"option": "search", "terms": ["UPS", "United Parcel Service"]}, {"option": "add"}, {"option": "done"}])
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    outcome = run.run("assistant")
    assert outcome.ended == "end" and outcome.state == "DONE"
    untrusted, written = "UNTRUSTED", "LLM_WRITTEN"
    assert entry(run, "lists.sizes")["result"] == []                                                          # row 3
    assert entry(run, "llm.next_step")["result"] == [written, untrusted] and permits(run, "llm.next_step") == {}   # row 4
    assert permits(run, "decide", 1) == {"NO_DECISIONS_FROM_UNTRUSTED": "LLM_CHOOSES_STEP_EX"}                   # row 5
    assert permits(run, "user.show", 1) == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "UNTRUSTED_TO_USER_EX"}               # row 6
    assert permits(run, "mail.read.search_senders") == {"NOTHING_LEAVES_NETWORK": "MAILBOX_APPROVED_EX"}             # row 7
    assert entry(run, "mail.read.search_senders")["result"] == [written, "PERSONAL", untrusted]
    chosen = entry(run, "user.choose_senders")                                                                # row 8
    assert permits(run, "user.choose_senders") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "UNTRUSTED_TO_USER_EX", "NO_PERSONAL_DATA_OUTPUT": "PERSONAL_TO_USER_EX"}
    assert chosen["result"] == ["PERSONAL"]                                                  # the tick removed UNTRUSTED and LLM_WRITTEN
    assert permits(run, "user.show", 2) == {"NO_PERSONAL_DATA_OUTPUT": "PERSONAL_TO_USER_EX"}                              # row 9
    sizes = entry(run, "lists.sizes", 1)                                                                      # row 10
    assert sizes["result"] == [written, untrusted] and sizes["requests"] == [["the whole value", "PERSONAL", False]]
    assert permits(run, "llm.next_step", 1) == {}            # row 11
    assert entry(run, "user.enter_senders")["result"] == ["PERSONAL"]                                         # row 12: entered, not approved
    assert permits(run, "mail.read.count_messages") == {"NOTHING_LEAVES_NETWORK": "MAILBOX_APPROVED_EX"}   # row 13: no rule forbids passing the mailbox an address
    assert entry(run, "mail.read.count_messages")["result"] == ["PERSONAL", untrusted]
    ticked = entry(run, "user.choose_senders", 1)                                                             # the tick is the approval
    assert ticked["result"] == ["PERSONAL"] and page.offered[1] == [{"address": "tracking@fedex.example", "messages": 0}]
    assert run.store.get("MONITORED_SENDERS").data == ["pkginfo@ups.example", "mcinfo@ups.example", "tracking@fedex.example"]
    # the LLM was passed the three typed sentences, the sizes and the offered options, and nothing else
    assert tools["LOCAL_LLM"].seen == [
        ("track my UPS packages", {"found": 0, "monitored": 0, "attention": 0, "links": 0}, ["search", "add", "clarify", "done"]),
        ("also add an address", {"found": 3, "monitored": 2, "attention": 0, "links": 0}, ["search", "add", "clarify", "done", "choose", "remove"]),
        ("that's all", {"found": 3, "monitored": 3, "attention": 0, "links": 0}, ["search", "add", "clarify", "done", "choose", "remove"])]
    assert "ups.example" not in json.dumps(run.log)


def test_three_unusable_answers_end_the_conversation(tmp_path: Path) -> None:
    tools = delivery_tools(Page("hello"), [{"option": "fly"}, "gibberish", {"option": "launch"}])
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("assistant").state == "FAILED_BAD_LLM"
    assert "3 times" in tools["BROWSER"].shown[-1][0]["note"]


def test_an_option_whose_list_is_empty_is_said_to_the_user_and_the_conversation_goes_on(tmp_path: Path) -> None:
    """The user types "remove some senders" with nothing monitored: the run once ended there; now it is said, and goes on."""
    tools = delivery_tools(Page("remove some senders", "ok, that's all"), [{"option": "remove"}, {"option": "done"}])
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("assistant").state == "DONE" and run.store.get("PROBLEMS").data == 0
    assert {"assistant": "There is nothing to remove at the moment: that list is empty. What else?"} in [shown[0] for shown in tools["BROWSER"].shown]


NUMBER = "1Z999AA10123456784"


NO_TIME = "The message gives no time: 08:00 to 17:00 is not a delivery time."
AWAITS = "A tracking link for this sender awaits approval."   # the test messages have a link no test approved


def first(made: dict[str, Any]) -> str:
    """The first line of an entry's description, without the sentence about the link that awaits approval."""
    line = made["description"].split("\n")[0].replace(AWAITS, "").strip()
    return "" if line.startswith("From ") else line


def notes(made: dict[str, Any]) -> str:
    """An entry's description without its last line, which says where the message is found."""
    lines = made["description"].split("\n")
    assert re.fullmatch(r"From notices@carrier\.example, received \d{4}-\d\d-\d\d \d\d:\d\d, message \d+\.", lines[-1])
    return "\n".join(lines[:-1]).replace(AWAITS, "").replace(NO_TIME, "").strip()                             # and without the sentence every entry with no time has


def letter(number: int, body: str, received: str = "2026-11-10T09:00:00+00:00", link: str = f"https://carrier.example/track?n={NUMBER}") -> dict[str, Any]:
    return {"id": number, "from": "notices@carrier.example", "received": received, "received_day": received[:10], "received_time": received[11:16], "subject": "", "body": body, "links": [link]}


def calendar_run(tmp_path: Path, mail: list[Any], kinds: Any, copies: Any, *edits: tuple[str, str, str]) -> tuple[Run, FileCalendar]:
    """`update_calendar` on `mail`, with a calendar file and an LLM that gives the answer `kinds` and the answer
    `copies`; either may be a function of the message's body."""
    calendar = FileCalendar(tmp_path / "calendar.ics")
    answer = lambda given: (lambda subject, body: given(body) if callable(given) else given)
    tools = delivery_tools(Page(), [], calendar=calendar, file=["notices@carrier.example"],
                           mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(classify=answer(kinds), copy_values=answer(copies)))
    run = start(copy_of(tmp_path, "deliveries", *edits) if edits else EXAMPLES / "deliveries", tmp_path, tools)
    outcome = run.run("update_calendar")
    assert outcome.ended == "end" and outcome.state == "DONE"
    return run, calendar


def test_a_call_is_permitted_when_each_part_that_forbids_it_is_covered_by_some_exception(tmp_path: Path) -> None:
    """C2. The calendar's write is passed checked values (UNTRUSTED, CHECKED) and a cleaned name (UNTRUSTED,
    SANITIZED): one exception covers each, and the two together permit the call. Passed the name not cleaned, one
    part is covered by neither, and the call is refused."""
    notice = lambda: [letter(1, f"Scheduled Delivery: Thursday 11/12/2026. {NUMBER} from Example Outfitters"), None]
    copied = {"tracking_number": NUMBER, "date": "11/12/2026", "shipper": "Example Outfitters"}
    run, _ = calendar_run(tmp_path / "a", notice(), "scheduled", copied)
    assert permits(run, "calendar.write_entry")["NO_UNTRUSTED_DATA_TO_OUTPUT"] == "CHECKED_TO_CALENDAR_EX, SHIPPER_TO_CALENDAR_EX"
    mail, calendar = notice(), FileCalendar(tmp_path / "b" / "calendar.ics")
    tools = delivery_tools(Page(), [], calendar=calendar, file=["notices@carrier.example"],
                           mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(classify=lambda subject, body: "scheduled", copy_values=lambda subject, body: copied))
    project = copy_of(tmp_path / "b", "deliveries", ("workflows/update_calendar.yaml", 'shipper: "{vars.shipper}", subject: "{vars.subject}", title: "Delivery"',
                                                    'shipper: "{vars.copied.shipper}", subject: "{vars.subject}", title: "Delivery"'))
    refused = start(project, tmp_path / "b", tools)
    assert refused.run("update_calendar").state == "FAILED" and calendar.entries() == {}
    assert permits(refused, "calendar.write_entry")["NO_UNTRUSTED_DATA_TO_OUTPUT"] is None
    assert entry(refused, "calendar.write_entry")["error"]["type"] == "refused"


def test_update_calendar_sets_a_message_aside_and_continues(tmp_path: Path) -> None:
    mail = [letter(1, f"Scheduled Delivery: Thursday 11/12/2026. {NUMBER}"), letter(2, f"{NUMBER} arrives shortly"), letter(3, "20% off"),
            letter(4, f"delivered {NUMBER}"), None]
    kinds = {mail[0]["body"]: " Scheduled. ", mail[1]["body"]: "scheduled", "20% off": "other", mail[3]["body"]: "delivered"}
    copies = {mail[0]["body"]: {"tracking_number": f" {NUMBER} ", "date": "11/12/2026"},          # read generously
              mail[1]["body"]: {"tracking_number": NUMBER, "date": "shortly"},
              mail[3]["body"]: {"tracking_number": NUMBER, "date": None}}                          # delivered: no date needed
    run, calendar = calendar_run(tmp_path, mail, kinds.get, copies.get)
    made = calendar.entries()[NUMBER]
    # the delivered notice gives no day and no time: the 15 minutes before the message was received
    assert (made["title"], made["start"], made["end"]) == (f"Delivered (carrier.example) {NUMBER}", "2026-11-10T08:45:00", "2026-11-10T09:00:00")
    assert made["description"] == ("The day is the day the message was received. The message gives no time of delivery: the entry ends when the message was received, 09:00. " + AWAITS + "\n"
                                   "From notices@carrier.example, received 2026-11-10 09:00, message 4.")     # by which the message is found again
    assert [json.loads(line)["what"] for line in calendar.history.read_text().splitlines()] == ["created", "updated"]
    assert permits(run, "calendar.mark_delivered")["NO_DESTRUCTIVE_ACTIONS"] == "MARK_DELIVERED_EX"
    assert entry(run, "calendar.write_entry")["arguments"]["entry"] == ["CHECKED", "PERSONAL", "UNTRUSTED"]   # checked, and still untrusted; LLM_WRITTEN removed
    fetch = entry(run, "mail.read.fetch_next_by_sender")
    assert fetch["arguments"] == {"senders": [], "handled": []} and "NO_PERSONAL_DATA_OUTPUT" not in permits(run, "mail.read.fetch_next_by_sender")
    assert run.store.get("HANDLED_MESSAGES").data == [1, 2, 3, 4] and run.store.get("HANDLED_MESSAGES").carried() == {"UNTRUSTED", "PERSONAL"}   # ids, with the mailbox's labels
    assert [e["arguments"]["handled"] for e in run.log if e["call"] == "mail.read.fetch_next_by_sender"] == [[]] + [["PERSONAL", "UNTRUSTED"]] * 4   # no rule forbids passing them
    aside = run.store.get("NEEDS_ATTENTION")
    assert [m["message"]["id"] for m in aside.data] == [2] and set(aside.carried()) == {"UNTRUSTED", "PERSONAL", "LLM_WRITTEN"}
    why = aside.data[0]["error"]                              # the reason is kept with the message, and what the LLM gave
    assert (why["type"], why["call"], why["message"]) == ("bad_values", "check.delivery_values", "the date is not in a form this check reads")
    assert why["detail"]["llm_gave"]["date"] == "shortly"
    assert entry(run, "check.delivery_values", 1)["error"]["type"] == "bad_values"


SET_ASIDE = [
    ("a tracking number that is not in the message", "a parcel comes 11/12/2026", "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026"}, "bad_values"),
    ("a date that is not in the message", f"{NUMBER} comes 11/12/2026", "scheduled", {"tracking_number": NUMBER, "date": "11/13/2026"}, "bad_values"),
    ("a date already written year-month-day by the LLM", f"{NUMBER} comes 11/12/2026", "scheduled", {"tracking_number": NUMBER, "date": "2026-11-12"}, "bad_values"),
    ("a date that is no day of the calendar", f"{NUMBER} comes 02/30/2026", "scheduled", {"tracking_number": NUMBER, "date": "02/30/2026"}, "bad_values"),
    ("a date far from the day the message was received", f"{NUMBER} comes 11/12/2027", "scheduled", {"tracking_number": NUMBER, "date": "11/12/2027"}, "bad_values"),
    ("a date more than 14 days after", f"{NUMBER} comes 11/25/2026", "scheduled", {"tracking_number": NUMBER, "date": "11/25/2026"}, "bad_values"),
    ("a date before the day the message was received", f"{NUMBER} came 11/09/2026", "scheduled", {"tracking_number": NUMBER, "date": "11/09/2026"}, "bad_values"),
    ("a day of the week that is not the date's", f"{NUMBER} comes Friday, 11/12/2026", "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026"}, "bad_values"),
    ("a tracking number the LLM wrote in small letters, which the message does not", f"{NUMBER} comes 11/12/2026", "scheduled", {"tracking_number": NUMBER.lower(), "date": "11/12/2026"}, "bad_values"),
    ("no date for a scheduled notice", f"{NUMBER} comes", "scheduled", {"tracking_number": NUMBER, "date": None}, "bad_values"),
    ("an answer that is not an object", f"{NUMBER} comes", "scheduled", NUMBER, "bad_answer"),
    ("a kind the workflow has no key for", f"{NUMBER} comes", "maybe", {}, "no_match"),
    ("a body longer than the workflow permits", "x" * 20001, "scheduled", {}, "too_long"),
]


@pytest.mark.parametrize("what, body, kind, copied, error", SET_ASIDE, ids=[c[0] for c in SET_ASIDE])
def test_a_message_that_cannot_be_read_is_set_aside_and_nothing_is_written(tmp_path: Path, what: str, body: str, kind: str,
                                                                          copied: Any, error: str) -> None:
    run, calendar = calendar_run(tmp_path, [letter(1, body), None], kind, copied)
    assert calendar.entries() == {}
    aside = run.store.get("NEEDS_ATTENTION").data
    assert [m["message"]["id"] for m in aside] == [1] and aside[0]["error"]["type"] == error
    assert error in [e["error"]["type"] for e in run.log if "error" in e]
    assert run.store.get("HANDLED_MESSAGES").data == [1]                   # set aside is dealt with: it is not fetched again


def test_a_run_that_stops_on_a_message_fetches_it_again_and_a_number_in_small_letters_is_a_number(tmp_path: Path) -> None:
    """The calendar fails on the first message: the run ends in FAILED and the message is not recorded as handled."""
    number = "ab12345678cd"
    mail = [letter(1, f"{number} comes 11/12/2026", link=f"https://c.example/{number}"), None]

    class Down(FileCalendar):
        def upsert(self, *arguments: Any, **named: Any) -> str:
            raise OSError("the calendar gave no answer")
    tools = delivery_tools(Page(), [], calendar=Down(tmp_path / "c.ics"), file=["n@c.example"], mailbox=Scripted(fetch_next=lambda senders, handled: mail[0]),
                           llm=Scripted(classify=lambda subject, body: "scheduled", copy_values=lambda subject, body: {"tracking_number": number, "date": "11/12/2026"}))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("update_calendar").state == "FAILED" and run.store.get("HANDLED_MESSAGES").data == []
    tools["GOOGLE_CALENDAR"] = calendar = FileCalendar(tmp_path / "c.ics")
    mail_two = [mail[0], None]
    tools["MAILBOX"] = Scripted(fetch_next=lambda senders, handled: None if handled else mail_two[0])
    assert run.run("update_calendar").state == "DONE"
    assert list(calendar.entries()) == [number] and run.store.get("HANDLED_MESSAGES").data == [1]


def test_reset_entries_removes_the_entries_and_the_record_only_when_the_user_ticks(tmp_path: Path) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026"), letter(2, "comes soon"), None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", lambda body: {"tracking_number": NUMBER, "date": "11/12/2026" if "2026" in body else None})
    assert list(calendar.entries()) == [NUMBER] and run.store.get("HANDLED_MESSAGES").data == [1, 2]
    run.store.put("MONITORED_SENDERS", Value.of(["a@x.example"]))
    page = run.tools["BROWSER"]
    page.answers.append(lambda items: [])                                               # confirmed with nothing ticked
    assert run.run("reset_entries").state == "KEPT" and list(calendar.entries()) == [NUMBER]
    page.answers.append(lambda items: items)
    assert run.run("reset_entries").state == "CLEAR"
    assert calendar.entries() == {} and run.store.get("HANDLED_MESSAGES").data == [] and run.store.get("NEEDS_ATTENTION").data == []
    assert run.store.get("MONITORED_SENDERS").data == ["a@x.example"]                   # the senders are kept
    assert permits(run, "calendar.remove_entries")["NO_DESTRUCTIVE_ACTIONS"] == "REMOVE_OWN_ENTRIES_EX"
    assert [json.loads(line)["what"] for line in calendar.history.read_text().splitlines()] == ["created", "removed"]


def test_reset_all_puts_every_store_back_to_its_declared_value_and_removes_the_entries(tmp_path: Path) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026"), letter(2, "comes soon"), None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", lambda body: {"tracking_number": NUMBER, "date": "11/12/2026" if "2026" in body else None})
    for name, value in (("MONITORED_SENDERS", ["a@x.example"]), ("FOUND_SENDERS", [{"address": "a@x.example", "messages": 1}]), ("TURNS", 5), ("PROBLEMS", 2)):
        run.store.put(name, Value.of(value))
    page = run.tools["BROWSER"]
    page.answers.append(lambda items: [])
    assert run.run("reset_all").state == "KEPT" and run.store.get("MONITORED_SENDERS").data == ["a@x.example"]
    page.answers.append(lambda items: items)
    assert run.run("reset_all").state == "CLEAR" and calendar.entries() == {}
    assert {name: run.store.get(name).data for name in run.project.stores} == dict(run.project.stores)    # every declared store
    reset = [s["with"]["store"] for s in run.project.workflows["reset_all"].states["CLEAR"]["steps"] if s["call"] == "store.put"]
    assert sorted(reset) == sorted(run.project.stores)                                 # a store added later and not reset fails here


def test_a_run_asked_to_stop_ends_before_its_next_step(tmp_path: Path) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026"), letter(2, f"{NUMBER} comes 11/13/2026"), None]
    calendar = FileCalendar(tmp_path / "c.ics")
    tools = delivery_tools(Page(), [], calendar=calendar, file=["n@c.example"], mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)

    def classify(subject: str, body: str) -> str:
        run.stop = True                                                                  # asked while the first message is read
        return "scheduled"
    tools["LOCAL_LLM"] = Scripted(classify=classify, copy_values=lambda subject, body: {"tracking_number": NUMBER, "date": "11/12/2026"})
    outcome = run.run("update_calendar")
    assert (outcome.ended, outcome.state) == ("stopped", "COPY_SCHEDULED") and calendar.entries() == {}
    assert run.log[-1]["call"] == "stopped" and run.store.get("HANDLED_MESSAGES").data == []


DATES = [   # what the message prints, the day it was received, date_order -> the day written, whether a note says the other order was used
    ("11/12/2026", "2026-11-10", "month_first", "2026-11-12", False),
    ("12/11/2026", "2026-11-10", "day_first", "2026-11-12", False),
    ("3/7/26", "2026-07-02", "month_first", "2026-07-03", True),          # read month first it is March: only day first is near
    ("7/3/26", "2026-07-02", "day_first", "2026-07-03", True),
    ("25.12.2026", "2026-12-20", "month_first", "2026-12-25", True),      # no month 25: only one reading exists
    ("2/3/2026", "2026-02-02", "month_first", "2026-02-03", False),       # both readings are near: the configured order
    ("2/3/2026", "2026-02-02", "day_first", "2026-02-03", True),         # 2 March is a Monday, and the message prints Tuesday
    ("2026-11-12", "2026-11-10", "day_first", "2026-11-12", False),
    ("November 12, 2026", "2026-11-10", "day_first", "2026-11-12", False),
    ("12 Nov 2026", "2026-11-10", "month_first", "2026-11-12", False),
    ("January 2nd", "2026-12-30", "month_first", "2027-01-02", False),    # no year: the one nearest to the day received
    ("11/10/2026", "2026-11-10", "month_first", "2026-11-10", False),     # the day it was received
]

WORDS = [   # what the message says, the day it was received (2026-11-10 is a Tuesday) -> the day written
    ("out for delivery today", "today", "2026-11-10", "2026-11-10"),
    ("arriving Tomorrow by 9pm", "Tomorrow", "2026-11-10", "2026-11-11"),
    ("arriving on Wednesday", "Wednesday", "2026-11-10", "2026-11-11"),
    ("arriving on Wednesday", "on Wednesday", "2026-11-10", "2026-11-11"),
    ("arriving Tuesday", "Tuesday", "2026-11-10", "2026-11-10"),          # the same day of the week: that day
    ("arriving Mon.", "Mon", "2026-11-10", "2026-11-16"),
    ("running late, arriving soon", "soon", "2026-11-10", "2026-11-10"),  # the workflow says soon is the day received
]


@pytest.mark.parametrize("says, copied, received, day", WORDS, ids=[w[1] + " " + w[3] for w in WORDS])
def test_a_day_stated_by_a_word_is_counted_from_the_day_the_message_was_received(tmp_path: Path, says: str, copied: str, received: str, day: str) -> None:
    mail = [letter(1, f"Your shipment {NUMBER} is {says}. Estimated between 10:25am and 2:25pm", received + "T15:00:00+00:00"), None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", {"tracking_number": NUMBER, "date": copied})
    made = calendar.entries()[NUMBER]
    assert made["start"] == f"{day}T08:00:00"
    assert notes(made) == f"The message says “{copied}”: the day was counted from {received}, the day the message was received."


def test_a_word_that_states_no_day_and_two_days_of_the_week_are_refused(tmp_path: Path) -> None:
    for number, (says, copied) in enumerate((("arriving shortly", "shortly"), ("arriving Monday or Tuesday", "Monday or Tuesday"),
                                             ("arriving today or tomorrow", "today or tomorrow"))):
        run, calendar = calendar_run(tmp_path / str(number), [letter(1, f"{NUMBER} is {says}"), None], "scheduled", {"tracking_number": NUMBER, "date": copied})
        assert calendar.entries() == {} and run.store.get("NEEDS_ATTENTION").data[0]["error"]["message"] == "the date is not in a form this check reads"


def test_with_no_day_of_the_week_and_both_readings_near_the_configured_order_decides(tmp_path: Path) -> None:
    for order, day in (("month_first", "2026-02-03"), ("day_first", "2026-02-10")):
        folder = tmp_path / order
        run, calendar = calendar_run(folder, [letter(1, f"{NUMBER} comes 2/3/2026 or 10/2/2026", "2026-02-02T09:00:00+00:00"), None], "scheduled",
                                     {"tracking_number": NUMBER, "date": "2/3/2026" if order == "month_first" else "10/2/2026"},
                                     ("workflows/update_calendar.yaml", "date_order: month_first", f"date_order: {order}"))
        assert (calendar.entries()[NUMBER]["start"], notes(calendar.entries()[NUMBER])) == (f"{day}T08:00:00", "")


def test_when_no_link_holds_the_number_the_entry_says_so_or_the_message_is_set_aside_as_the_workflow_says(tmp_path: Path) -> None:
    def run_it(folder: Path, *edits: tuple[str, str, str]) -> tuple[Run, FileCalendar]:
        mail = [letter(1, f"{NUMBER} comes 11/12/2026", link="https://click.carrier.example/x9f3"), None]
        return calendar_run(folder, mail, "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026"}, *edits)
    run, calendar = run_it(tmp_path / "noted")
    assert notes(calendar.entries()[NUMBER]) == "No link of the message holds the tracking number."
    run, calendar = run_it(tmp_path / "required", ("workflows/update_calendar.yaml", "number_in_link: false", "number_in_link: true"))
    assert calendar.entries() == {} and run.store.get("NEEDS_ATTENTION").data[0]["error"]["message"] == "no link of the message holds the tracking number"


TIMES = [   # what the message says of the time, what the LLM copies -> the entry's hours, and what its description says of them
    ("between 1:15 PM - 5:15 PM", {"from_time": "1:15 PM", "to_time": "5:15 PM"}, "13:15", "17:15", ""),
    ("Estimated between 10:25am and 2:25pm", {"from_time": "10:25am", "to_time": "2:25pm"}, "10:25", "14:25", ""),
    ("by 7:00 PM", {"to_time": "7:00 PM"}, "09:00", "19:00", "The message gives only a latest time, 19:00; the entry starts at 09:00."),
    ("by 8 AM", {"to_time": "8 AM"}, "07:00", "08:00", "The message gives only a latest time, 08:00; the entry starts at 07:00."),
    ("after 2:00 PM", {"from_time": "2:00 PM"}, "14:00", "19:00", "The message gives only an earliest time, 14:00; the entry ends at 19:00."),
    ("after 8:30 p.m.", {"from_time": "8:30 p.m."}, "20:30", "21:30", "The message gives only an earliest time, 20:30; the entry ends at 21:30."),
    ("from 14:00 to 16:30", {"from_time": "14:00", "to_time": "16:30"}, "14:00", "16:30", ""),
    ("by noon", {"to_time": "noon"}, "09:00", "12:00", "The message gives only a latest time, 12:00; the entry starts at 09:00."),
    ("by end of day", {"to_time": "end of day"}, "08:00", "17:00",
     "What the LLM gave as to_time was not used: the to_time is not a time of day this check reads. " + NO_TIME),
    ("by 7:00 PM", {"to_time": "19:00"}, "08:00", "17:00",          # the LLM rewrote the time: not used
     "What the LLM gave as to_time was not used: the to_time is not in the message exactly as the LLM gave it. " + NO_TIME),
    ("some day", {}, "08:00", "17:00", NO_TIME),
]


@pytest.mark.parametrize("says, copied, start, end, about", TIMES, ids=[t[0] + " " + str(len(t[1])) + t[2] for t in TIMES])
def test_the_hours_of_an_entry_are_the_times_the_message_gives(tmp_path: Path, says: str, copied: dict, start: str, end: str, about: str) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026 {says}"), None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026", **copied})
    made = calendar.entries()[NUMBER]
    assert (made["start"], made["end"]) == (f"2026-11-12T{start}:00", f"2026-11-12T{end}:00")
    assert first(made) == about


DELIVERED = [   # the delivered notice, what the LLM copies -> the entry's day and hours, the first sentence about them
    ("was delivered 11/11/2026 at 2:14 pm", {"date": "11/11/2026", "delivered_time": "2:14 pm"}, "2026-11-11", "14:14", "14:44", "Delivered at 14:14."),
    ("was delivered today at 11:52 AM", {"date": "today", "delivered_time": "11:52 AM"}, "2026-11-12", "11:52", "12:22",
     "The message says “today”: the day was counted from 2026-11-12, the day the message was received. Delivered at 11:52."),
    ("was delivered at 11:52 AM", {"delivered_time": "11:52 AM"}, "2026-11-12", "11:52", "12:22", "The day is the day the message was received. Delivered at 11:52."),
    ("was delivered", {}, "2026-11-12", "18:45", "19:00",           # no time: the message was sent shortly after the delivery
     "The day is the day the message was received. The message gives no time of delivery: the entry ends when the message was received, 19:00."),
    ("was delivered 11/11/2026", {"date": "11/11/2026"}, "2026-11-11", "08:00", "17:00",       # no time, and the day before the message
     "The message gives no time of delivery: 08:00 to 17:00 is not the time."),
    ("was delivered 11/20/2026", {"date": "11/20/2026"}, "2026-11-12", "18:45", "19:00",       # a day after the message: not used
     "What the LLM gave as date was not used: the date is not from 2 day(s) before to 0 days after the day the message was received, or is not the day "
     "of the week printed with it. The day is the day the message was received. The message gives no time of delivery: the entry ends when the message was received, 19:00."),
]


@pytest.mark.parametrize("says, copied, day, start, end, about", DELIVERED, ids=[d[0] for d in DELIVERED])
def test_a_delivered_entry_is_the_time_delivered_and_thirty_minutes(tmp_path: Path, says: str, copied: dict, day: str, start: str, end: str, about: str) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026 between 1:15 PM - 5:15 PM"), letter(2, f"{NUMBER} {says}", "2026-11-12T19:00:00+00:00"), None]
    scheduled = {"tracking_number": NUMBER, "date": "11/12/2026", "from_time": "1:15 PM", "to_time": "5:15 PM"}
    run, calendar = calendar_run(tmp_path, mail, lambda body: "delivered" if "was delivered" in body else "scheduled",
                                 lambda body: {"tracking_number": NUMBER, **copied} if "was delivered" in body else scheduled)
    made = calendar.entries()[NUMBER]
    assert (made["title"], made["start"], made["end"]) == (f"Delivered (carrier.example) {NUMBER}", f"{day}T{start}:00", f"{day}T{end}:00")
    assert first(made) == about


def test_a_delivered_notice_for_a_parcel_with_no_entry_makes_one(tmp_path: Path) -> None:
    run, calendar = calendar_run(tmp_path, [letter(1, f"{NUMBER} was delivered at 2:14 pm"), None], "delivered", {"tracking_number": NUMBER, "delivered_time": "2:14 pm"})
    made = calendar.entries()[NUMBER]
    assert (made["title"], made["start"]) == (f"Delivered (carrier.example) {NUMBER}", "2026-11-10T14:14:00") and run.store.get("NEEDS_ATTENTION").data == []


# ---- tracking links: a rule found by code, approved by the user, then filled in --------------------------------------

RULES = [   # a link of the message that holds the number -> the rule's site, path and parameter, or None when no rule is read from it
    (f"https://mydomain.example/stuff/track/?uid=123&tkid={NUMBER}&other_stuff=123457#after", ("https://mydomain.example", "/stuff/track/", "tkid")),
    (f"https://www.ups.com/track?loc=en_US&tracknum={NUMBER}&requester=NES", ("https://www.ups.com", "/track", "tracknum")),
    (f"https://WWW.Carrier.Example?n={NUMBER}", ("https://www.carrier.example", "", "n")),          # no path
    (f"http://carrier.example/track?n={NUMBER}", ("http://carrier.example", "/track", "n")),           # the site is kept as written: not https,
    (f"https://user:pw@carrier.example/track?n={NUMBER}", ("https://user:pw@carrier.example", "/track", "n")),   # a login,
    (f"https://carrier.example:8443/track?n={NUMBER}", ("https://carrier.example:8443", "/track", "n")),         # a port: for the user to see, and not to tick
    (f"https://carrier.example/t/{NUMBER}/status?utm=mail", None),                       # the number is in the path, not a parameter's value
    (f"https://carrier.example/track?n=x{NUMBER}y", None),                               # the number is a part of a parameter's value
    (f"https://carrier.example/track?n={NUMBER}&pad=" + "a" * 1000, None),                # longer than max_chars
    ("https://click.carrier.example/x9f3", None),                                        # the number is not in it
    (f"https://carrier.example/tr ack?n={NUMBER}", None),                                # a path with a space
]


def rule_of(site: str, path: str, parameter: str, sender: str = "carrier.example") -> dict[str, str]:
    return {"sender": sender, "site": site, "path": path, "parameter": parameter}


RULE = rule_of("https://carrier.example", "/track", "n")


@pytest.mark.parametrize("link, parts", RULES, ids=[r[0][:50] for r in RULES])
def test_a_link_rule_is_the_site_the_path_and_the_parameter_that_holds_the_number(link: str, parts: tuple[str, str, str] | None) -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    derived = project.declarations["links.derive"].function({"from": "x@Sender.Example".lower(), "links": ["https://other.example/", link, link]}, NUMBER, 1000)
    assert derived == {"rules": [rule_of(*parts, sender="sender.example")] if parts else []}


def test_a_link_is_written_only_when_the_user_approved_its_rule_and_a_new_rule_waits(tmp_path: Path) -> None:
    link = f"https://carrier.example/track?n={NUMBER}&session=abc"
    rule = RULE
    copied = {"tracking_number": NUMBER, "date": "11/12/2026"}
    run, calendar = calendar_run(tmp_path, [letter(1, f"{NUMBER} comes 11/12/2026", link=link), None], "scheduled", copied)
    assert calendar.entries()[NUMBER]["description"].split("\n")[0] == NO_TIME + " " + AWAITS             # no link is written
    waiting = run.store.get("LINKS_TO_APPROVE")
    assert waiting.data == [rule] and {"UNTRUSTED", "PERSONAL"} <= set(waiting.carried())                  # as it came from the mail
    # the user approves it in the conversation: the LLM chooses `links`, the user ticks the rule
    page = run.tools["BROWSER"]
    page.answers += ["approve the links", lambda items: items, "that's all"]
    run.tools["LOCAL_LLM"].answers["next_step"] = iter([{"option": "links"}, {"option": "done"}]).__next__
    run.tools["LOCAL_LLM"].answers["next_step"] = (lambda steps: (lambda **named: steps.pop(0)))([{"option": "links"}, {"option": "done"}])
    assert run.run("assistant").state == "DONE"
    approved = run.store.get("APPROVED_LINKS")
    assert approved.data == [rule] and "UNTRUSTED" not in approved.carried() and run.store.get("LINKS_TO_APPROVE").data == []
    # a later notice of the same form, with other fields in its link: the message's own link is written, whole
    mail = [letter(2, f"{NUMBER} comes 11/12/2026", link=f"https://carrier.example/track?other=9&n={NUMBER}"),
            letter(3, f"{NUMBER} comes 11/12/2026", link=f"https://evil.example/track?n={NUMBER}"), None]       # and a link elsewhere: a new rule
    run.tools["MAILBOX"] = Scripted(fetch_next=lambda senders, handled: mail.pop(0))
    seen = []
    real_upsert = calendar.upsert
    calendar.upsert = lambda key, **entry: (seen.append(entry["description"].split("\n")[0]), real_upsert(key, **entry))[1]
    assert run.run("update_calendar").state == "DONE"
    assert seen == [f"Track: https://carrier.example/track?other=9&n={NUMBER}", NO_TIME + " " + AWAITS]
    assert run.store.get("LINKS_TO_APPROVE").data == [rule_of("https://evil.example", "/track", "n")]
    assert "evil.example" not in json.dumps(calendar.entries()) and "session=abc" not in json.dumps(calendar.entries())


NOT_WRITTEN = [   # a link that holds the number, when RULE is approved -> the step at which it stopped, and whether a rule read from it awaits approval
    (f"https://carrier.example/track?n={NUMBER}&pad=" + "a" * 1000, "it is longer than 1000 characters.", False),
    (f"https://carrier.example/track?n={NUMBER}&note=<b>call now</b>", "it holds a character a web address may not hold.", False),
    (f"https://carrier.example/track?n={NUMBER}&note=call now", "it holds a character a web address may not hold.", False),
    (f"https://evil.example/track?n={NUMBER}", "its site is in no rule approved for this sender.", True),
    (f"http://carrier.example/track?n={NUMBER}", "its site is in no rule approved for this sender.", True),              # not https
    (f"https://user:pw@carrier.example/track?n={NUMBER}", "its site is in no rule approved for this sender.", True),      # a login
    (f"https://carrier.example:8443/track?n={NUMBER}", "its site is in no rule approved for this sender.", True),         # a port
    (f"https://carrier.example/other?n={NUMBER}", "its site is approved, its path is not.", True),
    (f"https://carrier.example/track/{NUMBER}", "its site is approved, its path is not.", False),                         # no rule is read from it
    (f"https://carrier.example/track?other={NUMBER}", "its site and its path are approved, but no approved parameter holds the tracking number.", True),
    (f"https://carrier.example/track?n=x{NUMBER}", "its site and its path are approved, but no approved parameter holds the tracking number.", False),
]


@pytest.mark.parametrize("link, stopped, waits", NOT_WRITTEN, ids=[n[0][:60] for n in NOT_WRITTEN])
def test_a_link_is_not_written_and_the_note_says_at_which_step_it_stopped(link: str, stopped: str, waits: bool) -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    fill = lambda links, approved, sender="x@carrier.example": project.declarations["links.fill"].function({"from": sender, "links": links}, approved, NUMBER, 1000)[0]
    said = "The message's tracking link was not written: " + stopped + (" " + AWAITS if waits else "")
    assert fill([link], [RULE]) == {"link": "", "note": AWAITS if waits and "site is in no rule" in stopped else said}
    good = f"https://carrier.example/track?utm=mail&n={NUMBER}#top"
    assert fill([link, good], [RULE]) == {"link": good, "note": ""}                      # every link is tried: a second one that passes is written, whole
    assert fill([good], [])["note"] == AWAITS                                            # no rule is approved
    assert fill([good], [RULE], "x@other.example")["note"] == AWAITS                     # approved for another sender only
    assert fill([good], [{"sender": "carrier.example", "link": "https://carrier.example/track?n={number}"}])["link"] == ""   # a rule of the earlier shape fits nothing
    assert fill(["https://carrier.example/unsubscribe"], [RULE]) == {"link": "", "note": ""}   # no link holds the number: nothing to say here
    assert fill([good.replace(NUMBER, "OTHER12345")], [RULE])["link"] == ""              # not the checked number


def test_discover_links_reads_the_mail_for_rules_and_writes_nothing_on_the_calendar(tmp_path: Path) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026", link=f"https://carrier.example/track?n={NUMBER}&a=1"),
            letter(2, f"{NUMBER} was delivered", link=f"https://carrier.example/track?n={NUMBER}&a=2"),       # the same rule: kept once
            letter(3, "20% off", link="https://shop.example/sale"), None]                                    # no number: passed over
    calendar = FileCalendar(tmp_path / "calendar.ics")
    rule = RULE
    page = Page(lambda items: [], lambda items: [])                                      # asked at once; the user approves none, then declines none
    tools = delivery_tools(page, [], calendar=calendar, file=["notices@carrier.example"], mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(copy_values=lambda subject, body: {"tracking_number": NUMBER if NUMBER in body else None}))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("discover_links").state == "APPROVED"
    assert page.offered == [[{"link": "https://carrier.example/track?n=<tracking number>", "in mail from": "carrier.example"}]] * 2   # a rule is displayed as the link it fits; offered to approve, then to decline
    assert run.store.get("LINKS_TO_APPROVE").data == [rule] and run.store.get("APPROVED_LINKS").data == []      # not ticked: it waits
    assert run.store.get("SCANNED_MESSAGES").data == [1, 2, 3] and run.store.get("HANDLED_MESSAGES").data == [] and calendar.entries() == {}
    tools["MAILBOX"] = Scripted(fetch_next=lambda senders, handled: None)
    page.answers.append(lambda items: items)                                             # started again: nothing new to read; the user ticks it
    assert run.run("discover_links").state == "APPROVED" and len(page.offered) == 3              # none is left: nothing is offered to decline
    assert run.store.get("APPROVED_LINKS").data == [rule] and run.store.get("LINKS_TO_APPROVE").data == []
    assert run.run("discover_links").state == "NONE" and page.shown[-1][0] == {"note": "The mail was read. No new form of tracking link was found."}


def test_a_declined_link_rule_is_not_offered_again_and_a_site_outside_the_senders_domain_has_a_warning(tmp_path: Path) -> None:
    survey = f"https://carrier.surveys.example/form?trackingNbr={NUMBER}"
    mail = lambda: [letter(1, f"{NUMBER} comes 11/12/2026", link=survey), None]
    declined = {"sender": "carrier.example", "site": "https://carrier.surveys.example", "path": "/form", "parameter": "trackingNbr"}
    page = Page(lambda items: [], lambda items: items)                                   # approves none, then declines the one offered
    first = mail()
    tools = delivery_tools(page, [], file=["notices@carrier.example"], mailbox=Scripted(fetch_next=lambda senders, handled: first.pop(0)),
                           llm=Scripted(copy_values=lambda subject, body: {"tracking_number": NUMBER}, classify=lambda subject, body: "scheduled"))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("discover_links").state == "APPROVED"
    assert page.offered[0] == [{"link": "https://carrier.surveys.example/form?trackingNbr=<tracking number>", "in mail from": "carrier.example",
                                "warning": "the site of this link is not carrier.example or under it"}]
    assert run.store.get("DECLINED_LINKS").data == [declined] and run.store.get("LINKS_TO_APPROVE").data == [] and run.store.get("APPROVED_LINKS").data == []
    again = mail()                                                                       # update_calendar reads the same message: the rule is not offered again
    tools["MAILBOX"] = Scripted(fetch_next=lambda senders, handled: again.pop(0))
    assert run.run("update_calendar").state == "DONE" and run.store.get("LINKS_TO_APPROVE").data == []


def test_the_shipper_is_on_the_entry_still_untrusted_and_a_later_notice_that_names_none_keeps_it(tmp_path: Path) -> None:
    shop = "Example Outfitters"
    mail = [letter(1, f"{NUMBER} comes 11/12/2026. Ship From: {shop}."), letter(2, f"{NUMBER} was delivered"), None]
    copies = {mail[0]["body"]: {"tracking_number": NUMBER, "date": "11/12/2026", "shipper": shop}, mail[1]["body"]: {"tracking_number": NUMBER}}
    seen: list[dict[str, Any]] = []
    calendar = FileCalendar(tmp_path / "calendar.ics")
    upsert = calendar.upsert
    calendar.upsert = lambda key, **entry: (seen.append(entry), upsert(key, **entry))[1]      # type: ignore[method-assign]
    tools = delivery_tools(Page(), [], calendar=calendar, file=["notices@carrier.example"],
                           mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(classify=lambda subject, body: "delivered" if "was delivered" in body else "scheduled",
                                        copy_values=lambda subject, body: copies[body]))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("update_calendar").state == "DONE"
    assert [e["title"] for e in seen] == [f"Delivery (carrier.example) shipped from {shop}, {NUMBER}", f"Delivered (carrier.example) shipped from {shop}, {NUMBER}"]
    assert [e["description"].split("\n")[0] for e in seen] == [f"Shipped from: {shop} (as the LLM read it in the sender's message; not checked)"] * 2
    assert "This message names no shipper: the shipper is the one an earlier message named." in seen[1]["description"] and "earlier" not in seen[0]["description"]
    # the name is passed to the calendar untrusted and sanitized, and one exception, written for that label, permits it
    written = entry(run, "calendar.write_entry")
    assert {"UNTRUSTED", "LLM_WRITTEN", "SANITIZED"} <= set(written["arguments"]["shipper"]) and written["arguments"]["entry"] == ["CHECKED", "PERSONAL", "UNTRUSTED"]
    assert permits(run, "calendar.write_entry")["NO_UNTRUSTED_DATA_TO_OUTPUT"] == "CHECKED_TO_CALENDAR_EX, SHIPPER_TO_CALENDAR_EX"   # each covers its parts (C2)


CLEANED_AS_HTML = "Characters were removed from a text, cleaned as html."
SHIPPERS = [   # what the LLM gave as the shipper -> the name written, the note (the workflow: html, 60 characters, trim, strip_urls)
    ("Example Outfitters", "Example Outfitters", ""),
    ("Lowe's & Sons, Inc.", "Lowe's & Sons, Inc.", ""),
    ("<b>Acme</b> <script>alert(1)</script>", "Acme alert(1)", CLEANED_AS_HTML),
    ("&lt;img src=x onerror=go()&gt; Acme", "Acme", CLEANED_AS_HTML),                     # a tag written as character references
    ("Acme <<b>b>", "Acme b", CLEANED_AS_HTML),                                           # no < or > is left
    ("Acme\x07 \u202eCo", "Acme Co", CLEANED_AS_HTML),                                    # characters that are not printed
    ("Acme https://evil.example/pay?x=1 www.evil.example evil.example/pay now", "Acme now", CLEANED_AS_HTML),   # web addresses
    ("Amazon.com", "Amazon.com", ""),                                                     # a name with a dot and no slash stays
    ("<i>" + "A" * 70 + "</i>", "A" * 60 + "...", CLEANED_AS_HTML + " A text was cut to 60 characters."),   # cut after cleaning
    (None, "", ""),
]


def test_string_clean_takes_its_options_and_refuses_a_long_text_when_it_may_not_trim() -> None:
    clean = load_project(EXAMPLES / "deliveries", "demo").declarations["string.clean"].function
    assert clean("<stuff>1234567890abcd</stuff>", "html", 10, {})[0] == {"text": "1234567890...", "note": CLEANED_AS_HTML + " A text was cut to 10 characters."}
    assert clean("see https://x.example/a", "html", 60, {})[0]["text"] == "see https://x.example/a"          # strip_urls is false when not given
    assert clean("see https://x.example/a", "html", 60, {"strip_urls": True})[0]["text"] == "see"
    with pytest.raises(ModuleError) as raised:
        clean("1234567890abcd", "html", 10, {"trim": False})
    assert raised.value.type == "too_long"
    for options, source_type in (({"trimm": True}, "html"), ({"trim": "yes"}, "html"), ({}, "sql")):
        with pytest.raises(ValueError):
            clean("x", source_type, 10, options)


@pytest.mark.parametrize("gave, name, note", SHIPPERS, ids=[str(g)[:30] for g, *_ in SHIPPERS])
def test_a_shipper_is_cleaned_never_cut_and_the_entry_says_what_was_done(tmp_path: Path, gave: str | None, name: str, note: str) -> None:
    mail = [letter(1, f"{NUMBER} comes 11/12/2026"), None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026", "shipper": gave})
    made = calendar.entries()[NUMBER]
    assert made["title"] == f"Delivery (carrier.example) " + (f"shipped from {name}, " if name else "") + NUMBER
    assert ("Shipped from: " + name + " (as the LLM read it in the sender's message; not checked)" in made["description"]) == bool(name)
    assert (note in made["description"]) and run.store.get("NEEDS_ATTENTION").data == [] and "<" not in made["title"] + made["description"]


def test_a_delivery_whose_day_passed_with_no_delivered_notice_says_so_and_a_later_notice_still_marks_it(tmp_path: Path) -> None:
    other = "1Z999AA10123456999"
    mail = [letter(1, f"{NUMBER} comes 11/12/2026", "2026-11-11T09:00:00+00:00"),
            letter(2, f"{other} comes 11/12/2026", "2026-11-11T09:00:00+00:00", link=f"https://carrier.example/track?n={other}"),
            letter(3, f"{other} was delivered at 2:14 pm", "2026-11-12T19:30:00+00:00", link=f"https://carrier.example/track?n={other}"), None]
    calendar = FileCalendar(tmp_path / "calendar.ics")
    number_of = lambda body: other if other in body else NUMBER
    tools = delivery_tools(Page(), [], calendar=calendar, file=["notices@carrier.example"], today="2026-11-13",
                           mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(classify=lambda subject, body: "delivered" if "was delivered" in body else "scheduled",
                                        copy_values=lambda subject, body: {"tracking_number": number_of(body), "date": "11/12/2026" if "comes" in body else None,
                                                                           "delivered_time": "2:14 pm" if "was delivered" in body else None}))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("update_calendar").state == "DONE"
    made = calendar.entries()
    assert made[NUMBER]["title"] == f"Delivery (carrier.example) {NUMBER}" and made[other]["title"].startswith("Delivered ")     # the 13th: one day after, not more
    tools["CLOCK"] = Scripted(today="2026-11-14")
    tools["MAILBOX"] = Scripted(fetch_next=lambda senders, handled: None)
    assert run.run("update_calendar").state == "DONE"
    made = calendar.entries()
    assert made[NUMBER]["title"] == f"No delivered notice (carrier.example) {NUMBER}" and made[NUMBER]["start"] == "2026-11-12T08:00:00"
    assert made[NUMBER]["description"].split("\n")[0] == "No notice that this parcel was delivered was received."
    assert made[other]["title"].startswith("Delivered ") and "No notice" not in made[other]["description"]                      # a delivered one is left alone
    writes = len(calendar.history.read_text().splitlines())
    assert run.run("update_calendar").state == "DONE" and len(calendar.history.read_text().splitlines()) == writes               # said once, not at every run
    late = [letter(4, f"{NUMBER} was delivered at 2:14 pm", "2026-11-14T10:00:00+00:00"), None]
    tools["MAILBOX"] = Scripted(fetch_next=lambda senders, handled: late.pop(0))
    assert run.run("update_calendar").state == "DONE"
    assert calendar.entries()[NUMBER]["title"] == f"Delivered (carrier.example) {NUMBER}" and "No notice" not in calendar.entries()[NUMBER]["description"]
    assert permits(run, "calendar.flag_unconfirmed")["NO_DESTRUCTIVE_ACTIONS"] == "FLAG_UNCONFIRMED_EX"


def test_of_what_a_sender_wrote_freely_only_the_subject_reaches_the_calendar_cleaned_and_on_a_line_that_says_so(tmp_path: Path) -> None:
    """The subject is on the entry so that the
    message can be found in a mail program: cleaned by `string.clean`, still UNTRUSTED, permitted by the exception
    that names SANITIZED. The body's text still reaches nothing."""
    mail = [{**letter(1, f"{NUMBER} comes 11/12/2026 CALL 555-0100 to pay a customs fee"), "subject": "URGENT pay now"}, None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026"})
    made = calendar.entries()[NUMBER]
    written = json.dumps(calendar.entries()) + (tmp_path / "calendar.ics").read_text()
    assert "555-0100" not in written and "customs" not in written                             # the body: nowhere
    assert "URGENT" not in made["title"]                                                      # the subject: not in the title
    assert [line for line in made["description"].split("\n") if "URGENT" in line] == ["Subject: URGENT pay now (as the sender wrote it; cleaned, not checked)"]


@pytest.mark.parametrize("printed, received, order, day, noted", DATES, ids=[f"{d[0]} {d[2]}" for d in DATES])
def test_the_date_is_copied_by_the_llm_and_read_by_code(tmp_path: Path, printed: str, received: str, order: str, day: str, noted: bool) -> None:
    weekday = datetime.date.fromisoformat(day).strftime("%A")             # the message prints the date's own day of the week
    body = f"Scheduled Delivery: {weekday}, {printed} by 7 PM. Tracking {NUMBER}"
    run, calendar = calendar_run(tmp_path, [letter(1, body, received + "T22:00:00+00:00"), None], "scheduled",
                                 {"tracking_number": NUMBER, "date": printed}, ("workflows/update_calendar.yaml", "date_order: month_first", f"date_order: {order}"))
    made = calendar.entries()[NUMBER]
    assert made["start"] == f"{day}T08:00:00"
    other = "day first" if order == "month_first" else "month first"
    assert notes(made).startswith(f"The date was read {other}, though date_order is {order}") if noted else notes(made) == ""



def test_update_calendar_gives_the_page_news_of_each_message_found_and_each_entry_written(tmp_path: Path) -> None:
    mail = [letter(1, f"Scheduled Delivery: Thursday 11/12/2026. {NUMBER}"), None]
    calendar = FileCalendar(tmp_path / "calendar.ics")
    page = Page()
    tools = delivery_tools(page, [], calendar=calendar, file=["notices@carrier.example"], mailbox=Scripted(fetch_next=lambda senders, handled: mail.pop(0)),
                           llm=Scripted(classify=lambda subject, body: "scheduled", copy_values=lambda subject, body: {"tracking_number": NUMBER, "date": "11/12/2026"}))
    run = start(EXAMPLES / "deliveries", tmp_path, tools)
    assert run.run("update_calendar").state == "DONE" and page.shown == []                   # news only: displayed when ui.yaml says show_updates
    found, written = page.updates
    assert found[0] == {"message_found": {"from": "notices@carrier.example", "received_day": "2026-11-10", "subject": ""}}
    assert set(found[1]["message_found"]["subject"]) == {"PERSONAL", "UNTRUSTED"}             # the news carries the labels of what it shows
    assert written[0]["calendar_entry_written"]["tracking_number"] == NUMBER and written[1]["calendar_entry_written"]["tracking_number"] == ["CHECKED", "PERSONAL", "UNTRUSTED"]


def test_an_entry_has_the_subject_cleaned_and_when_the_message_was_received(tmp_path: Path) -> None:
    mail = [{**letter(1, f"Scheduled Delivery: Thursday 11/12/2026. {NUMBER}", received="2026-11-10T09:14:00+00:00"),
             "subject": "Your <b>parcel</b> arrives Thursday - see https://evil.example/pay " + "x" * 90}, None]
    run, calendar = calendar_run(tmp_path, mail, "scheduled", {"tracking_number": NUMBER, "date": "11/12/2026"})
    lines = calendar.entries()[NUMBER]["description"].split("\n")
    assert lines[-1] == "From notices@carrier.example, received 2026-11-10 09:14, message 1."      # enough to find the message in a mail program
    assert lines[-2].startswith("Subject: Your parcel arrives Thursday - see x") and lines[-2].endswith("... (as the sender wrote it; cleaned, not checked)")
    assert "<b>" not in lines[-2] and "evil.example" not in lines[-2] and len(lines[-2]) < 150     # no tag, no web address, cut to 80 characters
    passed = entry(run, "calendar.write_entry")
    assert set(passed["arguments"]["subject"]) == {"PERSONAL", "SANITIZED", "UNTRUSTED"}          # still untrusted; the exception names SANITIZED
    assert permits(run, "calendar.write_entry")["NO_UNTRUSTED_DATA_TO_OUTPUT"] == "CHECKED_TO_CALENDAR_EX, SHIPPER_TO_CALENDAR_EX"   # each covers its parts (C2)
