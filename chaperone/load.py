"""Read a project's configuration and work out every module's facts (specification 2.2 to 2.11, 2.13).

A project is a folder:

    system.yaml          labels, stores, profiles, properties, exit_run_on_refusal
    setups/<name>.yaml   tools, and each module's line
    rules.yaml           rules, the properties they read, and the rules files it imports
    exceptions.yaml      exceptions to rules
    ui.yaml              the words and colour displayed for each label, the styles, the panes
    modules.py           the module declarations (or a folder `modules/` of .py files)
    workflows/<name>.yaml  the workflows

Nothing is assumed. A name that is used and not declared, a property with no value and no default, two statements
that disagree, and a key this file does not know are each reported, with the file and the line. Every problem found is
reported at once, in one ConfigError.

The workflow files are read by chaperone.workflow. Not built yet: a tool named by an argument.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import inspect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chaperone.errors import ConfigError, ConfigIssue
from chaperone.yaml_lines import load_yaml

from . import declare
from .declare import Declaration
from .workflow import load_workflows

ENGINE_CALLS = ("decide", "log")                             # what a selection may name beside modules (2.8)
ENGINE_OPERATIONS = ENGINE_CALLS + ("get", "pick", "equals", "labels_of", "store.put", "store.get", "store.add",
                                    "store.remove")


@dataclass
class ModuleFacts:
    """One module's facts (2.5): the value of every declared property, its `adds`, and where each was stated."""
    name: str
    tool: str | None
    values: dict[str, bool] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)    # property -> where its value was stated, or "default"
    adds: dict[str, str] = field(default_factory=dict)       # label -> where it was first stated


@dataclass
class Project:
    folder: Path
    setup: str
    labels: dict[str, dict[str, Any]]
    stores: dict[str, Any]
    profiles: dict[str, dict[str, Any]]
    properties: dict[str, dict[str, Any]]                    # name -> {} or {default: bool}; tools and profiles included
    rules: list[dict[str, Any]]
    exceptions: list[dict[str, Any]]
    tools: dict[str, dict[str, Any]]
    lines: dict[str, dict[str, Any]]                         # the setup's `modules:` section
    declarations: dict[str, Declaration]
    facts: dict[str, ModuleFacts]
    ui: dict[str, Any]
    unmatched_patterns: list[str]                            # keys of `modules:` with `*` that match no module
    workflows: dict[str, Any] = field(default_factory=dict)  # name -> chaperone.workflow.Workflow
    engine_facts: dict[str, ModuleFacts] = field(default_factory=dict)   # the engine operation `log`: the facts its line of the setup gives it
    note: str = ""                                           # the setup's `note`: displayed by the page, nothing else
    exit_run_on_refusal: Any = "never"                      # system.yaml: "never", "always", or the names whose refusal ends a run


class _Problems:
    def __init__(self) -> None:
        self.issues: list[ConfigIssue] = []

    def add(self, file: Path | str, node: Any, key: Any, message: str) -> None:
        """A problem at `key` of the mapping `node` (or at the mapping itself when it has no such key)."""
        line = getattr(node, "key_lines", {}).get(key) if key is not None else None
        self.issues.append(ConfigIssue(str(file), line or getattr(node, "line", None), message))

    def keys(self, file: Path, node: Any, what: str, allowed: tuple[str, ...], required: tuple[str, ...] = ()) -> bool:
        """`node` is a mapping with only the `allowed` keys and every `required` one. False when it is no mapping."""
        if not isinstance(node, dict):
            self.add(file, node, None, f"{what}: a mapping is expected, with the keys {', '.join(allowed)}")
            return False
        for key in node:
            if key not in allowed:
                self.add(file, node, key, f"{what}: the key `{key}` is not known; the keys are {', '.join(allowed)}")
        for key in required:
            if key not in node:
                self.add(file, node, None, f"{what}: `{key}` is required")
        return True


def default_setup(folder: str | Path) -> str:
    """The setup taken when none is named: `demo` when the project has one, otherwise `local`."""
    return "demo" if (Path(folder) / "setups" / "demo.yaml").is_file() else "local"


