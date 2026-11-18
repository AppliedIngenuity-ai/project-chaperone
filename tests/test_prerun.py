"""The pre-run check and its acceptance (specification, section 9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chaperone import load_project, prerun
from chaperone.engine import Run
from chaperone.store import Store
from chaperone.values import Problem
from chaperone.__main__ import main
from tests.test_engine import (EXAMPLES, FOUND, Page, Scripted, copy_of, delivery_tools, mail_tools,
                                     rag_tools, start, tiny)


def check(folder: Path) -> prerun.Result:
    return prerun.check(load_project(folder))


def refused(result: prerun.Result) -> list[tuple[str, str]]:
    return [(entry["call"], entry["rule"]) for entry in result.refused]


# ---- the three projects ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, checked, idle", [("ask_my_mail", 6, []), ("safe_rag", 113, []), ("deliveries", 130, [])])
def test_no_call_of_an_example_project_would_be_refused_and_every_exception_permits_one(name: str, checked: int, idle: list[str]) -> None:
    """Under the setup `local`."""
    result = check(EXAMPLES / name)
    assert result.refused == [] and result.idle == idle and result.unsaved == [] and result.checked == checked


def test_safe_retrieval_under_the_setup_hosted_is_a_possible_violation_until_the_user_adds_the_exception_at_the_start(tmp_path: Path) -> None:
    project = load_project(EXAMPLES / "safe_rag", "hosted")
    result = prerun.check(project)
    assert sorted((r["call"], r["rule"]) for r in result.refused) == sorted(                   # the LLM's call, in each workflow
        [(f"{workflow}.{state}: {module}", "NOTHING_LEAVES_NETWORK")
         for workflow, module in (("answer", "llm.answer"), ("multi_pass", "llm.answer_with_concepts")) for state in ("ANSWER", "ANSWER_2")]
        + [("just_ask_the_llm.ANSWER: llm.answer_alone", "NOTHING_LEAVES_NETWORK"), ("typical_rag.ANSWER: llm.answer_plainly", "NOTHING_LEAVES_NETWORK"),
           ("multi_pass.JUDGE: llm.judge_relevance", "NOTHING_LEAVES_NETWORK"), ("multi_pass.READ: llm.read_question", "NOTHING_LEAVES_NETWORK"),
           ("single_pass.READ: llm.read_question", "NOTHING_LEAVES_NETWORK"), ("single_pass.ANSWER: llm.answer_with_concepts", "NOTHING_LEAVES_NETWORK")])
    said: list[str] = []
    answers = ["1", "1", "1", "user"]                                                         # rules 1 to 3 are asked; 4 and 5 have nothing to accept
    assert prerun.at_start(project, result, tmp_path, lambda prompt: answers.pop(0), said.append, True) and answers == []
    assert "        USER_NOTHING_LEAVES_NETWORK_EX  except: {call: [llm.answer, llm.answer_alone, llm.answer_plainly, llm.answer_with_concepts, llm.judge_relevance, llm.read_question]}" in said
    assert prerun.check(project).refused == [] and "USER_NOTHING_LEAVES_NETWORK_EX" in prerun.user_exceptions_file(tmp_path).read_text()


def test_an_exception_for_one_field_is_seen_to_permit_its_call() -> None:
    """The defect the mock found: with requests declared by field, SHOW_CHECKED_EX is not reported as permitting nothing."""
    result = check(EXAMPLES / "safe_rag")
    calls = ("NOTHING_FOUND: user.show", "SHOW: user.show", "SHOW_2: user.show", "VERIFY: decide", "VERIFY_2: decide")
    for workflow, module, more in (("answer", "verify.response", ()), ("multi_pass", "verify.scored", ("FIRST_AGAIN: decide",))):
        decides = sorted(calls[3:] + more)                                                    # the second workflow has one more branch on the check's result
        assert [c for c in result.permits["SHOW_CHECKED_EX"] if c.startswith(workflow + ".")] == [f"{workflow}.{c}" for c in calls[:3]]
        assert [c for c in result.permits["SUPPORTED_DECIDES_EX"] if c.startswith(workflow + ".")] == [f"{workflow}.{c}" for c in decides]
        assert all(result.depends[f"{workflow}.{c}"] == [module] for c in calls + more)
    assert len(result.depends) == 14


def test_the_result_names_the_modules_whose_requests_a_call_depends_on() -> None:
    depends = check(EXAMPLES / "deliveries").depends
    assert depends["update_calendar.WRITE: calendar.write_entry"] == ["check.delivery_values", "links.fill", "string.clean"]   # the values, the shipper, the link
    assert set(depends) == {"update_calendar.WRITE: calendar.write_entry", "update_calendar.MARK: calendar.mark_delivered"}


def test_what_the_engine_permits_in_a_run_the_check_listed_before_it(tmp_path: Path) -> None:
    """Every call a run was permitted by an exception is among the calls the pre-run result lists for that exception."""
    page = Page("track my UPS packages", lambda items: items[:2], "also add an address", {"addresses": ["t@fedex.example"]}, lambda items: items, "that's all")
    mail = [{"id": 1, "received_day": "2026-11-10", "received_time": "09:00", "body": "1Z999AA101 on 11/12/2026", "links": ["https://c.example/1Z999AA101"]}, {"id": 2, "received_day": "2026-11-10", "received_time": "09:00", "body": "1Z999AA101 soon", "links": ["https://c.example/1Z999AA101"]}, None]
    runs = [("ask_my_mail", "ask_my_mail", mail_tools()), ("safe_rag", "answer", rag_tools()),
            ("deliveries", "assistant", delivery_tools(page, [{"option": "search", "terms": ["UPS"]}, {"option": "add"}, {"option": "done"}])),
            ("deliveries", "update_calendar", delivery_tools(Page(), [], mailbox=Scripted(fetch_next=lambda s, h: mail.pop(0)), file=["a@x.example"],
                llm=Scripted(classify=lambda subject, body: "scheduled", copy_values=lambda subject, body: {"tracking_number": "1Z999AA101", "date": "11/12/2026" if "2026" in body else "soon"})))]
    for number, (name, workflow, tools) in enumerate(runs):
        result = check(EXAMPLES / name)
        run = start(EXAMPLES / name, tmp_path / str(number), tools)
        run.run(workflow)
        seen = 0
        for entry in run.log:
            call = entry["call"]
            for verdict in entry.get("forbidden_by", []):
                assert verdict["permitted_by"] is not None
                listed = result.permits[verdict["permitted_by"]]
                assert any(c.startswith(f"{entry['workflow']}.{entry['state']}: ") and c.endswith(call) for c in listed), entry
                seen += 1
        assert seen > 0


# ---- changes that produce a refused call, each found before anything runs ------------------------------------------

REMOTE = (("setups/demo.yaml", "modules:", "  REMOTE_LLM: {client: {endpoint: configs/llm/vendor.yaml}, facts: {OUTPUT: false, LEAVES_NETWORK: true, adds: [UNTRUSTED]}}\nmodules:"),
          ("setups/demo.yaml", "llm.answer: {tool: LOCAL_LLM}", "llm.answer: {tool: REMOTE_LLM}"))
SEND_MAIL = (("modules/mail_send.py", 'which states OUTPUT false.\n"""\n',
               'which states OUTPUT false.\n"""\n\nimport chaperone\n\n\n@chaperone.module("mail.send.message", properties={"DESTRUCTIVE": True})\n'
               'def mail_send_message(ctx, to):\n    return ctx.send(to)\n'),
             ("workflows/assistant.yaml", '      - {call: store.get, with: {store: MONITORED_SENDERS}, save_as: monitored}\n      - {call: get,       with: {value: {monitored_senders:',
              '      - {call: store.get, with: {store: MONITORED_SENDERS}, save_as: monitored}\n      - {call: mail.send.message, with: {to: "{vars.monitored}"}}\n      - {call: get,       with: {value: {monitored_senders:'))
