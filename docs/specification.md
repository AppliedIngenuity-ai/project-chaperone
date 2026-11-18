# Chaperone: specification

The definitions the framework implements, numbered: the code cites them ("specification 2.12").

## 1. Overview

The framework is the program that runs workflows and performs the checks defined here. A workflow is a sequence of calls
to modules. The scalar parts of every value passed to or returned by a module carry labels. Labels are attached
and propagated by the rules of section 3, from declarations in configuration files. Each call is checked by the procedure
of section 4; a call that a rule forbids and no exception permits is refused. Section 2 defines the terms.

## 2. Definitions

**2.1 Value.** A value is a scalar, a list or a dictionary. A part of a value is the value itself or any element reachable from it by a path
of field names and list indices, written `a.b[2]`. A part that is neither a dictionary nor a list is a scalar part.

**2.2 Names and files.** Labels, properties, profiles, tools, modules, workflows, the states of a workflow and
the results it saves are identified by names. A name is an
identifier; the framework attaches no meaning to it beyond identity, and two names are the same only if written
identically, capital and small letters included. The name of a label, property, profile, tool, module or state that is used in a configuration file and declared nowhere makes the configuration invalid. Stores, error types, rules and exceptions also have names; a store is declared in `system.yaml` (2.12), and an error type is declared by its use. The user is whoever writes
the configuration files or interacts with the system while it runs; this specification does not distinguish them. A
project is one system built with the framework: its configuration files and its modules. The system configuration file
of a project is `system.yaml`; the other configuration files are named where they are defined.

**2.3 Module.** A module is a function with a declaration: one statement above the function, written with the
framework's decorator `chaperone.module`, giving the module's name, such as `llm.summarize`, optionally facts (2.5), as
`properties` and `adds`, and optionally `requests` (below):

```python
@chaperone.module("llm.summarize", properties={"DESTRUCTIVE": False})
def summarize(ctx, text: dict) -> dict: ...

@chaperone.module("code.verify", properties={"DESTRUCTIVE": False}, requests={"UNTRUSTED": False, "VERIFIED": True})
def verify(summary: dict) -> dict: ...
```

A module may communicate with an external system through a tool (2.6): an object the framework constructs from the tool's
declaration and passes to the function as its first argument, `ctx`. Which tool a module uses is stated in the setup
(2.7). A module's code may also communicate with an external system by other means; the framework does not observe this,
and such a module is declared with the facts of that system (2.5). A module may return, with its result, requested label
changes, each a triple (path, label, value): the function returns its result, or a pair of the result and the list of
requests. Its declaration states, as `requests`, each label it may request and the value. A label stated under the name of
a field of the result may be requested for that field only: `requests={"summary": {"CHECKED": True}, "results":
{"UNTRUSTED": False}}`. A module whose declaration states requests by field returns a dictionary with no other field;
a result with another field refuses the call (C3). Rule P4 of section 3 states
when a request is applied. A module raises an error by raising the framework's exception class, `chaperone.ModuleError`,
with a type name; the state the error continues in (2.12) may read it as `{error.type}`. A type is a name: letters,
digits and `_`, at most 64 characters. An error raised with any other type is an error of type `module_failed`
(the type of an error is never data). Any other error of the programming language raised in a
module is an error of type `module_failed`, whose `detail` names it. A module may not have the name of an engine operation (2.12). A project's
modules are declared in its file `modules.py`, or in the `.py` files of its folder `modules/`. A function takes `ctx`
if its first parameter has that name.

**2.4 Property.** A property is a named boolean attached to a module. Properties are declared in a rules file (2.10), by
the rules that read them, or in `system.yaml`; each profile and each tool also declares one, of its own name (2.5,
2.6). A declaration optionally gives a default. Example, the three properties
declared by the rules file of 2.10:

- OUTPUT: a value passed to the module is shown to a person or written where it stays: a page, a file, a calendar
  entry, a sent message. No default: a module's declaration states it, or its tool does when it holds for every
  module that uses the tool; a module for which neither states it is refused when the project is loaded, so a
  module added to a tool cannot take a value nobody stated A module that is passed a value and returns another, such as a call of an
  LLM or a search, is not OUTPUT for that; if the value leaves the user's network, LEAVES_NETWORK says so.
- LEAVES_NETWORK: a value passed to the module may be transmitted outside the user's network. Default false.
- DESTRUCTIVE: the module may modify or delete data that persists outside the program. No default.

Example, two properties declared in `system.yaml`:

```yaml
properties:
  MADE_IN_HOUSE:      {default: false}
  ENCRYPTED_AT_REST: {default: false}
```

**2.5 Facts and profiles.** The facts of a module are the values of its properties and its `adds`: the list of labels
attached to every part of every value the module returns (rule P1 of section 3). Facts are stated in four places, any of
which may be empty: the module's declaration (2.3); the module's line in the setup (2.7); the tool the module uses (2.6);
and profiles. Wherever facts are stated in a configuration file they are stated under two keys, both optional: `facts`,
a map of property values and `adds`; and `profiles`, a list of profile names. A profile is a named set of facts,
declared in `system.yaml`; naming a profile applies its facts there:

