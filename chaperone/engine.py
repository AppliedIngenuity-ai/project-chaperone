"""The call check and the workflow engine (specification, section 4: C1 to C4; 2.12; rules P1 to P6).

`check_call` is C1 and C2: which rules forbid a call with these arguments, and which exception permits each.
`Run` runs one workflow: before every call of a module or of `decide` it checks the call; a call
that a rule forbids and no exception permits is refused before the module runs. After a module returns, the labels of
its result are computed by P1, P2 and P4. The access log (C4) records names and labels, never a value.

A module is passed plain data. Its tool is the object given for the tool's name in `tools`; building that object from
the setup's `client` is `chaperone.clients.build_tools`.
"""

from __future__ import annotations

import copy
import re
import json
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any, Mapping

from .load import Project, selects
from .store import Store
from .values import Problem, Value, apply_requests, equals, error, parse_path, result, scalar_paths, show_path
from .workflow import Workflow, reference


ERROR_TYPE = re.compile(r"[A-Za-z0-9_]{1,64}")                # the type of an error is a name (2.3)


class ModuleError(Exception):
    """What a module raises to report an error, with its type (2.3): a name of letters, digits and _, at most 64.
    The state the error continues in may read the type as `{error.type}`."""

    def __init__(self, type: str, message: str = "", detail: Any = None) -> None:
        super().__init__(message or type)
        self.type, self.message, self.detail = type, message or type, detail


@dataclass(frozen=True)
class Forbidden:
    rule: str
    exceptions: tuple[str, ...]                             # the exceptions that together permit the call; none: refused
    argument: str = ""                                      # the first argument with a part that forbade the call
    label: str = ""

    @property
    def exception(self) -> str | None:
        """The ids of the exceptions that permit the call, as one text; None when the call is refused."""
        return ", ".join(self.exceptions) or None


def check_call(project: Project, name: str, arguments: Mapping[str, Value]) -> list[Forbidden]:
    """C1 and C2 for a call of the module or engine call `name`: one entry for each rule that forbids the call."""
    out: list[Forbidden] = []
    for rule in project.rules:
        never = rule["never"]
        if not selects(project, never["call"], name):
            continue
        forbidding: list[tuple[str, Value, tuple[Any, ...]]] = []          # (argument, its value, a scalar part)
        label = ""
        if "carrying" in never:
            (label, wanted), = never["carrying"].items()
            for argument, value in arguments.items():
                forbidding += [(argument, value, part) for part in _scalar_parts(value)
                               if (label in value.labels.get(part, ())) is wanted]
            if not forbidding:
                continue
        def holds(argument: str, needed: str, wanted: bool) -> bool:
            value = arguments.get(argument)                  # an argument not passed, or with no scalar part, does not hold:
            parts = _scalar_parts(value) if value is not None else []                        # the module would act on a value of its own
            return bool(parts) and all((needed in value.labels.get(part, ())) is wanted for part in parts)
        used = covering(project, rule, name, forbidding,
                        lambda part, needed, wanted: (needed in part[1].labels.get(part[2], ())) is wanted, holds)
        out.append(Forbidden(rule["id"], used, forbidding[0][0] if forbidding else "", label))
    return out


def covering(project: Project, rule: Mapping[str, Any], name: str, forbidding: list[Any], carries: Any, holds: Any) -> tuple[str, ...]:
    """C2: the exceptions that together permit a call of `name` that `rule` forbids, in the order written; none when
    the call is refused. An exception with neither `carrying` nor `arguments` permits the whole call. One with
    `arguments` permits the whole call when each named argument has a scalar part and every scalar part of it carries
    each label stated for it (`holds(argument, label, value)`; an argument not passed, or empty, does not hold). One with `carrying` covers the forbidding parts that carry its label
    (`carries(part, label, value)`); the call is permitted when every forbidding part is covered by some exception.
    An exception that covers no part still uncovered is not named."""
    used: list[str] = []
    left = list(forbidding)
    for exception in project.exceptions:
        allowed = exception["except"]
        if exception["rule"] != rule["id"] or not selects(project, allowed["call"], name):
            continue
        if "arguments" in allowed:
            if all(holds(argument, needed, wanted) for argument, labels in allowed["arguments"].items()
                   for needed, wanted in labels.items()):
                return (*used, exception["id"])
            continue
        if "carrying" not in allowed:
            return (*used, exception["id"])
        (needed, wanted), = allowed["carrying"].items()
        still = [part for part in left if not carries(part, needed, wanted)]
        if len(still) < len(left):
            used.append(exception["id"])
            left = still
        if not left:
            return tuple(used)
    return ()