def load_project(folder: str | Path, setup: str | None = None) -> Project:
    """Read the project in `folder` under the setup `setup` (by default, `default_setup`). Raises ConfigError listing
    every problem found."""
    folder = Path(folder)
    setup = setup or default_setup(folder)
    bad = _Problems()
    system_file, setup_file = folder / "system.yaml", folder / "setups" / f"{setup}.yaml"
    system, setup_data = load_yaml(system_file), load_yaml(setup_file)
    bad.keys(system_file, system, "system.yaml", ("labels", "stores", "profiles", "properties", "exit_run_on_refusal"))
    bad.keys(setup_file, setup_data, "the setup", ("tools", "modules", "note", "log"), ("tools", "modules", "log"))
    note = setup_data.get("note") if isinstance(setup_data, dict) else None
    if note is not None and not isinstance(note, str):
        bad.add(setup_file, setup_data, "note", "the setup: `note` is a text; the page displays it under the title")
    labels = _mapping(system.get("labels"))
    profiles = _mapping(system.get("profiles"))
    tools = _mapping(setup_data.get("tools"))
    lines = _mapping(setup_data.get("modules"))
    properties: dict[str, dict[str, Any]] = {}
    _properties(bad, system_file, system, properties)
    rules: list[dict[str, Any]] = []
    _rules_file(bad, folder / "rules.yaml", properties, rules, seen=[])
    for kind, names, file in (("tool", tools, setup_file), ("profile", profiles, system_file)):
        for name in names:                                   # each declares a property of its own name (2.5, 2.6)
            if name in properties:
                bad.add(file, names, name, f"{kind} {name}: a property or a tool or a profile already has this name")
            properties[name] = {"default": False}
    declarations = _declarations(bad, folder)

    project = Project(folder=folder, setup=setup, labels=labels, stores=_mapping(system.get("stores")),
                      profiles=profiles, properties=properties, rules=rules, exceptions=[], tools=tools, lines=lines,
                      declarations=declarations, facts={}, ui={}, unmatched_patterns=[],
                      note=note if isinstance(note, str) else "")
    _check_labels(bad, system_file, project)
    _check_profiles_and_tools(bad, system_file, setup_file, project)
    _facts(bad, setup_file, project)
    _log_facts(bad, setup_file, setup_data, project)
    _exit_on_refusal(bad, system_file, system, project)
    if not bad.issues:                                       # selections read the facts: only when those are whole
        _check_selections(bad, system_file, project)
        _exceptions(bad, folder / "exceptions.yaml", project)
        _ui(bad, folder / "ui.yaml", project)
        project.workflows = load_workflows(project, bad)
        order = project.ui.get("workflow_order") or []
        for name in order:                                   # ui.yaml: the order in which the page lists the workflows
            if name not in project.workflows:
                bad.add(folder / "ui.yaml", project.ui, "workflow_order", f"ui.yaml: workflow_order names {name}, which is not a workflow")
        project.workflows = {name: project.workflows[name] for name in
                             [n for n in order if n in project.workflows] + [n for n in project.workflows if n not in order]}
    if bad.issues:
        raise ConfigError(bad.issues)
    return project


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _properties(bad: _Problems, file: Path, data: Any, into: dict[str, dict[str, Any]]) -> None:
    for name, spec in _mapping(data.get("properties")).items():
        spec = {} if spec is None else spec
        if not bad.keys(file, spec, f"property {name}", ("default",)):
            continue
        if "default" in spec and not isinstance(spec["default"], bool):
            bad.add(file, spec, "default", f"property {name}: its default is true or false")
        if name in into and into[name] != dict(spec):
            bad.add(file, data["properties"], name, f"property {name}: declared twice, differently")
        into[name] = dict(spec)