```yaml
profiles:
  LOCAL_SOURCE:  {facts: {OUTPUT: true, LEAVES_NETWORK: false, adds: [UNTRUSTED]}}
  REMOTE_SOURCE: {facts: {OUTPUT: true, LEAVES_NETWORK: true,  adds: [UNTRUSTED]}}
```

Declaring a profile P also declares a property P with default false, true for every module whose facts, those taken
from its tool included, name P. A
module's `adds` is the union of the `adds` stated for it. Every declared property has a value for every module, from the
statements of its facts or, if none states it, from the default in the property's declaration. A configuration in which a
declared property has no value for some module, or in which two statements give one property different values for one
module, is invalid, and the system does not start. For a module whose tool is named by an argument (2.7), the facts taken
from the tool are those of the tool named in each call.

**2.6 Tool.** A tool is a profile together with a client: a named set of facts, as a profile is (2.5), and the means by which
the framework reaches one external system, such as a mail server, a database, an LLM server or the user's browser. A tool is
declared in the setup (2.7) by a name and three keys. `client`, required, is how the framework reaches the system:
either `kind`, the framework's code for one kind of external system, with the configuration that code needs, or
`endpoint`, the configuration file of an LLM server, with `prompts`, the project's file of prompts: for each method a
module calls on the tool, the text sent to the LLM, in which `{name}` is replaced by the argument of that name, and
whether the answer is read as text or as JSON. A path in a client is relative to the project's folder. `facts` and
`profiles` are as in 2.5. No check reads `client`; the
checks read only `facts` and `profiles`. `adds` is stated for every tool, an empty list written `[]`, and a tool whose
`adds` is empty returns values that carry no label by default. Every module that uses the tool has the tool's facts. Declaring a tool T also declares a
property T with default false, true for every module that uses T.

**2.7 Setup.** A setup is a file, `setups/<name>.yaml`, with two sections: `tools`, the tools declared as in 2.6, and
`modules`, whose keys are module names or patterns over module names (`*` matches any characters). A setup may also
have `note`, a text the page displays under its title, such as a statement that the setup's data is made-up; no check
reads it. A setup also has the line `log` (required), which states the facts of the engine operation `log` (2.12)
as the line of a module states a module's: `log: {tool: BROWSER, facts: {DESTRUCTIVE: false}}` where the page is the
tool BROWSER, so that `log` has that tool's facts and an exception that selects the tool selects `log`; or facts of
its own, `log: {facts: {OUTPUT: true, LEAVES_NETWORK: false, DESTRUCTIVE: false}}`. An example of a setup:

```yaml
tools:
  LOCAL_DB:   {client: {kind: postgres, host: db1.internal},      facts: {OUTPUT: true, LEAVES_NETWORK: false, adds: [CONFIDENTIAL]}}
  REMOTE_DB:  {client: {kind: postgres, host: db.vendor.example}, facts: {OUTPUT: true, LEAVES_NETWORK: true,  adds: [CONFIDENTIAL]}}
  LLM:        {client: {endpoint: configs/llm/local.yaml},        profiles: [LOCAL_SOURCE]}
  REMOTE_API: {client: {kind: http, host: api.example},           profiles: [REMOTE_SOURCE]}
  BROWSER:    {client: {kind: web},                                facts: {OUTPUT: true, LEAVES_NETWORK: false, adds: []}}
modules:
  db.*:             {tool: LOCAL_DB}
  api.post:         {tool: REMOTE_API}
  user.*:           {tool: BROWSER}
  vendor.summarize: {profiles: [REMOTE_SOURCE]}
  llm.summarize:    {tool: LLM, facts: {MADE_IN_HOUSE: true}}
```

A tool and a module's line take the same two keys for facts, with the same meaning:

| Key | On a tool | On a module's line | Meaning |
|---|---|---|---|
| `profiles` | optional | optional | A list of profile names; their facts apply. |
| `facts` | optional | optional | Property values and `adds`. |
| `client` | required | not permitted | How the framework reaches the external system. |
| `tool` | not permitted | optional | The tool the module uses. `from_argument` with `one_of`, defined below, may be written in its place. |

So a profile, or a fact, may be stated for a tool, for a module that uses a tool, for a module that uses none, and for
a pattern that matches many modules. `client` is stated only for a tool; a module that takes `ctx` communicates through the
tool its line names. The last line above is read when the configuration is loaded: `llm.summarize` uses the
tool LLM, has that tool's facts, and has the property MADE_IN_HOUSE with the value true. MADE_IN_HOUSE is a property of
the module (2.4), not a label on its results.

`vendor.summarize` takes no `ctx`; its code calls an LLM server of its own. Its line states the facts of that server, and
the rules then treat it as they treat `api.post`. A module matched by a line keyed by its exact name takes that line;
otherwise it takes the one pattern that matches it, and a module matched by two patterns and no exact name makes the
configuration invalid. One setup is in force for a run, named when the run is started. A module that takes `ctx` and no line of the setup in force gives
a tool makes the configuration invalid; so does a line that gives a tool to a module that takes no `ctx`, and a line
keyed by an exact name that no module has. A pattern that matches no module is stated in the report (section 9) as
a warning: it changes no call, and the result, and so its acceptance, does not depend on it. A line may name the tool by an argument of the module and list the tools the
argument may name:

