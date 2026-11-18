"""Commands of the framework.

    python -m chaperone facts  <project folder> [--setup NAME] [--sources]
    python -m chaperone check  <project folder> [--setup NAME] [--state DIR] [--all]
    python -m chaperone accept <project folder> [--setup NAME] [--state DIR] --by NAME
    python -m chaperone run    <project folder> [--setup NAME] [--state DIR] [--port N] [--host ADDRESS]
    python -m chaperone signin <project folder> [--setup NAME] [--port N]

Every command also takes --config FILE (the user's changes, by default private/local.yaml) or --defaults-only.

`facts` prints every module's facts under one setup; with --sources, where each value was stated. `check` runs the
pre-run check and prints its result; with --all, the calls each exception permits. `accept` records that NAME accepts
the current result; nothing runs under a setup until its result is accepted. `run` serves the project's page on this
machine and prints its address. Before that it prints every rule with the exceptions to it; when the result is not an
accepted one, the exceptions to each rule are accepted at the terminal, one rule at a time, or nothing starts. A
possible violation (calls a rule forbids and no exception permits) is answered there too: add the exception that
permits the calls, or acknowledge the risk with no exception, and a run then refuses each of those calls when it is
made. Runs are started on the page, which displays the result and accepts nothing. The state folder holds the
acceptance, the exceptions approved and the risks acknowledged at a start, the store and the access log; by default it is state/<project folder's name>/<setup>: each setup has
its own, so the made-up senders of one are not the real senders of another.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chaperone.errors import ConfigError

from . import prerun, yaml_lines
from .overrides import read_layer
from .load import Project, add_user_exceptions, default_setup, load_project
from .values import Problem


def facts_report(project: Project, sources: bool = False) -> str:
    own = [p for p in project.properties if p not in project.tools and p not in project.profiles]
    lines = [f"project {project.folder.name}, setup {project.setup}: {len(project.facts)} modules, "
             f"{len(project.tools)} tools, {len(project.labels)} labels, {len(project.rules)} rules, "
             f"{len(project.exceptions)} exceptions"]
    for prop in own:
        took = [m for m, f in project.facts.items() if f.sources[prop] == "default"]
        if took:
            default = "true" if project.properties[prop]["default"] else "false"
            lines.append(f"default taken: {prop} is {default} by default for {len(took)} of {len(project.facts)} modules")
    for key in project.unmatched_patterns:
        lines.append(f"no module matches the line `{key}` of the setup")
    lines.append("")
    width = max(len(m) for m in project.facts)
    for name, f in sorted(project.facts.items()):
        true = [p for p in own if f.values[p]]
        lines.append(f"{name:{width}}  tool {f.tool or 'none':16} true: {', '.join(true) or 'none':28} "
                     f"adds: {', '.join(f.adds) or 'none'}")
        if sources:
            for prop in own:
                lines.append(f"    {prop} is {'true' if f.values[prop] else 'false'}: {f.sources[prop]}")
            for label, source in f.adds.items():
                lines.append(f"    adds {label}: {source}")
    return "\n".join(lines)


LOCAL_FILE = Path("private") / "local.yaml"      # the user's changes to the shipped files, read when it exists
CONFIG_HELP = "a file of your changes to the shipped files; by default private/local.yaml, when it exists"
SETUP_HELP = "the setup, a file setups/NAME.yaml of the project; by default demo when the project has one, otherwise local"
PROJECT_HELP = "a project folder, such as examples/deliveries"
DESCRIPTION = ("Checks every route of a project's workflows against its rules before a run, and every call while it "
               "runs: a call a rule forbids and no exception permits is refused.")
EXAMPLES = """examples, from the folder project-chaperone:
  python -m chaperone run examples/share_notes      the smallest example: the rules refuse its e-mail
  python -m chaperone run examples/deliveries       mail and a calendar, on made-up data
  python -m chaperone check examples/deliveries     the pre-run check only