def _rules_file(bad: _Problems, file: Path, properties: dict[str, dict[str, Any]], rules: list[dict[str, Any]],
                seen: list[Path]) -> None:
    if file.resolve() in seen:
        bad.add(file, None, None, "this rules file imports itself, directly or through another")
        return
    data = load_yaml(file, layered=False)          # rules and exceptions: never changed by the user's layer
    if not bad.keys(file, data, "a rules file", ("imports", "properties", "rules")):
        return
    for path in data.get("imports") or []:                   # a path is relative to the file that imports
        _rules_file(bad, file.parent / str(path), properties, rules, seen + [file.resolve()])
    _properties(bad, file, data, properties)
    for rule in data.get("rules") or []:
        if not bad.keys(file, rule, "a rule", ("id", "never"), ("id", "never")):
            continue
        what = f"rule {rule.get('id')}"
        if any(r["id"] == rule.get("id") for r in rules):
            bad.add(file, rule, "id", f"{what}: a rule with this id is already declared; an imported rule cannot be "
                                      f"modified or declared again")
            continue
        if bad.keys(file, rule.get("never"), what, ("call", "carrying"), ("call",)):
            rules.append({"id": rule["id"], "never": rule["never"], "file": str(file),
                          "line": getattr(rule, "line", None)})


def _declarations(bad: _Problems, folder: Path) -> dict[str, Declaration]:
    """Import the project's modules.py, or every .py file of its folder `modules/`, and collect the declarations."""
    single, many = folder / "modules.py", folder / "modules"
    files = [single] if single.is_file() else sorted(many.glob("*.py")) if many.is_dir() else []
    if not files:
        bad.add(folder, None, None, "no modules.py and no folder modules/: the project declares no module")
    found: list[Declaration] = []
    declare._collecting.append(found)
    try:
        for index, file in enumerate(files):
            spec = importlib.util.spec_from_file_location(f"_guard_project_{abs(hash(str(folder)))}_{index}", file)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
    finally:
        declare._collecting.pop()
    out: dict[str, Declaration] = {}
    for d in found:
        where = f"{d.file}:{d.line}"
        if d.name in out:
            bad.issues.append(ConfigIssue(d.file, d.line, f"module {d.name}: declared twice (also at "
                                                           f"{out[d.name].file}:{out[d.name].line})"))
        elif d.name in ENGINE_OPERATIONS:
            bad.issues.append(ConfigIssue(d.file, d.line, f"module {d.name}: this is the name of an engine operation"))
        stated = list(d.properties.items()) + list(d.requests.items())
        stated += [(f"{part}: {label}", v) for part, changes in d.part_requests.items() for label, v in changes.items()]
        for prop, value in stated:
            if not isinstance(value, bool):
                bad.issues.append(ConfigIssue(d.file, d.line, f"module {d.name}: {prop} is {value!r}; true or false "
                                                               f"is expected ({where})"))
        out.setdefault(d.name, d)
    return out


def _check_labels(bad: _Problems, file: Path, project: Project) -> None:
    for name, spec in project.labels.items():
        if bad.keys(file, spec, f"label {name}", ("spreads", "may_add", "may_remove"), ("spreads",)):
            if not isinstance(spec.get("spreads", True), bool):
                bad.add(file, spec, "spreads", f"label {name}: spreads is true or false")


def _stated(bad: _Problems, file: Path, node: Any, what: str, project: Project) -> None:
    """The `facts` and `profiles` keys of a tool, a profile or a module's line: every name in them is declared."""
    facts = node.get("facts")
    if facts is not None and bad.keys(file, facts, f"{what}, facts", tuple(project.properties) + ("adds",)):
        for prop, value in facts.items():
            if prop == "adds":
                for label in value or []:
                    if label not in project.labels:
                        bad.add(file, facts, "adds", f"{what}: adds {label}, which is not a declared label")
            elif prop in project.tools or prop in project.profiles:
                bad.add(file, facts, prop, f"{what}: {prop} is the property of a tool or a profile; it is true for "
                                           f"the modules that use or name it and cannot be stated")
            elif not isinstance(value, bool):
                bad.add(file, facts, prop, f"{what}: {prop} is true or false")
    for profile in node.get("profiles") or []:
        if profile not in project.profiles:
            bad.add(file, node, "profiles", f"{what}: the profile {profile} is not declared in system.yaml")


