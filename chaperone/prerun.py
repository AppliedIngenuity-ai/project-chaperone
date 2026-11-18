"""The pre-run check and its acceptance (specification, section 9).

The call check of section 4 is evaluated before a run, over the workflow files, under one setup. Labels are
propagated by P1 to P6 through every state of every workflow until none is added: what is known of each saved name
and of each stored name only accumulates. A declared request is assumed applied, to the whole result or to the field
its declaration names. The result is the calls that would be refused, each with a route to it; for each call that is
not refused only because a request is assumed applied, the modules whose requests it depends on; and the references
to a name that some route does not save first.

What is known of a part before a run is two sets of labels: those it carries on every run (`must`) and those it may
carry (`may`). A rule forbids a call if a part may carry its label; an exception permits it only if every such part
must carry the exception's label. So the check answers "can this happen", and over-reports where it cannot tell.

The user accepts a result by recording it; a result that lists a refused call cannot be accepted, and a run starts
only if the current result equals an accepted one.
"""

from __future__ import annotations

import hashlib
import os
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Any, Mapping

from .engine import Run, covering
from chaperone.errors import ConfigError, ConfigIssue
from chaperone.yaml_lines import load_yaml

from .load import Project, selects
from .store import Store
from .values import Problem, parse_path
from .workflow import Workflow, reference, references, successors

Kind = tuple[frozenset, frozenset]                           # (must, may): what is known of one scalar part
Known = dict[Any, frozenset]                                 # a top field, or None for any other part -> its kinds
PLAIN: Kind = (frozenset(), frozenset())                     # a part written in a file: it carries no label


class NotAccepted(Exception):
    """A run was asked for and the current result of the pre-run check is not an accepted one."""


@dataclass(frozen=True)
class Call:
    workflow: str
    state: str
    call: str                                                # a module, or `decide`

    def __str__(self) -> str:
        return f"{self.workflow}.{self.state}: {self.call}"


@dataclass
class Result:
    project: str
    setup: str
    checked: int = 0                                         # calls checked: of modules and of branches
    refused: list[dict[str, Any]] = field(default_factory=list)      # {call, rule, label, route}
    permits: dict[str, list[str]] = field(default_factory=dict)      # exception -> the calls it permits
    depends: dict[str, list[str]] = field(default_factory=dict)      # call -> modules whose requests it depends on
    unsaved: list[dict[str, str]] = field(default_factory=list)      # {call, name}
    facts: dict[str, Any] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)              # setup lines with `*` that match no module: a warning,
                                                                    # not in the fingerprint (they change no call)
    rules: list[dict[str, Any]] = field(default_factory=list)       # each rule as written: {id, never}
    exceptions: list[dict[str, Any]] = field(default_factory=list)  # each exception as written: {id, rule, except}
    rests: dict[str, dict[str, list[str]]] = field(default_factory=dict)   # rule -> call -> the modules of `depends`, by the
                                                                    # rule that would refuse the call (not in the fingerprint: `depends` is)

    @property
    def idle(self) -> list[str]:
        return [name for name, calls in self.permits.items() if not calls]

    def fingerprint(self) -> str:
        """The same for two results that say the same; what an acceptance records. The rules and the exceptions as
        written are in it: the user accepts them as displayed, so a changed one is accepted again even when it permits
        the same calls. A comment, or the order of keys, changes nothing."""
        said = {"project": self.project, "setup": self.setup, "refused": self.refused, "permits": self.permits,
                "depends": self.depends, "unsaved": self.unsaved, "facts": self.facts, "rules": self.rules,
                "exceptions": self.exceptions}
        return hashlib.sha256(json.dumps(said, sort_keys=True).encode("utf-8")).hexdigest()


# ---- what is known of a value before a run ---------------------------------------------------------------------------

def _all(known: Known) -> frozenset:
    out: set[Kind] = set()
    for kinds in known.values():
        out |= kinds
    return frozenset(out)


def _join(a: Known, b: Known) -> Known:
    return {key: a.get(key, frozenset()) | b.get(key, frozenset()) for key in set(a) | set(b)}


def _part(known: Known, path: tuple[Any, ...]) -> Known:
    """What is known of the part at `path`: of its top field if the value is known by field, else of any part."""
    if not path:
        return known
    by_field = any(key is not None for key in known)
    if isinstance(path[0], str) and by_field:
        return {None: known.get(path[0], frozenset()) | known.get(None, frozenset())}
    return {None: _all(known)}


