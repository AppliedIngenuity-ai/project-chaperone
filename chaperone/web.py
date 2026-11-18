"""The user interface: a web page served from this machine (specification 2.13).

The client of kind `web` gives a project's modules four elements: show, describe, enter, choose. `Session` serves the
page on 127.0.0.1 only, unless the user names another address at start, at an address that includes a token; it starts runs, each in its own thread, and refuses to
start one unless the current result of the pre-run check is accepted. The page displays the conversation, the runs,
the panes of ui.yaml, the pre-run result (it is accepted at the terminal, not here), and the states of each workflow.

Every value is escaped where it is put into the page (`_e`), whatever it is and wherever it came from. The page runs
one script, written here; nothing taken from a value is ever a script, a style or an address.
"""

from __future__ import annotations

import hmac
import html
import json
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import prerun
from .clients import Endpoint, Scripted, build_tools
from .engine import Outcome, Run
from .load import Project
from .store import Store
from .values import Problem

FIELD_TYPES = ("text", "email_list")                        # the types the enter element knows
_ADDRESS = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+")


def _e(value: Any) -> str:
    """`value` as text that is safe anywhere in the page: in an element and in a quoted attribute."""
    return html.escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), quote=True)


# ---- the conversation: what the elements show and ask ----------------------------------------------------------------

class Page:
    def __init__(self) -> None:
        self._changed = threading.Condition()
        self.transcript: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []            # elements waiting for the user
        self.version = 0
        self._ids = 0

    def _add(self, entry: dict[str, Any]) -> None:
        with self._changed:
            self.transcript.append(entry)
            self.version += 1

    def show(self, report: Any, marks: Any, style: Any) -> None:
        self._add({"kind": "show", "report": report, "marks": marks, "style": style})

    def ask(self, kind: str, prompt: str, **more: Any) -> Any:
        """Display an element and wait until the user presses its button or closes it."""
        with self._changed:
            self._ids += 1
            element = {"id": self._ids, "kind": kind, "prompt": prompt, "feedback": [], "typed": {}, "done": False, **more}
            self.pending.append(element)
            self.version += 1
            self._changed.wait_for(lambda: element["done"])
            self.pending.remove(element)
            self.transcript.append({"kind": "answered", "element": element})
            self.version += 1
            return element["answer"]

    def answer(self, element_id: int, form: dict[str, list[str]], closed: bool) -> None:
        """What the user sent for the element `element_id`. A value of an enter element that cannot be parsed keeps
        the element open, with what is wrong said beside what was typed."""
        with self._changed:
            element = next((e for e in self.pending if e["id"] == element_id and not e["done"]), None)
            if element is None:
                return
            kind = element["kind"]
            if closed:
                element["closed"] = True
                element["answer"] = ("" if kind == "describe" else [] if kind == "choose" else
                                     {name: [] if type_ == "email_list" else "" for name, type_ in element["fields"].items()})
            elif kind == "describe":
                element["answer"] = form.get("text", [""])[0]
            elif kind == "choose":
                ticked = {int(i) for i in form.get("item", []) if i.isdigit()}
                element["answer"] = [item for index, item in enumerate(element["items"])
                                     if index in ticked and item not in element["already"]]
            else:
                parsed, feedback = {}, []
                for name, type_ in element["fields"].items():
                    typed = form.get(f"field_{name}", [""])[0]
                    element["typed"][name] = typed
                    if type_ == "text":
                        parsed[name] = typed.strip()
                        continue
                    lines = [line.strip() for line in typed.splitlines() if line.strip()]
                    for number, line in enumerate(lines, 1):
                        if not _ADDRESS.fullmatch(line):
                            feedback.append(f"{name}, line {number}: “{line}” is not an address. Remove it or complete it.")
                    parsed[name] = lines
                element["feedback"] = feedback
                self.version += 1
                if feedback:
                    self._changed.notify_all()
                    return
                element["answer"] = parsed
            element["done"] = True
            self._changed.notify_all()


class WebClient:
    """What a module on a tool of kind `web` is passed as `ctx`."""

    def __init__(self, page: Page, show_updates: bool = False) -> None:
        self._page, self._show_updates = page, show_updates

    def show(self, report: Any, marks: Any, style: Any = None, update: bool = False) -> None:
        """Displays a value. With `update`, the value is news of what a run is doing, and it is displayed only when
        ui.yaml says `show_updates: true`; the call is checked and recorded either way."""
        if not update or self._show_updates:
            self._page.show(report, marks, style)

    def describe(self, prompt: str) -> str:
        return self._page.ask("describe", prompt)

    def enter(self, prompt: str, fields: dict[str, str]) -> dict[str, Any]:
        for name, type_ in fields.items():
            if type_ not in FIELD_TYPES:
                raise Problem("bad_field", f"the field {name} has the type {type_}; the types are {', '.join(FIELD_TYPES)}")
        return self._page.ask("enter", prompt, fields=dict(fields))

    def choose(self, items: list[Any], prompt: str, already: list[Any] | None = None) -> list[Any]:
        """The items the user ticks. An item equal to one of `already` is displayed ticked and cannot be changed: it
        was chosen before. Such an item is not among those returned."""
        return self._page.ask("choose", prompt, items=list(items), already=list(already or []))


# ---- a session: the project, its runs, the server --------------------------------------------------------------------