def _check_profiles_and_tools(bad: _Problems, system_file: Path, setup_file: Path, project: Project) -> None:
    for name, spec in project.profiles.items():
        if bad.keys(system_file, spec, f"profile {name}", ("facts",), ("facts",)):
            _stated(bad, system_file, spec, f"profile {name}", project)
    for name, spec in project.tools.items():
        what = f"tool {name}"
        if not bad.keys(setup_file, spec, what, ("client", "facts", "profiles"), ("client",)):
            continue
        _stated(bad, setup_file, spec, what, project)
        client = spec.get("client")
        if isinstance(client, dict) and ("kind" in client) == ("endpoint" in client):
            bad.add(setup_file, spec, "client", f"{what}: its client has `kind` or `endpoint`, one of the two")
        elif not isinstance(client, dict):
            bad.add(setup_file, spec, "client", f"{what}: its client is a mapping with `kind` or `endpoint`")
        states_adds = "adds" in _mapping(spec.get("facts")) or any(
            "adds" in _mapping(project.profiles.get(p, {}).get("facts")) for p in spec.get("profiles") or [])
        if not states_adds:
            bad.add(setup_file, project.tools, name, f"{what}: `adds` is not stated; a tool that adds no label "
                                                     f"states `adds: []`")


def line_for(project: Project, module: str, bad: _Problems | None = None, file: Path | None = None) -> tuple[str, Any]:
    """The key and the line of the setup's `modules:` section that `module` takes: its exact name, else its one
    pattern (2.7). ("", {}) when none matches."""
    if module in project.lines:
        return module, project.lines[module]
    hits = [key for key in project.lines if "*" in key and fnmatch.fnmatchcase(module, key)]
    if len(hits) > 1 and bad is not None:
        bad.add(file or "", project.lines, hits[1], f"module {module}: matched by the patterns {' and '.join(hits)} "
                                                    f"and by no exact name")
    return (hits[0], project.lines[hits[0]]) if hits else ("", {})