class _Check:
    def __init__(self, project: Project, without: str | None = None) -> None:
        """`without`: a module whose declared requests are not assumed applied (to find what depends on them)."""
        self.project, self.without = project, without
        self.spreading = {name for name, spec in project.labels.items() if spec["spreads"]}
        self.saved: dict[str, dict[str, Known]] = {name: {} for name in project.workflows}
        self.errors: dict[tuple[str, str], Known] = {}      # (workflow, a state an error can route to) -> the errors
        self.stores: dict[str, Known] = {name: self._written(first) for name, first in project.stores.items()}
        self.verdicts: dict[Call, list[tuple[str, str | None, str]]] = {}     # call -> (rule, exception, label)
        self.changed = True
        while self.changed:
            self.changed = False
            self.verdicts = {}                               # of this pass: what is known only grows, so the last pass is final
            for workflow in project.workflows.values():
                for state_name, state in workflow.states.items():
                    self._state(workflow, state_name, state)

    # ---- one state -------------------------------------------------------------------------------------------------
    def _state(self, workflow: Workflow, state_name: str, state: dict[str, Any]) -> None:
        for step in state.get("steps") or []:
            call, given = step["call"], step.get("with") or {}
            arguments = {key: self._argument(workflow, state_name, template) for key, template in given.items()}
            may = self._may_spread(arguments.values())
            if call in self.project.declarations:
                facts, declaration = self.project.facts[call], self.project.declarations[call]
                self._check(Call(workflow.name, state_name, call), call, arguments)
                base: Kind = (frozenset(facts.adds), frozenset(facts.adds) | may)
                raised = base
                self._check(Call(workflow.name, state_name, "log"), "log", {"error": {None: frozenset({raised})}})   # what the module writes in an error
                applied = call != self.without

                def after(kind: Kind, requests: Mapping[str, bool]) -> Kind:
                    must, can = set(kind[0]), set(kind[1])
                    for label, wanted in requests.items() if applied else ():
                        (must.add(label), can.add(label)) if wanted else (must.discard(label), can.discard(label))
                    return frozenset(must), frozenset(can)
                whole = after(base, declaration.requests)
                out: Known = ({part: frozenset({after(whole, changes)}) for part, changes in declaration.part_requests.items()}
                              if declaration.part_requests else {None: frozenset({whole})})
            else:
                out, raised = self._operation(call, given, arguments), (frozenset(), may)
            self._error(workflow, state, raised)
            if "save_as" in step:
                self._grow(self.saved[workflow.name], step["save_as"], out)
        following = state.get("next")
        if isinstance(following, dict):
            tested = {"value": self._argument(workflow, state_name, following["switch"])}
            self._check(Call(workflow.name, state_name, "decide"), "decide", tested)
            self._error(workflow, state, (frozenset(), self._may_spread(tested.values())))

    def _operation(self, call: str, given: Mapping[str, Any], arguments: Mapping[str, Known]) -> Known:
        """P3 and P5, on what is known."""
        if call == "get":
            path = given.get("path")
            return _part(arguments["value"], reference_path(path)) if path is not None else arguments["value"]
        if call == "pick":
            return {name: _part(arguments["value"], (name,))[None] for name in given["fields"]}
        if call == "equals":
            return {None: frozenset({(frozenset(), self._may_spread(arguments.values()))})}
        if call == "labels_of":
            return {None: frozenset({PLAIN})}
        store = given["store"]
        kept = self.stores[store]
        if call == "store.get":
            return kept
        if call == "store.put":
            self._grow(self.stores, store, {None: _all(arguments["value"])})
            return {None: frozenset({PLAIN})}
        items = {None: _all(arguments["items"])}
        if call == "store.add":
            self._grow(self.stores, store, items)
            return {"added": items[None], "present": _all(self.stores[store])}
        return {"removed": _all(kept), "absent": items[None]}

    # ---- the check of one call --------------------------------------------------------------------------------------
    def _check(self, where: Call, name: str, arguments: Mapping[str, Known]) -> None:
        kinds = [(argument, kind) for argument, known in arguments.items() for kind in _all(known)]
        found: list[tuple[str, tuple[str, ...] | None, str]] = []
        for rule in self.project.rules:
            never = rule["never"]
            if not selects(self.project, never["call"], name):
                continue
            forbidding, label = [kind for _, kind in kinds], ""
            if "carrying" in never:
                (label, wanted), = never["carrying"].items()
                forbidding = [kind for _, kind in kinds if (label in kind[1] if wanted else label not in kind[0])]
                if not forbidding:
                    continue
            always = lambda kind, needed, value: needed in kind[0] if value else needed not in kind[1]   # on every run

            def holds(argument: str, needed: str, value: bool) -> bool:
                kinds = _all(arguments[argument]) if argument in arguments else frozenset()   # none: not passed, or written empty
                return bool(kinds) and all(always(kind, needed, value) for kind in kinds)
            permit = covering(self.project, rule, name, forbidding, always, holds) or None
            found.append((rule["id"], permit, label))
        kept = self.verdicts.setdefault(where, [])           # two steps of one state may call one module: every verdict is kept
        kept += [verdict for verdict in found if verdict not in kept]

    def _error(self, workflow: Workflow, state: dict[str, Any], raised: Kind) -> None:
        """P6: an error of a call is a value; the state it continues in may reference it as `{error}`. That state
        (2.12, as `Run._route` reads it) is the one the state's own `on_error` names, else the workflow's."""
        self._grow(self.errors, (workflow.name, state.get("on_error") or workflow.on_error), {None: frozenset({raised})})

    # ---- arguments -------------------------------------------------------------------------------------------------
    def _argument(self, workflow: Workflow, state_name: str, template: Any) -> Known:
        """What is known of an argument: a reference stands for the referenced value; what is written carries none."""
        if isinstance(template, dict):
            return {key: _all(self._argument(workflow, state_name, inner)) for key, inner in template.items()}
        if isinstance(template, list):
            out: set[Kind] = set()
            for inner in template:
                out |= _all(self._argument(workflow, state_name, inner))
            return {None: frozenset(out)}
        found = reference(template)
        if found is None:
            return {None: frozenset({PLAIN})}
        root, path = found
        if root == "error":
            return _part(self.errors.get((workflow.name, state_name), {}), path)    # `{error}` is read in the state routed to
        return _part(self.saved[workflow.name].get(path[0], {}), path[1:])

    @staticmethod
    def _written(value: Any) -> Known:
        has_scalar = value is None or isinstance(value, (str, int, float, bool)) or bool(value)
        return {None: frozenset({PLAIN})} if has_scalar else {}

    def _may_spread(self, arguments: Any) -> frozenset:
        out: set[str] = set()
        for known in arguments:
            for _, may in _all(known):
                out |= may
        return frozenset(out & self.spreading)

    def _grow(self, into: dict[Any, Known], key: Any, more: Known) -> None:
        joined = _join(into.get(key, {}), more)
        if joined != into.get(key, {}):
            into[key] = joined
            self.changed = True