```yaml
  db.*: {from_argument: db_name, one_of: [LOCAL_DB, REMOTE_DB]}
```

At each call the tool is the one the argument names; a call whose argument names a tool not listed is refused. The
argument is checked as any other (section 4).

Adding a module to a project: write the function and its declaration. If the module takes `ctx` and a pattern under
`modules` matches it, nothing further is required; if it takes `ctx` and no pattern matches it, one line naming its tool
is added; if it takes no `ctx`, nothing further is required, unless its code reaches an external system, when its
declaration or a line states that system's facts. Its property values follow from its facts and the defaults. Two
statements that disagree are reported when the configuration is loaded, naming the module and the property, and nothing
runs. Adding a tool: one line under `tools`, with its `client` and its facts, `adds` among them (2.6).

**2.8 Selection.** A selection denotes a set of modules. It is one of: a list of module names; `any`, every module;
`{where: {P: v, ...}}`, every module for which each listed property P has the value v; `{where: [{...}, {...}]}`, the
union of the sets denoted by the listed maps. A selection may name the engine operations `decide` and `log` (2.12)
as if they were modules; `where` selects `log` by the facts its line of the setup states (2.7), and never `decide`,
which has none; `any` does not include engine operations.

**2.9 Label.** A label is a named boolean attached to a part of a value. A label not attached to a part has the value
false on that part. Labels are attached to scalar parts (2.1); a dictionary or list part carries L with the value true
if some scalar part within it does, and a request (P4) naming a dictionary or list part applies to every scalar part
within it. A value carries L if some part of it carries L with the value true. Labels are declared in `system.yaml`:

```yaml
labels:
  UNTRUSTED:    {spreads: true,  may_remove: [code.verify]}
  CONFIDENTIAL: {spreads: true}
  VERIFIED:     {spreads: false, may_add: [code.verify]}
```

`spreads` (boolean, required) selects between propagation rules P2 and P3. `may_add` and `may_remove` are selections, each
denoting no module when absent: the modules permitted to request that the label be attached, and the modules permitted
to request that it be removed. A module whose declaration states a request (L, true) and is not selected by `L.may_add`,
or (L, false) and is not selected by `L.may_remove`, makes the configuration invalid.

Label propagation is whether a module's result carries a label when an argument of the call carried it. It is
configured for each label, with `spreads`. Which modules may request that a label be attached or removed is configured
with `may_add` and `may_remove`. A label on a value that is passed, stored or moved by an engine operation stays on it
in every case (P3, P5).

**2.10 Rule.** A rule states a call that must never occur. A rules file has the keys `imports` (rules files whose rules
it imports, each by a path relative to the importing file), `properties` (the properties its rules read, declared as in 2.4) and `rules`:

```yaml
properties:
  OUTPUT:         {}
  LEAVES_NETWORK: {default: false}
  DESTRUCTIVE:    {}
rules:
  - id: NO_UNTRUSTED_DATA_TO_OUTPUT
    never: {call: {where: {OUTPUT: true}}, carrying: {UNTRUSTED: true}}
```

`call` is a selection. `carrying`, optional, is a label and a value, written `{L: v}`; a rule without `carrying` forbids
every call to the selected modules. A branch in a workflow (2.12) is a call of the engine operation `decide` with the
value it tests. No rule is checked when a workflow takes an error route (2.12): it is a route, as `next: STATE` is. A project's rules are in its `rules.yaml`. An imported rule cannot be modified or removed by the importing file.
Example, a rules file `rules/default.yaml` of five rules, imported by the projects of sections 6 to 8 with `imports:
[../rules/default.yaml]`: NOTHING_LEAVES_NETWORK (`call: {where:
{LEAVES_NETWORK: true}}`), NO_DECISIONS_FROM_UNTRUSTED (`call: [decide], carrying: {UNTRUSTED: true}`),
NO_UNTRUSTED_DATA_TO_OUTPUT (above), NO_DESTRUCTIVE_ACTIONS (`call: {where: {DESTRUCTIVE: true}}`), and
NO_PERSONAL_DATA_OUTPUT (`call: {where: {OUTPUT: true}}, carrying: {PERSONAL: true}`), which its comment calls
optional: a project that does not want it imports a rules file without it (the examples treat it as required). Every project that imports the file declares the labels its rules name. Example, a rule added by a
project:

```yaml
rules:
  - id: NO_CONFIDENTIAL_OUT
    never: {call: {where: {LEAVES_NETWORK: true}}, carrying: {CONFIDENTIAL: true}}
```

**2.11 Exception.** An exception permits calls that one rule forbids. Exceptions are in the project's `exceptions.yaml`, under the key `exceptions`:

