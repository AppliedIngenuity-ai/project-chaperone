"""Ask "safe retrieval" questions from the command line, with no web page: a Python script that runs a workflow.

    python scripts/measure_safe_rag.py                      # the 24 questions of examples/safe_rag/questions.yaml
    python scripts/measure_safe_rag.py --file questions_compound.yaml                         # the 14 harder ones
    python scripts/measure_safe_rag.py "Why is the judiciary the least dangerous branch?"     # questions of your own

The workflow is `single_pass`, the one the paper describes, unless `--workflow` names another (`multi_pass`,
`answer`); each shows an answer in SHOW (or SHOW_2), and no answer in NOTHING_FOUND.

The run is refused until the pre-run result of the setup is accepted, as for the page: start
`python -m chaperone run examples/safe_rag --setup local` once and accept at the terminal, or use
`python -m chaperone accept examples/safe_rag --setup local --by NAME`. The state folder is the one the page uses.

How a script runs a workflow, in the four steps `python -m chaperone run` takes:
1. Load the project under a setup (`load_project`), with the exceptions the user approved at a start
   (`add_user_exceptions`).
2. Build the setup's tools (`build_tools`). The tool whose client is of kind `web` (BROWSER, the user's page) is this
   script: an object with the page's methods. `describe` returns the next question; `show` keeps what is shown. The
   tool's facts do not change, so every call is checked by the rules exactly as on the page.
3. Open a run (`open_run`), refused unless the current pre-run result is an accepted one.
4. Run the workflow (`run.run`) and read what was shown, by the state that showed it. A screen may be several
   shows (in `single_pass`: the LLM's words, the documents, the note); all the shows after a question are its screen.

For each question of the file, the line says whether the screen is the one its `expect` calls for: a screen with an
answer and quotes found in the documents for `answered`; the nothing-found screen otherwise. Whether an answer is
right is not judged here. "Quotes found" counts the underlined runs of text: two quotes the LLM gave that touch
each other count as one."""

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from chaperone import load_project                                # noqa: E402
from chaperone.clients import build_tools                         # noqa: E402
from chaperone.load import add_user_exceptions                    # noqa: E402
from chaperone.prerun import NotAccepted, open_run, user_exceptions_file   # noqa: E402
from chaperone.yaml_lines import load_yaml                        # noqa: E402

ANSWER_SCREENS = {"SHOW", "SHOW_2"}                           # the states that show an answer, in each workflow of the project
NOTHING_SCREEN = "NOTHING_FOUND"


class User:
    """Stands in for the page: asks the questions in turn, then an empty box, which ends the workflow."""

    def __init__(self, questions: list[str]) -> None:
        self.left, self.asked, self.shown, self.run = list(questions), -1, [], None

    def describe(self, prompt: str) -> str:
        self.asked += 1
        return self.left.pop(0) if self.left else ""

    def show(self, report, marks, style=None, update=False) -> None:
        self.shown.append((self.asked, self.run.at[1], report))   # the question it answers, and the state that showed it


def screens(user: User, count: int) -> list[tuple[str, list]]:
    """For each question: the state of its first show, and every value shown before the next question."""
    return [(shows[0][1] if shows else "(nothing shown)", [report for _, _, report in shows])
            for shows in ([shown for shown in user.shown if shown[0] == i] for i in range(count))]


def quotes(reports: list) -> list[tuple[str, str]]:
    """(the document's title, a quote found in it) for each quoted piece of the documents shown: in a list of
    documents with `passages` (`single_pass`, `multi_pass`), or under `from_the_documents` (`answer`)."""
    found = []
    for report in reports:
        if isinstance(report, list):
            for document in report:
                for passage in document.get("passages", []) if isinstance(document, dict) else []:
                    run = []                                  # a quote is a run of quoted pieces: coloured words split it
                    for piece in passage["pieces"] + [{"text": "", "quoted": False}]:
                        if piece.get("quoted"):
                            run.append(piece["text"])
                        elif run:
                            found.append((document["title"], "".join(run)))
                            run = []
            continue
        for name in ("from_the_documents", "chunks_the_llm_named_relevant"):
            for document in report.get(name) or []:
                for chunk in document["chunks"]:
                    found += [(document["title"], piece["text"]) for piece in chunk["pieces"] if piece["marked"]]
    return found


def field(reports: list, name: str):
    """The value under `name` in the first value shown that has it."""
    return next((report[name] for report in reports if isinstance(report, dict) and name in report), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("questions", nargs="*", help="questions to ask; none: the project's questions.yaml")
    parser.add_argument("--project", default=str(REPO / "examples" / "safe_rag"), help="the project folder; by default examples/safe_rag")
    parser.add_argument("--setup", default="local", help="the setup; by default local")
    parser.add_argument("--workflow", default="single_pass", help="the workflow; by default single_pass (also: multi_pass, answer)")
    parser.add_argument("--file", default="questions.yaml", help="the questions file, in the project's folder")
    parser.add_argument("--state", help="the state folder; by default the page's, state/<project folder's name>/<setup>")
    args = parser.parse_args(argv)

    project = load_project(args.project, args.setup)                                        # step 1
    state = Path(args.state or REPO / "state" / project.folder.resolve().name / project.setup)
    add_user_exceptions(project, user_exceptions_file(state))
    items = ([{"question": q} for q in args.questions] if args.questions
             else load_yaml(project.folder / args.file)["questions"])
    user = User([item["question"] for item in items])
    tools = build_tools(project, lambda tool: user, state)                                  # step 2
    try:
        run = open_run(project, tools, state)                                               # step 3
    except NotAccepted as refusal:
        print(f"Not run: {refusal}")
        return 1
    user.run = run
    outcome = run.run(args.workflow)                                                        # step 4
    if outcome.ended != "end":
        print(f"The run ended: {outcome.ended} in state {outcome.state}.")
    tally, as_expected = Counter(), 0
    shown = screens(user, len(items))
    for item, (screen, reports) in zip(items, shown):
        found, left_out = quotes(reports), field(reports, "left_out")
        line = f"{screen:13} quotes found: {len(found)}"
        if isinstance(left_out, dict):
            line += f", left out: {sum(n for n in left_out.values() if isinstance(n, int))}"
        if "expect" in item:
            wanted = screen in ANSWER_SCREENS if item["expect"] == "answered" else screen == NOTHING_SCREEN
            as_expected += wanted
            tally[(item["expect"], screen)] += 1
            line = f"{item['expect']:9} {'as expected' if wanted else 'NOT as expected':15} {line}"
        print(f"{line} | {item['question']}")
        if not args.questions:
            continue
        if field(reports, "answer"):
            print(f"    answer (the LLM's, untrusted): {field(reports, 'answer')}")
        for title, text in found:
            print(f"    quote found in {title.split(':')[0]}: {' '.join(text.split())}")
        if field(reports, "note"):
            print(f"    note: {field(reports, 'note')}")
    if sum(1 for _, reports in shown if reports) < len(items):
        print(f"Shown: screens for {sum(1 for _, reports in shown if reports)} of {len(items)} questions.")
    if tally:
        print(f"\n{as_expected} of {len(items)} as expected. By what the file expects and the screen shown:")
        for (expect, screen), count in sorted(tally.items()):
            print(f"  {expect:9} {screen:13} {count}")
        print("Whether each answer is right is not judged by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