def reference_path(written: Any) -> tuple[Any, ...]:
    return parse_path(str(written))


# ---- routes, and names a route may not have saved --------------------------------------------------------------------

def _edges(workflow: Workflow, state_name: str) -> list[str]:
    state = workflow.states[state_name]
    raises = bool(state.get("steps")) or isinstance(state.get("next"), dict)
    shared = [workflow.on_error] if workflow.on_error and raises and not state.get("on_error") else []
    return [n for n in successors(state) + shared if n != state_name or n in successors({"next": state.get("next")})]


def route_to(workflow: Workflow, target: str) -> list[str] | None:
    """A shortest sequence of states from `start` to `target`; None when no route reaches it."""
    routes, queue = {workflow.start: [workflow.start]}, [workflow.start]
    while queue:
        here = queue.pop(0)
        if here == target:
            return routes[here]
        for following in _edges(workflow, here):
            if following not in routes:
                routes[following] = routes[here] + [following]
                queue.append(following)
    return None


def _unsaved(workflow: Workflow) -> list[dict[str, str]]:
    """References to a name that some route does not save before the reference. For each state, the names saved on
    every route into it; an error may leave a state before any of its steps has saved."""
    every = {step["save_as"] for state in workflow.states.values() for step in state.get("steps") or [] if "save_as" in step}
    entering: dict[str, set[str]] = {name: set(every) for name in workflow.states}
    entering[workflow.start] = set()
    changed = True
    while changed:
        changed = False
        for name, state in workflow.states.items():
            leaving = entering[name] | {s["save_as"] for s in state.get("steps") or [] if "save_as" in s}
            normal = successors({"next": state.get("next")})
            for following in _edges(workflow, name):
                arriving = leaving if following in normal else entering[name]
                if not entering[following] <= arriving and following != workflow.start:
                    entering[following] &= arriving
                    changed = True
    out = []
    for name, state in workflow.states.items():
        if route_to(workflow, name) is None:
            continue
        have = set(entering[name])
        templates = [(s["call"], s.get("with") or {}, s.get("save_as")) for s in state.get("steps") or []]
        if isinstance(state.get("next"), dict):
            templates.append(("decide", state["next"]["switch"], None))
        for call, template, saves in templates:
            for root, path in references(template):
                if root == "vars" and path[0] not in have:
                    out.append({"call": str(Call(workflow.name, name, call)), "name": str(path[0])})
            if saves:
                have.add(saves)
    return out