```yaml
exceptions:
  - {id: REMOTE_SOURCES_APPROVED, rule: NOTHING_LEAVES_NETWORK,      except: {call: {where: {REMOTE_SOURCE: true}}}}
  - {id: SUMMARIES_BY_LLM,        rule: NO_UNTRUSTED_DATA_TO_OUTPUT, except: {call: {where: {LLM: true}}}}
  - {id: VERIFIED_TO_API,         rule: NO_CONFIDENTIAL_OUT,         except: {call: [api.post], carrying: {VERIFIED: true}}}
```

`rule` names the rule. `except.call` is a selection. `except.carrying`, optional, is a label and a value; it is permitted
only on an exception to a rule that has `carrying`; its effect is stated in section 4, C2. `except.arguments`, optional, is a map: for each
argument it names, the labels that argument must carry, each with a value. The exception then permits the call when
every named argument carries its labels, whatever any other argument carries. It may be written on
an exception to any rule, and is not written with `carrying`:

```yaml
  - {id: PERSONAL_TO_APPROVED_EX, rule: NO_PERSONAL_DATA_OUTPUT, except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}}}
  - {id: REVIEWED_TO_APPROVED_EX, rule: NO_PERSONAL_DATA_OUTPUT, except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}, body: {REVIEWED: true}}}}
```

The first permits a call of `mail.send.message` whose argument `to` carries APPROVED_RECIPIENT; the second, one whose
`to` carries APPROVED_RECIPIENT and whose `body` carries REVIEWED. An argument is named whole: a field within it is
not. An argument the call does not pass, and an argument with no scalar part, do not satisfy the exception. Each of these makes the configuration invalid: a selected module that has no argument of a name written; a
selection that includes `decide` or `log`, or selects no module; a label that is not declared; `arguments` together
with `carrying`. The first exception selects by
the profile REMOTE_SOURCE, so it covers `api.post` and `vendor.summarize`.

**2.12 Workflow.** A workflow is a file, `workflows/<name>.yaml`, that describes a finite state machine: named states,
the calls each state makes, and the state that follows it. A workflow has no expressions and no operators. An argument
is a reference or a literal, and every computation is a call. A route is a sequence of states a run may enter, from
`start`.

```yaml
workflow: summarize
start: READ
on_error: FAILED
states:
  READ:
    steps:
      - {call: db.read, with: {id: 7}, save_as: record}
    next: {switch: "{vars.record.kind}", goto: {report: SUMMARIZE, note: DONE}}
  SUMMARIZE:
    steps:
      - {call: llm.summarize, with: {text: "{vars.record}"},     save_as: summary}
      - {call: code.verify,   with: {summary: "{vars.summary}"}, save_as: checked}
      - {call: api.post,      with: {body: "{vars.checked}"}}
    next: DONE
  DONE:   {end: true}
  FAILED: {end: true}
```

Keys of the file:

- `workflow`: the workflow's name, the same as the file's.
- `start`: the name of the first state.
- `states`: the states, each under its name. A state may not have the name of a key of the file or of a state. This one
  comparison ignores letter case: no state is named `next`, `NEXT`, `start` or `END`.
- `on_error`: the state in which a run continues after an error, defined below. Required.
- `max_states`: the number of state entries one run may make. Required when a route can enter a state twice. A run that
  would make more ends, the error `too_many_states` recorded (C4) and no route taken.

Keys of a state:

- `steps`: a list of calls, made in order. Each has `call`, a module or an engine operation; `with`, its arguments by
  name, checked against the function's parameters when the file is loaded; and optionally `save_as`, the name under which the result is saved. Optional.
- `next`: the state that follows, in one of the two forms below.
- `end: true`: the workflow ends after the state's steps. A state has `next` or `end`, not both.
- `on_error`: the state in which a run continues after an error of this state's steps or of its branch, in place of the
  workflow's. Optional.

Every workflow names its states, `start` and `on_error`, a workflow with no branch included: it has one state for its
steps and one that an error continues in (`examples/share_notes/workflows/share_summary.yaml`). A file of `workflow` and `steps` alone makes the configuration
invalid.

References. A saved result is referenced in a later step as `"{vars.name}"`, and a part of it as `"{vars.name.field}"`.
A reference is the whole of a quoted text: `"{vars.a}+{vars.b}"` and `"Dear {vars.name}"` are not references, and a text
that contains a reference and anything else makes the configuration invalid. An argument is a reference, or a value
written in the workflow file, whose parts may themselves be references. A referenced part stands for the referenced
value with its labels; a part written in the file carries no label. A saved name keeps its value until a later step
saves a result under the same name, and is read in every state of the run. A reference to a name that some route does not save before the reference is
reported by the pre-run check (section 9).

`next` has two forms:

- `next: STATE`. The named state follows.
- `next: {switch: VALUE, goto: {KEY: STATE, ...}}`. The state named for the key equal to the value follows. A key is a text,
  a number, `true` or `false`. This is a branch: one call of `decide` with the value. `else: STATE`, optional, names the
  state that follows when no key is equal; without it, a value equal to no key is an error of type `no_match`.
  YAML reads the unquoted words `yes`, `no`, `on` and `off` as `true` or `false`; a key meant as one of those texts is
  written in quotes.

