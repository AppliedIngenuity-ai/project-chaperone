# Share notes

*Part of [Project Chaperone](../../README.md): a rule-enforcing harness for workflows, including ones in which an LLM
chooses the next step. Rules say what must never happen; every route of every workflow is checked against them before
a run, and every call is checked while it runs. [Install it](../../README.md#install) first; run every command below
from the folder `project-chaperone`.*

A note is found, an LLM summarizes it, the summary is shown to you, and then the workflow tries to e-mail it. The
e-mail is refused: the summary carries the labels PERSONAL and UNTRUSTED, and nothing permits sending either. The
smallest project that shows a refused call. Everything is made up, and nothing is sent.

## Run it

```
python -m chaperone run examples/share_notes
```

1. Before anything runs, the terminal goes through the five rules:
   - Rule 1, `NOTHING_LEAVES_NETWORK`: type `1` to accept its exception (the mail server may be called).
   - Rules 3, `NO_UNTRUSTED_DATA_TO_OUTPUT`, and 5, `NO_PERSONAL_DATA_OUTPUT`: each reports a **possible
     violation**, the call `share_summary.MAIN: mail.send.message`. Type `4` at each: "keep this call forbidden, as
     an acknowledged risk". (`1` would add an exception that permits the e-mail.)
   - Then a name for the record.
2. Open the address it prints. Press Start beside `share_summary`.
3. The conversation shows the summary, "You fly on the 3rd.", marked "untrusted", "personal" and "written by the
   LLM". The run then ends in `FAILED`: the e-mail was refused before the mail server was called.
4. The refusal is listed on the tab "Rules and exceptions", under "Calls refused in the runs since the program
   started", and on the tab "Log".

To be asked again at the start, delete the folder `state/share_notes/demo`.

## How it works

One workflow, `share_summary`. Its steps are in the state `MAIN`; an error, the refused e-mail included, continues
in the state `FAILED`, which ends the run:

| Step | Call (tool) | What it returns | Its labels, and the check |
|---|---|---|---|
| 1 | `notes.search` (NOTES) | the note "Trip" | PERSONAL: the tool NOTES adds it |
| 2 | `llm.summarize` (LOCAL_LLM) | "You fly on the 3rd." | UNTRUSTED and LLM_WRITTEN, which the LLM's tool adds; PERSONAL, which spreads from the note |
| 3 | `labels_of` | the summary's labels, for the page | |
| 4 | `user.show` (BROWSER) | | Showing untrusted, personal data is forbidden by rules 3 and 5, and permitted on the page by `UNTRUSTED_TO_USER_EX` and `PERSONAL_TO_USER_EX` |
| 5 | `mail.send.message` (MAILBOX) | refused | Forbidden by rules 3 and 5 (a module that sends is output); no exception permits it. Rule 1 also forbids it (the mail server is off this machine), and `MAILBOX_APPROVED_EX` permits that part |

The pre-run check finds step 5 before anything runs: that is the possible violation at the start. Acknowledging the
risk lets the program start, and the run still refuses the call.

## Its files

| File | Holds |
|---|---|
| `system.yaml` | the three labels: UNTRUSTED, PERSONAL, LLM_WRITTEN |
| `setups/demo.yaml` | the four tools (NOTES, LOCAL_LLM, MAILBOX, BROWSER) and their facts; every one but the page answers from `demo/` |
| `rules.yaml` | imports the five shared rules, `../rules/default.yaml` |
| `exceptions.yaml` | the three exceptions |
| `workflows/share_summary.yaml` | the workflow |
| `modules.py` | the four modules |
| `ui.yaml` | the words and the colour the page shows for each label |
| `demo/` | the made-up answers: the note, the LLM's summary, the mail server's reply |
