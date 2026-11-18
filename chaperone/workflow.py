"""Read a project's workflow files and refuse what the specification (2.12) does not permit.

A workflow is a finite state machine with no expressions: an argument is a reference or a value written in the file.
Every workflow names its states, its first state and the state an error continues in: a file of `workflow` and
`steps` alone is refused.

Checked when the project is loaded: every key, every state a `next` or an `on_error` names, every call and its
argument names, every store, the form of every reference, and `max_states` where a route can enter a state twice.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chaperone.errors import ConfigIssue
from chaperone.yaml_lines import load_yaml

from .values import Problem, parse_path

FILE_KEYS = ("workflow", "description", "start", "states", "on_error", "max_states")
STATE_KEYS = ("steps", "next", "end", "on_error")
STEP_KEYS = ("call", "with", "save_as")
RESERVED = {key.lower() for key in FILE_KEYS + STATE_KEYS + STEP_KEYS + ("switch", "goto", "else", "default")}
OPERATIONS = {                                              # engine operation -> (required arguments, optional ones)
    "get": (("value",), ("path",)), "pick": (("value", "fields"), ()), "equals": (("a", "b"), ()),
    "labels_of": (("value",), ()), "store.put": (("store", "value"), ()), "store.get": (("store",), ()),
    "store.add": (("store", "items"), ()), "store.remove": (("store", "items"), ()),
}
REFERENCE = re.compile(r"\{(vars|error)((?:\.[A-Za-z_]\w*|\[\d+\])*)\}")


@dataclass
class Workflow:
    name: str
    file: Path
    start: str
    states: dict[str, dict[str, Any]]
    on_error: str                                          # the state an error continues in
    max_states: int | None
    description: str = ""                                   # one line for whoever reads the page; no check reads it


def reference(text: Any) -> tuple[str, tuple[str | int, ...]] | None:
    """("vars" or "error", path) when `text` is a reference, None when it is a value written in the file. A text
    that contains a reference and anything else is refused (Problem)."""
    if not isinstance(text, str) or ("{vars" not in text and "{error" not in text):
        return None
    whole = REFERENCE.fullmatch(text)
    if whole is None:
        raise Problem("bad_reference", f"`{text}` mixes a reference with other text, or is not a reference: a "
                                       f"reference is the whole of a quoted text, \"{{vars.name.field}}\" or \"{{error}}\"")
    return whole.group(1), parse_path(whole.group(2).lstrip("."))


def references(template: Any) -> list[tuple[str, tuple[str | int, ...]]]:
    if isinstance(template, dict):
        return [r for inner in template.values() for r in references(inner)]
    if isinstance(template, list):
        return [r for inner in template for r in references(inner)]
    found = reference(template)
    return [found] if found else []


def successors(state: dict[str, Any]) -> list[str]:
    """Every state that may follow `state`: by its `next`, and by its own `on_error`."""
    own = state.get("on_error")
    following = state.get("next")
    out = [following] if isinstance(following, str) else []
    if isinstance(following, dict):
        out += list((following.get("goto") or {}).values()) + ([following["else"]] if "else" in following else [])
    return out + ([own] if isinstance(own, str) else [])


def load_workflows(project: Any, bad: Any) -> dict[str, Workflow]:
    """Every file of the project's folder `workflows/`. Problems are added to `bad` (chaperone.load's collector)."""
    out: dict[str, Workflow] = {}
    folder = project.folder / "workflows"
    for file in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        data = load_yaml(file)
        before = len(bad.issues)
        workflow = _read(project, bad, file, data)
        if workflow is not None and len(bad.issues) == before:
            out[workflow.name] = workflow
    return out


def _read(project: Any, bad: Any, file: Path, data: Any) -> Workflow | None:
    if isinstance(data, dict) and "states" not in data and "steps" in data:
        bad.add(file, data, "steps", "a workflow: `steps` are written inside a named state. Write `start: MAIN`, `on_error: FAILED` and "
                                     "`states: {MAIN: {steps: [...], end: true}, FAILED: {end: true}}`")
        return None
    if not bad.keys(file, data, "a workflow", FILE_KEYS, ("workflow", "start", "states", "on_error")):
        return None
    states, start, on_error, limit = data["states"], data["start"], data["on_error"], data.get("max_states")
    if not isinstance(states, dict):
        bad.add(file, data, "states", "a workflow: `states` is a mapping")
        return None
    if not isinstance(on_error, str):
        bad.add(file, data, "on_error", "a workflow: `on_error` names one state; a map by error type is not accepted")
        return None
    if start not in states:
        bad.add(file, data, "start", f"a workflow: start names {start}, which is not a state")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        bad.add(file, data, "max_states", "a workflow: max_states is a whole number above zero")
    name = data.get("workflow")
    if not isinstance(data.get("description", ""), str):
        bad.add(file, data, "description", "a workflow: `description` is a text")
    if name != file.stem:
        bad.add(file, data, "workflow", f"a workflow: its name is {name} and its file is {file.name}; the two are the same")
    saved = {step.get("save_as") for state in states.values() if isinstance(state, dict)
             for step in state.get("steps") or [] if isinstance(step, dict)} - {None}
    if on_error is not None and on_error not in states:
        bad.add(file, data, "on_error", f"on_error names {on_error}, which is not a state")
    for state_name, state in states.items():
        where = f"{name}.{state_name}"
        if str(state_name).lower() in RESERVED:
            bad.add(file, states, state_name, f"{where}: a state may not have the name of a key")
        if not bad.keys(file, state, where, STATE_KEYS):
            continue
        if ("next" in state) == bool(state.get("end")):
            bad.add(file, states, state_name, f"{where}: a state has `next` or `end: true`, one of the two")
        _next(bad, file, state, where, states, saved)
        target = state.get("on_error")
        if target is not None and not isinstance(target, str):
            bad.add(file, state, "on_error", f"{where}: on_error names one state; a map by error type is not accepted")
        elif target is not None and target not in states:
            bad.add(file, state, "on_error", f"{where}: on_error names {target}, which is not a state")
        for step in state.get("steps") or []:
            _step(project, bad, file, step, where, saved)
    if limit is None and _has_cycle(states, on_error, start):
        bad.add(file, data, "start", f"{name}: a route can enter a state twice, and max_states is not given")
    return Workflow(name=name, file=file, start=start, states=states, on_error=on_error, max_states=limit,
                    description=data.get("description") if isinstance(data.get("description"), str) else "")


def _next(bad: Any, file: Path, state: dict[str, Any], where: str, states: dict[str, Any], saved: set[Any]) -> None:
    following = state.get("next")
    if following is None:
        return
    if isinstance(following, str):
        if following not in states:
            bad.add(file, state, "next", f"{where}: next names {following}, which is not a state")
        return
    if not bad.keys(file, following, f"{where}, next", ("switch", "goto", "else"), ("switch", "goto")):
        return
    if "switch" not in following or "goto" not in following:
        return
    _references(bad, file, following, "switch", following["switch"], where, saved)
    if not isinstance(following["goto"], dict) or not following["goto"]:
        bad.add(file, following, "goto", f"{where}: `goto` maps each key to a state")
        return
    for key, target in list(following["goto"].items()) + ([("else", following["else"])] if "else" in following else []):
        if isinstance(key, (dict, list)) or key is None:
            bad.add(file, following, "goto", f"{where}: a key of `goto` is a text, a number, true or false")
        if target not in states:
            bad.add(file, following, "goto", f"{where}: {key} goes to {target}, which is not a state")


def _references(bad: Any, file: Path, node: Any, key: Any, template: Any, where: str, saved: set[Any]) -> None:
    try:
        for root, path in references(template):
            if root == "vars" and (not path or path[0] not in saved):
                bad.add(file, node, key, f"{where}: a reference names {path[0] if path else 'no name'}, which no "
                                         f"step of the workflow saves")
    except Problem as problem:
        bad.add(file, node, key, f"{where}: {problem}")


def _step(project: Any, bad: Any, file: Path, step: Any, where: str, saved: set[Any]) -> None:
    if not bad.keys(file, step, f"{where}, a step", STEP_KEYS, ("call",)):
        return
    call, given = step["call"], step.get("with") or {}
    what = f"{where}, {call}"
    if not isinstance(given, dict):
        bad.add(file, step, "with", f"{what}: `with` gives the arguments by name")
        return
    _references(bad, file, step, "with", given, what, saved)
    if call in OPERATIONS:
        required, optional = OPERATIONS[call]
        if call.startswith("store.") and given.get("store") not in project.stores:
            bad.add(file, step, "with", f"{what}: the store {given.get('store')} is not declared in system.yaml")
    elif call in project.declarations:
        declaration = project.declarations[call]
        parameters = list(inspect.signature(declaration.function).parameters.values())[int(declaration.takes_ctx):]
        required = tuple(p.name for p in parameters if p.default is inspect.Parameter.empty)
        optional = tuple(p.name for p in parameters if p.default is not inspect.Parameter.empty)
    else:
        bad.add(file, step, "call", f"{what}: this is not a module of the project, nor an engine operation that is "
                                    f"written as a step")
        return
    for argument in given:
        if argument not in required + optional:
            bad.add(file, step, "with", f"{what}: it has no argument {argument}; its arguments are "
                                        f"{', '.join(required + optional) or 'none'}")
    for argument in required:
        if argument not in given:
            bad.add(file, step, "with" if "with" in step else "call", f"{what}: the argument {argument} is not given")


def _has_cycle(states: dict[str, Any], on_error: str | None, start: str) -> bool:
    """Whether a route can enter a state twice. An error raised in the state its own error route names ends the run
    (chaperone.engine), so that route is no cycle; a state with no step and no branch raises nothing."""
    seen: set[str] = set()
    stack: set[str] = set()

    def visit(name: str) -> bool:
        if name in stack:
            return True
        if name in seen or name not in states or not isinstance(states[name], dict):
            return False
        seen.add(name)
        stack.add(name)
        state = states[name]
        raises = bool(state.get("steps")) or isinstance(state.get("next"), dict)     # else no error can arise in it
        own = state.get("on_error") if isinstance(state.get("on_error"), str) else on_error
        routes = [own] if own and own != name else []
        nexts = [n for n in successors({"next": state.get("next")})]
        found = any(visit(n) for n in nexts + (routes if raises else []))
        stack.discard(name)
        return found
    return visit(start)