README.md has the rest."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chaperone", description=DESCRIPTION, epilog=EXAMPLES,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    facts = commands.add_parser("facts", help="every module's facts under one setup")
    facts.add_argument("project", help=PROJECT_HELP)
    facts.add_argument("--setup", help=SETUP_HELP)
    facts.add_argument("--config", help=CONFIG_HELP)
    facts.add_argument("--defaults-only", action="store_true", help="read the shipped files only: no private/local.yaml, no --config")
    facts.add_argument("--sources", action="store_true", help="also where each value was stated")
    for name, text in (("check", "the pre-run check only: its result, with no questions; nothing starts"),
                       ("accept", "accept the current result of the pre-run check with no questions, for a script")):
        command = commands.add_parser(name, help=text)
        command.add_argument("project", help=PROJECT_HELP)
        command.add_argument("--setup", help=SETUP_HELP)
        command.add_argument("--config", help=CONFIG_HELP)
        command.add_argument("--defaults-only", action="store_true", help="read the shipped files only: no private/local.yaml, no --config")
        command.add_argument("--state", help="the state folder; by default state/<project folder's name>/<setup>")
        if name == "check":
            command.add_argument("--all", action="store_true", help="also the calls each exception permits")
        else:
            command.add_argument("--by", required=True, help="who accepts")
    serve = commands.add_parser("run", help="the pre-run check, with its questions at the terminal until its result is "
                                            "accepted; then the project's page on this machine")
    serve.add_argument("project", help=PROJECT_HELP)
    serve.add_argument("--setup", help=SETUP_HELP)
    serve.add_argument("--config", help=CONFIG_HELP)
    serve.add_argument("--defaults-only", action="store_true", help="read the shipped files only: no private/local.yaml, no --config")
    serve.add_argument("--state", help="the state folder; by default state/<project folder's name>/<setup>")
    serve.add_argument("--port", type=int, default=0, help="by default a free port")
    serve.add_argument("--host", default="127.0.0.1", help="the address to serve on; by default this machine only. "
                                                           "0.0.0.0 serves every network the machine is on")
    signin = commands.add_parser("signin", help="sign in to Google, for a setup whose calendar is Google's; again "
                                                "when Google ends the sign-in")
    signin.add_argument("project", help=PROJECT_HELP)
    signin.add_argument("--setup", help=SETUP_HELP)
    signin.add_argument("--config", help=CONFIG_HELP)
    signin.add_argument("--defaults-only", action="store_true", help="read the shipped files only: no private/local.yaml, no --config")
    signin.add_argument("--port", type=int, default=8765, help="where the browser is sent back to on this machine")
    if not (sys.argv[1:] if argv is None else argv):        # no command: the help, with the examples
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    folder = Path(args.project)
    if not (folder / "system.yaml").is_file():              # a path that is no project: said first, with a folder of that name one level down
        meant = sorted(str(found.parent) for found in Path(".").glob(f"*/{folder.name}/system.yaml"))
        print(f"NO SUCH PROJECT: {args.project} is not a project folder (it has no system.yaml)."
              + (f" Did you mean {' or '.join(meant)}?" if meant else ""))
        return 2
    layer_file = None if args.defaults_only else Path(args.config) if args.config else LOCAL_FILE if LOCAL_FILE.is_file() else None
    if args.config and not args.defaults_only and not Path(args.config).is_file():
        print(f"NO SUCH FILE: {args.config} (--config).")
        return 2
    try:
        yaml_lines.LAYER = read_layer(layer_file) if layer_file else None
    except ConfigError as error:
        print(f"INVALID CHANGES: {len(error.issues)} problem(s) in {layer_file}. Nothing runs.")
        for issue in error.issues:
            print(f"  {issue}")
        return 2
    try:
        return _command(args)
    finally:
        yaml_lines.LAYER = None


