# Terminology

*The short form of the definitions in the [specification](specification.md), section 2. Where the two differ, the
specification holds. The number after each term is its section there.*

## The parts of a project

| Term | Definition | Example |
|---|---|---|
| Project (2.2) | One system built with the framework: its configuration files and its modules. | `examples/deliveries` |
| User (2.2) | The person who writes the configuration files or uses the system while it runs. | |
| Module (2.3) | A Python function, with one line above it, its declaration, that gives the module's name and may state facts about the module. | `mail.read.search_senders` |
| Tool (2.6) | A name the setup declares for one external system: the facts stated for that system, and the means by which the framework reaches it (its client). Declaring a tool also declares a property of the same name, true for each module that uses the tool. | `MAILBOX`, a mail server; `LOCAL_LLM`, an LLM server; `BROWSER`, the user's web page; `{where: {MAILBOX: true}}` selects the modules that use `MAILBOX` |
| Setup (2.7) | A file that declares the tools and states which tool each module uses. One setup is in force for a run. | `setups/local.yaml` |
| Workflow (2.12) | A file that describes a finite state machine: named states, the calls each state makes, and the state that follows. | `workflows/update_calendar.yaml` |
| State (2.12) | A named part of a workflow: a list of steps, then the state that follows or the end of the run. Written with its workflow as `workflow.STATE`. | `update_calendar.FETCH` |
| Step (2.12) | One call in a state, with its arguments. | `{call: mail.read.search_senders, with: {terms: ...}}` |
| Run (2.12) | One execution of a workflow, from its first state to an end. | |
| Store (2.12) | Named values the framework keeps between runs. | `MONITORED_SENDERS` |

## Data, and what is stated about a module

| Term | Definition | Example |
|---|---|---|
| Value (2.1) | A scalar, a list or a dictionary that a module is passed or returns. | a message; a list of addresses |
| Label (2.9) | A named true-or-false mark attached to a value or to a part of it. A value marked with a label carries it. | `UNTRUSTED`, displayed as "untrusted" (the words are in `ui.yaml`) |
| `adds` (2.5) | The labels attached to everything a module returns. Stated for a tool, it holds for every module that uses the tool. | `MAILBOX` adds `UNTRUSTED` and `PERSONAL` |
| Propagating label (2.9) | A label that every result of a module carries when any argument of the call carried it. | `UNTRUSTED` |
| Label propagation (2.9) | Whether a module's result carries a label when an argument of the call carried it. Configured for each label, with `spreads`. Which modules may request that a label be added or removed is configured with `may_add` and `may_remove`. A label on a value that is passed, stored or moved by an engine operation always stays on it. | `UNTRUSTED: {spreads: true, may_remove: [check.delivery_values]}` |
| Label request (2.3) | A change of label that a module asks for when it returns its result: add one label, or remove one. It is applied only when `system.yaml` names that module for that label. | `string.clean` requests that `SANITIZED` be added |
| Property (2.4) | A named true-or-false statement about a module. | `LEAVES_NETWORK` |
| Facts (2.5) | Everything stated about one module: its property values and its `adds`. A module that uses a tool has the tool's facts. | `OUTPUT: false, LEAVES_NETWORK: true, adds: [UNTRUSTED]` |
| Profile (2.5) | A set of facts written once, under a name, and applied by that name. | `REMOTE_SOURCE` |

The three properties the shipped rules read (2.4):

| Property | True for a module when |
|---|---|
| `OUTPUT` | a value passed to the module is shown to a person or written where it stays: a page, a file, a calendar entry, a sent message. A module that is passed a value and returns another (an LLM, a search) is not `OUTPUT` for that. |
| `LEAVES_NETWORK` | a value passed to the module may be transmitted outside the user's network. |
| `DESTRUCTIVE` | the module may modify or delete data that persists outside the program. |

## Rules and checks

