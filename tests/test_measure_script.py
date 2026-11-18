"""scripts/measure_safe_rag.py: a Python script runs a workflow of "safe retrieval" (here `answer`) with no page, and only
after the pre-run result is accepted. The LLM and the documents are scripted (the page tests' setup `demo`)."""

import importlib.util
import shutil
from pathlib import Path

from chaperone import load_project, prerun
from tests.test_web import scripted_rag

REPO = Path(__file__).resolve().parents[1]


def measure_script():
    spec = importlib.util.spec_from_file_location("measure_safe_rag", REPO / "scripts" / "measure_safe_rag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_asks_from_the_command_line_after_the_acceptance_and_reads_the_screens(tmp_path: Path, capsys) -> None:
    shutil.copytree(REPO / "examples" / "rules", tmp_path / "rules")
    folder = tmp_path / "safe_rag"
    shutil.copytree(REPO / "examples" / "safe_rag", folder, ignore=shutil.ignore_patterns("__pycache__", "corpus"))
    for file, text in scripted_rag().items():
        (folder / file).parent.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text(text)
    measure, state = measure_script(), tmp_path / "state"
    asked = ["--project", str(folder), "--setup", "demo", "--state", str(state), "--workflow", "answer",
             "Where does power come from?"]                              # the scripted LLM answers `answer`'s prompt only

    assert measure.main(asked) == 1                                       # not accepted: nothing runs
    assert "Not run: the pre-run result of safe_rag under the setup demo is not accepted" in capsys.readouterr().out

    prerun.accept(prerun.check(load_project(folder, "demo")), state, "test")
    assert measure.main(asked) == 0
    out = capsys.readouterr().out
    assert "SHOW          quotes found: 1, left out: 1 | Where does power come from?" in out
    assert "answer (the LLM's, untrusted): Power comes from the people." in out
    assert "quote found in FEDERALIST No. 49: the only legitimate fountain of power" in out
    assert "ambition" not in out                                          # the quote not in its chunk is left out, not shown

    assert measure.main(asked[:-1]) == 0                                  # no question given: the project's 24, one line each
    out = capsys.readouterr().out
    assert "16 of 24 as expected." in out                                 # the scripted LLM answers every question alike
    assert "  not_there SHOW          4" in out and "answer (the LLM's" not in out


def test_the_script_reads_a_screen_of_several_shows_and_the_quotes_of_passages() -> None:
    """`single_pass` shows a screen as three values: the LLM's words, the documents (a list with `passages`), the note."""
    measure = measure_script()
    user = measure.User(["q1", "q2"])

    class At:
        at = ("single_pass", "SHOW")
    user.run = At()
    user.describe("?")
    documents = [{"title": "FEDERALIST No. 84: Objections", "passages": [{"pieces": [
        {"text": "would even be ", "quoted": True}, {"text": "dangerous", "quoted": True},     # one quote, split by a coloured word
        {"text": "other", "quoted": False}, {"text": "A BILL OF RIGHTS", "quoted": True}]}]}]
    for report in ({"answer": "No."}, documents, {"left_out": {"snippets_not_in_their_chunk": 1}, "note": "n"}):
        user.show(report, {})
    At.at = ("single_pass", "NOTHING_FOUND")
    user.describe("?")
    user.show({"note": "none"}, {})
    (first, one), (second, two) = measure.screens(user, 2)
    assert (first, second) == ("SHOW", "NOTHING_FOUND")
    assert measure.quotes(one) == [("FEDERALIST No. 84: Objections", "would even be dangerous"),
                                   ("FEDERALIST No. 84: Objections", "A BILL OF RIGHTS")] and measure.quotes(two) == []
    assert measure.field(one, "answer") == "No." and measure.field(one, "left_out") == {"snippets_not_in_their_chunk": 1}
