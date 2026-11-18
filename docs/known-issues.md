# Known issues

What does not work, or not yet, in this release, and what these documents do not yet cover. Where there is a way
around an issue, it is given. What the checks cannot show by design is in the [specification](specification.md),
section 10.

## Running it

- Python: run with 3.13.7 only. `pyproject.toml` asks for 3.13 or newer, so pip refuses older versions.
- The LLM is not tried at the start. With no server, the first call that needs it fails: `safe_rag` says so on its
  screen; `update_calendar` sets every message aside and still ends in DONE. Try the server first: README, "Check
  your LLM".
- The first start of `deliveries` lists 12 exceptions under the five rules. All are the project's own, and the
  answer to each is `1`; the screen does not say which matter most.
- A start lists your changes in `private/local.yaml` in two blocks: those to a setup before the rules, those to the
  LLM's file after them.
- `scripts/measure_safe_rag.py` reads the shipped files only, not `private/local.yaml`.
- Each state folder makes its own Google calendar. A second state folder makes a second calendar with the same name,
  and nothing says so.
- A `calendar_name` changed in `private/local.yaml` does nothing once the state folder has the calendar's record.
  Rename the calendar in Google instead.
- While the Google Cloud project is in *Testing*, Google ends the sign-in after seven days: run `signin` again.

## The examples

- `deliveries`, real mail: a message is read only when it has an `Authentication-Results` header that says
  `dkim=pass` or `dmarc=pass`. A sender can write that header, and a message signed by one domain with a From
  address at another passes, so it does not prove who sent the message. The senders you tick, and the checks on each
  value, still apply.
- `deliveries`: a wrong value that is in the notice and passes every check is written. A tracking number held in a
  link's path, not in a parameter, fits no link rule. A notice that part of a shipment was delivered marks the whole
  entry delivered.
- `deliveries`, setup `demo`: the four made-up messages all give `received_time: "09:01"`, and the fourth reports a
  delivery at 2:14 PM, after it was received.
- `safe_rag`: its README, "Limits".
- `ask_my_mail` and `share_notes` run on made-up data only.
- Not yet tried by anyone: Ollama, Gmail, the setup `hosted` with an API key.

## Documentation gaps

- No guide to debugging a run: which file of the state folder answers "did it write anything, and where".
- Not every setting is explained where the shipped file sets it. `configs/llm/local.yaml` is; `days`, `max_messages`
  and `authenticated_only` in the setups of `deliveries`, for example, are not.
- No guide to writing a project of your own, beyond "How a project is written" in the README, the
  [terminology](terminology.md) and the [specification](specification.md).
- The specification's sections 6 to 8 are pointers to the example folders, with no text of their own.
- The `safe_rag` README is long, about 2,300 words, and describes five workflows.
- The page is described in words only, with no pictures.
- The paper's link in the README is a placeholder.