| Term | Definition | Example |
|---|---|---|
| Engine operation (2.12) | An operation the framework defines. It is not a module. | `get`, `store.put`, `decide`, `log` |
| Call (section 4) | One use of a module, or of the engine operation `decide` or `log`, with its arguments. | |
| `log` (2.12) | The engine operation that keeps, for the page to display, what a module wrote in an error. The framework calls it; the setup states its facts on the line `log`, and a rule or an exception selects it as it selects a module. Refused, it is an error of type `refused`, as any refused call. | `log: {tool: BROWSER, facts: {DESTRUCTIVE: false}}` |
| Selection (2.8) | A set of modules: a list of names; `any`; or `where`, every module with the listed property values. A list of maps under `where` is their union: a module with the values of any one map. | `{where: {OUTPUT: true}}`; `{where: [{OUTPUT: true}, {LEAVES_NETWORK: true}]}` |
| Policy | Something an organization does not allow its systems to do. Not a term of the system, and in no file: a policy is written as one or more rules. | "Our data does not leave our network." |
| Rule (2.10) | A statement of a call that must never occur: a selection and, optionally, a label that an argument carries. | `never: {call: {where: {OUTPUT: true}}, carrying: {UNTRUSTED: true}}` |
| Exception (2.11) | A statement that permits calls that one rule forbids. One limited to a label (`carrying`) covers the parts of the arguments that carry it; a call is permitted when every part that forbids it is covered by some exception. One with `arguments` in place of `carrying` permits the call when each argument it names carries the labels written for it. | `{id: SHOW_TO_USER_EX, rule: NO_UNTRUSTED_DATA_TO_OUTPUT, except: {call: [user.show]}}`; `except: {call: [mail.send.message], arguments: {to: {APPROVED_RECIPIENT: true}}}` |
| Branch (2.12) | A choice of the state that follows, made by comparing a value with the keys written in the workflow. It is a call of `decide` with that value. | `next: {switch: "{vars.kind}", goto: {scheduled: WRITE, other: DONE}}` |
| Error (2.12) | A value that states that a call failed, with a type (a name: letters, digits and `_`) and a message. It carries the module's `adds` and the propagating labels of the call's arguments. | a module raised `bad_values`; a line of a module's code failed; the framework refused the call |
| Route (2.12) | A sequence of states a run may enter. A state is followed by the state its `next` names, by the state a branch chooses, or, after an error, by its error route. | `FETCH`, `CLASSIFY`, `SET_ASIDE` |
| Error route (2.12) | The one state a workflow names, under `on_error`, to continue in after an error; a state may name its own. The file fixes it: no error chooses among states, and no rule is checked when it is taken. | `on_error: FAILED` |
| Configured exception (section 9) | An exception written in the project's `exceptions.yaml`. | |
| User-approved exception (section 9) | An exception the user adds at the terminal, when the program starts, for a possible violation. In force as a configured exception is; kept in `user_exceptions.yaml` in the state folder, outside the configuration. | |
| Possible violation (section 9) | The calls that one rule forbids and no exception permits, as the pre-run check finds them. The check runs nothing, so a possible violation may be one that no run produces. | |
| Acknowledged risk (section 9) | A call of a possible violation that the user accepted at the terminal with no exception. Nothing permits it: the program starts, and a run refuses the call when it is made. Kept in `acknowledged_risks.yaml` in the state folder; deleting its line makes the next start ask again. | `{rule: NO_PERSONAL_DATA_OUTPUT, call: "share_summary.MAIN: mail.send.message"}` |
| Call check (section 4) | The test made before each call: a call that a rule forbids and no exception permits is refused. | |
| Pre-run check (section 9) | The call check, made before anything runs, on every call of every workflow under one setup. | `python -m chaperone check examples/deliveries` |
| Acceptance (section 9) | The user's recorded yes to the whole result of the pre-run check, given at the terminal when the program is started: the exceptions to each rule are accepted one rule at a time, and each possible violation is given an exception or acknowledged as a risk. A run starts only when the current result is an accepted one. | |

## Where each is written

| File or folder | Holds |
|---|---|
| `system.yaml` | labels, with the modules that may request each; profiles; stores; a project's own properties; `exit_run_on_refusal`, what a refused call does to the run |
| `setups/<name>.yaml` | tools; each module's tool and facts; the facts of `log` |
| `rules.yaml`, and the rules files it imports | rules, and the properties they read |
| `exceptions.yaml` | exceptions |
| `workflows/<name>.yaml` | one workflow |
| `modules.py`, or the `.py` files of the folder `modules/` | the modules, each with its declaration |
| `ui.yaml` | the words and the colour the page displays for each label |
| `state/<project>/<setup>/` | what the system keeps for one setup: the stored values, the acceptance, the logs |