```yaml
    next: {switch: "{vars.count.too_many}", goto: {true: STOP, false: CONTINUE}}
```

Errors. An error is a value (P6) with the fields `type` and `message`, the fields `state` and `call`, which name where
it arose, and optionally `detail`, which a module may fill, for example with an error of the programming language.
`on_error` names the one state in which the run continues after an error: `on_error: FAILED`. It is required for the
workflow and optional for a state; a state's own is taken in place of the workflow's. It names a state, not a state
for each type of error: the file fixes where an error continues, so no error and no value chooses
among states, and no rule is checked when the route is taken. The state it names is passed the error as `"{error}"`,
with the labels P6 gives it, and every call there is checked like any other; a workflow that must act differently on
different types of error branches there on `"{error.type}"`, an ordinary branch (C1 applies). An error raised in
the state its own route names ends the run, recorded. A refusal (C2, C3, or a tool not listed, 2.7) is an
error of type `refused`. What a refusal does to the run is stated once for the project, in `system.yaml`:
`exit_run_on_refusal: never`, the default, and the refusal takes the error route as any error; `always`, and the run
ends, recorded, with no route taken; or a list of names of modules, `decide` and `log`, whose refusal ends the run. A name in the list that is no module, `decide` or `log` makes the configuration invalid. The steps of the state an error routes to reference it as `"{error}"`.

Engine operations are defined by the framework, each with its arguments:

- `get` (`value`, `path`): a part of a value. Without `path`, the value itself, which assembles one value from parts.
- `pick` (`value`, `fields`): a projection onto named fields.
- `equals` (`a`, `b`): whether two values are equal.
- `decide`: a branch. Written as `next`, never as a step.
- `log` (`error`): keeps, for the page to display, what a module wrote in an error: its message and its detail.
  Never written in a workflow: the framework calls it when a module raises an error or fails, and the call is
  checked as any other (section 4), with the facts the setup states for `log` (2.7). Refused, the refusal is the
  error, of type `refused` as for any refused call, and the page displays the type of the module's error and the
  rule, and not what the module wrote. An error whose message the framework wrote (a
  refused call, a branch with no match) names no value and is no call of `log` (what is sent to a log is not exempt from the rules).
- `labels_of` (`value`): the labels of each part, returned as a value with the same paths.
- `store.put` (`store`, `value`) and `store.get` (`store`): write and read a stored value.
- `store.add` (`store`, `items`): adds to a stored list the items not already in it, by content, and returns `added`,
  the items added, and `present`, the items already in it.
- `store.remove` (`store`, `items`): removes the items present, by content, and returns `removed`, the items removed,
  and `absent`, the rest.

The store is the framework's persistent state: named values that survive runs. Each stored name is declared in
`system.yaml` with the value it has before anything is put to it:

```yaml
stores:
  NOTES: []
  COUNT: 0
```

More than one run may be in progress at once. Runs share the store and nothing else, and each store operation is
applied whole before the next begins.

**2.13 User interface.** The user interacts with a project through a web page. The framework's client of kind `web`
serves the page from the machine the project runs on, to that machine only unless the user names another address
when starting it, at an address that includes a token printed when the project starts. A tool with that client is declared as any other. The client supplies four elements. A
project's module displays one by calling it through `ctx`, and the workflow calls that module as any other; a project
may write several modules on one element, each with its own declaration. The examples name theirs `user.show`,
`user.describe`, `user.enter` and `user.choose`.

- **show** (`report`, `marks`, `style`): displays a value and does not wait. `marks` is the result of `labels_of`, or
  `{}`. Each part is displayed with the words `ui.yaml` gives for each of its labels, in the colour of the first of
  them that `ui.yaml` lists. `style`, optional, names a layout in `ui.yaml`. The client has three layouts:
  `one_part_per_line`; `table`, which draws a list of dictionaries as rows; and `passages`, for text
  displayed in part and opened by the user. `passages` draws a list of groups, each a dictionary: its `passages` is
  a list of dictionaries, each with `pieces`, a list of `{text, level, quoted, first}`; every other value of a group
  is its heading, and of a passage its caption. A group is one box, in the colour of its labels, with the labels
  once. A passage displays its pieces whose `first` is true, with a mark the page writes where pieces are left
  out, and all of its pieces when the user opens it; a piece is coloured by its `level`, 0 to 3, and underlined when
  `quoted` is true. Every text is escaped. A value that has not this form, and a group whose parts do not all
  carry the same labels, is drawn as any value, each part with its labels. A module may call show with `update`:
  the value is then news of what a run is doing, displayed only when `ui.yaml` says `show_updates: true`; the call
  is checked and recorded either way.
- **describe** (`prompt`): the user types free text and presses the button. Returns the text.
- **enter** (`prompt`, `fields`): the user types values in a fixed format, one field per name, each with a type the
  client knows: `text`, or `email_list`, one address per line. The client parses each field when the button is
  pressed; a value it cannot parse keeps the element open, with what is wrong stated beside what was typed. Returns the parsed values under the field names.
- **choose** (`items`, `prompt`, `already`): the user ticks items of a list and presses the button. Returns the ticked
  items. `already`, optional, is a list of items chosen before: each is displayed ticked, cannot be changed, and is
  not returned.