CASES = [
    ("a remote LLM", "ask_my_mail", REMOTE, [("ask_my_mail.MAIN: llm.answer", "NOTHING_LEAVES_NETWORK")]),
    ("a module that sends mail, passed the monitored senders", "deliveries", SEND_MAIL,
     [("assistant.DONE: mail.send.message", "NO_DESTRUCTIVE_ACTIONS"), ("assistant.DONE: mail.send.message", "NO_PERSONAL_DATA_OUTPUT")]),
    ("the exception for the calendar removed", "deliveries", (("exceptions.yaml", "  - {id: CALENDAR_APPROVED_EX,    rule: NOTHING_LEAVES_NETWORK,      except: {call: {where: {GOOGLE_CALENDAR: true}}}}\n", ""),),
     [("reset_all.CLEAR: calendar.remove_entries", "NOTHING_LEAVES_NETWORK"), ("reset_entries.CLEAR: calendar.remove_entries", "NOTHING_LEAVES_NETWORK"),
      ("update_calendar.DONE: calendar.flag_unconfirmed", "NOTHING_LEAVES_NETWORK"), ("update_calendar.MARK: calendar.mark_delivered", "NOTHING_LEAVES_NETWORK"),
      ("update_calendar.WRITE: calendar.write_entry", "NOTHING_LEAVES_NETWORK")]),
    ("the reply shown without the verification", "safe_rag", (("workflows/answer.yaml", 'report: "{vars.screen}", marks', 'report: "{vars.response}", marks'),),
     [("answer.SHOW: user.show", "NO_UNTRUSTED_DATA_TO_OUTPUT")]),
    ("the error itself shown", "safe_rag", (("workflows/answer.yaml", 'report: {note: "The LLM\'s reply could not be read', 'report: {error: "{error}", note: "The LLM\'s reply could not be read'),),
     [("answer.BAD_REPLY: user.show", "NO_UNTRUSTED_DATA_TO_OUTPUT")]),
    ("a branch on the LLM's reply before the check", "safe_rag", (("workflows/answer.yaml", "    on_error: NO_LLM\n    next: VERIFY",
        '    on_error: NO_LLM\n    next: {switch: "{vars.response.text}", goto: {"none": DONE}, else: VERIFY}'),),
     [("answer.ANSWER: decide", "NO_DECISIONS_FROM_UNTRUSTED")]),
]


@pytest.mark.parametrize("what, name, edits, expected", CASES, ids=[c[0] for c in CASES])
def test_a_change_that_produces_a_refused_call_is_found_before_a_run(tmp_path: Path, what: str, name: str, edits: Any, expected: Any) -> None:
    assert refused(check(copy_of(tmp_path, name, *edits))) == expected


def test_a_refused_call_is_listed_with_a_route_to_it(tmp_path: Path) -> None:
    result = check(copy_of(tmp_path, "deliveries", *SEND_MAIL))
    personal = next(entry for entry in result.refused if entry["rule"] == "NO_PERSONAL_DATA_OUTPUT")
    assert personal["route"] == ["GREET", "TURN", "ROUTE", "DONE"] and personal["label"] == "PERSONAL"
    text = prerun.report(result, None)
    assert ("REFUSED assistant.DONE: mail.send.message is forbidden by NO_PERSONAL_DATA_OUTPUT (an argument may carry PERSONAL); "
            "no exception permits it. Route: GREET > TURN > ROUTE > DONE") in text
    assert "not accepted; nothing runs. A refused call is answered at the terminal" in text


def test_an_exception_that_permits_no_call_is_named(tmp_path: Path) -> None:
    result = check(copy_of(tmp_path, "ask_my_mail", ("exceptions.yaml", "exceptions:", "exceptions:\n  - {id: UNUSED, rule: NO_DESTRUCTIVE_ACTIONS, except: {call: [mail.search]}}")))
    assert result.idle == ["UNUSED"] and "exceptions no call needs: 1 (UNUSED)" in prerun.report(result, None)