def _facts(bad: _Problems, setup_file: Path, project: Project) -> None:
    for key, line in project.lines.items():
        what = f"the line `{key}`"
        if not bad.keys(setup_file, line, what, ("tool", "profiles", "facts", "from_argument", "one_of")):
            continue
        if "from_argument" in line or "one_of" in line:
            bad.add(setup_file, line, "from_argument", f"NOT YET IMPLEMENTED: a tool named by an argument ({what})")
        if "tool" in line and line["tool"] not in project.tools:
            bad.add(setup_file, line, "tool", f"{what}: the tool {line['tool']} is not declared")
        _stated(bad, setup_file, line, what, project)
        if "*" not in key and key not in project.declarations:
            bad.add(setup_file, project.lines, key, f"{what}: no module has this name")
        elif "*" in key and not any(fnmatch.fnmatchcase(m, key) for m in project.declarations):
            project.unmatched_patterns.append(key)
    if bad.issues:
        return                                               # facts are worked out from statements that are valid
    for name, d in project.declarations.items():
        key, line = line_for(project, name, bad, setup_file)
        tool = line.get("tool")
        where_declared = f"its declaration, {Path(d.file).name}:{d.line}"
        if d.takes_ctx and not tool:
            bad.issues.append(ConfigIssue(d.file, d.line, f"module {name}: takes `ctx`, and no line of the setup "
                                                           f"{project.setup} gives it a tool"))
            continue
        if tool and not d.takes_ctx:
            bad.add(setup_file, project.lines, key, f"module {name}: the line `{key}` gives it the tool {tool}, and "
                                                    f"its function takes no `ctx`")
            continue
        facts = ModuleFacts(name=name, tool=tool)
        named: set[str] = set()

        def take(source: str, values: dict[str, Any], adds: Any) -> None:
            for prop, value in values.items():
                if prop not in project.properties:
                    bad.issues.append(ConfigIssue(d.file, d.line, f"module {name}: the property {prop} ({source}) "
                                                                   f"is not declared"))
                elif prop in facts.values and facts.values[prop] != value:
                    bad.issues.append(ConfigIssue(str(setup_file), None, (
                        f"module {name}: {prop} is {_word(facts.values[prop])} in {facts.sources[prop]} and "
                        f"{_word(value)} in {source}")))
                else:
                    facts.values.setdefault(prop, value)
                    facts.sources.setdefault(prop, source)
            for label in adds or []:
                if label not in project.labels:
                    bad.issues.append(ConfigIssue(d.file, d.line, f"module {name}: adds {label} ({source}), which "
                                                                   f"is not a declared label"))
                facts.adds.setdefault(label, source)

        def take_entry(source: str, entry: dict[str, Any]) -> None:
            stated = dict(_mapping(entry.get("facts")))
            adds = stated.pop("adds", None)
            take(source, stated, adds)
            for profile in entry.get("profiles") or []:
                named.add(profile)
                take_entry(f"the profile {profile}", project.profiles[profile])

        take(where_declared, d.properties, d.adds)
        if key:
            take_entry(f"the line `{key}`", line)
        if tool:
            named.add(tool)
            take_entry(f"the tool {tool}", project.tools[tool])
        for prop, spec in project.properties.items():
            if prop in project.tools or prop in project.profiles:
                facts.values[prop] = prop in named
                facts.sources[prop] = "uses or names it" if prop in named else "does not use or name it"
            elif prop not in facts.values:
                if "default" not in spec:
                    bad.issues.append(ConfigIssue(d.file, d.line, f"module {name}: the property {prop} has no "
                                                                   f"value: nothing states it and it has no default"))
                else:
                    facts.values[prop], facts.sources[prop] = spec["default"], "default"
        project.facts[name] = facts


def _log_facts(bad: _Problems, setup_file: Path, setup_data: Any, project: Project) -> None:
    """The engine operation `log` (2.12) keeps, for display, what a module wrote in an error. The setup's `log:` line
    names the tool it uses, or states its facts; it then has the facts a module with that line would have, so a rule
    that selects by property selects it. `adds` is about what a module returns: `log` returns nothing."""
    line = setup_data.get("log") if isinstance(setup_data, dict) else None
    what = "the line `log`"
    if not isinstance(line, dict):
        if isinstance(setup_data, dict) and "log" in setup_data:
            bad.add(setup_file, setup_data, "log", f"{what}: a mapping with `tool` or `facts`")
        return
    if not bad.keys(setup_file, line, what, ("tool", "profiles", "facts")):
        return
    tool = line.get("tool")
    if tool is not None and tool not in project.tools:
        bad.add(setup_file, line, "tool", f"{what}: the tool {tool} is not declared")
        return
    before = len(bad.issues)
    _stated(bad, setup_file, line, what, project)
    if len(bad.issues) > before:
        return
    facts = ModuleFacts(name="log", tool=tool)
    named: set[str] = set()

    def take_entry(source: str, entry: dict[str, Any]) -> None:
        for prop, value in _mapping(entry.get("facts")).items():
            if prop == "adds":
                continue
            if prop in facts.values and facts.values[prop] != value:
                bad.issues.append(ConfigIssue(str(setup_file), None, f"log: {prop} is {_word(facts.values[prop])} in "
                                                                      f"{facts.sources[prop]} and {_word(value)} in {source}"))
            else:
                facts.values.setdefault(prop, value)
                facts.sources.setdefault(prop, source)
        for profile in entry.get("profiles") or []:
            if profile in project.profiles:
                named.add(profile)
                take_entry(f"the profile {profile}", project.profiles[profile])
    take_entry(what, line)
    if tool:
        named.add(tool)
        take_entry(f"the tool {tool}", project.tools[tool])
    for prop, spec in project.properties.items():
        if prop in project.tools or prop in project.profiles:
            facts.values[prop] = prop in named
            facts.sources[prop] = "uses or names it" if prop in named else "does not use or name it"
        elif prop not in facts.values:
            if "default" not in spec:
                bad.issues.append(ConfigIssue(str(setup_file), getattr(line, "line", None),
                                              f"log: the property {prop} has no value: nothing states it and it has no default"))
            else:
                facts.values[prop], facts.sources[prop] = spec["default"], "default"
    project.engine_facts["log"] = facts


