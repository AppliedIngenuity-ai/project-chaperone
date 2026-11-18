"""Loading a project, and the facts of its modules.

The three example projects load; every invalid configuration the specification names is refused, with the reason.
Each refusal is one change made to a copy of an example project.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chaperone.errors import ConfigError
from chaperone import load_project, selects
from chaperone.__main__ import facts_report, main

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def copy_of(tmp_path: Path, name: str, *edits: tuple[str, str, str]) -> Path:
    """A copy of the example project `name`, with each (file, old, new) replaced once. `old` must be in the file."""
    shutil.copytree(EXAMPLES / "rules", tmp_path / "rules")
    project = tmp_path / name
    shutil.copytree(EXAMPLES / name, project, ignore=shutil.ignore_patterns("__pycache__"))
    for file, old, new in edits:
        text = (project / file).read_text()
        assert old in text, f"{file} does not contain {old!r}"
        (project / file).write_text(text.replace(old, new, 1))
    return project


@pytest.mark.parametrize("name, modules, exceptions", [("ask_my_mail", 5, 4), ("deliveries", 27, 12), ("safe_rag", 16, 4), ("share_notes", 4, 3)])
def test_the_three_projects_load(name: str, modules: int, exceptions: int) -> None:
    project = load_project(EXAMPLES / name)                       # each project's default setup
    assert len(project.facts) == modules
    assert len(project.exceptions) == exceptions
    assert len(project.rules) == 5                                   # the five of rules/default.yaml


def test_facts_come_from_the_declaration_the_line_and_the_tool() -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    entered = project.facts["user.enter_senders"]
    assert entered.tool == "BROWSER"                                  # its exact line wins over `user.*`
    assert entered.values["OUTPUT"] is True and entered.sources["OUTPUT"] == "the tool BROWSER"
    assert entered.values["BROWSER"] is True and entered.values["MAILBOX"] is False
    assert entered.adds == {"PERSONAL": "the line `user.enter_senders`"}
    assert project.facts["user.show"].adds == {}                      # the pattern line adds nothing
    marked = project.facts["calendar.mark_delivered"]
    assert marked.values["DESTRUCTIVE"] is True and marked.sources["DESTRUCTIVE"].startswith("its declaration")
    sizes = project.facts["lists.sizes"]                              # no ctx, no tool: the declared defaults
    assert sizes.tool is None and sizes.values["OUTPUT"] is False and sizes.sources["OUTPUT"].startswith("its declaration")


def test_a_selection_denotes_modules_by_name_by_property_and_any() -> None:
    project = load_project(EXAMPLES / "deliveries", "demo")
    assert selects(project, ["decide"], "decide") and not selects(project, "any", "decide")
    assert selects(project, "any", "lists.sizes")
    assert selects(project, {"where": {"MAILBOX": True}}, "mail.read.search_senders")
    assert not selects(project, {"where": {"MAILBOX": True}}, "user.show")
    both = {"where": [{"BROWSER": True}, {"LOCAL_LLM": True}]}
    assert selects(project, both, "user.show") and selects(project, both, "llm.copy_values")
    assert not selects(project, both, "calendar.write_entry")         # GOOGLE_CALENDAR is neither
    assert not selects(project, {"where": {"OUTPUT": True, "LEAVES_NETWORK": True}}, "user.show")


def test_a_profile_states_facts_and_is_a_property_of_what_names_it(tmp_path: Path) -> None:
    project = load_project(copy_of(
        tmp_path, "ask_my_mail",
        ("system.yaml", "labels:", "profiles:\n  REMOTE_SOURCE: {facts: {LEAVES_NETWORK: true, adds: [UNTRUSTED]}}\nlabels:"),
        ("setups/demo.yaml", "facts: {LEAVES_NETWORK: true,  adds: [UNTRUSTED, PERSONAL]}",
         "profiles: [REMOTE_SOURCE], facts: {adds: [PERSONAL]}")))
    search = project.facts["mail.search"]
    assert search.values["REMOTE_SOURCE"] is True and search.values["LEAVES_NETWORK"] is True
    assert search.sources["LEAVES_NETWORK"] == "the profile REMOTE_SOURCE"
    assert set(search.adds) == {"UNTRUSTED", "PERSONAL"}
    assert project.facts["llm.answer"].values["REMOTE_SOURCE"] is False


def test_a_pattern_that_matches_no_module_is_said_not_refused(tmp_path: Path) -> None:
    project = load_project(copy_of(tmp_path, "ask_my_mail", ("setups/demo.yaml", "modules:", "modules:\n  db.*: {tool: MAILBOX}")))
    assert project.unmatched_patterns == ["db.*"]
    assert "no module matches the line `db.*`" in facts_report(project)


def test_the_pre_run_report_warns_of_a_pattern_that_matches_no_module_and_the_result_does_not_change(tmp_path: Path) -> None:
    from chaperone import prerun
    from chaperone.web import Session, _check
    plain = prerun.check(load_project(EXAMPLES / "ask_my_mail", "demo"))
    folder = copy_of(tmp_path, "ask_my_mail", ("setups/demo.yaml", "modules:", "modules:\n  db.*: {tool: MAILBOX}"),
                     ("setups/demo.yaml", "modules:", "modules:\n  db.*: {tool: MAILBOX}"))
    result = prerun.check(load_project(folder))
    assert result.unmatched == ["db.*"] and result.fingerprint() == plain.fingerprint()       # a warning: no call changes
    assert "WARNING no module matches the line `db.*` of the setup demo" in prerun.report(result, None)
    assert "not accepted; nothing runs until it is" in prerun.report(result, None)          # it can be accepted
    session = Session(load_project(folder, "demo"), tmp_path / "state")                    # the made-up setup: no real client
    try:
        assert "no module matches this line of <code>modules</code>" in _check(session) and "db.*: {tool: MAILBOX}" in _check(session)
    finally:
        session.server.server_close()


SETUP, SYSTEM, MODULES, RULES, EXCEPTIONS, UI = ("setups/demo.yaml", "system.yaml", "modules.py", "rules.yaml",
                                                 "exceptions.yaml", "ui.yaml")
DECLARED = 'properties={"DESTRUCTIVE": False}'
REFUSED = [
    # (what, project, file, old, new, a part of the reason)
    ("a key system.yaml does not know", "deliveries", SYSTEM, "stores:", "storse:", "the key `storse` is not known"),
    ("a label without spreads", "ask_my_mail", SYSTEM, "PERSONAL:  {spreads: true}", "PERSONAL:  {}", "`spreads` is required"),
    ("a profile that is not declared", "ask_my_mail", SETUP, "mail.*:     {tool: MAILBOX}", "mail.*:     {tool: MAILBOX, profiles: [REMOTE]}", "the profile REMOTE is not declared"),
    ("a tool that does not state adds", "ask_my_mail", SETUP, "LEAVES_NETWORK: false, adds: []}", "LEAVES_NETWORK: false}", "`adds` is not stated"),
    ("a client with kind and endpoint", "ask_my_mail", SETUP, "{kind: web}", "{kind: web, endpoint: x.yaml}", "`kind` or `endpoint`, one of the two"),
    ("a line naming a tool that is not declared", "ask_my_mail", SETUP, "{tool: LOCAL_LLM}", "{tool: REMOTE_LLM}", "the tool REMOTE_LLM is not declared"),
    ("a bare tool name as a module's line", "ask_my_mail", SETUP, "{tool: LOCAL_LLM}", "LOCAL_LLM", "a mapping is expected"),
    ("a line for a module that does not exist", "ask_my_mail", SETUP, "llm.answer:", "llm.anwser:", "no module has this name"),
    ("a module with ctx and no tool", "deliveries", SETUP, "  calendar.*:             {tool: GOOGLE_CALENDAR}\n", "", "takes `ctx`, and no line of the setup demo gives it a tool"),
    ("a tool for a module without ctx", "deliveries", SETUP, "modules:", "modules:\n  lists.sizes: {tool: BROWSER}", "its function takes no `ctx`"),
    ("two patterns for one module", "deliveries", SETUP, "modules:", "modules:\n  mail.read.s*: {tool: MAILBOX}", "matched by the patterns"),
    ("two statements that disagree", "deliveries", SETUP, "llm.*:                  {tool: LOCAL_LLM}", "llm.*:                  {tool: LOCAL_LLM, facts: {DESTRUCTIVE: true}}", "DESTRUCTIVE is false in its declaration"),
    ("a property with no value and no default", "ask_my_mail", MODULES, '@chaperone.module("mail.search", properties={"DESTRUCTIVE": False, "OUTPUT": False})', '@chaperone.module("mail.search")', "the property DESTRUCTIVE has no value"),
    ("a module on a tool that does not state OUTPUT, and that does not state it either", "ask_my_mail", MODULES, '@chaperone.module("mail.search", properties={"DESTRUCTIVE": False, "OUTPUT": False})',
     '@chaperone.module("mail.search", ' + DECLARED + ")", "module mail.search: the property OUTPUT has no value"),
    ("a fact for a property that is not declared", "ask_my_mail", SETUP, "{tool: LOCAL_LLM}", "{tool: LOCAL_LLM, facts: {AUDITED: true}}", "the key `AUDITED` is not known"),
    ("adds of a label that is not declared", "ask_my_mail", SETUP, "adds: [UNTRUSTED]}", "adds: [SECRET]}", "adds SECRET, which is not a declared label"),
    ("a tool's property stated as a fact", "ask_my_mail", SETUP, "{tool: LOCAL_LLM}", "{tool: LOCAL_LLM, facts: {MAILBOX: true}}", "cannot be stated"),
    ("a request the label does not permit", "deliveries", "modules/llm.py", '@chaperone.module("llm.next_step", ' + DECLARED + ")", '@chaperone.module("llm.next_step", ' + DECLARED + ', requests={"SANITIZED": True})', "SANITIZED.may_add in system.yaml does not select it"),
    ("the LLM requesting the label of the documents' text", "safe_rag", MODULES, '@chaperone.module("llm.answer", properties={"DESTRUCTIVE": False})',
     '@chaperone.module("llm.answer", properties={"DESTRUCTIVE": False}, requests={"ORIGINAL_SOURCE": True})', "ORIGINAL_SOURCE.may_add in system.yaml does not select it"),
    ("the LLM requesting the label of the checked reply", "safe_rag", MODULES, '@chaperone.module("llm.answer", properties={"DESTRUCTIVE": False})',
     '@chaperone.module("llm.answer", properties={"DESTRUCTIVE": False}, requests={"CHECKED": True})', "CHECKED.may_add in system.yaml does not select it"),
    ("a request for a label that is not declared", "deliveries", "modules/code.py", 'requests={"PERSONAL": False}', 'requests={"SECRET": False}', "requests SECRET, which is not a declared label"),
    ("a module declared twice", "ask_my_mail", MODULES, '@chaperone.module("llm.answer", ', '@chaperone.module("mail.search", ', "declared twice"),
    ("a module named as an engine operation", "ask_my_mail", MODULES, '"llm.answer"', '"store.get"', "the name of an engine operation"),
    ("an imported rule declared again", "deliveries", RULES, "imports: [../rules/default.yaml]", "imports: [../rules/default.yaml]\nrules:\n  - {id: NOTHING_LEAVES_NETWORK, never: {call: any}}", "an imported rule cannot be modified"),
    ("a rules file that imports itself", "ask_my_mail", RULES, "imports: [../rules/default.yaml]", "imports: [rules.yaml]", "imports itself"),
    ("a setup that does not say what log is", "ask_my_mail", SETUP, "log: {tool: BROWSER, facts: {DESTRUCTIVE: false}}", "", "`log` is required"),
    ("log with a property that has no value", "ask_my_mail", SETUP, "log: {tool: BROWSER, facts: {DESTRUCTIVE: false}}", "log: {tool: BROWSER}", "log: the property DESTRUCTIVE has no value"),
    ("log with a tool that is not declared", "ask_my_mail", SETUP, "log: {tool: BROWSER, facts: {DESTRUCTIVE: false}}", "log: {tool: PAGE, facts: {DESTRUCTIVE: false}}", "the line `log`: the tool PAGE is not declared"),
    ("a rule on a property that is not declared", "deliveries", RULES, "imports: [../rules/default.yaml]", "imports: [../rules/default.yaml]\nrules:\n  - {id: OWN, never: {call: {where: {OUTPT: true}}}}", "the property OUTPT is not declared"),
    ("a rule carrying a label that is not declared", "deliveries", RULES, "imports: [../rules/default.yaml]", "imports: [../rules/default.yaml]\nrules:\n  - {id: OWN, never: {call: any, carrying: {PERSNAL: true}}}", "PERSNAL is not a declared label"),
    ("an exception to a rule that is not declared", "ask_my_mail", EXCEPTIONS, "rule: NOTHING_LEAVES_NETWORK", "rule: NOTHING_LEAVES", "the rule NOTHING_LEAVES is not declared"),
    ("an exception naming no module", "ask_my_mail", EXCEPTIONS, "{call: [user.show]}", "{call: [user.shw]}", "user.shw is not a module"),
    ("carrying on an exception to a rule without it", "deliveries", EXCEPTIONS, "except: {call: {where: {GOOGLE_CALENDAR: true}}}}", "except: {call: {where: {GOOGLE_CALENDAR: true}}, carrying: {PERSONAL: true}}}", "NOTHING_LEAVES_NETWORK has none"),
    ("two exceptions with one id", "ask_my_mail", EXCEPTIONS, "id: SHOW_TO_USER_EX", "id: MAILBOX_APPROVED_EX", "an exception with this id is already declared"),
    ("may_remove naming no module", "deliveries", SYSTEM, "may_remove: [lists.sizes]", "may_remove: [lists.size]", "lists.size is not a module"),
    ("a label with no words in ui.yaml", "deliveries", UI, '  PERSONAL:         {words: "personal",           colour: "#1e4fa0"}\n', "", "the label PERSONAL has no words and no colour"),
    ("a style that is not declared", "deliveries", UI, "default_style: fields", "default_style: cards", "default_style names cards"),
    ("an order that names no workflow", "deliveries", UI, "workflow_order: [assistant,", "workflow_order: [assistants,", "workflow_order names assistants, which is not a workflow"),
    ("a description that is not a text", "deliveries", "workflows/assistant.yaml", 'description: "A conversation: choose whose mail is monitored."', "description: [a]", "`description` is a text"),
    ("a pane of a store that is not declared", "deliveries", UI, "stores: [MONITORED_SENDERS, HANDLED_MESSAGES, NEEDS_ATTENTION, LINKS_TO_APPROVE, APPROVED_LINKS, DECLINED_LINKS]", "stores: [MONITORED]", "MONITORED, which is not a declared store"),
    ("a tool named by an argument", "deliveries", SETUP, "mail.read.*:            {tool: MAILBOX, facts: {OUTPUT: false}}", "mail.read.*:            {from_argument: box, one_of: [MAILBOX]}", "NOT YET IMPLEMENTED"),
    ("a tool with the name of a property", "ask_my_mail", SETUP, "  LOCAL_LLM: {", "  OUTPUT: {", "already has this name"),
    ("a word YAML reads as true or false", "ask_my_mail", SYSTEM, "PERSONAL:  {spreads: true}", "PERSONAL:  {spreads: yes}", "quote it"),
]


@pytest.mark.parametrize("what, name, file, old, new, reason", REFUSED, ids=[r[0] for r in REFUSED])
def test_an_invalid_configuration_is_refused_with_its_reason(tmp_path: Path, what: str, name: str, file: str, old: str,
                                                             new: str, reason: str) -> None:
    with pytest.raises(ConfigError) as refused:
        load_project(copy_of(tmp_path, name, (file, old, new)))
    assert reason in str(refused.value)


def test_a_project_with_a_web_tool_needs_ui_yaml(tmp_path: Path) -> None:
    project = copy_of(tmp_path, "ask_my_mail")
    (project / "ui.yaml").unlink()
    with pytest.raises(ConfigError, match="there is no ui.yaml"):
        load_project(project)


def test_every_problem_is_reported_at_once_with_file_and_line(tmp_path: Path) -> None:
    project = copy_of(tmp_path, "ask_my_mail",
                      ("setups/demo.yaml", "{tool: LOCAL_LLM}", "{tool: REMOTE_LLM}"),
                      ("setups/demo.yaml", "mail.*:", "mial.x:"))
    with pytest.raises(ConfigError) as refused:
        load_project(project)
    issues = refused.value.issues
    assert len(issues) == 2 and all(i.file.endswith("demo.yaml") and i.line for i in issues)


def test_a_path_that_is_no_project_is_said_with_the_folder_meant(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.chdir(EXAMPLES.parent)
    assert main(["signin", "deliveries", "--setup", "real"]) == 2      # the folder's name, not its path
    out = capsys.readouterr().out
    assert out.startswith("NO SUCH PROJECT: deliveries is not a project folder (it has no system.yaml).")
    assert "examples/deliveries" in out and "setup" not in out
    assert main(["check", "nowhere"]) == 2
    assert capsys.readouterr().out.strip() == "NO SUCH PROJECT: nowhere is not a project folder (it has no system.yaml)."


def test_the_facts_command(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["facts", str(EXAMPLES / "deliveries"), "--sources"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("project deliveries, setup demo: 27 modules, 6 tools, 5 labels, 5 rules, 12 exceptions")
    assert "default taken: LEAVES_NETWORK is false by default for 8 of 27 modules" in out and "OUTPUT is false by default" not in out
    assert "    adds PERSONAL: the line `user.enter_senders`" in out
    broken = copy_of(tmp_path, "ask_my_mail", ("system.yaml", "PERSONAL:  {spreads: true}", "PERSONAL:  {}"))
    assert main(["facts", str(broken)]) == 2
    assert capsys.readouterr().out.startswith("INVALID CONFIGURATION: 1 problem(s). Nothing runs.")