# ---- on a project of three modules -----------------------------------------------------------------------------------

UNSAVED = """workflow: flow
start: A
on_error: FAILED
states:
  A:
    steps: [{call: make.value, with: {value: 1}, save_as: first}]
    next: {switch: "{vars.first}", goto: {1: B, 2: C}}
  B:
    steps: [{call: make.value, with: {value: 2}, save_as: second}]
    next: C
  C:
    steps: [{call: make.value, with: {value: "{vars.second}"}, save_as: third}]
    next: DONE
  DONE: {end: true}
  FAILED:
    steps: [{call: make.value, with: {value: "{vars.first}"}}]
    end: true
  ISLAND:
    steps: [{call: source.read, save_as: text}, {call: make.fail, with: {type: "{vars.text}"}}]
    next: {switch: "{vars.text}", goto: {a: DONE}}
"""


def test_a_reference_some_route_does_not_save_first_is_listed(tmp_path: Path) -> None:
    result = check(tiny(tmp_path, UNSAVED))
    assert result.unsaved == [{"call": "flow.C: make.value", "name": "second"},        # A goes straight to C on 2
                              {"call": "flow.FAILED: make.value", "name": "first"}]     # A's own step may be the one that fails
    assert "UNSAVED flow.C: make.value references second, which some route does not save first" in prerun.report(result, None)


ERRORS = """workflow: flow
start: A
on_error: PLAIN
states:
  A:
    steps: [{call: source.read, save_as: text}, {call: make.fail, with: {type: "{vars.text}"}}]
    on_error: TAINTED
    next: B
  B:
    steps: [{call: make.fail, with: {type: other}}]
    next: DONE
  TAINTED:
    on_error: DONE
    next: {switch: "{error.type}", goto: {a: DONE}, else: DONE}
  PLAIN:
    next: {switch: "{error.type}", goto: {a: DONE}, else: DONE}
  DONE: {end: true}
"""


def test_an_error_is_known_in_the_state_it_routes_to_and_in_no_other(tmp_path: Path) -> None:
    """A's error carries UNTRUSTED (its step was passed what the source returned) and routes to TAINTED only; B's
    carries no label and routes to PLAIN. Kept by workflow, as it was, PLAIN's branch was reported too."""
    assert refused(check(tiny(tmp_path, ERRORS))) == [("flow.TAINTED: decide", "NO_DECISIONS_FROM_UNTRUSTED")]


def test_a_module_that_can_fail_while_passed_a_label_is_a_call_of_log_the_rules_check(tmp_path: Path) -> None:
    """ERRORS: A's module is passed what the source returned, so what it writes in an error can hold it; B's is not."""
    from tests.test_engine import LOG_IS_OUTPUT, LOG_PERMITTED
    folder = tiny(tmp_path, ERRORS)
    setup = folder / "setups" / "local.yaml"
    setup.write_text(setup.read_text().replace(*LOG_IS_OUTPUT))
    assert ("flow.A: log", "NO_UNTRUSTED_DATA_TO_OUTPUT") in refused(check(folder))
    assert not any(call == "flow.B: log" for call, _ in refused(check(folder)))
    (folder / "exceptions.yaml").write_text(LOG_PERMITTED)
    result = check(folder)
    assert not any(call.endswith(": log") for call, _ in refused(result)) and result.permits["ERRORS_IN_LOG_EX"] == ["flow.A: log"]


TWICE = """workflow: flow
start: A
on_error: DONE
states:
  A:
    steps:
      - {call: source.read, save_as: text}
      - {call: make.value, with: {value: "{vars.text}"}}
      - {call: make.value, with: {value: plain}}
    next: DONE
  DONE: {end: true}
"""


def test_a_state_that_calls_a_module_twice_keeps_the_verdict_of_each_call(tmp_path: Path) -> None:
    """The second call is passed a value written in the file; the first, what the source returned. The verdict of
    the first was replaced by that of the second, so its refusal was reported nowhere."""
    folder = tiny(tmp_path, TWICE)
    (folder / "rules.yaml").write_text("imports: [../rules/default.yaml]\nrules:\n  - id: NO_UNTRUSTED_VALUES\n    never: {call: [make.value], carrying: {UNTRUSTED: true}}\n")
    assert refused(check(folder)) == [("flow.A: make.value", "NO_UNTRUSTED_VALUES")]


def test_a_refused_call_in_a_state_no_route_reaches_is_said_so(tmp_path: Path) -> None:
    result = check(tiny(tmp_path, UNSAVED))
    island = [entry for entry in result.refused if entry["call"].startswith("flow.ISLAND")]
    assert [e["call"] for e in island] == ["flow.ISLAND: decide"]
    assert all(entry["route"] is None for entry in island)
    assert "Route: no route from the start reaches this state" in prerun.report(result, None)


FALSE_RULE = """workflow: flow
start: MAIN
on_error: FAILED
states:
  MAIN:
    steps:
      - {call: source.read, save_as: text}
      - {call: make.value, with: {value: "{vars.text}"}}
      - {call: make.value, with: {value: written}}
    end: true
  FAILED: {end: true}
"""


def test_a_rule_on_a_label_that_is_absent_forbids_alike_before_and_in_a_run(tmp_path: Path) -> None:
    folder = tiny(tmp_path, FALSE_RULE)
    (folder / "rules.yaml").write_text("imports: [../rules/default.yaml]\nrules:\n  - {id: ONLY_UNTRUSTED, never: {call: [make.value], carrying: {UNTRUSTED: false}}}\n")
    assert refused(check(folder)) == [("flow.MAIN: make.value", "ONLY_UNTRUSTED")]
    run = start(folder, tmp_path, {"SOURCE": Scripted(read="text")})
    outcome = run.run("flow")
    assert [e.get("error", {}).get("type") for e in run.log if e["call"] == "make.value"] == [None, "refused"]
    assert outcome.state == "FAILED" and "forbidden by ONLY_UNTRUSTED" in run.reasons[max(run.reasons)]["message"]