def _word(value: bool) -> str:
    return "true" if value else "false"


def selects(project: Project, selection: Any, name: str) -> bool:
    """Whether `selection` (2.8) denotes the module or engine call `name`."""
    if selection == "any":
        return name in project.declarations
    if isinstance(selection, list):
        return name in selection
    known = project.facts.get(name) or project.engine_facts.get(name)
    if known is None:
        return False
    where = selection["where"]
    maps = where if isinstance(where, list) else [where]
    return any(all(known.values[p] == v for p, v in m.items()) for m in maps)


def _exit_on_refusal(bad: _Problems, file: Path, system: Any, project: Project) -> None:
    """`exit_run_on_refusal` in system.yaml: what a refused call does to the run (2.12). `never` (the default: the
    refusal is an error and takes the error route), `always` (the run ends), or a list of modules, `log` and `decide`
    included, whose refusal ends the run."""
    value = system.get("exit_run_on_refusal", "never") if isinstance(system, dict) else "never"
    if value in ("never", "always"):
        project.exit_run_on_refusal = value
    elif isinstance(value, list) and value and all(isinstance(name, str) for name in value):
        for name in value:
            if name not in project.declarations and name not in ENGINE_CALLS:
                bad.add(file, system, "exit_run_on_refusal", f"system.yaml: exit_run_on_refusal names {name}, which is not a module, "
                                                              "nor `decide` or `log`")
        project.exit_run_on_refusal = frozenset(value)
    else:
        bad.add(file, system, "exit_run_on_refusal", "system.yaml: exit_run_on_refusal is `never`, `always`, or a list of "
                                                     "names of modules, `decide` and `log`")


def _selection(bad: _Problems, file: Path | str, node: Any, key: str, what: str, project: Project) -> bool:
    """The selection at `node[key]` has one of the forms of 2.8 and names only what is declared."""
    selection = node[key]
    before = len(bad.issues)
    if selection == "any":
        return True
    if isinstance(selection, list):
        for name in selection:
            if name not in project.declarations and name not in ENGINE_CALLS:
                bad.add(file, node, key, f"{what}: {name} is not a module, nor `decide` or `log`")
    elif isinstance(selection, dict) and set(selection) == {"where"}:
        where = selection["where"]
        for one in where if isinstance(where, list) else [where]:
            if not isinstance(one, dict):
                bad.add(file, node, key, f"{what}: `where` is a mapping of properties, or a list of such mappings")
                continue
            for prop, value in one.items():
                if prop not in project.properties:
                    bad.add(file, node, key, f"{what}: the property {prop} is not declared")
                elif not isinstance(value, bool):
                    bad.add(file, node, key, f"{what}: {prop} is compared with true or false")
    else:
        bad.add(file, node, key, f"{what}: a selection is a list of module names, `any`, or `{{where: ...}}`")
    return len(bad.issues) == before