class Session:
    def __init__(self, project: Project, state: str | FilePath, port: int = 0, host: str = "127.0.0.1") -> None:
        """`host` is the address the page is served on. The default serves this machine only. Another address,
        such as 0.0.0.0 for every network the machine is on, is the user's choice at start: the token is then the
        only thing between that network and the page, and it travels unencrypted."""
        self.project, self.state = project, FilePath(state)
        self._fresh()
        self.token = secrets.token_urlsafe(24)
        self.server = ThreadingHTTPServer((host, port), _handler(self))
        self.host, self.port = host, self.server.server_address[1]
        self.local_only = host in ("127.0.0.1", "localhost", "::1")
        shown = "<this machine's address>" if host in ("0.0.0.0", "::") else host
        self.address = f"http://{shown}:{self.port}/?token={self.token}"

    def _fresh(self) -> None:
        self.page = Page()
        self.tools = build_tools(self.project, lambda tool: WebClient(self.page, bool(self.project.ui.get("show_updates"))), self.state)
        self.store = Store(self.state / "store.json", self.project.stores)
        self.runs: dict[str, dict[str, Any]] = {}          # workflow -> {run, thread, outcome}

    def running(self) -> list[str]:
        return [name for name, entry in self.runs.items() if entry["thread"].is_alive()]

    def stop(self, workflow: str) -> str:
        """Ask the run of `workflow` to stop. It ends before its next step: a call in progress finishes first. Every
        element that waits for the user is closed, so a run that waits on one goes on to its next step and ends."""
        if workflow not in self.running():
            return f"{workflow} is not running."
        self.runs[workflow]["run"].stop = True
        for element in list(self.page.pending):
            self.page.answer(element["id"], {}, closed=True)
        return ""

    def kept_files(self) -> list[FilePath]:
        """The files this system keeps in the state folder: the store, the acceptance, the logs, a calendar file with
        its record and history. A file the user wrote (the lines of a `text_file` client) is not among them."""
        names = ["store.json", "accepted.json", "user_exceptions.yaml", "access.jsonl", "llm.jsonl", "mail.jsonl", "calendar.jsonl"]
        for spec in self.project.tools.values():
            kind = spec["client"].get("kind")
            if kind in ("calendar_file", "google_calendar"):  # for Google, the record of the calendar's id is kept:
                base = FilePath(str(spec["client"]["file"]))  # without it the next write would make a second calendar
                names += ([base.name] if kind == "calendar_file" else []) + [base.with_suffix(".entries.json").name, base.with_suffix(".history.jsonl").name]
        return [self.state / name for name in names if (self.state / name).is_file()]

    def reset(self) -> str:
        """Delete every file of `kept_files`, and begin again: no run, no conversation, no acceptance. Refused while
        a run is in progress. Returns what to tell the user."""
        if self.running():
            return f"Not reset: {', '.join(self.running())} is running. Stop it first."
        for name, spec in self.project.tools.items():       # entries on Google would be left there with no record of them
            if spec["client"].get("kind") == "google_calendar" and self.tools[name].entries():
                return (f"Not reset: {len(self.tools[name].entries())} entries of this system are on Google Calendar. "
                        f"Run a workflow that removes them first.")
        files = self.kept_files()
        for file in files:
            file.unlink()
        self._fresh()
        return f"Reset: {len(files)} file(s) deleted from {self.state}. Nothing is accepted, monitored or handled."

    def result(self) -> tuple[prerun.Result, dict[str, Any] | None]:
        result = prerun.check(self.project)
        return result, prerun.acceptance_of(result, self.state)

    def start(self, workflow: str) -> str:
        """Start a run of `workflow` in its own thread. Returns what to tell the user, or "" when it started."""
        if workflow not in self.project.workflows:
            return f"{workflow} is not a workflow of this project."
        if self.result()[1] is None:
            return "Nothing runs until the result of the pre-run check is accepted, at the terminal: stop the program and start it again."
        if workflow in self.runs and self.runs[workflow]["thread"].is_alive():
            return f"{workflow} is already running."
        tools = {name: tool.fresh() if isinstance(tool, Scripted) else tool for name, tool in self.tools.items()}
        run = Run(self.project, tools, self.store, self.state / "access.jsonl")
        entry: dict[str, Any] = {"run": run, "outcome": None}

        def work() -> None:
            entry["outcome"] = run.run(workflow)
            with self.page._changed:
                self.page.version += 1
        entry["thread"] = threading.Thread(target=work, name=f"run {workflow}", daemon=True)
        self.runs[workflow] = entry
        entry["thread"].start()
        return ""

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---- drawing ------------------------------------------------------------------------------------------------------------

def _labels_at(marks: Any, path: tuple[Any, ...]) -> list[str]:
    for step in path:
        if isinstance(marks, dict) and step in marks:
            marks = marks[step]
        elif isinstance(marks, list) and isinstance(step, int) and step < len(marks):
            marks = marks[step]
        else:
            return []
    return [m for m in marks if isinstance(m, str)] if isinstance(marks, list) and all(isinstance(m, str) for m in marks) else []


def _scalars(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, dict):
        return [p for key, inner in value.items() for p in _scalars(inner, path + (key,))]
    if isinstance(value, list):
        return [p for index, inner in enumerate(value) for p in _scalars(inner, path + (index,))]
    return [path]


def _chips(labels: list[str], ui: dict[str, Any]) -> str:
    order = list(ui.get("labels", {}))
    known = sorted((l for l in labels if l in order), key=order.index) + [l for l in labels if l not in order]
    return "".join(f'<span class="chip lab{order.index(l) if l in order else "x"}">{_e(ui["labels"][l]["words"] if l in order else l)}</span>'
                   for l in known)


def _first(labels: list[str], ui: dict[str, Any]) -> str:
    order = list(ui.get("labels", {}))
    known = sorted((l for l in labels if l in order), key=order.index)
    return f"lab{order.index(known[0])}" if known else "labx"


def render_report(report: Any, marks: Any, ui: dict[str, Any], style: Any = None) -> str:
    """A shown value. A part with labels is one outline that holds the value and the words of its labels, in the
    colour of the first of them ui.yaml lists. When every part has the same labels, the box has one bar in that
    colour and the labels once, in its heading."""
    layout = (ui.get("styles", {}).get(style or ui.get("default_style"), {}) or {}).get("layout")
    sets = [tuple(sorted(_labels_at(marks, path))) for path in _scalars(report)]
    whole = list(sets[0]) if sets and sets[0] and all(s == sets[0] for s in sets) and len(sets) > 1 else []

    def scalar(value: Any, path: tuple[Any, ...]) -> str:
        labels = [] if whole else _labels_at(marks, path)
        text = f'<span class="val">{_e(value)}</span>'
        return f'<span class="lv {_first(labels, ui)}">{text}{_chips(labels, ui)}</span>' if labels else text

    def node(value: Any, path: tuple[Any, ...]) -> str:
        if isinstance(value, dict):
            return '<div class="fields">' + "".join(
                f'<div class="part"><span class="key">{_e(str(key))}</span>{node(inner, path + (key,))}</div>'
                for key, inner in value.items()) + "</div>"
        if isinstance(value, list):
            if layout == "table" and value and all(isinstance(item, dict) for item in value):
                columns = list(dict.fromkeys(key for item in value for key in item))
                head = "".join(f"<th>{_e(str(c))}</th>" for c in columns)
                rows = "".join("<tr>" + "".join(f"<td>{node(item[c], path + (i, c)) if c in item else ''}</td>" for c in columns) + "</tr>"
                               for i, item in enumerate(value))
                return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'
            if not value:
                return '<span class="key">none</span>'
            return '<div class="items">' + "".join(f'<div class="part">{node(item, path + (i,))}</div>' for i, item in enumerate(value)) + "</div>"
        return scalar(value, path)

    if layout == "passages" and _is_passages(report):
        return '<div class="report">' + "".join(_group(group, marks, (i,), ui) or f'<div class="part">{node(group, (i,))}</div>'
                                                for i, group in enumerate(report)) + "</div>"
    heading = f'<div class="rhead">{_chips(whole, ui)}<span class="key">every part</span></div>' if whole else ""
    return f'<div class="report{" whole " + _first(whole, ui) if whole else ""}">{heading}{node(report, ())}</div>'


