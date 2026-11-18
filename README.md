# Project Chaperone

A rule-enforcing harness for workflows, including workflows that work like agents with tools: an LLM may choose the
next step, and every call it leads to is checked against rules you write. It adds two capabilities:

1. **A report before running** of every possible violation of the rules, over every route of every workflow. A person
   responsible for policy can review a system without reading its workflows.
2. **Enforcement at runtime**, through labels on the data (`UNTRUSTED`, `PERSONAL`, ...) and properties of the modules
   (`OUTPUT`, `LEAVES_NETWORK`, ...): a call that a rule forbids and no exception permits is refused before it is made.

A possible violation that is not worth designing away can be accepted as a risk: the call stays forbidden, and is
refused if a run attempts it.

Agents with tools are flexible, but a prompt cannot enforce a rule. Hard-coded workflows can be analyzed, because their
states are fixed, but the analysis is manual and is often never done. Chaperone makes that analysis a program's job,
and enforces the rules while the system runs.

Five shared rules ship with it, in `examples/rules/default.yaml`: no destructive actions, nothing leaves the network,
no personal or untrusted data output, and no decision made from untrusted data, such as text an LLM wrote. Each
project adds a few explicit exceptions. An LLM is optional: a module may call one, and a state may let one choose the
next state among those the workflow lists.

The paper: *(link to be added)*.

## Install