# ---- acceptance -------------------------------------------------------------------------------------------------------

def test_a_run_starts_only_under_an_accepted_result(tmp_path: Path) -> None:
    project, state = load_project(EXAMPLES / "ask_my_mail", "demo"), tmp_path / "state"
    with pytest.raises(prerun.NotAccepted, match="is not accepted"):
        prerun.open_run(project, mail_tools(), state)
    result = prerun.check(project)
    entry = prerun.accept(result, state, "user")
    assert entry["by"] == "user" and prerun.acceptance_of(result, state) == entry
    prerun.accept(result, state, "user")                                     # accepting again records it once
    assert len(prerun.accepted(state)) == 1
    run = prerun.open_run(project, mail_tools(), state)
    assert run.run("ask_my_mail").ended == "end" and (state / "access.jsonl").is_file()


def test_a_changed_result_is_not_the_accepted_one_and_a_comment_changes_nothing(tmp_path: Path) -> None:
    folder, state = copy_of(tmp_path, "ask_my_mail"), tmp_path / "state"
    prerun.accept(check(folder), state, "user")
    (folder / "exceptions.yaml").write_text((folder / "exceptions.yaml").read_text() + "# a comment\n")
    prerun.open_run(load_project(folder), mail_tools(), state)               # the result says the same
    text = (folder / "setups" / "demo.yaml").read_text().replace("adds: [UNTRUSTED]}}", "adds: []}}")
    (folder / "setups" / "demo.yaml").write_text(text)                      # the LLM declared trusted
    with pytest.raises(prerun.NotAccepted):
        prerun.open_run(load_project(folder), mail_tools(), state)


def test_a_result_that_lists_a_refused_call_cannot_be_accepted(tmp_path: Path) -> None:
    result = check(copy_of(tmp_path, "ask_my_mail", *REMOTE))
    with pytest.raises(Problem, match="lists 1 refused call"):
        prerun.accept(result, tmp_path / "state", "user")
    assert prerun.accepted(tmp_path / "state") == []


def test_the_check_and_accept_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = str(tmp_path / "state")
    assert main(["check", str(EXAMPLES / "deliveries"), "--state", state]) == 0
    out = capsys.readouterr().out
    assert out.startswith("pre-run check: project deliveries, setup demo\n  refused calls: 0\n  calls checked: 130\n")
    assert "not accepted; nothing runs until it is" in out and "MAILBOX_APPROVED_EX permits" not in out
    assert main(["accept", str(EXAMPLES / "deliveries"), "--state", state, "--by", "user"]) == 0
    assert "accepted by user" in capsys.readouterr().out
    assert main(["check", str(EXAMPLES / "deliveries"), "--state", state, "--all"]) == 0
    out = capsys.readouterr().out
    assert "accepted by user" in out and "PERSONAL_TO_CALENDAR_EX permits 2 call(s)" in out
    assert "    update_calendar.WRITE: calendar.write_entry: rests on check.delivery_values, links.fill, string.clean" in out
    broken = copy_of(tmp_path, "ask_my_mail", *REMOTE)
    assert main(["accept", str(broken), "--state", state, "--by", "user"]) == 1
    assert "NOT ACCEPTED: the result lists 1 refused call(s)" in capsys.readouterr().out


def test_a_module_that_declares_requests_by_field_returns_no_other_field(tmp_path: Path) -> None:
    folder = copy_of(tmp_path, "safe_rag", ("modules.py", '"documents": list(documents.values()), "left_out": read["left_out"]}', '"documents": list(documents.values()), "left_out": read["left_out"], "raw": response}'))
    run = start(folder, tmp_path, rag_tools())
    outcome = run.run("answer")
    assert outcome.state == "DONE" and [e["error"]["type"] for e in run.log if e["call"] == "verify.response"] == ["refused"]   # by VERIFY's own error state, BAD_REPLY


# ---- the report at the start of the program, and its acceptance at the terminal ---------------------------------------

def _at_start(project: Any, state: Path, *answers: str, asks: bool = True) -> tuple[bool, list[str], list[str]]:
    said, left = [], list(answers)

    def read(prompt: str) -> str:                            # with no answer left, the terminal has closed
        if not left:
            raise EOFError
        return left.pop(0)
    accepted = prerun.at_start(project, prerun.check(project), state, read, said.append, asks)
    return accepted, said, left