def _command(args: argparse.Namespace) -> int:
    """The command, once the user's changes (if any) are set for the loader."""
    layer = yaml_lines.LAYER
    setup = args.setup or default_setup(args.project)
    if not (Path(args.project) / "setups" / f"{setup}.yaml").is_file():
        names = sorted(f.stem for f in (Path(args.project) / "setups").glob("*.yaml"))
        print(f"NO SUCH SETUP: {args.project} has no setup {setup}. Name one with --setup NAME: {', '.join(names) or 'none'}.")
        return 2
    try:
        project = load_project(args.project, setup)
    except ConfigError as error:
        for line in layer.report() if layer else []:
            print(line)
        print(f"INVALID CONFIGURATION: {len(error.issues)} problem(s). Nothing runs.")
        for issue in error.issues:
            print(f"  {issue}")
        return 2
    for line in layer.report() if layer else []:
        print(line)
    if args.command == "signin":
        from chaperone.connect.google_calendar import CalendarUnavailable, sign_in
        from .clients import google_auth
        found = {name: spec["client"] for name, spec in project.tools.items() if spec["client"].get("kind") == "google_calendar"}
        if not found:
            print(f"NOT SIGNED IN: the setup {project.setup} has no tool whose client is of kind google_calendar.")
            return 2
        for name, client in found.items():
            auth = google_auth(project, client)
            if not auth.client_secret_path.is_file():
                print(f"NOT SIGNED IN: tool {name}: {auth.client_secret_path} does not exist: the OAuth client file downloaded "
                      f"from Google Cloud (a client of type Desktop app).")
                return 2
            print(f"tool {name}: signing in to Google. The token is written to {auth.token_path}.")
            try:
                sign_in(auth, port=args.port)
            except (CalendarUnavailable, ValueError) as problem:
                print(f"NOT SIGNED IN: {problem}")
                return 1
        return 0
    if args.command == "facts":
        print(facts_report(project, args.sources))
        return 0
    state = args.state or f"state/{project.folder.resolve().name}/{project.setup}"
    try:                                                     # the exceptions the user approved at a start, kept in the state folder
        add_user_exceptions(project, prerun.user_exceptions_file(state))
    except ConfigError as error:
        print(f"INVALID USER-APPROVED EXCEPTIONS: {len(error.issues)} problem(s). Nothing runs. Correct or delete the line.")
        for issue in error.issues:
            print(f"  {issue}")
        return 2
    try:                                                     # the acknowledged risks, kept there too: refused calls accepted with no exception
        known = prerun.acknowledged(state)
    except ConfigError as error:
        print(f"INVALID ACKNOWLEDGED RISKS: {len(error.issues)} problem(s). Nothing runs. Correct or delete the line.")
        for issue in error.issues:
            print(f"  {issue}")
        return 2
    if args.command == "run":
        from .web import Session
        if not prerun.at_start(project, prerun.check(project), state, input, print, sys.stdin.isatty()):   # the report, and the acceptance of what it lists, come first
            print("NOT STARTED.")
            return 1
        print()
        try:
            session = Session(project, state, args.port, args.host)
        except Problem as problem:
            print(f"NOT STARTED: {problem}")
            return 2
        for line in (layer.report() + layer.unused()) if layer else []:     # files read when the tools were built, such as an LLM's
            print(line)
        print(f"project {project.folder.resolve().name}, setup {project.setup}: the page is at")
        print(f"  {session.address}")
        if not session.local_only:
            print(f"WARNING: served on {session.host}, not on this machine only. Any machine that can reach port "
                  f"{session.port} and has the token can use the page; the token is sent unencrypted.")
        print(f"What it keeps (the stored values, the logs, the files clients write there) is in {state}/; the tab Log lists the files.")
        print("Ctrl-C stops it.", flush=True)
        try:
            session.serve_forever()
        except KeyboardInterrupt:
            session.close()
        return 0
    result = prerun.check(project)
    if args.command == "accept":
        try:
            prerun.accept(result, state, args.by)
        except Problem as refused:
            print(prerun.report(result, None, known=known))
            print(f"NOT ACCEPTED: {refused}")
            return 1
    print(prerun.report(result, prerun.acceptance_of(result, state), detail=getattr(args, "all", False), known=known))
    return 1 if prerun.unacknowledged(result, state) else 0


if __name__ == "__main__":
    sys.exit(main())