The page displays describe and enter differently, as free text and as fixed-format input. What may be done with the
value either returns is decided by its labels and the rules, as for any value; in `examples/deliveries`, what the user enters
in the box of `user.enter_senders` is passed to no LLM. An element other than show ends when the user presses its button or closes it; a closed
element returns an empty text or an empty list.

`ui.yaml` is required when a tool has a client of kind `web`. It has four required keys and four optional (`show_updates`, above, among them), and `labels` has an
entry for every declared label. `labels` gives, for each label, the words and the colour displayed, the colour written `"#rrggbb"`. `styles` gives the named
layouts, and `default_style` names the one used when a show names none. `panes` lists the panes displayed beside the
elements, each a title and the stored names whose values or sizes it displays; the first pane, which the framework
supplies, displays each run in progress with its workflow and state, the last calls of each run by state and name,
each error of the run with its type and its message, and the number of calls of each module. The page
also displays, on the tab "Rules and exceptions", the result of the pre-run check, which is accepted at the terminal and not on the page (section 9): the acceptance, each rule with its file and line, each exception with the calls it permits, and the calls refused in the runs since the program started; the states of each
workflow, and for each workflow the control that starts a run of it, in the order the optional key `workflow_order`
gives, each with the `description` its file states, an optional text no check reads. With the optional key `state_tab: true`, the page
has a tab that lists every stored value and every value the steps of each run saved, each displayed with its labels
as show displays a value; with `reset_control: true` as well, that tab has a control that deletes the files the
system keeps (the stored values, the acceptance, the logs), refused while a run is in progress. The page has a tab
that lists every call of each run and each error with its type, its message and its detail, and beside each run in
progress a control that stops it: the run ends before its next step, and an element that waits for the user is closed.

## 3. Label propagation

- **P1, defaults.** Every part of a value returned by a module carries each label in the module's `adds`.
- **P2, propagation.** For each label L with `spreads: true`: if any part of any argument of a call of a module carries L, every part
  of the result carries L.
- **P3, engine operations.** `get`, `pick`, `store.put`, `store.get`, `store.add` and `store.remove` move parts unchanged and preserve all
  their labels. `equals` computes a new value, which carries each label with `spreads: true` carried by any operand; `decide`
  returns no value.
  The value `labels_of` returns carries no label. A label with `spreads: false` is attached to a part only by P1 or P4.
- **P4, requests.** A request (path, L, v) names a part of the result; the change applies to that part and every part
  within it. A request is applied if the module's declaration states (L, v), for any part or for the field the part is in; by 2.9 a declared request is one the labels
  permit. A request the declaration does not state, or whose path names no part of the result, is a defect of the module
  and refuses the call (section 4, C3). Requests are applied after P1 and P2; a module whose `adds` lists L and whose
  declaration states (L, false) may therefore return a result without L. P4 is the only way a scalar part of a module's result lacks a
  propagating label that an argument carried, and it needs three things at once: `L.may_remove` selects the module (2.9),
  the module's declaration states (L, false) (2.3), and the module returns the request for that part.
- **P5, store.** A stored value keeps its labels and is returned with them.
- **P6, errors.** An error a module raises, and the framework's refusal of a call, is a value. It carries the module's
  `adds` and, by P2, every propagating label carried by any argument of the call. No request applies to it. The error `no_match` carries the
  propagating labels of the value the branch tested; `too_many_states` carries none. The framework
  records an error's type and labels (C4) and never its content; the content of an error reaches a screen, a file or a
  server only as the argument of a module call written in the workflow, checked by C2.

Example, with the declarations of section 2, every module stating `DESTRUCTIVE: false`.
`db.read`, using LOCAL_DB, returns a record; every part carries CONFIDENTIAL
(P1). `llm.summarize`, using LLM, is passed the record and returns a summary; the summary carries UNTRUSTED (P1) and
CONFIDENTIAL (P2). `code.verify`, which takes no `ctx` and uses no tool, is passed the summary and returns a copy with
the requests (whole value, UNTRUSTED, false) and (whole value, VERIFIED, true); its declaration states both, so both are
applied (P4), and the copy carries CONFIDENTIAL (P2) and VERIFIED. `api.post`, using REMOTE_API, may be passed the copy: NOTHING_LEAVES_NETWORK is
permitted by REMOTE_SOURCES_APPROVED, and NO_CONFIDENTIAL_OUT by VERIFIED_TO_API (section 4). `api.post` may not be passed
the summary: no exception to NO_CONFIDENTIAL_OUT applies to a part that does not carry VERIFIED.

## 4. The call check

Before a module M, or the engine operation `decide` or `log`, is called with arguments A1 to An, under the setup
in force:

