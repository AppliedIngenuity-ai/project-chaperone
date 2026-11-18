# Exceptions, and the answers at the start

A rule says what must never happen. An exception permits some of the calls that one rule forbids. The full
definitions are in the [specification](specification.md), 2.10 and 2.11; the terms in [terminology.md](terminology.md).

## Two places an exception is written

- **In the project:** its `exceptions.yaml`. A change there is a change to the project, accepted again at the next
  start.
- **Approved at a start:** `user_exceptions.yaml` in the state folder, written when you answer `1` to a possible
  violation. No file of the project changes. It is in force as the project's exceptions are.

An exception selects the calls it permits:

| Form | Permits |
|---|---|
| `{call: [calendar.write_entry]}` | every call of the modules named |
| `{call: {where: {BROWSER: true}}}` | every call of the modules with that property (here, those that use the tool BROWSER) |
| `{call: [...], carrying: {CHECKED: true}}` | the parts of a call that carry the label; a call is permitted when each part the rule forbids is covered by such an exception |
| `{call: [...], arguments: {to: {APPROVED_RECIPIENT: true}}}` | a call whose named arguments carry the labels |

## The answers at the start

A rule with exceptions only: `[1] Accept`, `[2] More details`, `[3] Skip for now` (the program does not start).

A possible violation (calls a rule forbids and no exception permits):

| Answer | What it does |
|---|---|
| `[1] Permit` | adds the exception shown, `USER_<RULE>_EX`, naming the modules called, to `user_exceptions.yaml` |
| `[2] More details` | lists each call, and asks again |
| `[3] Skip for now` | the program does not start; nothing is recorded |
| `[4] Keep forbidden, as an acknowledged risk` | the program starts; a run refuses the call when it is made. One line in `acknowledged_risks.yaml` |

Nothing is written until every rule is answered and a name is given.

## The files in the state folder

`state/<project>/<setup>/` holds, for the start:

- `accepted.json`: each acceptance, with who, when, and the result it accepted. A start whose result is an accepted one
  asks nothing.
- `user_exceptions.yaml`: the exceptions you permitted.
- `acknowledged_risks.yaml`: the risks you acknowledged.

The same folder holds what the project stores and its logs.

## Changing an answer

- **An exception you permitted:** delete its line in `user_exceptions.yaml`.
- **A risk you acknowledged:** delete its line in `acknowledged_risks.yaml`.

Either way the result is no longer the accepted one, so the next start asks every rule again; the rule whose line you
deleted shows its possible violation, with the four answers.

- **Everything:** start over by deleting the folder. This also forgets what the project stored.

## Without the questions

`python -m chaperone accept <project> --by NAME` records the acceptance with no questions, for a script. It adds no
exception and acknowledges no risk: a result with a possible violation is accepted only when each of its calls was
acknowledged as a risk at an earlier start.