# ---- the result, its acceptance, a run --------------------------------------------------------------------------------

def check(project: Project) -> Result:
    full = _Check(project)
    result = Result(project=project.folder.name, setup=project.setup, checked=len(full.verdicts),
                    permits={exception["id"]: [] for exception in project.exceptions},
                    unmatched=list(project.unmatched_patterns),
                    rules=[{"id": r["id"], "never": r["never"]} for r in project.rules],
                    exceptions=[{"id": e["id"], "rule": e["rule"], "except": e["except"]} for e in project.exceptions],
                    facts={name: {"tool": f.tool, "values": dict(sorted(f.values.items())), "adds": sorted(f.adds)}
                           for name, f in sorted(project.facts.items())})
    for where, found in sorted(full.verdicts.items(), key=lambda item: str(item[0])):
        for rule, permit, label in found:
            if permit is None:
                route = route_to(project.workflows[where.workflow], where.state)
                result.refused.append({"call": str(where), "rule": rule, "label": label, "route": route})
            else:
                for exception in permit:                     # each exception that covers a part of the call is needed by it
                    if str(where) not in result.permits[exception]:
                        result.permits[exception].append(str(where))
    refused_now = {(r["call"], r["rule"]) for r in result.refused}
    requesting = [name for name, d in project.declarations.items() if d.requests or d.part_requests]
    for module in sorted(requesting):                        # what would be refused if this module's requests were not applied
        strict = _Check(project, without=module)
        for where, found in strict.verdicts.items():
            would = [rule for rule, permit, _ in found if permit is None and (str(where), rule) not in refused_now]
            if would:
                result.depends.setdefault(str(where), []).append(module)
            for rule in would:
                result.rests.setdefault(rule, {}).setdefault(str(where), []).append(module)
    result.depends = dict(sorted(result.depends.items()))
    for workflow in project.workflows.values():
        result.unsaved += _unsaved(workflow)
    return result


def accepted_file(state: str | FilePath) -> FilePath:
    return FilePath(state) / "accepted.json"


def accepted(state: str | FilePath) -> list[dict[str, Any]]:
    file = accepted_file(state)
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else []


def accept(result: Result, state: str | FilePath, by: str, rule_by_rule: bool = False) -> dict[str, Any]:
    """Record that `by` accepts `result`: every rule and every exception in it, as written. `rule_by_rule` records
    how: asked at the terminal, one rule at a time, or (false) by the command `accept`, with no question. A result
    that lists a refused call which no acknowledged risk of the state folder names cannot be accepted (Problem)."""
    open_ = unacknowledged(result, state)
    if open_:
        raise Problem("not_acceptable", f"the result lists {len(open_)} refused call(s) that no exception permits and no "
                                        f"acknowledged risk names; they are answered at the terminal, when `python -m chaperone run` starts")
    entry = {"result": result.fingerprint(), "project": result.project, "setup": result.setup, "by": by,
             "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "rule_by_rule": rule_by_rule,
             "exceptions": [e["id"] for e in result.exceptions],
             "acknowledged": sorted({f"{r['rule']}: {r['call']}" for r in result.refused})}
    kept = [e for e in accepted(state) if e["result"] != entry["result"]] + [entry]
    file = accepted_file(state)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    return entry


def how_accepted(acceptance: Mapping[str, Any]) -> str:
    return ("rule by rule, at the terminal" if acceptance.get("rule_by_rule") else "by the command accept, with no question asked")


def acceptance_of(result: Result, state: str | FilePath) -> dict[str, Any] | None:
    """The acceptance on record of `result`, or None. A result that lists a refused call is accepted only while each
    such call is an acknowledged risk of the state folder: a line deleted there ends the acceptance."""
    if unacknowledged(result, state):
        return None
    return next((e for e in accepted(state) if e["result"] == result.fingerprint()), None)


# ---- acknowledged risks: possible violations accepted with no exception; a run refuses their calls ---------------------

def acknowledged_file(state: str | FilePath) -> FilePath:
    return FilePath(state) / "acknowledged_risks.yaml"