def test_at_the_start_the_exceptions_to_each_rule_are_accepted_one_rule_at_a_time(tmp_path: Path) -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    accepted, said, left = _at_start(project, tmp_path, "1", "2", "1", "1", "1", "1", "user")
    assert accepted and left == [] and said[-1].startswith("ACCEPTED by user, ")
    assert any(line.startswith("RULE 2 of 5: NO_DECISIONS_FROM_UNTRUSTED  (") and line.endswith("examples/rules/default.yaml:9)") for line in said)
    assert "  never: {call: [decide], carrying: {UNTRUSTED: true}}" in said                    # the rule as written
    assert any(line.startswith("    LLM_CHOOSES_STEP_EX  except: {call: [decide]}  (") and line.endswith("examples/deliveries/exceptions.yaml:7)") for line in said)
    assert "    assistant.ROUTE: decide" in said                                              # more details: the calls the exception permits
    assert "  Exceptions written in the configuration; each is accepted before a run:" in said
    assert "      permits calls in 1 state: update_calendar.DONE" in said                         # FLAG_UNCONFIRMED_EX: where it permits a call
    assert any(line.startswith("      permits calls in ") and line.endswith(", ...") and line.count(",") == 5 for line in said)   # more than five states: the first five
    assert said.count("  [1] Accept   [2] More details   [3] Skip for now: the program does not start until this rule is accepted") == 6   # five rules, one asked again after its details
    accepted, said, _ = _at_start(project, tmp_path / "other", "1", "1", "2", "3")                      # the details of rule 3: the calls that rest on label requests
    at = said.index("  Refused under this rule if the label requests of the modules named were not applied:")
    assert said[at + 2] == "    update_calendar.WRITE: calendar.write_entry  (check.delivery_values, links.fill, string.clean)"
    again, said, left = _at_start(project, tmp_path, "never read")                            # accepted and unchanged: said, nothing listed, nothing asked
    assert again and left == ["never read"] and said[2].startswith("Previously accepted, no changes: by user, ") and ", rule by rule, at the terminal; result " in said[2]
    record = prerun.acceptance_of(prerun.check(project), tmp_path)                            # what is kept: who, when, how, and the exceptions accepted
    assert record["rule_by_rule"] is True and len(record["exceptions"]) == 12 and "WRITE_ENTRY_EX" in record["exceptions"]
    assert not any(line.startswith("RULE ") for line in said)
    at = next(i for i, line in enumerate(said) if line.startswith("WARNING "))                # the line of the setup, with its place and as written
    assert said[at].endswith("demo.yaml:13: no module matches this line of `modules`; no call changes.") and said[at + 1] == "    mail.send.*: {tool: MAILBOX, facts: {OUTPUT: true}}"


def test_at_the_start_a_skipped_rule_or_no_terminal_records_nothing(tmp_path: Path) -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    accepted, said, _ = _at_start(project, tmp_path, "1", "3", "1", "1", "1")                 # rule 2 skipped: the others are still listed and asked
    assert not accepted and said[-2:] == ["SKIPPED: RULE 2.", "NOT ACCEPTED. Nothing was recorded."]
    assert any(line.startswith("RULE 5 of 5: ") for line in said)
    accepted, said, _ = _at_start(project, tmp_path, asks=False)
    assert not accepted and said[-1] == "NOT ACCEPTED." and "Not accepted. There is no terminal to ask at." in said
    assert not prerun.accepted_file(tmp_path).exists()


def test_at_the_start_a_possible_violation_is_asked_with_its_exception_which_is_kept_outside_the_configuration(tmp_path: Path) -> None:
    from chaperone.load import add_user_exceptions
    def without() -> Any:
        project = load_project(EXAMPLES / "deliveries", "demo")
        project.exceptions[:] = [e for e in project.exceptions if e["id"] != "WRITE_ENTRY_EX"]
        add_user_exceptions(project, prerun.user_exceptions_file(tmp_path))
        return project
    project = without()
    accepted, said, _ = _at_start(project, tmp_path, "1", "1", "1", "3", "1")                 # the possible violation skipped: nothing is kept
    assert not accepted and not prerun.user_exceptions_file(tmp_path).exists() and not prerun.accepted_file(tmp_path).exists()
    assert said[-2] == "SKIPPED: RULE 4."
    assert said[1:3] == ["5 rules, 11 exceptions in the configuration, 0 approved at a start, 1 possible violation.",
                         "Not accepted: 1 possible violation, at RULE 4. Each rule is asked below; nothing is recorded until the last."]
    at = said.index("  POSSIBLE VIOLATION: no exception permits this call, in the configuration or approved at a start:")
    assert said[at + 1:at + 7] == ["    update_calendar.WRITE: calendar.write_entry",
                                   f"  [1] Permit this call: add this exception to {prerun.user_exceptions_file(tmp_path)}",
                                   "        USER_NO_DESTRUCTIVE_ACTIONS_EX  except: {call: [calendar.write_entry]}",
                                   "  [2] More details",
                                   "  [3] Skip for now: the program does not start",
                                   "  [4] Keep this call forbidden, as an acknowledged risk: the program starts; a run refuses it when it is made"]
    assert not _at_start(without(), tmp_path, asks=False)[0]                                  # no terminal: nothing is added
    accepted, said, _ = _at_start(project, tmp_path, *"11111", "user")
    assert accepted and said[-2].startswith("ADDED 1 exception to ") and said[-2].endswith("user_exceptions.yaml: USER_NO_DESTRUCTIVE_ACTIONS_EX.")
    assert "  - {id: USER_NO_DESTRUCTIVE_ACTIONS_EX, rule: NO_DESTRUCTIVE_ACTIONS, except: {call: [calendar.write_entry]}}" in prerun.user_exceptions_file(tmp_path).read_text()
    again = without()                                                                         # started again: read from the state folder, in force, nothing asked
    added = [e for e in again.exceptions if e.get("user")]
    assert [e["id"] for e in added] == ["USER_NO_DESTRUCTIVE_ACTIONS_EX"] and prerun.check(again).refused == []
    accepted, said, left = _at_start(again, tmp_path, "never read")
    assert accepted and left == ["never read"] and said[1] == "5 rules, 11 exceptions in the configuration, 1 approved at a start, 0 possible violations."
    prerun.user_exceptions_file(tmp_path).unlink()                                            # the line withdrawn: asked again
    assert len(prerun.check(without()).refused) == 1