def _is_passages(report: Any) -> bool:
    """Whether a shown value has the form the layout `passages` draws (specification 2.13): a list of groups, each a
    dictionary whose `passages` is a list of dictionaries, each with `pieces`, a list of dictionaries with a text
    under `text`; every other value of a group and of a passage is a scalar."""
    def scalars_but(value: dict[str, Any], key: str) -> bool:
        return all(not isinstance(inner, (dict, list)) for name, inner in value.items() if name != key)
    return (isinstance(report, list) and bool(report) and all(
        isinstance(group, dict) and isinstance(group.get("passages"), list) and scalars_but(group, "passages") and all(
            isinstance(passage, dict) and isinstance(passage.get("pieces"), list) and scalars_but(passage, "pieces") and all(
                isinstance(piece, dict) and isinstance(piece.get("text"), str) and scalars_but(piece, "")
                for piece in passage["pieces"])
            for passage in group["passages"])
        for group in report))


def _group(group: dict[str, Any], marks: Any, path: tuple[Any, ...], ui: dict[str, Any]) -> str:
    """One group of the layout `passages`: a box in the colour of its labels, with the labels once; its heading is the
    group's own values; each passage is closed on its pieces marked `first`, and opens to all of its pieces. The page
    writes the mark of a gap; every text is a value, escaped. Returns "" when the parts of the group do not all
    carry the same labels: the group is then drawn as any value, each part with its own labels."""
    sets = {tuple(sorted(_labels_at(marks, path + inner))) for inner in _scalars(group)}
    if len(sets) != 1:
        return ""
    labels = list(sets.pop())

    def piece(one: dict[str, Any], opened: bool) -> str:
        level = one.get("level") if isinstance(one.get("level"), int) and not isinstance(one.get("level"), bool) and 0 <= one.get("level") <= 3 else 0
        classes = [f"em{level}"] + (["quoted"] if one.get("quoted") is True else []) + (["rest"] if opened and one.get("first") is not True else [])
        return f'<span class="{" ".join(classes)}">{_e(one["text"])}</span>'

    def passage(one: dict[str, Any]) -> str:
        caption = " · ".join(_e(value) for name, value in one.items() if name != "pieces")
        closed, hidden = [], False
        for each in one["pieces"]:
            if each.get("first") is True:
                closed.append(('<span class="gap">[…]</span> ' if hidden else "") + piece(each, False))
                hidden = False
            else:
                hidden = True
        if hidden:
            closed.append(' <span class="gap">[…]</span>')
        return (f'<details class="passage"><summary><span class="key">{caption}</span><span class="first">{"".join(closed)}</span></summary>'
                f'<div class="whole">{"".join(piece(each, True) for each in one["pieces"])}</div></details>')

    own = [_e(value) for name, value in group.items() if name != "passages"]
    heading = (f"<b>{own[0]}</b> " if own else "") + " ".join(f'<span class="key">{value}</span>' for value in own[1:])
    return (f'<div class="group {_first(labels, ui)}"><div class="rhead">{_chips(labels, ui)}{heading}</div>'
            f'{"".join(passage(one) for one in group["passages"])}</div>')


def _item(item: Any) -> str:
    if isinstance(item, dict):                               # the first value, then each other with its key; a list one item per line
        plain = " ".join(f'<span class="val">{_e(v)}</span>' if i == 0 else f'<span class="key">{_e(str(k))} {_e(v)}</span>'
                         for i, (k, v) in enumerate(item.items()) if not isinstance(v, list))
        lists = "".join(f'<div class="sub"><span class="key">{_e(str(k))}</span>' + "".join(f"<div>{_e(one)}</div>" for one in v) + "</div>"
                        for k, v in item.items() if isinstance(v, list) and v)
        return plain + lists
    return f'<span class="val">{_e(item)}</span>'


def _element(element: dict[str, Any], token: str) -> str:
    kind, done = element["kind"], element["done"]
    words = {"describe": "describe · free text", "enter": "enter · fixed format", "choose": "choose"}[kind]
    head = f'<div class="ehead"><span>{_e(element["prompt"])}</span><span class="ekind {kind}">{words}</span></div>'
    if done:
        if element.get("closed"):
            body = '<span class="done">Closed.</span>'
        elif kind == "describe":
            return f'<div class="turn"><span class="who">assistant</span><div class="said">{_e(element["prompt"])}</div></div>' \
                   f'<div class="turn user"><span class="who">you</span><div class="said">{_e(element["answer"])}</div></div>'
        elif kind == "choose":
            body = "".join(f'<div class="row">{_item(item)}</div>' for item in element["answer"]) or '<span class="done">Nothing chosen.</span>'
        else:
            body = "".join(f'<div class="row"><span class="key">{_e(name)}</span> '
                           + (" ".join(_item(one) for one in value) if isinstance(value, list) and value else _item("none" if value == [] else value))
                           + "</div>" for name, value in element["answer"].items())
        return f'<div class="element">{head}<div class="ebody">{body}</div></div>'
    if kind == "describe":
        inner = '<textarea name="text" rows="2" autofocus></textarea>'
    elif kind == "choose":
        inner = "".join(f'<label class="row"><input type="checkbox" checked disabled> {_item(item)} <span class="done">already chosen</span></label>'
                        if item in element["already"] else
                        f'<label class="row"><input type="checkbox" name="item" value="{index}"> {_item(item)}</label>'
                        for index, item in enumerate(element["items"])) or '<span class="done">The list is empty.</span>'
    else:
        inner = ""
        for name, type_ in element["fields"].items():
            typed = _e(element["typed"].get(name, ""))
            box = (f'<textarea name="field_{_e(name)}" rows="3">{typed}</textarea>' if type_ == "email_list"
                   else f'<input type="text" name="field_{_e(name)}" value="{typed}">')
            inner += f'<label class="field"><span class="key">{_e(name)}</span>{box}</label>'
        inner += '<div class="feedback">' + "".join(f'<span class="bad">{_e(line)}</span>' for line in element["feedback"]) + "</div>"
    button = {"describe": "Send", "enter": "Save", "choose": "Confirm"}[kind]
    return (f'<form class="element active" method="post" action="/answer">{head}<div class="ebody">'
            f'<input type="hidden" name="token" value="{_e(token)}"><input type="hidden" name="element" value="{element["id"]}">'
            f'{inner}<div class="actions"><button class="btn go" name="act" value="send">{button}</button>'
            f'<button class="btn" name="act" value="close">Close</button></div></div></form>')