def acknowledged(state: str | FilePath) -> dict[tuple[str, str], dict[str, Any]]:
    """The acknowledged risks of the state folder, by (rule, call): calls that a rule forbids and no exception
    permits, which the user accepted at a start with no exception. Nothing permits such a call: a run refuses it
    when it is made. A file that is not there holds none; one of another form is refused (ConfigError)."""
    file = acknowledged_file(state)
    if not file.is_file():
        return {}
    data = load_yaml(file, layered=False)
    entries = data.get("acknowledged_risks") if isinstance(data, dict) else None
    entries = [] if isinstance(data, dict) and entries is None else entries
    if not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get("rule"), str)
                                            or not isinstance(e.get("call"), str) for e in entries):
        raise ConfigError([ConfigIssue(str(file), None, 'expected `acknowledged_risks:`, a list of lines '
                                                         '{rule: RULE, call: "workflow.STATE: module"}')])
    return {(e["rule"], e["call"]): dict(e) for e in entries}


def unacknowledged(result: Result, state: str | FilePath) -> list[dict[str, Any]]:
    """The refused calls of `result` that no acknowledged risk names: while there is one, nothing runs."""
    known = acknowledged(state)
    return [r for r in result.refused if (r["rule"], r["call"]) not in known]


def _write_acknowledged(state: str | FilePath, entries: list[dict[str, Any]]) -> None:
    file = acknowledged_file(state)
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Acknowledged risks: calls that a rule forbids and no exception permits, accepted when the program started",
             "# (python -m chaperone run) with no exception. Nothing here permits a call: a run refuses each of these calls when",
             "# it is made. To be asked again at the next start, delete a line.", "acknowledged_risks:"]
    lines += [f'  - {{rule: {e["rule"]}, call: "{e["call"]}", by: "{e.get("by", "")}", at: "{e.get("at", "")}"}}' for e in entries]
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- the report at the start, rule by rule, and its acceptance at the terminal ------------------------------------------

def _flow(value: Any) -> str:
    """A value as the configuration files write it, on one line: {call: [decide], carrying: {UNTRUSTED: true}}."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {_flow(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(item) for item in value) + "]"
    return {True: "true", False: "false"}.get(value, str(value)) if isinstance(value, bool) else str(value)


def _place(entry: Mapping[str, Any]) -> str:
    return f"{os.path.normpath(entry['file'])}:{entry['line']}"


def unmatched_lines(project: Project) -> list[tuple[str, str]]:
    """Each line of the setup's `modules` whose pattern matches no module: its file and line, and the line as written."""
    file = os.path.normpath(project.folder / "setups" / f"{project.setup}.yaml")
    out = []
    for pattern in project.unmatched_patterns:
        line = getattr(project.lines, "key_lines", {}).get(pattern)
        out.append((f"{file}:{line}" if line else file, f"{pattern}: {_flow(project.lines[pattern])}"))
    return out


def user_exceptions_file(state: str | FilePath) -> FilePath:
    return FilePath(state) / "user_exceptions.yaml"


def _write_user_exceptions(project: Project, state: str | FilePath) -> None:
    """The file of the user-approved exceptions, written whole from those in force, as exceptions.yaml is written."""
    file = user_exceptions_file(state)
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Exceptions the user approved when the program started (python -m chaperone run). They are in force as the",
             "# exceptions of the project's exceptions.yaml are, and are kept here, outside the configuration.",
             "# To withdraw one, delete its line; the next start asks again.", "exceptions:"]
    for number, e in enumerate((e for e in project.exceptions if e.get("user")), len(lines) + 1):
        lines.append(f"  - {{id: {e['id']}, rule: {e['rule']}, except: {_flow(e['except'])}}}")
        e["file"], e["line"] = str(file), number
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


SHOWN_STATES = 5                                             # the states listed under an exception at the start; the rest are in More details


def _states(calls: list[str], setup: str) -> str:
    """Where an exception permits a call, as the states of the workflows (`workflow.STATE`): the first few, by name."""
    states = sorted({call.split(": ", 1)[0] for call in calls})
    if not states:
        return f"under the setup {setup}, no call needs this exception"
    more = ", ..." if len(states) > SHOWN_STATES else ""
    return f"permits calls in {len(states)} state{'' if len(states) == 1 else 's'}: {', '.join(states[:SHOWN_STATES])}{more}"