def test_a_possible_violation_acknowledged_as_a_risk_starts_and_the_run_refuses_its_calls(tmp_path: Path) -> None:
    """The third answer to a possible violation : no exception; the program starts; a run refuses
    each of those calls when it is made. Kept in the state folder; a deleted line ends the acceptance."""
    project = load_project(EXAMPLES / "share_notes", "demo")
    assert len(prerun.check(project).refused) == 2
    with pytest.raises(Problem, match="no acknowledged risk names"):                          # the command accepts no refused call
        prerun.accept(prerun.check(project), tmp_path, "user")
    accepted, said, left = _at_start(project, tmp_path, "1", "4", "4", "user")                # rule 1 accepted; rules 3 and 5 acknowledged
    assert accepted and left == [] and said[-1].startswith("ACCEPTED by user, ")
    at = next(i for i, line in enumerate(said) if line.startswith("ACKNOWLEDGED 2 risks, kept in "))
    assert said[at].endswith("acknowledged_risks.yaml; a run refuses each of these calls when it is made:")
    assert said[at + 1:at + 3] == ["    NO_UNTRUSTED_DATA_TO_OUTPUT: share_summary.MAIN: mail.send.message",
                                   "    NO_PERSONAL_DATA_OUTPUT: share_summary.MAIN: mail.send.message"]
    assert not prerun.user_exceptions_file(tmp_path).exists()                                 # no exception was added: nothing permits the send
    result = prerun.check(project)
    assert len(result.refused) == 2 and prerun.unacknowledged(result, tmp_path) == []
    record = prerun.acceptance_of(result, tmp_path)
    assert record["acknowledged"] == ["NO_PERSONAL_DATA_OUTPUT: share_summary.MAIN: mail.send.message",
                                      "NO_UNTRUSTED_DATA_TO_OUTPUT: share_summary.MAIN: mail.send.message"]
    mailbox = Scripted(send={"sent": True})                                                   # a run opens, and refuses the send before the module runs
    tools = {"NOTES": Scripted(search=[{"title": "Trip", "text": "Flight on the 3rd."}]), "LOCAL_LLM": Scripted(summarize="You fly on the 3rd."),
             "MAILBOX": mailbox, "BROWSER": Page()}
    run = prerun.open_run(project, tools, tmp_path)
    assert run.run("share_summary").state == "FAILED" and mailbox.calls == []
    refused = next(e for e in run.log if e["call"] == "mail.send.message")
    assert refused["error"]["type"] == "refused" and refused["arguments"]["body"] == ["LLM_WRITTEN", "PERSONAL", "UNTRUSTED"]
    again, said, left = _at_start(project, tmp_path, "never read")                            # started again: accepted, and the risks are listed
    assert again and left == ["never read"] and said[1] == "5 rules, 3 exceptions in the configuration, 0 approved at a start, 2 possible violations, 2 of them acknowledged."
    assert said[3].startswith("ACKNOWLEDGED RISKS, 2: no exception permits these calls; a run refuses each when it is made. Kept in ")
    assert said[4:6] == ["    NO_UNTRUSTED_DATA_TO_OUTPUT: share_summary.MAIN: mail.send.message", "    NO_PERSONAL_DATA_OUTPUT: share_summary.MAIN: mail.send.message"]
    text = prerun.acknowledged_file(tmp_path).read_text()                                     # one line withdrawn: not accepted, asked again for that rule only
    prerun.acknowledged_file(tmp_path).write_text("\n".join(line for line in text.splitlines() if "NO_PERSONAL_DATA_OUTPUT" not in line) + "\n")
    assert prerun.acceptance_of(result, tmp_path) is None
    with pytest.raises(prerun.NotAccepted):
        prerun.open_run(project, tools, tmp_path)
    accepted, said, _ = _at_start(project, tmp_path, "1", "1", "1", "user")                   # rule 3: its risk stays; rule 5: the exception is added
    assert accepted and "  ACKNOWLEDGED RISK: no exception permits these calls; a run refuses each when it is made:" in said
    assert [e["id"] for e in project.exceptions if e.get("user")] == ["USER_NO_PERSONAL_DATA_OUTPUT_EX"]
    assert [(r["rule"], r["call"]) for r in prerun.check(project).refused] == [("NO_UNTRUSTED_DATA_TO_OUTPUT", "share_summary.MAIN: mail.send.message")]
    prerun.acknowledged_file(tmp_path).write_text("acknowledged_risks: nothing\n")           # a file of another form is refused, not read as empty
    with pytest.raises(Exception, match="acknowledged_risks"):
        prerun.acknowledged(tmp_path)


def test_a_workflow_file_with_steps_and_no_named_state_is_refused(tmp_path: Path) -> None:
    """Every workflow names its states: a file of `workflow` and `steps` alone is refused."""
    old = "workflow: share_summary\nsteps:\n  - {call: notes.search, with: {keyword: trip}, save_as: notes}\n"
    folder = copy_of(tmp_path, "share_notes")
    (folder / "workflows" / "share_summary.yaml").write_text(old)
    with pytest.raises(Exception, match="`steps` are written inside a named state"):
        load_project(folder, "demo")


def test_an_exception_written_differently_is_accepted_again_and_no_name_is_no_acceptance(tmp_path: Path) -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    accepted, said, _ = _at_start(project, tmp_path, *"11111")                                # the terminal closes at the question for a name
    assert not accepted and said[-1] == "NOT ACCEPTED: no name was given. Nothing was recorded." and not prerun.accepted_file(tmp_path).exists()
    assert _at_start(project, tmp_path, *"11111", "user")[0]
    changed = next(e for e in project.exceptions if e["id"] == "WRITE_ENTRY_EX")
    changed["except"] = {"call": ["calendar.write_entry", "calendar.write_entry"]}            # it permits the same calls, written differently
    result = prerun.check(project)
    assert result.refused == [] and prerun.acceptance_of(result, tmp_path) is None            # displayed differently: asked again