We recommend a Python virtual environment of its own ([how to make one](https://docs.python.org/3/tutorial/venv.html)).
It has been run with Python 3.13.7. Older versions are not tested, and `pyproject.toml` asks for 3.13 or newer.

In the environment:

```
git clone https://github.com/AppliedIngenuity-ai/project-chaperone.git
cd project-chaperone
pip install -e .
```

Check the install:

```
pip install -e ".[test]"
python -m pytest
```

Every test passes; 2 are skipped, because they ask a real LLM server (see "Step 1: your LLM" below).

## Run an example

From the folder `project-chaperone`:

```
python -m chaperone run examples/deliveries
```

1. The terminal lists the five rules, each with the exceptions the project wrote. Type `1` at each, then a name for
   the record. The `WARNING` above the rules is expected: the line `mail.send.*` of the setup marks where a module
   that sends mail would go, and no such module is written.
2. It prints the page's address, `http://127.0.0.1:<port>/?token=...`. Open it in a browser on this machine.
3. On the page, press Start beside `assistant` and type anything at each question: in `demo` the LLM is a script that searches the
   mail, opens a box for addresses (type any address), then finishes. Tick the senders it found. Then press Start
   beside `update_calendar`: the conversation shows each made-up message as it is read, and each entry's values.
   One message is set aside on purpose: it gives no date. [Its README](examples/deliveries/README.md) has each step.
4. Ctrl-C stops it. The next start asks nothing, until the result of the check changes: a rule, an exception, a
   workflow, or what is stated about a module.
5. To change your answers, start over: delete the folder `state/<project>/<setup>/`. This also forgets what the
   project stored, such as the senders. Under the setup `real`, do not delete it: Start `reset_all`, then press
   "Start from nothing" on the tab State, which keeps the record of your Google calendar (deleting the folder makes
   the next run create a second calendar). To change one answer, and for what each answer does:
   [docs/exceptions.md](docs/exceptions.md).

With no `--setup`, it runs the setup `demo`, on made-up data: nothing is read from your mail and nothing is written
anywhere but the folder `state/deliveries/demo`. To start over, delete that folder.

## The examples

Each folder holds the files of "How a project is written" (below); its README has the details.

### share_notes

The smallest: a note, an LLM's summary of it, shown to you, then e-mailed. The start reports the e-mail as a possible
violation of rules 3 and 5: answer `4` to each (an acknowledged risk), and the run refuses the e-mail when it is made.

- Try it: `python -m chaperone run examples/share_notes`
- Setup: `demo` (made-up answers, from `demo/`).
- Workflow: `share_summary`: summarizes a note, shows the summary, then tries to send it by e-mail.
- [Its README](examples/share_notes/README.md).

### ask_my_mail

A keyword and a question about your mail; the LLM's answer shown with its labels.

- Try it: `python -m chaperone run examples/ask_my_mail`
- Setup: `demo` (made-up mail and answers, from `demo/`).
- Workflow: `ask_my_mail`: searches the mail for the keyword, passes the messages and the question to the LLM, shows
  the answer.
- [Its README](examples/ask_my_mail/README.md).

### deliveries

Delivery notices from your mail, written to a calendar. An LLM chooses the next step of a conversation; addresses,
ticks and approvals are made in boxes the LLM is not passed.

- Try it: `python -m chaperone run examples/deliveries` ("Run an example", above)
- Setups: `demo`, `demo_llm`, `real_mail`, `real` (see "Use your own LLM, mail and calendar", below).
- Workflows: `assistant`, a conversation that chooses whose mail is monitored; `discover_links`, which finds the forms
  of those senders' tracking links and asks which to use; `update_calendar`, which reads their new mail and writes
  the calendar; `reset_entries`, which removes the entries and keeps the senders; `reset_all`, which removes
  everything stored.
- Also: `prompts.yaml` (the LLM's prompts), `modules/` (one file per tool), `demo/` (the made-up mail, LLM answers and
  calendar).
- [Its README](examples/deliveries/README.md).

### safe_rag

Questions about the Federalist Papers, or your own documents. An answer is shown only with quotes found in the
documents.

- Try it: `python -m chaperone run examples/safe_rag` (needs your LLM: "Step 1: your LLM", below)
- Setups: `local` (the papers and your LLM), `arxiv` (arXiv abstracts), `hosted` (Claude).
- Workflows: `single_pass`, an answer shown only when a quote is found word for word; `multi_pass`, with a second
  round when none is; `answer`, the simpler form; `typical_rag` and `just_ask_the_llm`, for comparison, with nothing
  checked.
- Also: `corpus/` (the 85 papers), `prompts.yaml`, `questions.yaml` and `questions_compound.yaml` (the measured
  questions), [results.md](examples/safe_rag/results.md) (the last measurement).
- [Its README](examples/safe_rag/README.md).

Every setup but `demo` needs your own LLM, mail or calendar: "Use your own LLM, mail and calendar", below, says how.

## Changing configurations

Do not edit the shipped files. Write your changes in `private/local.yaml` (git ignores `private/`): the path of a
shipped file, then only the keys you change, nested as that file nests them.

For example, to use an LLM server at another address:

```yaml
configs/llm/local.yaml:
  api_base: http://localhost:8080/v1
```

One file holds all your changes. A longer example:

```yaml
# Your LLM server, and longer answers
configs/llm/local.yaml:
  api_base: http://localhost:8080/v1
  max_tokens: 8192

# deliveries, setup real: 7 days of mail, your time zone, the calendar's name
examples/deliveries/setups/real.yaml:
  tools:
    MAILBOX:         {client: {days: 7, timezone: Europe/Berlin}}
    GOOGLE_CALENDAR: {client: {timezone: Europe/Berlin, calendar_name: "My deliveries"}}
    CLOCK:           {client: {timezone: Europe/Berlin}}

# safe_rag, setup local: your own notes
examples/safe_rag/setups/local.yaml:
  tools: {DOCS_DB: {client: {folder: ~/notes, overview: "My notes on work and reading."}}}
```

- The keys you can change are the ones the shipped file has: open it to see them.
- A key you do not write keeps its shipped value. A list you write replaces the shipped list whole.
- Your mail login is not here: it is in `private/env.sh` (step 2, below).
- Rules and exceptions cannot be changed this way.

**At the next start**, run from the folder `project-chaperone`, the program lists the changes it read: those to a
setup before the rules are asked, those to the LLM's file after them, just before the page's address. For the
longer example and `--setup demo_llm`, which reads the LLM's file and neither of the two setups:

```
Configuration: 2 settings changed by private/local.yaml:
    configs/llm/local.yaml: api_base (private/local.yaml:3)
    configs/llm/local.yaml: max_tokens (private/local.yaml:4)
NOTE private/local.yaml: examples/deliveries/setups/real.yaml was not read by this run; nothing in it changed.
NOTE private/local.yaml: examples/safe_rag/setups/local.yaml was not read by this run; nothing in it changed.
```

A key the shipped file does not have changes nothing; the start says so with a `WARNING`. `--defaults-only` reads
none of your changes, for one run; `--config FILE` reads another file in place of `private/local.yaml`.

## Use your own LLM, mail and calendar

Three steps, in this order. Each adds one real service to the `deliveries` example and ends with a command that shows
it working. A setup is accepted once, at its first start: type `1` at each rule it asks, then a name.

### Your private files

Your own logins live in the folder `private/`, which git never commits. Nothing in it is shipped; you make each file:

| File | What it does | How you get it |
|---|---|---|
| `private/env.sh` | Sets your mail login (and, for a hosted LLM, its key) in your shell, when you run `source private/env.sh`. The program reads the variables, never the file. | Copied once from the template `private.example/env.sh`, then filled in (step 2) |
| `private/local.yaml` | Your changes to the shipped settings: "Changing configurations", above. | Written by you |
| `private/google_client_secret.json` | Tells Google which program is asking for your calendar. | Downloaded from Google Cloud and renamed (step 3); no template |
| `private/google_token.json` | Your Google sign-in. | Written by `python -m chaperone signin` (step 3) |

Any other file whose name ends in `_priv` before its extension, such as `configs/llm/local_priv.yaml`, is never
committed either.

### Step 1: your LLM

**To change the LLM, change `configs/llm/local.yaml` in `private/local.yaml`** ("Changing configurations", above).
Every setup that uses your LLM reads that file, and each of its lines says what it is. The two that point at your
server, as shipped:

```yaml
model: "Qwen3.6-35B-A3B-MTP-UD-Q6_K_XL"     # the model's name, as your server calls it
api_base: http://localhost:8077/v1          # the server's address
```

The server must speak the OpenAI chat API.

**llama.cpp** (the measurements in `examples/safe_rag/results.md` used it, with Qwen3.6-35B-A3B):

```
llama-server -m <your model>.gguf --port 8077
```

Nothing to change: `local.yaml` already points at port 8077.

**Ollama** (not yet tried with this project):

```
ollama pull qwen3
```

Then, in `private/local.yaml`:

```yaml
configs/llm/local.yaml:
  model: "qwen3"
  api_base: http://localhost:11434/v1
```

**A server on another machine:** in your network, put its address in `api_base`. Outside your network (a provider,
a cloud machine), your data goes there: also state it for each setup you run with it (the tool is `LOCAL_LLM` in
`deliveries`, `LLM` in `safe_rag`):

```yaml
examples/deliveries/setups/real.yaml:
  tools: {LOCAL_LLM: {facts: {LEAVES_NETWORK: true}}}
```

The rule `NOTHING_LEAVES_NETWORK` then reports it at the next start; answer `1` to permit it. The check reads what
the setup states, not the address.

#### Check your LLM


```
CHAPERONE_LIVE_LLM=configs/llm/local.yaml python -m pytest tests/test_llm_client.py
```

- `17 passed`: the server answers, and the model chose the expected steps and read the made-up notices right.
- Any failure that says `no answer from ...`: nothing answers at the address in `api_base`. Is the server running?
  The other failures then follow from it.
- Failures, none of which says `no answer from`: the server answers, but the model's answers were not the expected
  ones. A smaller model may still work on the page, less well.

See it:

```
python -m chaperone run examples/deliveries --setup demo_llm
```

On the page, press Start beside `assistant` and type `track my UPS packages`. Your LLM chooses what happens next; the
mail and the calendar are still made up.

### Step 2: your mail

The program reads your inbox over IMAP, read-only: it never changes, moves or sends a message. (Gmail: a message you
archived is no longer in the inbox, and is not read.)

**Gmail:**

1. In your Google account, *Security* → turn on *2-Step Verification* (Google requires it for the next step).
2. Make an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords): any name,
   such as `chaperone`, then *Create*. Google shows 16 letters once; keep them. (A work or school account may not
   allow app passwords; its administrator decides.)
3. Copy the template (a `private/env.sh` you already have is left as it is):

   ```
   mkdir -p private && [ -e private/env.sh ] || cp private.example/env.sh private/env.sh
   ```

4. In `private/env.sh`, fill in the two lines marked `FILL IN`: your Gmail address, and the 16 letters without
   spaces. The Gmail server is already set.

If Gmail refuses the login, check that the password is the app password, not your account's.

**Proton Mail:**

1. Install and start [Proton Mail Bridge](https://proton.me/mail/bridge), signed in to your account. Its window shows
   the IMAP port and a password made for the bridge.
2. Copy the template:

   ```
   mkdir -p private && [ -e private/env.sh ] || cp private.example/env.sh private/env.sh
   ```

3. In `private/env.sh`, fill in the two lines marked `FILL IN`: your address, and the bridge's password. Then remove
   the `#` at the start of the five lines under `# Proton Mail Bridge`, and change the port there if the bridge shows
   another than 1143.

**Then, for either:**

1. Your time zone, if it is not New York's: the days and times on your calendar are taken there. In
   `private/local.yaml`, with your zone's name (from
   [this list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)):

   ```yaml
   examples/deliveries/setups/real_mail.yaml:
     tools:
       MAILBOX: {client: {timezone: Europe/Berlin}}
       CLOCK:   {client: {timezone: Europe/Berlin}}
   ```

   The setup `real` (step 3) has the same two lines and `GOOGLE_CALENDAR`'s: "Changing configurations" shows all three.
2. In every new terminal, before running:

   ```
   source private/env.sh
   ```

3. Run it:

   ```
   python -m chaperone run examples/deliveries --setup real_mail
   ```

   On the page: press Start beside `assistant`, name the carriers or shops whose notices you get (`UPS and FedEx`),
   tick the senders it finds, and say you are done. Then press Start beside `update_calendar`. It reads the last 21
   days of mail from those senders, at most 300 messages, and writes the calendar file
   `state/deliveries/real_mail/deliveries.ics`, which any calendar program opens.

Only a message with an `Authentication-Results` header that says `dkim=pass` or `dmarc=pass` is read. Your mail
server adds that header, but a sender can write one too, so it does not prove who sent the message ("Known issues",
below). The measured runs used Proton Mail Bridge; Gmail is not yet tried with this project.

### Step 3: Google Calendar

The program creates one calendar of its own in your Google account and writes only there: the access Google gives it
(`calendar.app.created`) covers calendars it created, and nothing else.

Once, about ten minutes:

1. At [console.cloud.google.com](https://console.cloud.google.com/), sign in and create a project (any name). A
   personal Gmail account works; no billing is needed.
2. *APIs & Services* → *Library* → search `Google Calendar API` → *Enable*.
3. *APIs & Services* → *OAuth consent screen*: any app name, your e-mail, audience **External**. Then *Audience* →
   *Test users* → add your Google account. Leave it in *Testing*.
4. *Clients* → *Create client* → type **Desktop app** → *Create*. In the window that opens, press *Download JSON*
   (do it now, from that window). Google names the file `client_secret_<numbers>.apps.googleusercontent.com.json`;
   move it into place, under the name the program looks for:

   ```
   mkdir -p private && mv ~/Downloads/client_secret_*.json private/google_client_secret.json
   ```

   (If you downloaded it on another computer, copy it to that path here.) It holds a client id and a client
   secret that Google made for your project; it starts with `{"installed": {"client_id": ...`. There is no
   template for it: only the file Google gives you works.
5. Optional: the calendar's name, `calendar_name: "Deliveries (agent)"` as shipped. To change it, and your time zone
   if it is not New York's: "Changing configurations", above.
6. Sign in:

   ```
   python -m chaperone signin examples/deliveries --setup real
   ```

   Open the address it prints, choose your account and allow access. Google then sends the browser to
   `http://127.0.0.1:8765/`. If that page does not load (the browser is on another machine), copy the whole address
   from the browser's address bar and paste it at the terminal. The sign-in is saved as `private/google_token.json`.

Then, as in step 2:

```
source private/env.sh
python -m chaperone run examples/deliveries --setup real
```

Each setup keeps its own state, so choose the senders again in `assistant`, then Start `update_calendar`. The entries
appear in a new calendar of your Google account, "Deliveries (agent)", each on its parcel's day: look back up to
three weeks. If the calendar is not listed, reload the Google Calendar page (Shift and reload).

- Each state folder makes its own calendar. A second one (another copy of this folder, or `--state`) makes a second
  calendar with the same name.
- While the Google project is in *Testing*, Google ends the sign-in after seven days: run the `signin` command again.
- To remove this program's entries from Google: Start `reset_all`. Deleting the state folder does not remove them.

### A hosted LLM instead (Claude)

The example `safe_rag` has a setup for it, `hosted`:

1. Make an API key at [console.anthropic.com](https://console.anthropic.com/).
2. In `private/env.sh` (the copy command of step 2 makes it), remove the `#` before `export ANTHROPIC_API_KEY` and
   put your key between the quotes, so that the line reads as below. Then run `source private/env.sh`.

   ```
   export ANTHROPIC_API_KEY="your key"
   ```

3. Run it:

   ```
   python -m chaperone run examples/safe_rag --setup hosted
   ```

   At the start, the rule `NOTHING_LEAVES_NETWORK` reports the LLM's calls as a possible violation: your question
   and parts of the documents go to Anthropic. Answer `1` to permit them.

## Commands

```
python -m chaperone run    <project> [--setup NAME]   # the start's report and acceptance, then the page
python -m chaperone check  <project> [--setup NAME]   # the pre-run check only
python -m chaperone accept <project> [--setup NAME] --by NAME   # accept without the questions, for a script
python -m chaperone facts  <project> [--setup NAME]   # every module's facts
python -m chaperone signin <project> [--setup NAME]   # sign in to Google, for a setup whose calendar is Google's
```

`--setup` is `demo` when the project has one, otherwise `local`. `--state DIR` names the state folder; by default it is
`state/<project>/<setup>`.

## How a project is written

| File | Holds |
|---|---|
| `system.yaml` | the labels, with the modules that may add or remove each; profiles; stores; what a refused call does to a run |
| `setups/<name>.yaml` | the tools (a mail server, an LLM, the page) with their facts; which tool each module uses |
| `rules.yaml` | the rules, and the rules files it imports (`examples/rules/default.yaml`, the five shared rules) |
| `exceptions.yaml` | the exceptions to the rules |
| `workflows/<name>.yaml` | one workflow: its states, the calls each makes, and the state that follows |
| `modules.py`, or `modules/` | the modules: Python functions, each with one line above it that declares it |
| `ui.yaml` | the words and the colour the page shows for each label |

Each term is defined in [docs/terminology.md](docs/terminology.md); the full definitions, which the code cites by
number, are in [docs/specification.md](docs/specification.md).

## Known issues

The ones a first run may meet:

- The first start of `deliveries` lists 12 exceptions under the five rules, each answered `1`. It is the report of
  what the project may do, and it is long.
- The LLM is not tried at the start: with no server, the first call that needs it fails. "Check your LLM" (Step 1)
  tries it first.
- It has been run with Python 3.13.7 only.
- Each state folder makes its own Google calendar, so two can have the same name.
- The mail check reads a header that a sender can write (Step 2).

All of them, and what these documents do not yet cover: [docs/known-issues.md](docs/known-issues.md).

## License

MIT, Copyright (c) 2026 Applied Ingenuity LLC. See [LICENSE](LICENSE).