def _scalar_parts(value: Value) -> list[tuple[Any, ...]]:
    return list(scalar_paths(value.data))


@dataclass
class Outcome:
    workflow: str
    ended: str                                              # "end", "error", "too_many_states", "stopped"
    state: str                                              # the last state entered
    error: Value | None = None                              # the error that ended the run, if one did
    saved: dict[str, Value] = field(default_factory=dict)


class Run:
    def __init__(self, project: Project, tools: Mapping[str, Any], store: Store,
                 log_file: str | FilePath | None = None) -> None:
        self.project, self.tools, self.store = project, tools, store
        self.spreading = {name for name, spec in project.labels.items() if spec["spreads"]}
        self.log: list[dict[str, Any]] = []                 # the access log of this run (C4)
        self.log_file = FilePath(log_file) if log_file else None
        self.at: tuple[str, str] | None = None             # the workflow and the state now being run, for a display
        self.saved: dict[str, Value] = {}
        self.reasons: dict[int, dict[str, Any]] = {}        # the message and detail of each error, by its place in the log; in memory only
        self.stop = False                                   # set by whoever started the run: it ends before its next step
        missing = sorted({f.tool for f in project.facts.values() if f.tool and f.tool not in tools})
        if missing:
            raise ValueError(f"no object was given for the tool(s) {', '.join(missing)}")

    # ---- one workflow ----------------------------------------------------------------------------------------------
    def run(self, name: str) -> Outcome:
        workflow: Workflow = self.project.workflows[name]
        saved: dict[str, Value] = {}
        self.saved = saved                                  # what the steps of this run saved so far, for a display
        state_name, entries, raised = workflow.start, 0, None
        while True:
            entries += 1
            if workflow.max_states is not None and entries > workflow.max_states:
                self._record(workflow, state_name, "too_many_states", error_type="too_many_states", error_labels=[])
                return Outcome(name, "too_many_states", state_name, None, saved)
            state = workflow.states[state_name]
            self.at = (name, state_name)
            try:
                for step in state.get("steps") or []:
                    if self.stop:
                        self._record(workflow, state_name, "stopped")
                        return Outcome(name, "stopped", state_name, None, saved)
                    value = self._step(workflow, state_name, step, saved, raised)
                    if "save_as" in step:
                        saved[step["save_as"]] = value
                if state.get("end"):
                    return Outcome(name, "end", state_name, None, saved)
                state_name, raised = self._next(workflow, state_name, state, saved, raised), None
            except _Raised as problem:
                target = None if self._ends(problem.error) else self._route(workflow, state_name, state)
                if target is None:
                    return Outcome(name, "error", state_name, problem.error, saved)
                state_name, raised = target, problem.error

    # ---- a step ----------------------------------------------------------------------------------------------------
    def _step(self, workflow: Workflow, state: str, step: dict[str, Any], saved: dict[str, Value],
              raised: Value | None) -> Value:
        call, given = step["call"], step.get("with") or {}
        try:
            arguments = {key: self._resolve(template, saved, raised) for key, template in given.items()}
        except Problem as problem:
            raise self._error(workflow, state, call, problem.type, str(problem), [], {}) from None
        if call not in self.project.declarations:
            return self._operation(workflow, state, call, arguments)
        declaration, facts = self.project.declarations[call], self.project.facts[call]
        verdicts = check_call(self.project, call, arguments)
        refused = next((v for v in verdicts if v.exception is None), None)
        if refused is not None:                              # C2: refused before the module runs
            where = f" for the argument {refused.argument}, which carries {refused.label}" if refused.label else ""
            raise self._error(workflow, state, call, "refused", f"the call of {call} is forbidden by {refused.rule}"
                              f"{where}; no exception permits it", facts.adds, arguments, verdicts)
        plain = {key: copy.deepcopy(value.data) for key, value in arguments.items()}
        try:
            returned = (declaration.function(self.tools[facts.tool], **plain) if declaration.takes_ctx
                        else declaration.function(**plain))
            raw, requests = returned if isinstance(returned, tuple) and len(returned) == 2 else (returned, [])
            fields = declaration.part_requests
            if fields and (not isinstance(raw, dict) or set(raw) - set(fields)):
                raise Problem("bad_request", f"the declaration of {call} states requests for the fields "
                                             f"{', '.join(fields)}; its result is a dictionary with no other field")
            value = result(raw, facts.adds, arguments.values(), self.spreading)                       # P1, P2
            value = apply_requests(value, requests, declaration.requests, declaration.part_requests)  # P4
        except ModuleError as raised_by_module:
            if not isinstance(raised_by_module.type, str) or not ERROR_TYPE.fullmatch(raised_by_module.type):
                raise self._error(workflow, state, call, "module_failed", f"{call} raised an error whose type does not match "
                                  f"{ERROR_TYPE.pattern}; the type is not shown, because it may hold data",
                                  facts.adds, arguments, verdicts) from None
            raise self._error(workflow, state, call, raised_by_module.type, raised_by_module.message, facts.adds,
                              arguments, verdicts, raised_by_module.detail, written_by_module="message") from None
        except Problem as problem:                           # C3: the result is discarded, no label is changed
            kind = "refused" if problem.type == "bad_request" else problem.type
            raise self._error(workflow, state, call, kind, str(problem), facts.adds, arguments, verdicts) from None
        except Exception as unexpected:                      # an error of the programming language, passed along
            raise self._error(workflow, state, call, "module_failed", f"{call} raised {type(unexpected).__name__}",
                              facts.adds, arguments, verdicts,
                              {"python": type(unexpected).__name__, "text": str(unexpected)}, written_by_module="detail") from None
        self._record(workflow, state, call, arguments=arguments, result=value, requests=requests, verdicts=verdicts)
        return value

    def _operation(self, workflow: Workflow, state: str, call: str, arguments: dict[str, Value]) -> Value:
        """The engine operations written as steps (2.12, P3). No rule selects them."""
        try:
            if call == "get":
                value = arguments["value"]
                out = value.get(parse_path(str(arguments["path"].data))) if "path" in arguments else value
            elif call == "pick":
                out = arguments["value"].pick(arguments["fields"].data)
            elif call == "equals":
                out = equals(arguments["a"], arguments["b"], self.spreading)
            elif call == "labels_of":
                out = arguments["value"].marks()
            elif call == "store.get":
                out = self.store.get(arguments["store"].data)
            elif call == "store.put":
                self.store.put(arguments["store"].data, arguments["value"])
                out = Value.of(None)
            elif call == "store.add":
                out = self.store.add(arguments["store"].data, arguments["items"])
            else:
                out = self.store.remove(arguments["store"].data, arguments["items"])
        except Problem as problem:
            raise self._error(workflow, state, call, problem.type, str(problem), [], arguments) from None
        self._record(workflow, state, call, arguments=arguments, result=out)
        return out

    # ---- a branch, an error route ----------------------------------------------------------------------------------
    def _next(self, workflow: Workflow, state_name: str, state: dict[str, Any], saved: dict[str, Value],
              raised: Value | None) -> str:
        following = state["next"]
        if isinstance(following, str):
            return following
        try:
            tested = self._resolve(following["switch"], saved, raised)
        except Problem as problem:
            raise self._error(workflow, state_name, "decide", problem.type, str(problem), [], {}) from None
        arguments = {"value": tested}
        verdicts = check_call(self.project, "decide", arguments)
        refused = next((v for v in verdicts if v.exception is None), None)
        if refused is not None:
            raise self._error(workflow, state_name, "decide", "refused", f"the branch is forbidden by {refused.rule}: "
                              f"the value it tests carries {refused.label}; no exception permits it", [], arguments,
                              verdicts)
        for key, target in following["goto"].items():
            if type(key) is type(tested.data) and key == tested.data:
                self._record(workflow, state_name, "decide", arguments=arguments, verdicts=verdicts)
                return target
        if "else" in following:
            self._record(workflow, state_name, "decide", arguments=arguments, verdicts=verdicts)
            return following["else"]
        raise self._error(workflow, state_name, "decide", "no_match", "the value the branch tests is equal to no key "
                          "of `goto`, and the branch has no `else`", [], arguments, verdicts)

    def _route(self, workflow: Workflow, state_name: str, state: dict[str, Any]) -> str | None:
        """The state an error continues in, or None when the run ends: an error raised in the state its route names
        ends the run. The state's own `on_error` is taken before the
        workflow's. The file names the state; no error and no value chooses it, so no rule is checked here. What that
        state does with the error is checked, call by call."""
        target = state.get("on_error") or workflow.on_error
        return None if target == state_name else target

    # ---- references, errors, the access log ------------------------------------------------------------------------
    def _resolve(self, template: Any, saved: dict[str, Value], raised: Value | None) -> Value:
        """An argument: a reference stands for the referenced value with its labels; what is written carries none."""
        def place(node: Any) -> Any:
            if isinstance(node, dict):
                return {key: place(inner) for key, inner in node.items()}
            if isinstance(node, list):
                return [place(inner) for inner in node]
            found = reference(node)
            if found is None:
                return copy.deepcopy(node) if isinstance(node, (dict, list)) else node
            root, path = found
            if root == "error":
                if raised is None:
                    raise Problem("no_such_value", "`{error}` is referenced in a state no error routed to")
                return raised.get(path)
            if path[0] not in saved:
                raise Problem("no_such_value", f"{path[0]} is referenced before a step saved it")
            return saved[path[0]].get(path[1:])
        return Value.build(place(_plain(template)))

    def _error(self, workflow: Workflow, state: str, call: str, kind: str, message: str, adds: Any,
               arguments: Mapping[str, Value], verdicts: list[Forbidden] | None = None, detail: Any = None,
               written_by_module: str = "") -> "_Raised":
        """An error, recorded. `written_by_module`: "message" when a module wrote the message and the detail, "detail"
        when it wrote the detail only (the text of an error of the programming language), "" when the framework wrote
        both, which then name no value. What a module wrote is kept for display only through a call of the engine
        operation `log` (2.12), checked like any call. Refused, the refusal is the error, of type `refused` as any
        refused call's, and the display has the module's error's type and the rule, not what the module wrote."""
        value = error(kind, message, state=state, call=call, adds=adds, arguments=arguments.values(),
                      spreading=self.spreading, detail=detail)
        kept: dict[str, Any] = {"message": message, "detail": detail}
        refusal: Value | None = None
        if written_by_module:
            logged = check_call(self.project, "log", {"error": value})
            refused = next((verdict for verdict in logged if verdict.exception is None), None)
            if refused is not None:
                where = f" for the argument error, which carries {refused.label}" if refused.label else ""
                refusal = error("refused", f"the call of log is forbidden by {refused.rule}{where}; no exception "
                                          "permits it", state=state, call="log", adds=[],
                               arguments=[value], spreading=self.spreading)
                kept = {"message": None if written_by_module == "message" else message, "detail": None,
                        "withheld": {"rule": refused.rule, "label": refused.label}}
            self._record(workflow, state, "log", arguments={"error": value}, verdicts=logged,
                         error_type="refused" if refusal else None, error_labels=sorted(refusal.carried()) if refusal else None)
        self._record(workflow, state, call, arguments=arguments, verdicts=verdicts or [], error_type=kind,
                     error_labels=sorted(value.carried()))
        self.reasons[len(self.log) - 1] = kept              # for a display; the access log has the type only
        return _Raised(value if refusal is None else refusal)

    def _ends(self, raised: Value) -> bool:
        """A refused call ends the run when system.yaml's `exit_run_on_refusal` says so (2.12): `always`, or a list
        that names the call (a module, `decide` or `log`). Any other error takes the error route."""
        exit_on = self.project.exit_run_on_refusal
        if raised.data.get("type") != "refused" or exit_on == "never":
            return False
        return exit_on == "always" or raised.data.get("call") in exit_on

    def _record(self, workflow: Workflow, state: str, call: str, *, arguments: Mapping[str, Value] | None = None,
                result: Value | None = None, requests: Any = (), verdicts: list[Forbidden] | None = None,
                error_type: str | None = None, error_labels: list[str] | None = None) -> None:
        """C4: names and labels, never a value."""
        entry: dict[str, Any] = {"workflow": workflow.name, "state": state, "call": call,
                                 "arguments": {key: sorted(value.carried()) for key, value in (arguments or {}).items()}}
        if result is not None:
            entry["result"] = sorted(result.carried())
        if requests:
            entry["requests"] = [[show_path(parse_path(path)), label, wanted] for path, label, wanted in requests]
        if verdicts:
            entry["forbidden_by"] = [{"rule": v.rule, "permitted_by": v.exception} for v in verdicts]
        if error_type is not None:
            entry["error"] = {"type": error_type, "labels": error_labels or []}
        self.log.append(entry)
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


class _Raised(Exception):
    """An error on its way to the error route."""
    def __init__(self, error: Value) -> None:
        super().__init__(error.data.get("type"))
        self.error = error


def _plain(node: Any) -> Any:
    """The loader's dictionaries and lists as plain ones, so a value written in a workflow is ordinary data."""
    if isinstance(node, dict):
        return {key: _plain(inner) for key, inner in node.items()}
    if isinstance(node, list):
        return [_plain(inner) for inner in node]
    return node