def _talk(session: Session) -> str:
    ui, page, out = session.project.ui, session.page, []
    with page._changed:
        transcript, pending = list(page.transcript), list(page.pending)
    for entry in transcript:
        out.append(render_report(entry["report"], entry["marks"], ui, entry["style"]) if entry["kind"] == "show"
                   else _element(entry["element"], session.token))
    out += [_element(element, session.token) for element in pending]
    if not out:
        out.append('<p class="done">Nothing is running. Start a workflow from “Runs”.</p>')
    return "".join(out)


def _side(session: Session) -> str:
    ui, out = session.project.ui, ['<div class="pane"><h2>Runs</h2><div class="in">']
    counts: dict[str, int] = {}
    for name in session.project.workflows:
        entry = session.runs.get(name)
        if entry is None:
            status, dot = "not started", "off"
        elif entry["thread"].is_alive():
            waiting = any(not e["done"] for e in session.page.pending)
            at = entry["run"].at
            status, dot = f'state {at[1] if at else "…"} · {"waiting for you" if waiting else "running"}', "wait" if waiting else "on"
        else:
            outcome: Outcome = entry["outcome"]
            ended = {"end": "ended", "error": "stopped on an error", "too_many_states": "stopped: too many states",
                     "stopped": "stopped by the user"}[outcome.ended]
            kind = f' ({outcome.error.data["type"]})' if outcome.error is not None else ""
            status, dot = f"{ended} in {outcome.state}{kind}", "off"
        if entry is not None:
            for record in entry["run"].log:
                if record["call"] in session.project.declarations:
                    counts[record["call"]] = counts.get(record["call"], 0) + 1
        start = (f'<form method="post" action="/stop"><input type="hidden" name="token" value="{_e(session.token)}">'
                 f'<input type="hidden" name="workflow" value="{_e(name)}"><button class="btn small">Stop</button></form>'
                 ) if entry is not None and entry["thread"].is_alive() else (
            f'<form method="post" action="/start"><input type="hidden" name="token" value="{_e(session.token)}">'
            f'<input type="hidden" name="workflow" value="{_e(name)}"><button class="btn small">Start</button></form>')
        about = session.project.workflows[name].description
        out.append(f'<div class="run"><div><span class="dot {dot}"></span><strong>{_e(name)}</strong>'
                   + (f'<div class="done">{_e(about)}</div>' if about else "") +
                   f'<div class="state">{_e(status)}</div></div>{start}</div>')
    out.append("</div></div>")
    models = [(name, tool._model.settings) for name, tool in session.tools.items() if isinstance(tool, Endpoint)]
    if models:
        out.append('<div class="pane"><h2>LLM servers</h2><div class="in">' + "".join(
            f'<div class="kv"><span class="mono">{_e(name)}</span><span>{_e(settings.model)}</span></div>' for name, settings in models) + "</div></div>")
    for pane in ui.get("panes") or []:
        out.append(f'<div class="pane"><h2>{_e(pane["title"])}</h2><div class="in">')
        for store in pane["stores"]:
            kept = session.store.get(store).data
            out.append(f'<div class="kv"><span>{_e(store)}</span><span>{_e(len(kept) if isinstance(kept, (list, dict)) else kept)}</span></div>')
        out.append("</div></div>")
    for name, entry in session.runs.items():
        run = entry["run"]
        log = [(index, record) for index, record in enumerate(list(run.log)) if record["workflow"] == name]
        errors = [(index, record) for index, record in log if "error" in record]
        rows = "".join(f'<div class="act"><span class="mono">{_e(record["state"])} · {_e(record["call"])}</span></div>' for _, record in log[-8:])
        said = "".join(f'<div class="act bad"><span class="mono">{_e(record["state"])} · {_e(record["call"])} · {_e(record["error"]["type"])}</span>'
                       f'<div>{_e(_said(run.reasons.get(index, {})))}</div></div>' for index, record in errors[-3:])
        out.append(f'<div class="pane"><h2>Activity · {_e(name)}</h2><div class="in"><div class="done">{len(log)} calls so far; the last {min(len(log), 8)}:</div>'
                   f'{rows}<div class="done">{len(errors)} error(s){"; the last " + str(min(len(errors), 3)) + " (all of them: the tab Log):" if errors else ""}</div>{said}</div></div>')
    if counts:
        out.append('<div class="pane"><h2>Calls</h2><div class="in">' + "".join(
            f'<div class="kv"><span class="mono">{_e(call)}</span><span>{number}</span></div>' for call, number in sorted(counts.items())) + "</div></div>")
    out.append('<div class="pane"><h2>Labels</h2><div class="in">' + "".join(
        f'<div>{_chips([label], ui)}</div>' for label in ui.get("labels", {})) + "</div></div>")
    return "".join(out)