def by_rule(project: Project, result: Result, known: Any = ()) -> list[dict[str, Any]]:
    """For each rule, in the order written: its lines for the terminal; the exceptions to it, those of the
    configuration apart from those the user approved at a start; the calls each permits and the calls that rest on
    label requests (`details`); the calls the rule forbids that no exception permits (`refused`): those among them
    that `known`, the acknowledged risks, names (`acknowledged`) and the others (`open`), and for the others the
    exception that would permit them (`proposed`): it names exactly the modules called."""
    blocks, ids = [], {e["id"] for e in project.exceptions}
    for number, rule in enumerate(project.rules, 1):
        exceptions = [e for e in project.exceptions if e["rule"] == rule["id"]]
        refused = sorted({r["call"] for r in result.refused if r["rule"] == rule["id"]})
        lines = [f"RULE {number} of {len(project.rules)}: {rule['id']}  ({_place(rule)})", f"  never: {_flow(rule['never'])}"]
        for title, some in (("Exceptions written in the configuration", [e for e in exceptions if not e.get("user")]),
                            ("Exceptions approved at an earlier start, not in the configuration", [e for e in exceptions if e.get("user")])):
            if some:
                lines.append(f"  {title}; each is accepted before a run:")
                width = max(len(e["id"]) for e in some)
                for e in some:
                    lines.append(f"    {e['id']:<{width}}  except: {_flow(e['except'])}  ({_place(e)})")
                    lines.append("      " + _states(result.permits.get(e["id"], []), result.setup))
        proposed = None
        acknowledged_ = [call for call in refused if (rule["id"], call) in known]
        open_ = [call for call in refused if (rule["id"], call) not in known]
        if open_:
            name = next(n for n in [f"USER_{rule['id']}_EX"] + [f"USER_{rule['id']}_{i}_EX" for i in range(2, 1000)] if n not in ids)
            proposed = {"id": name, "rule": rule["id"], "except": {"call": sorted({call.split(": ", 1)[1] for call in open_})}, "user": True,
                        "file": "", "line": None}
            these = "this call" if len(open_) == 1 else "these calls"
            lines.append(f"  POSSIBLE VIOLATION: no exception permits {these}, in the configuration or approved at a start:")
            lines += [f"    {call}" for call in open_]
        if acknowledged_:
            lines.append("  ACKNOWLEDGED RISK: no exception permits these calls; a run refuses each when it is made:")
            lines += [f"    {call}" for call in acknowledged_]
        if not exceptions and not refused:
            lines.append("  The current configuration shows no violation.")
        details = []
        for e in exceptions:                                 # `log` is written in no workflow: its states are counted, not listed
            calls = result.permits.get(e["id"], [])
            logs = sum(1 for call in calls if call.endswith(": log"))
            details += [f"  {e['id']} permits:"] + [f"    {call}" for call in calls if not call.endswith(": log")]
            details += [f"    log, in {logs} state{'s' if logs != 1 else ''}"] if logs else []
        for entry in (r for r in result.refused if r["rule"] == rule["id"]):
            route = " > ".join(entry["route"]) if entry["route"] else "no route from the start reaches this state"
            details.append(f"  {entry['call']}" + (f"  an argument may carry {entry['label']}" if entry["label"] else "") + f"  route: {route}")
        rests = result.rests.get(rule["id"], {})
        if rests:
            details.append("  Refused under this rule if the label requests of the modules named were not applied:")
            details += [f"    {call}  ({', '.join(modules)})" for call, modules in sorted(rests.items())]
        blocks.append({"rule": rule["id"], "lines": lines, "asked": bool(exceptions or rests or refused), "refused": refused,
                       "open": open_, "acknowledged": acknowledged_, "proposed": proposed, "details": details})
    return blocks