def _arguments(bad: _Problems, file: Path | str, permit: Any, what: str, project: Project) -> bool:
    """`arguments` (2.11) is a map: for each named argument, the labels it must carry, each with true or false. Each
    name is a parameter of every module the exception selects. It is not written with `carrying`."""
    named, before = permit["arguments"], len(bad.issues)
    if "carrying" in permit:
        bad.add(file, permit, "arguments", f"{what}: an exception has `carrying` or `arguments`, not both")
    if not isinstance(named, dict) or not named:
        bad.add(file, permit, "arguments", f"{what}: `arguments` is written {{ARGUMENT: {{LABEL: true}}}}, for one argument or more")
        return False
    selected = [module for module in project.declarations if selects(project, permit["call"], module)]
    if any(selects(project, permit["call"], call) for call in ENGINE_CALLS):
        bad.add(file, permit, "arguments", f"{what}: `arguments` is for modules; `decide` and `log` have no named argument")
    if not selected:
        bad.add(file, permit, "arguments", f"{what}: `arguments` is written, and the exception selects no module")
    for name, labels in named.items():
        if not isinstance(labels, dict) or not labels:
            bad.add(file, permit, "arguments", f"{what}: the argument {name} is followed by the labels it must carry, written {{LABEL: true}}")
            continue
        for label, value in labels.items():
            if label not in project.labels:
                bad.add(file, permit, "arguments", f"{what}: {label} is not a declared label")
            if not isinstance(value, bool):
                bad.add(file, permit, "arguments", f"{what}: the value of {label} for the argument {name} is true or false")
        for module in selected:
            declaration = project.declarations[module]
            parameters = list(inspect.signature(declaration.function).parameters)[int(declaration.takes_ctx):]
            if name not in parameters:
                bad.add(file, permit, "arguments", f"{what}: the module {module} has no argument {name}")
    return len(bad.issues) == before


def _carrying(bad: _Problems, file: Path | str, node: Any, what: str, project: Project) -> None:
    carrying = node.get("carrying")
    if carrying is None:
        return
    if not isinstance(carrying, dict) or len(carrying) != 1:
        bad.add(file, node, "carrying", f"{what}: `carrying` is one label and a value, written {{LABEL: true}}")
        return
    (label, value), = carrying.items()
    if label not in project.labels:
        bad.add(file, node, "carrying", f"{what}: {label} is not a declared label")
    if not isinstance(value, bool):
        bad.add(file, node, "carrying", f"{what}: the value is true or false")


def _check_selections(bad: _Problems, system_file: Path, project: Project) -> None:
    for name, spec in project.labels.items():
        for key in ("may_add", "may_remove"):
            if key in spec:
                _selection(bad, system_file, spec, key, f"label {name}, {key}", project)
    for rule in project.rules:
        what = f"rule {rule['id']}"
        _selection(bad, rule["file"], rule["never"], "call", what, project)
        _carrying(bad, rule["file"], rule["never"], what, project)
    if bad.issues:
        return
    for name, d in project.declarations.items():             # a declared request is one the labels permit (2.9)
        changes = list(d.requests.items()) + [c for part in d.part_requests.values() for c in part.items()]
        for label, value in changes:
            key = "may_add" if value else "may_remove"
            if label not in project.labels:
                bad.issues.append(ConfigIssue(d.file, d.line, f"module {name}: requests {label}, which is not a "
                                                               f"declared label"))
            elif not selects(project, project.labels[label].get(key, []), name):
                bad.issues.append(ConfigIssue(d.file, d.line, (
                    f"module {name}: its declaration states the request ({label}, {_word(value)}), and "
                    f"{label}.{key} in system.yaml does not select it")))


def _exceptions(bad: _Problems, file: Path, project: Project) -> None:
    data = load_yaml(file, layered=False)          # rules and exceptions: never changed by the user's layer
    if not bad.keys(file, data, "exceptions.yaml", ("exceptions",), ("exceptions",)):
        return
    rules = {r["id"]: r for r in project.rules}
    seen: set[Any] = set()
    for entry in data.get("exceptions") or []:
        if not bad.keys(file, entry, "an exception", ("id", "rule", "except"), ("id", "rule", "except")):
            continue
        what = f"exception {entry.get('id')}"
        if entry.get("id") in seen:
            bad.add(file, entry, "id", f"{what}: an exception with this id is already declared")
        seen.add(entry.get("id"))
        rule = rules.get(entry.get("rule"))
        if rule is None:
            bad.add(file, entry, "rule", f"{what}: the rule {entry.get('rule')} is not declared")
        permit = entry.get("except")
        if not bad.keys(file, permit, what, ("call", "arguments", "carrying"), ("call",)):
            continue
        ok = _selection(bad, file, permit, "call", what, project)
        _carrying(bad, file, permit, what, project)
        if "arguments" in permit:                            # the labels that named arguments must carry (2.11)
            ok = _arguments(bad, file, permit, what, project) and ok
        elif rule is not None and "carrying" in permit and "carrying" not in rule["never"]:
            bad.add(file, permit, "carrying", f"{what}: `carrying` is permitted only on an exception to a rule that "
                                              f"has `carrying`; {rule['id']} has none")
        if ok and rule is not None:
            project.exceptions.append({"id": entry["id"], "rule": entry["rule"], "except": permit, "file": str(file),
                                       "line": getattr(entry, "line", None)})