def test_share_notes_reports_the_send_before_a_run_and_refuses_it_in_one(tmp_path: Path) -> None:
    """The example of the paper's figure "what it is": a note that is PERSONAL, an
    LLM's summary of it (PERSONAL, UNTRUSTED, LLM_WRITTEN), shown to the user and then passed to a module that sends
    e-mail. Before a run: two possible violations, both the send. In a run started anyway: the show is permitted,
    the send is refused, and nothing is sent."""
    project = load_project(EXAMPLES / "share_notes", "demo")
    result = prerun.check(project)
    assert {(r["rule"], r["call"]) for r in result.refused} == {
        ("NO_UNTRUSTED_DATA_TO_OUTPUT", "share_summary.MAIN: mail.send.message"),
        ("NO_PERSONAL_DATA_OUTPUT", "share_summary.MAIN: mail.send.message")}
    assert result.idle == []
    mailbox = Scripted(send={"sent": True})
    tools = {"NOTES": Scripted(search=[{"title": "Trip", "text": "Flight on the 3rd."}]), "LOCAL_LLM": Scripted(summarize="You fly on the 3rd."),
             "MAILBOX": mailbox, "BROWSER": Page()}
    run = Run(project, tools, Store(tmp_path / "store.json", project.stores), tmp_path / "access.jsonl")
    assert run.run("share_summary").state == "FAILED" and mailbox.calls == []                 # refused before the module runs
    by_call = {e["call"]: e for e in run.log}
    assert by_call["notes.search"]["result"] == ["PERSONAL"]
    assert by_call["llm.summarize"]["result"] == ["LLM_WRITTEN", "PERSONAL", "UNTRUSTED"]
    assert {f["rule"]: f["permitted_by"] for f in by_call["user.show"]["forbidden_by"]} == {
        "NO_UNTRUSTED_DATA_TO_OUTPUT": "UNTRUSTED_TO_USER_EX", "NO_PERSONAL_DATA_OUTPUT": "PERSONAL_TO_USER_EX"}
    assert {f["rule"]: f["permitted_by"] for f in by_call["mail.send.message"]["forbidden_by"]} == {
        "NOTHING_LEAVES_NETWORK": "MAILBOX_APPROVED_EX", "NO_UNTRUSTED_DATA_TO_OUTPUT": None, "NO_PERSONAL_DATA_OUTPUT": None}
    assert by_call["mail.send.message"]["error"]["type"] == "refused"


APPROVED_RECIPIENT = [                                       # share_notes, with an address book whose addresses the user approved
    ("system.yaml", "labels:", "labels:\n  APPROVED_RECIPIENT: {spreads: false}"),
    ("ui.yaml", "labels:", 'labels:\n  APPROVED_RECIPIENT: {words: "approved recipient", colour: "#1d5e20"}'),
    ("setups/demo.yaml", "tools:", "tools:\n  ADDRESS_BOOK: {client: {kind: scripted, answers: demo/notes.yaml}, facts: {OUTPUT: false, LEAVES_NETWORK: false, adds: [APPROVED_RECIPIENT]}}"),
    ("setups/demo.yaml", "modules:", "modules:\n  contacts.*:  {tool: ADDRESS_BOOK}"),
    ("modules.py", "import chaperone\n", 'import chaperone\n\n\n@chaperone.module("contacts.mine", properties={"DESTRUCTIVE": False})\ndef contacts_mine(ctx):\n    return ctx.mine()\n'),
    ("exceptions.yaml", "exceptions:", "exceptions:\n"
     "  - {id: PERSONAL_TO_APPROVED_EX,  rule: NO_PERSONAL_DATA_OUTPUT,     except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}}}\n"
     "  - {id: UNTRUSTED_TO_APPROVED_EX, rule: NO_UNTRUSTED_DATA_TO_OUTPUT, except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}}}"),
]
TO_MINE = ("workflows/share_summary.yaml", '  - {call: mail.send.message, with: {to: "friend@example.com", body: "{vars.summary}"}}',
           '  - {call: contacts.mine, save_as: mine}\n      - {call: mail.send.message, with: {to: "{vars.mine}", body: "{vars.summary}"}}')


def share_notes(folder: Path, tmp_path: Path) -> tuple[Any, Scripted, prerun.Result]:
    project = load_project(folder, "demo")
    mailbox = Scripted(send={"sent": True})
    tools = {"NOTES": Scripted(search=[{"title": "Trip", "text": "Flight on the 3rd."}]), "LOCAL_LLM": Scripted(summarize="You fly on the 3rd."),
             "MAILBOX": mailbox, "BROWSER": Page(), "ADDRESS_BOOK": Scripted(mine=["me@example.com"])}
    run = Run(project, {name: tool for name, tool in tools.items() if name in project.tools},
              Store(tmp_path / "store.json", project.stores), tmp_path / "access.jsonl")
    return run, mailbox, prerun.check(project)


def test_an_exception_may_ask_that_one_named_argument_carries_a_label(tmp_path: Path) -> None:
    """2.11, C2: `except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}}` permits the
    whole call when every part of `to` carries the label. With an approved address the summary, personal and
    untrusted, is sent; with an address written in the workflow, which carries no label, the call is refused as before."""
    run, mailbox, result = share_notes(copy_of(tmp_path / "a", "share_notes", *APPROVED_RECIPIENT, TO_MINE), tmp_path / "a")
    assert result.refused == [] and result.idle == []
    assert run.run("share_summary").ended == "end" and [method for method, _ in mailbox.calls] == ["send"]
    sent = [e for e in run.log if e["call"] == "mail.send.message"][0]
    assert sent["arguments"]["to"] == ["APPROVED_RECIPIENT"]
    assert {f["rule"]: f["permitted_by"] for f in sent["forbidden_by"]} == {
        "NOTHING_LEAVES_NETWORK": "MAILBOX_APPROVED_EX", "NO_UNTRUSTED_DATA_TO_OUTPUT": "UNTRUSTED_TO_APPROVED_EX",
        "NO_PERSONAL_DATA_OUTPUT": "PERSONAL_TO_APPROVED_EX"}
    run, mailbox, result = share_notes(copy_of(tmp_path / "b", "share_notes", *APPROVED_RECIPIENT), tmp_path / "b")
    assert len(result.refused) == 2 and result.idle == ["PERSONAL_TO_APPROVED_EX", "UNTRUSTED_TO_APPROVED_EX"]
    assert run.run("share_summary").state == "FAILED" and mailbox.calls == []