def _exception_rows(exceptions: list[dict[str, Any]], result: Any, accepted: str) -> str:
    rows = []
    for exception in exceptions:
        calls = result.permits.get(exception["id"], [])
        listed = "".join(f"<div>{_e(call)}</div>" for call in calls)
        rows.append(f'<tr><td><code>{_e(exception["id"])}</code></td><td><code>{_e(exception["rule"])}</code></td>'
                    f'<td class="mono">except: {_e(prerun._flow(exception["except"]))}</td><td class="mono">{_e(prerun._place(exception))}</td>'
                    f'<td><details><summary>{len(calls) if calls else "no call needs it"}</summary>{listed}</details></td><td>{_e(accepted)}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>Exception</th><th>Rule</th><th>As written</th><th>Where</th><th>Calls it permits</th>'
            f'<th>Accepted</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _check(session: Session) -> str:
    """The tab "Rules and exceptions". Three parts: the configuration (the rules and its exceptions, as written, with
    where); the exceptions the user approved at a start, which are not in the configuration; the state now (the
    acceptance, the calls refused in runs, and, closed until opened, details for whoever runs the system). It
    reports; the result is accepted at the terminal, when the program starts."""
    project = session.project
    result, acceptance = session.result()
    known = prerun.acknowledged(session.state)
    risks = [entry for entry in result.refused if (entry["rule"], entry["call"]) in known]
    violations = len({entry["rule"] for entry in result.refused})
    configured = [e for e in project.exceptions if not e.get("user")]
    approved = [e for e in project.exceptions if e.get("user")]
    accepted = f'{acceptance["by"]}, {acceptance["at"]}' if acceptance else "not accepted"
    out = ['<div class="verdict">',
           f'<div class="stat"><span class="big">{len(project.rules)}</span><small>rules</small></div>',
           f'<div class="stat"><span class="big">{len(configured)}</span><small>exceptions in the configuration</small></div>',
           f'<div class="stat"><span class="big">{len(approved)}</span><small>exceptions approved at a start</small></div>',
           f'<div class="stat"><span class="big {"zero" if not violations else "some"}">{violations}</span><small>possible violations</small></div>',
           f'<div class="stat"><span class="big {"zero" if not risks else "some"}">{len(risks)}</span><small>calls acknowledged as a risk: refused when made</small></div>',
           "</div>"]
    for place, line in prerun.unmatched_lines(project):     # a warning: it changes no call
        out.append(f'<div class="message">Warning, {_e(place)}: no module matches this line of <code>modules</code>; no call changes.'
                   f'<div class="mono">{_e(line)}</div></div>')
    for entry in result.unsaved:
        out.append(f'<div class="refused">{_e(entry["call"])} references {_e(entry["name"])}, which some route does not save first.</div>')

    out.append('<h2 class="part">1. The configuration</h2>')
    out.append('<div class="pane"><h2>Rules</h2><div class="in">')
    for rule in project.rules:
        refused = [entry for entry in result.refused if entry["rule"] == rule["id"]]
        named = lambda some: ", ".join(f"<code>{_e(e['id'])}</code>" for e in some if e["rule"] == rule["id"]) or "none"
        inside = [f'<div class="mono">never: {_e(prerun._flow(rule["never"]))}</div>',
                  f'<div>Exceptions to this rule, in the configuration: {named(configured)}</div>',
                  f'<div>Exceptions to this rule, approved at a start, not in the configuration: {named(approved)}</div>']
        for entry in refused:
            route = " > ".join(entry["route"]) if entry["route"] else "no route from the start reaches this state"
            because = f' (an argument may carry {entry["label"]})' if entry["label"] else ""
            risk = " Acknowledged risk: a run refuses this call when it is made." if (entry["rule"], entry["call"]) in known else ""
            inside.append(f'<div class="refused">Possible violation: <strong>{_e(entry["call"])}</strong>{_e(because)}; no exception permits it. Route: {_e(route)}.{risk}</div>')
        out.append(f'<details{" open" if refused else ""}><summary><code>{_e(rule["id"])}</code> <span class="done">{_e(prerun._place(rule))}'
                   f'{" · possible violation" if refused else ""}</span></summary><div class="in">{"".join(inside)}</div></details>')
    out.append("</div></div>")
    out.append('<div class="pane"><h2>Exceptions in the configuration</h2>' + (_exception_rows(configured, result, accepted) if configured
               else '<div class="in"><span class="done">None.</span></div>') + "</div>")

    out.append('<h2 class="part">2. Exceptions approved at a start, not in the configuration</h2>')
    out.append('<div class="pane"><h2>' + _e(str(prerun.user_exceptions_file(session.state))) + '</h2>' + (_exception_rows(approved, result, accepted) if approved
               else '<div class="in"><span class="done">None: every exception in force is in the configuration.</span></div>') + "</div>")

    out.append('<h2 class="part">3. Risks acknowledged at a start: no exception; a run refuses each call when it is made</h2>')
    rows = "".join(f'<tr><td class="mono">{_e(rule)}</td><td class="mono">{_e(call)}</td><td>{_e(str(line.get("by", "")))}, {_e(str(line.get("at", "")))}</td>'
                   f'<td>{"yes" if (rule, call) in {(e["rule"], e["call"]) for e in result.refused} else "no: the call is not refused now; the line has no effect"}</td></tr>'
                   for (rule, call), line in known.items())
    out.append('<div class="pane"><h2>' + _e(str(prerun.acknowledged_file(session.state))) + '</h2>' + (
        f'<div class="scroll"><table><thead><tr><th>Rule</th><th>Call</th><th>Acknowledged by</th><th>In force</th></tr></thead><tbody>{rows}</tbody></table></div>'
        if known else '<div class="in"><span class="done">None: every call a rule forbids is permitted by an exception.</span></div>') + "</div>")

    out.append('<h2 class="part">4. The state now</h2>')
    if acceptance:
        said = (f'<span class="done">Accepted by {_e(acceptance["by"])}, {_e(acceptance["at"])}, {_e(prerun.how_accepted(acceptance))}: every rule and '
                f'every exception above, as written{f", and {len(risks)} call(s) acknowledged as a risk, which a run refuses" if risks else ""}. Runs may start under the setup {_e(result.setup)}.</span>')
    elif len(risks) < len(result.refused):
        said = '<span class="bad">Not accepted: a possible violation is listed under its rule. Nothing runs. To accept: stop the program and start it again.</span>'
    else:
        said = '<span class="bad">Not accepted. Nothing runs. To accept: stop the program and start it again.</span>'
    out.append(f'<div class="pane"><h2>Acceptance</h2><div class="in"><div>{said}</div>'
               f'<div class="kv"><span>result</span><span>{_e(result.fingerprint()[:12])}</span></div>'
               f'<div class="kv"><span>kept in</span><span>{_e(str(prerun.accepted_file(session.state)))}</span></div></div></div>')
    refusals = [(name, entry) for name, kept in session.runs.items() for entry in kept["run"].log
                if (entry.get("error") or {}).get("type") == "refused" and any(not v["permitted_by"] for v in entry.get("forbidden_by", []))]
    carried = lambda entry: "; ".join(f"{argument}: {', '.join(labels) or 'no label'}" for argument, labels in entry.get("arguments", {}).items())
    risked = lambda name, entry: all((v["rule"], f'{name}.{entry.get("state", "")}: {entry.get("call", "")}') in known
                                     for v in entry.get("forbidden_by", []) if not v["permitted_by"])
    rows = "".join(f'<tr><td class="mono">{_e(name)}.{_e(entry.get("state", ""))}</td><td class="mono">{_e(entry.get("call", ""))}</td><td class="mono">'
                   f'{_e(", ".join(v["rule"] for v in entry.get("forbidden_by", []) if not v["permitted_by"]))}</td>'
                   f'<td class="mono">{_e(carried(entry))}</td><td>{"yes" if risked(name, entry) else "no"}</td></tr>' for name, entry in refusals)
    out.append('<div class="pane"><h2>Calls refused in the runs since the program started</h2>' + (
        f'<div class="scroll"><table><thead><tr><th>State</th><th>Call</th><th>Rule that forbids it; no exception permits it</th>'
        f'<th>Each argument, and the labels it carries</th><th>Acknowledged as a risk at a start</th></tr></thead><tbody>{rows}</tbody></table></div>'
        if refusals else '<div class="in"><span class="done">None.</span></div>') + "</div>")
    more = [f'<div class="kv"><span>calls checked</span><span>{result.checked}</span></div>',
            f'<div class="kv"><span>exceptions that no call needs</span><span>{_e(", ".join(result.idle) or "none")}</span></div>',
            '<div>Calls that rest on label requests: each is not refused only while the label requests of the modules named are applied.'
            + ("" if result.depends else " None.") + "</div>"]
    more += [f'<div class="kv"><span class="mono">{_e(call)}</span><span>{_e(", ".join(modules))}</span></div>' for call, modules in result.depends.items()]
    out.append(f'<details class="pane"><summary><h2 style="display:inline">More details, for whoever runs the system</h2></summary><div class="in">{"".join(more)}</div></details>')
    return '<div class="stack">' + "".join(out) + "</div>"


def _workflows(session: Session) -> str:
    out = []
    for workflow in session.project.workflows.values():
        rows = ""
        for name, state in workflow.states.items():
            calls = ", ".join(step["call"] for step in state.get("steps") or []) or "none"
            following = state.get("next")
            if isinstance(following, dict):
                after = "; ".join(f"{key}: {target}" for key, target in following["goto"].items())
                after += f"; else: {following['else']}" if "else" in following else ""
            else:
                after = following or "the workflow ends"
            routes = state.get("on_error") or ""
            rows += f"<tr><td><code>{_e(name)}</code></td><td>{_e(calls)}</td><td>{_e(after)}</td><td>{_e(routes)}</td></tr>"
        shared = workflow.on_error or "none: an error ends the run"
        out.append(f'<div class="pane"><h2>{_e(workflow.name)} · starts at {_e(workflow.start)} · error routes {_e(shared)}</h2>'
                   + (f'<div class="in">{_e(workflow.description)}</div>' if workflow.description else "") +
                   f'<div class="scroll"><table><thead><tr><th>State</th><th>Calls</th><th>Next</th><th>Its error routes</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table></div></div>')
    return '<div class="stack">' + "".join(out) + "</div>"


def _values(session: Session) -> str:
    """The tab "State" (ui.yaml, `state_tab: true`): every stored value, and what each run's steps saved so far, each
    drawn with its labels as a shown value is. Closed until the user opens one."""
    ui = session.project.ui

    def one(name: str, value: Any) -> str:
        data = value.data                                     # what the line says before it is opened: a list's length, a
        if isinstance(data, list):                            # record's number of fields, or a short value itself (0 is 0)
            size = f"list, {len(data)} item{'' if len(data) == 1 else 's'}"
        elif isinstance(data, dict):
            size = f"one record, {len(data)} field{'' if len(data) == 1 else 's'}"
        else:
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            size = _e(text) if len(text) <= 40 else "one value"
        return (f'<details class="held"><summary><span class="mono">{_e(name)}</span> <span class="done">{size}</span> '
                f'{_chips(sorted(value.carried()), ui)}</summary>{render_report({name: value.data}, {name: value.marks().data}, ui)}</details>')
    out = ['<div class="pane"><h2>Stored values · kept between runs</h2><div class="in">'
           + ("".join(one(name, session.store.get(name)) for name in session.project.stores) or '<span class="done">The project declares no store.</span>')
           + "</div></div>"]
    for workflow, entry in session.runs.items():
        saved = dict(entry["run"].saved)
        out.append(f'<div class="pane"><h2>Saved by the steps of the run of {_e(workflow)}</h2><div class="in">'
                   + ("".join(one(name, value) for name, value in saved.items()) or '<span class="done">Nothing saved yet.</span>') + "</div></div>")
    if ui.get("reset_control"):
        files = "".join(f"<li><code>{_e(file.name)}</code></li>" for file in session.kept_files()) or "<li>none yet</li>"
        out.append(f'<div class="pane"><h2>Start from nothing</h2><div class="in"><div>Deletes what this system keeps in '
                   f'<code>{_e(str(session.state))}</code>: the stored values, the acceptance, the logs, the calendar file. '
                   f'Refused while a run is in progress. The files:</div><ul>{files}</ul>'
                   f'<form class="actions" method="post" action="/reset"><input type="hidden" name="token" value="{_e(session.token)}">'
                   f'<label><input type="checkbox" name="sure" value="yes" required> Delete these files</label>'
                   f'<button class="btn">Start from nothing</button></form></div></div>')
    return '<div class="stack">' + "".join(out) + "</div>"


def _said(reason: dict[str, Any]) -> str:
    """What the page displays of an error beside its type: the message, and, when a rule refused the call of `log`
    (2.12), that what the module wrote is not shown, with the rule."""
    withheld = reason.get("withheld")
    if not withheld:
        return reason.get("message") or ""
    carried = f" (the error carries {withheld['label']})" if withheld.get("label") else ""
    return ((reason.get("message") + ". " if reason.get("message") else "")
            + f"What the module wrote is not shown: {withheld['rule']} forbids the call of log{carried} and no exception permits it.")


def _log(session: Session) -> str:
    """The tab "Log": every call of every run since the page started, and each error with its message and detail. It
    does not refresh by itself, so its text can be selected and copied. The detail of an error may hold values."""
    what = {"store.json": "the stored values", "accepted.json": "the accepted results of the pre-run check",
            "access.jsonl": "every call of every run: names and labels, no value", "llm.jsonl": "each LLM call: model, time, tokens",
            "mail.jsonl": "each mail search: ids and counts", "calendar.jsonl": "each calendar write: key and outcome"}
    files = ""
    for file in session.kept_files():
        about = what.get(file.name) or ("the calendar’s history: every write with its title, times and description" if file.name.endswith(".history.jsonl")
                                         else "the calendar’s entries now, by key" if file.name.endswith(".entries.json") else "the calendar file")
        files += f"<tr><td><code>{_e(str(file))}</code></td><td>{_e(about)}</td></tr>"
    out = [f'<div class="pane"><h2>Files of this setup · {_e(str(session.state))}</h2><div class="scroll"><table><tbody>'
           f'{files or "<tr><td>none yet</td></tr>"}</tbody></table></div></div>']
    for name, entry in session.runs.items():
        run = entry["run"]
        log = [(index, record) for index, record in enumerate(list(run.log)) if record["workflow"] == name]
        errors, rows = "", ""
        for index, record in log:
            failed = record.get("error")
            reason = run.reasons.get(index, {})
            if failed:
                detail = json.dumps(reason.get("detail"), ensure_ascii=False, default=str) if reason.get("detail") is not None else ""
                errors += (f'<tr><td>{index + 1}</td><td><code>{_e(record["state"])}</code></td><td><code>{_e(record["call"])}</code></td>'
                           f'<td>{_e(failed["type"])}</td><td>{_e(_said(reason))}</td><td><code>{_e(detail)}</code></td></tr>')
            labels = ", ".join(record.get("result", [])) if "result" in record else ""
            rows += (f'<tr><td>{index + 1}</td><td><code>{_e(record["state"])}</code></td><td><code>{_e(record["call"])}</code></td>'
                     f'<td>{_e(labels)}</td><td>{_e(failed["type"]) if failed else ""}</td></tr>')
        out.append(f'<div class="pane"><h2>{_e(name)} · {len(log)} calls · errors</h2><div class="scroll"><table><thead><tr><th>#</th><th>State</th>'
                   f'<th>Call</th><th>Type</th><th>Message</th><th>Detail</th></tr></thead><tbody>{errors}</tbody></table></div></div>'
                   f'<details class="pane"><summary><h2 style="display:inline">{_e(name)} · every call</h2></summary><div class="scroll"><table><thead><tr><th>#</th>'
                   f'<th>State</th><th>Call</th><th>Labels of the result</th><th>Error</th></tr></thead><tbody>{rows}</tbody></table></div></details>')
    if not session.runs:
        out.append('<div class="pane"><div class="in">No run since the page started. Earlier runs: access.jsonl, above.</div></div>')
    return '<div class="stack">' + "".join(out) + "</div>"


_CSS = """
:root{--bg:#f3f5f7;--surface:#fff;--sunk:#e9edf1;--line:#cfd6de;--fg:#18212b;--muted:#5b6876;--accent:#0b6b6f;--accent-fg:#fff;
--attention:#8a5a00;--attention-bg:#fbf0d6;--good:#1e6f3a;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#12171d;--surface:#1a212a;--sunk:#232c37;--line:#36414e;--fg:#e6ebf0;--muted:#9aa7b5;
--accent:#4fc3c7;--accent-fg:#06282a;--attention:#f0c060;--attention-bg:#3a2e12;--good:#7fd49a;--bad:#ff8a80;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:18px 16px 40px}
.wrap{max-width:1120px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 16px}h1{font-size:20px;margin:0}
.sub,.done{color:var(--muted);font-size:13px}.mono,code,.key,.val,.who,.state,.chip,.ekind,th,.pane h2{font-family:ui-monospace,Menlo,Consolas,monospace}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line)}nav a{color:var(--muted);text-decoration:none;font-weight:500;padding:8px 12px;border-bottom:3px solid transparent}
nav a.on{color:var(--fg);border-bottom-color:var(--accent)}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;align-items:start}@media (max-width:820px){.cols{grid-template-columns:minmax(0,1fr)}}
.talk,.side,.stack{display:flex;flex-direction:column;gap:10px;min-width:0}.stack{gap:14px}
.gone{position:sticky;top:0;z-index:5}.gone[hidden]{display:none}h2.part{font-size:15px;margin:10px 0 0;padding-top:12px;border-top:2px solid var(--line)}.pane details>summary{cursor:pointer}
@media (min-width:821px){body.fixed{height:100vh;box-sizing:border-box;overflow:hidden;padding-bottom:18px}body.fixed .wrap{height:100%}body.fixed .cols{flex:1;min-height:0;align-items:stretch}body.fixed .talk,body.fixed .side{overflow-y:auto;min-height:0;padding-right:6px}body.fixed .talk>*,body.fixed .side>*{flex-shrink:0}}
.turn{display:flex;flex-direction:column;gap:4px}.who{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.said{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:9px 12px;white-space:pre-wrap;overflow-wrap:anywhere}
.turn.user .said{background:var(--sunk);align-self:flex-end;max-width:85%}.turn.user .who{align-self:flex-end}
.report{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:8px 12px;display:flex;flex-direction:column;gap:6px}
.report.whole{border-left-width:6px}.rhead,.part{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.fields,.items{display:flex;flex-direction:column;gap:5px;min-width:0}.key{font-size:12px;color:var(--muted)}.val{font-size:13px;overflow-wrap:anywhere;white-space:pre-wrap}
.lv{display:inline-flex;flex-wrap:wrap;gap:3px 6px;align-items:baseline;border:1px solid var(--line);border-radius:3px;padding:1px 4px 1px 7px;min-width:0}
.chip{font-size:11px;padding:0 6px;border-radius:3px;white-space:nowrap;background:var(--surface);border:1px solid currentColor}
.element{background:var(--surface);border:1px solid var(--line);border-radius:4px;display:flex;flex-direction:column}
.element.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.ehead{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 12px;padding:7px 12px;border-bottom:1px solid var(--line);font-size:13px}
.ekind{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}.ekind.enter,.ekind.choose{color:var(--good)}
.ebody{padding:10px 12px;display:flex;flex-direction:column;gap:8px}.row{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:3px}textarea,input[type=text]{font:13px ui-monospace,Menlo,Consolas,monospace;width:100%;box-sizing:border-box;
background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:3px;padding:7px 9px}
.feedback{display:flex;flex-direction:column;gap:2px;font-size:12px}.bad{color:var(--attention);background:var(--attention-bg);padding:1px 6px;border-radius:3px;align-self:flex-start}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.btn{font:inherit;font-weight:500;border-radius:3px;padding:6px 14px;cursor:pointer;border:1px solid var(--line);background:var(--surface);color:var(--fg)}
.btn.go{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}.btn.small{padding:2px 10px;font-size:13px}
button:focus-visible,input:focus-visible,textarea:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pane{background:var(--surface);border:1px solid var(--line);border-radius:4px}.pane h2{font-size:12px;font-weight:500;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin:0;padding:7px 12px;border-bottom:1px solid var(--line)}
.pane .in{padding:9px 12px;display:flex;flex-direction:column;gap:7px;font-size:13px}.kv,.run{display:flex;justify-content:space-between;gap:10px;align-items:center}
.kv span:last-child{font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;text-align:right}.state{font-size:12px;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--line);margin-right:6px}.dot.on{background:var(--good)}.dot.wait{background:var(--attention)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;font-weight:500;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}td.n{font-variant-numeric:tabular-nums;text-align:right}
.verdict{display:flex;flex-wrap:wrap;gap:10px 28px;align-items:center;background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:12px 14px}
.big{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}.big.zero{color:var(--good)}.big.some{color:var(--bad)}.stat{display:flex;flex-direction:column}.stat small{color:var(--muted);font-size:12px}
.sub{margin:2px 0 6px 26px;font-size:12px;color:var(--muted)}
.act{font-size:12px;padding:1px 0}.act.bad{display:block;margin:3px 0}
.held{border-bottom:1px solid var(--line);padding:6px 0}.held summary{cursor:pointer}.held[open] summary{margin-bottom:6px}
.group{border:2px solid var(--line);border-radius:6px;padding:8px 10px;display:flex;flex-direction:column;gap:6px}
.passage{border:1px solid var(--line);border-radius:4px;padding:4px 8px}.passage summary{cursor:pointer}.passage summary .key{display:block}
.passage[open] .first{display:none}.passage .whole{margin-top:4px}.passage .rest{color:var(--muted)}.gap{color:var(--muted)}
.em1{background:rgba(255,200,0,.18)}.em2{background:rgba(255,190,0,.42)}.em3{background:rgba(255,160,0,.7);font-weight:600}
.quoted{text-decoration:underline;text-decoration-thickness:2px}
.setup-note{border:1px solid var(--attention);color:var(--attention);border-radius:4px;padding:6px 12px;font-size:13px;margin-bottom:8px}
.refused,.message{background:var(--attention-bg);color:var(--attention);border-radius:4px;padding:8px 12px;font-size:13px}
.labx{color:var(--muted)}
"""


def _label_css(ui: dict[str, Any]) -> str:
    """One class for each label of ui.yaml; its colour was checked to be #rrggbb when the project was loaded."""
    out = ""
    for index, spec in enumerate(ui.get("labels", {}).values()):
        colour = spec["colour"] if re.fullmatch(r"#[0-9a-fA-F]{6}", str(spec["colour"])) else "#777777"
        out += (f".chip.lab{index}{{color:{colour}}}.lv.lab{index}{{border-color:{colour};background:color-mix(in srgb,{colour} 12%,transparent)}}"
                f".report.whole.lab{index}{{border-left-color:{colour}}}.group.lab{index}{{border-color:{colour}}}")
    return out


# The conversation follows its newest part: each time the page is drawn, its last part (the element that waits, or the
# newest output) is brought into the window, its end at the bottom, or its start at the top when it is taller than the
# window. The page is drawn again only when the conversation changed, and a position the user had scrolled to is
# not kept.
_FOLLOW = ('history.scrollRestoration="manual";'
           'const last=document.getElementById("talk")&&document.getElementById("talk").lastElementChild;'
           'const show=()=>{if(last)last.scrollIntoView({block:last.offsetHeight>innerHeight?"start":"end"})};'
           'show();addEventListener("load",show)')


def render(session: Session, tab: str, message: str = "", nonce: str = "") -> str:
    project = session.project
    tabs = "".join(f'<a href="{path}"{" class=\"on\"" if tab == name else ""}>{title}</a>'
                   for name, path, title in (("talk", "/", "Conversation"), ("check", "/check", "Rules and exceptions"), ("workflows", "/workflows", "Workflows"))
                   + (("log", "/log", "Log"),) + ((("values", "/values", "State"),) if project.ui.get("state_tab") else ()))
    if tab == "talk":
        body = f'<div class="cols"><div class="talk" id="talk">{_talk(session)}</div><aside class="side" id="side">{_side(session)}</aside></div>'
    else:
        body = {"check": _check, "workflows": _workflows, "values": _values, "log": _log}[tab](session)
    note = f'<div class="message">{_e(message)}</div>' if message else ""
    note = (f'<div class="setup-note">{_e(project.note)}</div>' if project.note else "") + note
    # Every tab asks the program, every 1.5 seconds, whether it still answers; when it does not (stopped, or the
    # connection is gone), a bar at the top says so, since a link or a button would then do nothing. On the
    # conversation's tab the same answer brings the side up to date and draws the page again when it changed.
    talk = ('const side=document.getElementById("side");if(side)side.innerHTML=s.side;'
            'const busy=document.activeElement&&["TEXTAREA","INPUT"].includes(document.activeElement.tagName)&&document.activeElement.value;'
            'if(s.version!==seen&&!busy)location.reload();') if tab == "talk" else ""
    script = (f'<script nonce="{nonce}">let seen={session.page.version};const gone=document.getElementById("gone");'
              f'setInterval(async()=>{{try{{const r=await fetch("/state",{{signal:AbortSignal.timeout(4000)}});'
              f'gone.hidden=r.ok;if(!r.ok)return;const s=await r.json();{talk}}}catch(e){{gone.hidden=false}}}},1500);'
              f'{_FOLLOW if tab == "talk" else ""}</script>')
    gone = ('<div id="gone" class="message bad gone" hidden>The program does not answer: it was stopped, or the connection to it is lost. '
            'Nothing on this page works until it answers again. What is displayed is from before.</div>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_e(project.folder.name)}</title><style>{_CSS}{_label_css(project.ui)}</style></head><body{" class=\"fixed\"" if tab == "talk" else ""}><div class="wrap">'
            f'<header><h1>{_e(project.folder.name)}</h1><span class="sub">setup <span class="mono">{_e(project.setup)}</span></span></header>'
            f'<nav>{tabs}</nav>{gone}{note}{body}</div>{script}</body></html>')


# ---- the server ------------------------------------------------------------------------------------------------------

def _handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *arguments: Any) -> None:    # the access log of C4 is the record; this one is not kept
            return

        def _send(self, status: int, body: str, kind: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None,
                  nonce: str = "") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
                                                        f"connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _cookie_ok(self) -> bool:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return "guard_token" in cookie and hmac.compare_digest(cookie["guard_token"].value, session.token)

        def _page(self, tab: str, message: str = "") -> None:
            nonce = secrets.token_urlsafe(12)
            self._send(200, render(session, tab, message, nonce), nonce=nonce)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            given = parse_qs(url.query).get("token", [""])[0]
            if given:
                if not hmac.compare_digest(given, session.token):
                    return self._send(403, "The token in the address is not this session's.", "text/plain; charset=utf-8")
                return self._send(303, "", extra={"Location": url.path or "/", "Set-Cookie": f"guard_token={session.token}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._cookie_ok():
                return self._send(403, "Open the address printed when the project started; it includes a token.", "text/plain; charset=utf-8")
            if url.path == "/state":
                return self._send(200, json.dumps({"version": session.page.version, "side": _side(session)}), "application/json")
            tab = {"/": "talk", "/check": "check", "/workflows": "workflows", "/log": "log",
                   **({"/values": "values"} if session.project.ui.get("state_tab") else {})}.get(url.path)
            if tab is None:
                return self._send(404, "There is no such page.", "text/plain; charset=utf-8")
            self._page(tab)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 1_000_000:
                return self._send(413, "Too much was sent.", "text/plain; charset=utf-8")
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
            if not self._cookie_ok() or not hmac.compare_digest(form.get("token", [""])[0], session.token):
                return self._send(403, "The token is missing or is not this session's.", "text/plain; charset=utf-8")
            path = urlsplit(self.path).path
            if path == "/answer":
                element = form.get("element", [""])[0]
                if element.isdigit():
                    session.page.answer(int(element), form, closed=form.get("act", [""])[0] == "close")
                return self._send(303, "", extra={"Location": "/"})
            if path == "/start":
                message = session.start(form.get("workflow", [""])[0])
                return self._page("talk", message) if message else self._send(303, "", extra={"Location": "/"})
            if path == "/stop":
                message = session.stop(form.get("workflow", [""])[0])
                return self._page("talk", message) if message else self._send(303, "", extra={"Location": "/"})
            if path == "/reset":
                if not session.project.ui.get("reset_control") or form.get("sure", [""])[0] != "yes":
                    return self._send(404, "There is no such page.", "text/plain; charset=utf-8")
                return self._page("values", session.reset())
            self._send(404, "There is no such page.", "text/plain; charset=utf-8")
    return Handler