- **C1.** Determine, for each scalar part of each argument, the set of labels it carries with the value true.
- **C2.** For each rule R whose `call` selects M: if R has no `carrying`, the call is forbidden by R; otherwise the call is
  forbidden by R if some scalar part of some argument carries R's label with R's value. An exception E to R whose `call` selects M permits
  the call if E has no `carrying`; if E has `arguments`, it permits the call when each argument it names
  has a scalar part and every scalar part of that argument carries each label stated for it, with the value stated
  (an argument the call does not pass, or one with no scalar part, such as an empty list, does not satisfy it: a
  module given nothing may act on a value of its own);
  if E has `carrying`, it covers each
  scalar part that forbade the call and carries E's label with E's value. A call forbidden by R is permitted if some such exception permits it, or if every
  scalar part that forbade the call is covered by some such exception: two exceptions may each cover some of the
  parts. A call forbidden by some rule and not
  permitted is refused. The refusal names M, the
  argument, the label and R, and never the value.
- **C3.** M runs. The labels of its result are computed by P1, P2 and P4. If M returns a request its declaration does not
  state, or whose path names no part of the result, the result is discarded, no label is changed, and the call is
  refused (section 10). If M raises an error, or the
  call is refused, the error is a value with the labels P6 gives it, and the workflow takes its error route (2.12). No
  rule is checked when the route is taken; each call the state it leads to makes is checked as any other.
- **C4.** The access log, a file the framework writes, records M, the labels of each argument and of the result, each
  applied request and, for an error or a refusal, its type and labels. It contains names and labels, never a value.

The check reads the module's declaration, the configuration files and the setup. It does not read the function's body.

## 5. Configuration files