def test_an_argument_that_is_empty_or_not_passed_does_not_satisfy_an_exception(tmp_path: Path) -> None:
    """A module "send money" whose argument is optional acts on a value of its own when given
    nothing. So `arguments` holds only for an argument that has a scalar part, every one carrying the label. An
    empty list of recipients: refused in the run. KNOWN ISSUE: the pre-run check cannot tell that a module's result
    will be empty, so it does not list that call; the run refuses more than the report lists, never less. An empty
    list written in the workflow, and an argument not passed at all, are seen before the run."""
    folder = copy_of(tmp_path / "a", "share_notes", *APPROVED_RECIPIENT, TO_MINE)
    project = load_project(folder, "demo")
    mailbox = Scripted(send={"sent": True})
    tools = {"NOTES": Scripted(search=[{"text": "Flight."}]), "LOCAL_LLM": Scripted(summarize="You fly."), "MAILBOX": mailbox,
             "BROWSER": Page(), "ADDRESS_BOOK": Scripted(mine=[])}
    run = Run(project, tools, Store(tmp_path / "store.json", project.stores), tmp_path / "access.jsonl")
    assert run.run("share_summary").state == "FAILED" and mailbox.calls == []
    assert prerun.check(project).refused == []                                               # the known issue
    literal = ("workflows/share_summary.yaml", 'to: "friend@example.com"', "to: []")
    run, mailbox, result = share_notes(copy_of(tmp_path / "b", "share_notes", *APPROVED_RECIPIENT, literal), tmp_path / "b")
    assert len(result.refused) == 2 and run.run("share_summary").state == "FAILED" and mailbox.calls == []
    optional = ("modules.py", "def mail_send_message(ctx, to, body):", "def mail_send_message(ctx, body, to=None):")
    unpassed = ("workflows/share_summary.yaml", '{to: "friend@example.com", body: "{vars.summary}"}', '{body: "{vars.summary}"}')
    run, mailbox, result = share_notes(copy_of(tmp_path / "c", "share_notes", *APPROVED_RECIPIENT, optional, unpassed), tmp_path / "c")
    assert len(result.refused) == 2 and run.run("share_summary").state == "FAILED" and mailbox.calls == []
    null = ("workflows/share_summary.yaml", 'to: "friend@example.com"', "to: null")
    run, mailbox, result = share_notes(copy_of(tmp_path / "d", "share_notes", *APPROVED_RECIPIENT, null), tmp_path / "d")
    assert len(result.refused) == 2 and run.run("share_summary").state == "FAILED" and mailbox.calls == []   # None is a value, with no label


def test_an_exception_may_ask_a_label_of_each_of_several_arguments(tmp_path: Path) -> None:
    """`arguments: {to: {...}, body: {...}}`: every named argument must carry its labels. The summary is written by
    the LLM, so asking LLM_WRITTEN of the body holds and the send is permitted; asking that the body is not UNTRUSTED
    does not hold, and the approved address alone no longer permits the send."""
    both = lambda body: [(file, old, new.replace("arguments: {to: {APPROVED_RECIPIENT: true}}", "arguments: {to: {APPROVED_RECIPIENT: true}, body: " + body + "}"))
                         for file, old, new in APPROVED_RECIPIENT]
    run, mailbox, result = share_notes(copy_of(tmp_path / "a", "share_notes", *both("{LLM_WRITTEN: true}"), TO_MINE), tmp_path / "a")
    assert result.refused == [] and run.run("share_summary").ended == "end" and len(mailbox.calls) == 1
    run, mailbox, result = share_notes(copy_of(tmp_path / "b", "share_notes", *both("{UNTRUSTED: false}"), TO_MINE), tmp_path / "b")
    assert len(result.refused) == 2 and run.run("share_summary").state == "FAILED" and mailbox.calls == []
    run, mailbox, result = share_notes(copy_of(tmp_path / "c", "share_notes", *both("{LLM_WRITTEN: true, UNTRUSTED: true}"), TO_MINE), tmp_path / "c")
    assert result.refused == [] and run.run("share_summary").ended == "end"                   # two labels asked of one argument
    # A label the argument carries only because it spread (PERSONAL, from the notes): the run permits the send; the
    # pre-run check knows a spread label only as one the value may carry, and reports the call (over-reported, as for `carrying`).
    run, mailbox, result = share_notes(copy_of(tmp_path / "d", "share_notes", *both("{PERSONAL: true}"), TO_MINE), tmp_path / "d")
    assert len(result.refused) == 2 and run.run("share_summary").ended == "end" and len(mailbox.calls) == 1


@pytest.mark.parametrize("permit, said", [
    ("{call: [mail.send.message], arguments: {recipient: {APPROVED_RECIPIENT: true}}}", "the module mail.send.message has no argument recipient"),
    ("{call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}, carrying: {APPROVED_RECIPIENT: true}}", "`carrying` or `arguments`, not both"),
    ("{call: [mail.send.message], arguments: {}}", "for one argument or more"),
    ("{call: [mail.send.message], arguments: {to: {NO_SUCH_LABEL: true}}}", "NO_SUCH_LABEL is not a declared label"),
    ("{call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: 1}}}", "is true or false"),
    ("{call: [log], arguments: {to: {APPROVED_RECIPIENT: true}}}", "`decide` and `log` have no named argument"),
    ("{call: {where: {BROWSER: true}}, arguments: {to: {APPROVED_RECIPIENT: true}}}", "the module user.show has no argument to"),
])
def test_an_exception_whose_arguments_cannot_be_checked_is_refused(tmp_path: Path, permit: str, said: str) -> None:
    edits = APPROVED_RECIPIENT[:-1] + [("exceptions.yaml", "exceptions:", "exceptions:\n  - {id: X_EX, rule: NO_PERSONAL_DATA_OUTPUT, except: " + permit + "}")]
    with pytest.raises(Exception) as refused:
        load_project(copy_of(tmp_path, "share_notes", *edits), "demo")
    assert said in str(refused.value)
