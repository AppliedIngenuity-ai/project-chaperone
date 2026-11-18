# Deliveries

*Part of [Project Chaperone](../../README.md): a rule-enforcing harness for workflows, including ones in which an LLM
chooses the next step. Rules say what must never happen; every route of every workflow is checked against them before
a run, and every call is checked while it runs. [Install it](../../README.md#install) first; run every command below
from the folder `project-chaperone`.*

Reads parcel-delivery notices in your mail and keeps a calendar of them: one entry per parcel, moved when the day
changes, marked when delivered, with the notice's own tracking link when it fits a form you approved. An LLM reads
each notice; code checks every value it copies before anything reaches the calendar. In a conversation, an LLM
chooses the next step, but every address, tick and approval is made by you, in a box the LLM is not passed.

## Setups

| Setup | What is real | What it needs |
|---|---|---|
| `demo` | nothing: the mail, the LLM and the calendar answer from files in `demo/` | nothing |
| `demo_llm` | your LLM; the mail and the calendar are made up | an LLM server: [main README](../../README.md), "Step 1: your LLM" |
| `real_mail` | your mail and your LLM; the calendar is the file `state/deliveries/real_mail/deliveries.ics` | also your mail: "Step 2: your mail" |
| `real` | your mail, your LLM and Google Calendar | also Google: "Step 3: Google Calendar" |

## Run it

```
python -m chaperone run examples/deliveries
```

Before anything runs, the terminal lists the five rules, each with the exceptions written for this project: type `1`
at each, then a name for the record. Then open the address it prints. The page lists five workflows, each with a
Start button. To see each part:

1. **Start `assistant`**, the conversation. In `demo` the LLM is a script: whatever you type, it searches the mail
   for senders, then opens the box for addresses, then finishes. Tick the senders it found; type any address in the
   box.
2. **Start `discover_links`.** It reads the notices and offers the form of tracking link it found in them,
   `https://www.ups.example/track?tracknum=<tracking number>`, "in mail from ups.example". Tick it to approve it.
3. **Start `update_calendar`.** The conversation shows each notice as it is read:
   - the first, scheduled for 11/12 between 1:15 and 5:15 PM, shipped from Example Outfitters: an entry is written;
   - the second, "delayed", gives no date: it is set aside, for you to review in `assistant`;
   - the third, a sale, is not a delivery: nothing is written;
   - the fourth says delivered at 2:14 PM: the entry is marked delivered.

In `demo` the calendar is made up too, so no entry is written anywhere: the conversation shows each entry's values.
The made-up mailbox gives all four messages whatever senders you tick; a real mailbox gives only the ticked senders'
mail.
With `real_mail`, open `state/deliveries/real_mail/deliveries.ics` in a calendar program to see the entries, each
with its notice's tracking link.

`reset_entries` removes the entries this program made, so that `update_calendar` reads the mail again;
`reset_all` also forgets the senders and everything else stored. To start over completely, delete the folder
`state/deliveries/demo`.

## How it works

**`assistant`**, a conversation. At each turn the LLM is passed what you typed, what you were asked, and how many
items each list holds, never the addresses; it chooses one of the options offered now: search the mail for senders,
open the box for addresses, ask you something, show the senders found, stop monitoring a sender, review notices set
aside, approve tracking links, or finish. An option whose list is empty is not offered. A sender is monitored only
when you tick it.

**`update_calendar`**, unattended. For each new notice from a monitored sender:

1. The LLM sorts it: scheduled, delivered, or other.
2. The LLM copies the values, each exactly as printed: the tracking number, the date, the times, the shipper.
3. Code checks them (`check.delivery_values`): each value must be in the notice exactly as copied; the date is read
   by code and must be near the day received (up to 14 days after it for a scheduled delivery, up to 2 days before
   it for a delivered one); the tracking number must have the form written in the workflow. A notice that fails is set aside, with the reason.
4. The shipper's name and the subject are cleaned (`string.clean`: no tags, no web addresses, at most 60 and 80
   characters). They stay untrusted, and the entry says they were not checked.
5. The notice's own tracking link is written only if it fits a form you approved (`links.fill`).
6. The entry is written, or marked delivered. At the end, an entry whose day passed with no delivered notice is
   flagged.

**The rules and the exceptions that permit what this project does** (`exceptions.yaml`):

| Rule | Forbids here | Permitted by |
|---|---|---|
| `NOTHING_LEAVES_NETWORK` | calling the mail server and Google Calendar | `MAILBOX_APPROVED_EX`, `CALENDAR_APPROVED_EX` |
| `NO_DECISIONS_FROM_UNTRUSTED` | choosing the next state by the LLM's choice, or by what a notice says | `LLM_CHOOSES_STEP_EX` |
| `NO_UNTRUSTED_DATA_TO_OUTPUT` | showing mail and the LLM's words on the page; writing a notice's values on the calendar | `UNTRUSTED_TO_USER_EX` (the page); `CHECKED_TO_CALENDAR_EX` (values the check passed); `SHIPPER_TO_CALENDAR_EX` (the cleaned shipper's name and subject) |
| `NO_DESTRUCTIVE_ACTIONS` | writing, marking, flagging and removing this program's entries | `WRITE_ENTRY_EX`, `MARK_DELIVERED_EX`, `FLAG_UNCONFIRMED_EX`, `REMOVE_OWN_ENTRIES_EX` |
| `NO_PERSONAL_DATA_OUTPUT` | showing personal data on the page; writing it on the calendar | `PERSONAL_TO_USER_EX`, `PERSONAL_TO_CALENDAR_EX` |

**What a wrong or hostile LLM can do:** put a wrong entry on the calendar, when the wrong value is in the notice and
passes every check; leave a notice off the calendar; put up to 60 and 80 cleaned characters of a sender's text (the
shipper's name, the subject) on an entry, marked as not checked; search the mail for terms it chose, which are shown
before they run. It cannot monitor a sender, approve a link form, or write any other value that the check did not pass.

## Its files

| File | Holds |
|---|---|
| `system.yaml` | the five labels (UNTRUSTED, PERSONAL, LLM_WRITTEN, SANITIZED, CHECKED), with the modules that may add or remove each; the ten stored lists and counts |
| `setups/` | one file per setup: the tools (MAILBOX, LOCAL_LLM, GOOGLE_CALENDAR, SENDERS_FILE, CLOCK, BROWSER), their facts and which modules use each |
| `rules.yaml` | imports the five shared rules, `../rules/default.yaml` |
| `exceptions.yaml` | the twelve exceptions |
| `workflows/` | `assistant`, `discover_links`, `update_calendar`, `reset_entries`, `reset_all` |
| `modules/` | the modules, one file per tool; `code.py` holds those that use no tool (the checks, the link rules, the cleaning) |
| `prompts.yaml` | the three prompts: `next_step` (the conversation), `classify`, `copy_values` |
| `ui.yaml` | the words and colour of each label; the panes; the order of the workflows |
| `demo/` | the made-up answers of `demo`: four notices, the senders found, the LLM's answers, the calendar |