def at_start(project: Project, result: Result, state: str | FilePath, read: Any, write: Any, asks: bool) -> bool:
    """The report at the start of the program, on the terminal: every rule with the exceptions to it. When the result
    is not an accepted one, the user answers one rule at a time (`read` gives each answer): accepts the exceptions to
    the rule, or skips it. For a possible violation the user also chooses between the exception that would permit its
    calls, which is then a user-approved exception, kept in the state folder and not in the configuration, and an
    acknowledged risk: no exception, the program starts, and a run refuses each of those calls when it is made.
    Nothing is recorded until every rule is answered, none skipped, and a name is given. Returns whether the result
    then current is accepted. `asks` is whether there is a terminal."""
    known = acknowledged(state)
    blocks, acceptance = by_rule(project, result, known), acceptance_of(result, state)
    violating = [f"RULE {number}" for number, block in enumerate(blocks, 1) if block["refused"]]
    open_ = [f"RULE {number}" for number, block in enumerate(blocks, 1) if block["open"]]
    risks = [(block["rule"], call) for block in blocks for call in block["acknowledged"]]
    plural = lambda count, word: f"{count} {word}{'' if count == 1 else 's'}"
    approved = sum(1 for e in project.exceptions if e.get("user"))
    write(f"PRE-RUN CHECK: project {result.project}, setup {result.setup}.")
    write(f"{plural(len(project.rules), 'rule')}, {plural(len(project.exceptions) - approved, 'exception')} in the configuration, "
          f"{approved} approved at a start, {plural(len(violating), 'possible violation')}"
          + (f", {len(violating) - len(open_)} of them acknowledged" if risks else "") + ".")
    if acceptance:
        write(f"Previously accepted, no changes: by {acceptance['by']}, {acceptance['at']}, {how_accepted(acceptance)}; "
              f"result {result.fingerprint()[:12]}.")
        if risks:
            write(f"ACKNOWLEDGED RISKS, {len(risks)}: no exception permits these calls; a run refuses each when it is made. "
                  f"Kept in {acknowledged_file(state)}; delete a line to be asked again.")
            for rule, call in risks:
                write(f"    {rule}: {call}")
    elif not asks:
        write("Not accepted. There is no terminal to ask at." + (f" {plural(len(open_), 'possible violation')}, at {', '.join(open_)}." if open_ else ""))
    elif open_:
        write(f"Not accepted: {plural(len(open_), 'possible violation')}, at {', '.join(open_)}. "
              f"Each rule is asked below; nothing is recorded until the last.")
    else:
        write("Not accepted. Each rule is asked below; nothing is recorded until the last.")
    listed = {(r["rule"], r["call"]) for r in result.refused}
    for rule, call in sorted(set(known) - listed):           # a line of the file that names a call no rule forbids now: said, not silent
        write(f"NOTE {acknowledged_file(state)}: the line for {rule}: {call} names a call that is not refused now; it has no effect.")
    for place, line in unmatched_lines(project):             # a warning: the line states facts for modules that do not exist
        write(f"WARNING {place}: no module matches this line of `modules`; no call changes.")
        write(f"    {line}")
    for entry in result.unsaved:
        write(f"UNSAVED {entry['call']} references {entry['name']}, which some route does not save first")
    if acceptance:                                           # accepted and unchanged: said, with whose acceptance and when; not listed again
        return True
    added, risked, skipped = [], [], []
    for number, block in enumerate(blocks, 1):
        write("")
        for line in block["lines"]:
            write(line)
        if not asks and block["proposed"]:                   # no terminal, no choices: the exception that would permit the calls is still said
            write(f"  The exception that would permit {'this call' if len(block['open']) == 1 else 'these calls'}:")
            write(f"        {block['proposed']['id']}  except: {_flow(block['proposed']['except'])}")
        if not asks or not block["asked"]:
            continue
        while True:
            if block["proposed"]:                            # each choice says what it does; the exception is written out, with the file it goes to
                one = len(block["open"]) == 1
                these, each = ("this call", "it") if one else ("these calls", "each")
                write(f"  [1] Permit {these}: add this exception to {user_exceptions_file(state)}")
                write(f"        {block['proposed']['id']}  except: {_flow(block['proposed']['except'])}")
                write("  [2] More details")
                write("  [3] Skip for now: the program does not start")
                write(f"  [4] Keep {these} forbidden, as an acknowledged risk: the program starts; a run refuses {each} when it is made")
            else:
                write("  [1] Accept   [2] More details   [3] Skip for now: the program does not start until this rule is accepted")
            try:
                answer = read("> ").strip()
            except EOFError:
                answer = "3"
            if answer == "2":
                for line in block["details"]:
                    write(line)
            elif answer == "1":
                added += [block["proposed"]] if block["proposed"] else []
                break
            elif answer == "3":
                skipped.append(f"RULE {number}")
                break
            elif answer == "4" and block["proposed"]:
                risked += [(block["rule"], call) for call in block["open"]]
                break
    write("")
    if not asks:
        write("NOT ACCEPTED.")
        return False
    if skipped:
        write(f"SKIPPED: {', '.join(skipped)}.")
        write("NOT ACCEPTED. Nothing was recorded.")
        return False
    try:
        by = read("Name for the record of this acceptance: ").strip() or "unnamed"
    except EOFError:                                         # no answer is no acceptance
        write("")
        write("NOT ACCEPTED: no name was given. Nothing was recorded.")
        return False
    if added:
        project.exceptions.extend(added)
        result = check(project)
    still = {(r["rule"], r["call"]) for r in result.refused}
    if still - set(risked) - set(known):                     # an exception that names the modules called permits their calls: not expected
        if added:
            del project.exceptions[-len(added):]
        write(f"NOT ACCEPTED: {len(still - set(risked) - set(known))} call(s) are still refused and not acknowledged. Nothing was recorded.")
        return False
    if added:
        _write_user_exceptions(project, state)
        write(f"ADDED {plural(len(added), 'exception')} to {user_exceptions_file(state)}: {', '.join(e['id'] for e in added)}.")
    if risked:
        at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _write_acknowledged(state, list(known.values()) + [{"rule": rule, "call": call, "by": by, "at": at} for rule, call in risked])
        write(f"ACKNOWLEDGED {plural(len(risked), 'risk')}, kept in {acknowledged_file(state)}; a run refuses each of these calls when it is made:")
        for rule, call in risked:
            write(f"    {rule}: {call}")
    entry = accept(result, state, by, rule_by_rule=True)
    write(f"ACCEPTED by {entry['by']}, {entry['at']}.")
    return True