| Defines | File | Example |
|---|---|---|
| Labels: `spreads`, `may_add`, `may_remove` | `system.yaml`, `labels:` | `UNTRUSTED: {spreads: true, may_remove: [code.verify]}` |
| Properties and their defaults | `properties:` in a rules file (those its rules read) or in `system.yaml` (a project's own) | `MADE_IN_HOUSE: {default: false}` |
| Profiles: named sets of facts | `system.yaml`, `profiles:` | `REMOTE_SOURCE: {facts: {OUTPUT: true, LEAVES_NETWORK: true, adds: [UNTRUSTED]}}` |
| Tools: configuration and facts | `setups/<name>.yaml`, `tools:` | `LOCAL_DB: {client: {kind: postgres, host: db1.internal}, facts: {OUTPUT: true, LEAVES_NETWORK: false, adds: [CONFIDENTIAL]}}` |
| Each module's tool, or facts, by name or pattern | `setups/<name>.yaml`, `modules:` | `db.*: {tool: LOCAL_DB}`; `vendor.summarize: {profiles: [REMOTE_SOURCE]}` |
| Facts a module states for itself, and the requests it may make | its declaration | `@chaperone.module("code.verify", properties={"DESTRUCTIVE": False}, requests={"UNTRUSTED": False, "VERIFIED": True})` |
| Rules, and the properties they read | the project's `rules.yaml`, and any rules file it imports | `never: {call: {where: {OUTPUT: true}}, carrying: {UNTRUSTED: true}}` |
| Exceptions | `exceptions.yaml` | `except: {call: [api.post], carrying: {VERIFIED: true}}`; `except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}}` |
| Which fields of a value a module is passed | the workflow, `pick` | `{call: pick, with: {value: "{vars.record}", fields: [id, title]}}` |
| Which labels the user is shown | the workflow, `labels_of` | `{call: labels_of, with: {value: "{vars.summary}"}}` |
| Stored names and their first values | `system.yaml`, `stores:` | `NOTES: []` |
| What a refused call does to the run | `system.yaml`, `exit_run_on_refusal:` | `never` (the default), `always`, `[log, calendar.write_entry]` |
| The words and colour shown for each label, the layouts, the panes | `ui.yaml` | `labels: {UNTRUSTED: {words: "untrusted", colour: red}}` |

The shipped files are not edited to change a setting: the user's changes are in one file of theirs, by default
`private/local.yaml`, which names a file by its path and the keys to change, merged over the file when it is read
(README, "Changing configurations"). Rules files, exceptions files and the files of the state folder are read
without it. A changed fact changes the result of the pre-run check (section 9), which is then accepted again.

## 6 to 8. The example projects

Sections 6 to 8 are the example projects `examples/ask_my_mail`, `examples/deliveries` and
`examples/safe_rag`, and a fourth, `examples/share_notes`: their files, and the README in each folder. This file has
no text of its own for them.

## 9. Pre-run check and report

- **Configured exception:** an exception written in the project's `exceptions.yaml` (2.11).
- **User-approved exception:** an exception the user adds from the screen of the pre-run report. It is in force as a
  configured exception is, and is kept outside the configuration, in `user_exceptions.yaml` in the state folder of
  the setup.
- **Possible violation:** the calls that one rule forbids and no exception, configured or user-approved, permits,
  as the pre-run check finds them. "Possible", because the check runs nothing: it lists a call when it cannot show
  that the rule does not forbid it.
- **Acknowledged risk:** a call of a possible violation that the user accepted at a start with no exception. Nothing permits the call: the program starts, and a run refuses the call when it is made (section 4).
  Kept outside the configuration, in `acknowledged_risks.yaml` in the state folder of the setup, one line for each
  rule and call; deleting a line ends the acceptance, and the next start asks again.

The check of section 4 can be evaluated before a run, over the workflow files, under one setup. Labels are propagated
by P1 to P6 through every state of every workflow until no label is added; what is known of each saved name and of
each stored name only accumulates, so this ends. The following hold in it:

- A declared request is assumed applied, to the whole result or to the field its declaration names.
- A stored name carries the union of the labels of every value any workflow puts to it.
- What is known of a part before a run is the labels it carries on every run and the labels it may carry. A rule
  forbids a call if a part may carry its label. An exception with `carrying` covers such a part only if the part
  carries the exception's label on every run (C2); one with `arguments` permits only if every part of each named
  argument carries each label on every run. A label that a part carries only because it spread is one the part may
  carry, so an exception that asks for it is reported as not permitting the call, though a run may permit it. The
  check cannot tell that a value a module returns will have no scalar part: a call whose named argument is such a
  value is not listed before the run and is refused when it is made. In this case the run refuses a call the result
  did not list; it never permits one. Where the check cannot tell, it reports the call as refused.
- A module whose tool is named by an argument is taken to use the tool named by an argument given as a literal in the
  workflow, otherwise each tool listed for it.

The result is:

- the calls that would be refused, each with a route to it, or the statement that no route reaches its state;
- for each call that is not refused only because a request is assumed applied, the modules whose requests it depends on;
- every reference to a name that some route does not save first (2.12);
- a warning for each pattern of the setup that matches no module (2.7);
- for each exception, the calls it permits, each with its workflow and state; an exception that permits no call is
  named;
- every module's facts.

When the program is started it prints, at the terminal, every rule as written with the exceptions to it. When the current result is not an accepted one, the user answers each rule, one rule at a time (accept its exceptions; more details: the calls each exception permits; or skip, and the report goes on to the next rule), and gives a name; the acceptance of the whole result is then recorded in a file the framework keeps. With a rule skipped, nothing is recorded and the program does not start. For a possible violation, the rule's block also displays the exception that would permit its calls, which names exactly the modules called (`{call: [calendar.write_entry]}`; id `USER_<RULE>_EX`), and the user chooses: accept, which adds it as a user-approved exception; or acknowledge the risk, which adds no exception, so that the calls stay forbidden and a run refuses each when it is made (an acknowledged risk). The user-approved exceptions are written to `user_exceptions.yaml` in the state folder when the name is given, are read at every start as the exceptions of `exceptions.yaml` are, and are withdrawn by deleting a line; no file of the configuration is changed. The page displays the result and accepts nothing. The command `accept --by NAME` records the same acceptance without the questions, for a script. A result that lists a refused
call is accepted only while each such call is an acknowledged risk; the command `accept` acknowledges none. A run under a setup starts only if the current result for
that setup equals an accepted one. A refused call of an accepted result does to a run what any refusal does (2.12): the error route, or the end of the run where `exit_run_on_refusal` says so. The result includes each rule and each exception as written, so a rule or an exception that is changed is accepted again, even when it permits the same calls. A change to the configuration that leaves the result the same, such as a comment,
needs no new acceptance.

The report of the check states the counts first, then each refused call. On request it also lists the calls under
each exception and the source of each module's facts, and counts the modules that took each default.

## 10. Limits

- Facts are statements. The check reads a module's declaration and the configuration files, not the function's body,
  the tool's address or the tool's behaviour. A module whose code communicates with an external system and whose facts do
  not say so, or that misuses a permission in `may_add` or `may_remove`, is not detected. The modules selected by any
  `may_remove` or `may_add`, and the modules that take no `ctx` yet communicate outside the program, are the modules
  whose code must be reviewed.
- A request a module makes outside its declaration is a defect of the module, detected when the module returns (C3). No
  label is changed and nothing receives the result; work the module did through its tool before returning has been done.
  The call itself was permitted by C2. With declarations that match the code, the case does not arise.
- Argument types are not checked by the framework. A value of the wrong shape passed to a module is detected by the
  module's code.
- The configuration is read when a run starts; a change to it takes effect in the next run,
  after the pre-run result for the changed configuration is accepted (section 9). A run does not continue after the
  process that started it ends; the store does. A task that takes days, such as a refund that waits for the post, is written as several
  short workflows: each reads the task's state from the store, does one part, writes the state and ends. What starts the
  next workflow, a timer or an arriving message, is outside the framework. A single run that waits for days is not
  supported.
- The browser is trusted. The page escapes every value it displays and runs no script taken from a value; a defect
  there would let a displayed value act as the user. The token in the address is the only check of who the user is; served to more than
  the machine itself, it travels unencrypted.
- A value with no scalar part, an empty list or dictionary, carries no label (2.9), and the pre-run check cannot
  tell that a value will be empty. An exception's `arguments` is not satisfied by such an argument, so the call is
  refused in the run though the result did not list it. A rule whose `carrying` has the value false, which forbids a
  call when some part lacks a label, does not forbid a call passed an empty value: no part lacks the label. No
  shipped rule is written so.
- Labels have no expiry. A label on a stored value remains until a module that `may_remove` permits removes
  it or the value is removed from the store.