def add_user_exceptions(project: Project, file: Path | str) -> None:
    """The user-approved exceptions (specification, section 9): exceptions the user added when the program started,
    kept in `user_exceptions.yaml` in the state folder, outside the configuration. Each is read and checked as an
    exception of exceptions.yaml is, is in force as one, and is marked `user`. A file that is not there adds none."""
    file = Path(file)
    if not file.is_file():
        return
    bad, configured = _Problems(), {e["id"] for e in project.exceptions}
    before = len(project.exceptions)
    _exceptions(bad, file, project)
    for exception in project.exceptions[before:]:
        exception["user"] = True
        if exception["id"] in configured:
            bad.issues.append(ConfigIssue(str(file), exception["line"], f"exception {exception['id']}: the configuration "
                                                                         f"has an exception with this id"))
    if bad.issues:
        del project.exceptions[before:]
        raise ConfigError(bad.issues)


def _ui(bad: _Problems, file: Path, project: Project) -> None:
    """ui.yaml (2.13). Required when a tool has a client of kind `web`; read whenever the file exists."""
    web = [name for name, tool in project.tools.items() if _mapping(tool.get("client")).get("kind") == "web"]
    if not file.is_file():
        if web:
            bad.add(file, None, None, f"the tool {web[0]} has a client of kind `web`, and there is no ui.yaml")
        return
    ui = load_yaml(file)
    if not bad.keys(file, ui, "ui.yaml", ("labels", "styles", "default_style", "panes", "state_tab", "reset_control", "show_updates", "workflow_order"),
                    ("labels", "styles", "default_style", "panes")):
        return
    for switch in ("state_tab", "reset_control", "show_updates"):
        if not isinstance(ui.get(switch, False), bool):
            bad.add(file, ui, switch, f"ui.yaml: {switch} is true or false")
    if ui.get("reset_control") and not ui.get("state_tab"):
        bad.add(file, ui, "reset_control", "ui.yaml: the reset control is on the tab \"State\"; it needs state_tab: true")
    shown = _mapping(ui.get("labels"))
    for label, spec in shown.items():
        if label not in project.labels:
            bad.add(file, shown, label, f"ui.yaml: {label} is not a declared label")
        if bad.keys(file, spec, f"ui.yaml, label {label}", ("words", "colour"), ("words", "colour")):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(spec.get("colour"))):
                bad.add(file, spec, "colour", f"ui.yaml, label {label}: a colour is written \"#rrggbb\"")
    for label in project.labels:
        if label not in shown:
            bad.add(file, ui, "labels", f"ui.yaml: the label {label} has no words and no colour")
    if ui.get("default_style") not in _mapping(ui.get("styles")):
        bad.add(file, ui, "default_style", f"ui.yaml: default_style names {ui.get('default_style')}, which is not "
                                           f"under `styles`")
    for pane in ui.get("panes") or []:
        if bad.keys(file, pane, "ui.yaml, a pane", ("title", "stores"), ("title", "stores")):
            for store in pane.get("stores") or []:
                if store not in project.stores:
                    bad.add(file, pane, "stores", f"ui.yaml: the pane {pane.get('title')} names {store}, which is "
                                                  f"not a declared store")
    project.ui = dict(ui)