def open_run(project: Project, tools: Mapping[str, Any], state: str | FilePath) -> Run:
    """A run of `project` with its store and its access log in the folder `state`. Refused (NotAccepted) unless the
    current result of the pre-run check is an accepted one. An accepted result may list refused calls, each an
    acknowledged risk: the run refuses such a call when it is made, as it refuses any call no exception permits."""
    result = check(project)
    if acceptance_of(result, state) is None:
        raise NotAccepted(f"the pre-run result of {result.project} under the setup {result.setup} is not accepted: "
                          f"{len(unacknowledged(result, state))} refused call(s) that no exception permits and no acknowledged risk names. "
                          f"It is accepted at the terminal, when `python -m chaperone run` starts.")
    state = FilePath(state)
    return Run(project, tools, Store(state / "store.json", project.stores), state / "access.jsonl")


def report(result: Result, acceptance: dict[str, Any] | None, detail: bool = False, known: Any = ()) -> str:
    """The report of section 9: the verdict first, as counts; every refused call; the rest on request."""
    lines = [f"pre-run check: project {result.project}, setup {result.setup}",
             f"  refused calls: {len(result.refused)}",
             f"  calls checked: {result.checked}",
             f"  exceptions: {len(result.permits)}; exceptions no call needs: {len(result.idle)}" +
             (f" ({', '.join(result.idle)})" if result.idle else ""),
             f"  calls that depend on a module's label requests: {len(result.depends)}",
             f"  references a route may not have saved: {len(result.unsaved)}"]
    if acceptance:
        lines.append(f"  result {result.fingerprint()[:12]}: accepted by {acceptance['by']}, {acceptance['at']}")
    elif any((r["rule"], r["call"]) not in known for r in result.refused):
        lines.append(f"  result {result.fingerprint()[:12]}: not accepted; nothing runs. A refused call is answered at the terminal, "
                     f"when `python -m chaperone run` starts: an exception, or an acknowledged risk")
    else:
        lines.append(f"  result {result.fingerprint()[:12]}: not accepted; nothing runs until it is")
    lines += [f"WARNING no module matches the line `{pattern}` of the setup {result.setup}" for pattern in result.unmatched]
    for entry in result.refused:
        because = f" (an argument may carry {entry['label']})" if entry["label"] else ""
        route = " > ".join(entry["route"]) if entry["route"] else "no route from the start reaches this state"
        risk = ". Acknowledged risk: a run refuses it when it is made" if (entry["rule"], entry["call"]) in known else ""
        lines.append(f"REFUSED {entry['call']} is forbidden by {entry['rule']}{because}; no exception permits it. "
                     f"Route: {route}{risk}")
    for entry in result.unsaved:
        lines.append(f"UNSAVED {entry['call']} references {entry['name']}, which some route does not save first")
    if detail:
        lines.append("")
        for exception, calls in result.permits.items():
            lines.append(f"{exception} permits {len(calls)} call(s)")
            lines += [f"    {call}" for call in calls]
        if result.depends:
            lines.append("")
            lines.append("not refused only because a declared request is assumed applied:")
            lines += [f"    {call}: rests on {', '.join(modules)}" for call, modules in result.depends.items()]
    return "\n".join(lines)
