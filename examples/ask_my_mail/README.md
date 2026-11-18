# Ask my mail

*Part of [Project Chaperone](../../README.md): a rule-enforcing harness for workflows, including ones in which an LLM
chooses the next step. Rules say what must never happen; every route of every workflow is checked against them before
a run, and every call is checked while it runs. [Install it](../../README.md#install) first; run every command below
from the folder `project-chaperone`.*

You type a keyword and a question. The mailbox is searched for the keyword, and an LLM answers the question from the
messages found. The answer is shown marked "untrusted" and "personal": the labels of the mail spread to what the LLM
wrote. Nothing checks the answer; the labels say what it is.

**Made-up only, for now.** Its one setup, `demo`, answers from files: the mailbox returns the same two
messages for any keyword, and the LLM always answers "The parcel 1Z999AA10123456784 is scheduled for Tuesday.". A
setup for your own mail is not built yet.

## Run it

```
python -m chaperone run examples/ask_my_mail
```

1. Before anything runs, the terminal goes through the five rules. Rules 1, 3 and 5 have exceptions: type `1` at
   each, then a name for the record.
2. Open the address it prints. Press Start beside `ask_my_mail`.
3. A box asks for a keyword; then the page asks for your question. The answer is shown with its labels.

To be asked again at the start, delete the folder `state/ask_my_mail/demo`.

## How it works

One workflow, `ask_my_mail`. Its steps are in the state `MAIN`; an error continues in the state `FAILED`, which
ends the run:

| Step | Call (tool) | What it returns | Its labels, and the check |
|---|---|---|---|
| 1 | `user.enter` (BROWSER) | the keyword | none: the page adds no label |
| 2 | `user.describe` (BROWSER) | the question | none |
| 3 | `mail.search` (MAILBOX) | the messages with the keyword | UNTRUSTED and PERSONAL: the tool MAILBOX adds them. Calling the mail server is forbidden by rule 1, `NOTHING_LEAVES_NETWORK`, and permitted by `MAILBOX_APPROVED_EX` |
| 4 | `llm.answer` (LOCAL_LLM) | the LLM's answer | UNTRUSTED, which the LLM's tool adds; PERSONAL, which spreads from the messages |
| 5 | `labels_of` | the answer's labels, for the page | |
| 6 | `user.show` (BROWSER) | | Showing it is forbidden by rules 3 and 5, and permitted by `SHOW_TO_USER_EX` and `PERSONAL_TO_USER_EX` |

`ERRORS_IN_LOG_EX` lets the page display what a module wrote in an error.

## Its files

| File | Holds |
|---|---|
| `system.yaml` | the two labels: UNTRUSTED, PERSONAL |
| `setups/demo.yaml` | the three tools (MAILBOX, LOCAL_LLM, BROWSER) and their facts; the mailbox and the LLM answer from `demo/` |
| `rules.yaml` | imports the five shared rules, `../rules/default.yaml` |
| `exceptions.yaml` | the four exceptions |
| `workflows/ask_my_mail.yaml` | the workflow |
| `modules.py` | the five modules |
| `ui.yaml` | the words and the colour the page shows for each label |
| `demo/` | the made-up answers: the mailbox's two messages, the LLM's one answer |
