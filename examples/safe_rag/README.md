# Safe retrieval ("SaferRAG")

*Part of [Project Chaperone](../../README.md): a rule-enforcing harness for workflows, including ones in which an LLM
chooses the next step. Rules say what must never happen; every route of every workflow is checked against them before
a run, and every call is checked while it runs. [Install it](../../README.md#install) first; run every command below
from the folder `project-chaperone`.*

An LLM answers a question from your documents. Its own words reach the screen marked as its own. Text displayed as
"from the documents" is read from the documents by code, never taken from the LLM's reply.

"SaferRAG" is a working name for this project; its folder is `examples/safe_rag`.

## Getting started

Three ways to start, each one command. Each needs an LLM server running on this machine (the one
`configs/llm/local.yaml` names), or the setup `hosted` below.

| To ask about | Do this first | Then run |
|---|---|---|
| The 85 Federalist Papers (shipped) | nothing | `python -m chaperone run examples/safe_rag` |
| Your own folder of notes, such as an Obsidian vault | two lines in `private/local.yaml`: "Your own documents", below | `python -m chaperone run examples/safe_rag` |
| arXiv abstracts | `python scripts/load_arxiv.py --category cs.AI --since 2025-11-14` (about ten minutes): "arXiv abstracts", below | `python -m chaperone run examples/safe_rag --setup arxiv` |

Then, for all three:

1. The terminal prints the rules and the exceptions to them, and asks you to accept them, once.
2. It prints the page's address. Open it, press Start beside `single_pass` (the first in the list), and type
   a question.
3. An empty box ends the run.

The shipped documents are the 85 Federalist Papers (`corpus/`). Questions to try: "Why is the judiciary the least
dangerous branch?", "What is the capital of France?" (the papers do not answer it, and no answer is displayed).

## What one question does

The workflow is `single_pass` (`workflows/single_pass.yaml`).

1. **Read.** The LLM is passed your question, what the documents are (`overview`), and how many chunks hold each of
   your words. It gives the question rephrased, and the concepts worth searching for. Your question is never
   replaced.
2. **Search.** Three searches: your question, the rephrased one, the concepts. Of what they find, at most 8
   documents, 4 chunks each.
3. **Answer.** The LLM is passed your question and the chunks, numbered. It replies with passages copied from them
   (quotes) and an answer. The prompts are in `prompts.yaml`.
4. **Check.** `verify.scored` (in `modules.py`) reads each chunk from the documents by its id, and keeps a quote only
   when it is in its chunk, word for word.
5. **The screen.** With a quote found: the answer, marked "written by the LLM" and "untrusted", and the documents'
   own text, marked "from the documents", the quotes underlined. With none: no answer.

`answer` is an earlier, simpler form: one search, and a second for the LLM's words for the question when no quote is
found.

## Your own documents

Change two values of the tool DOCS_DB, in `private/local.yaml` (the main README, "Changing configurations"):

    examples/safe_rag/setups/local.yaml:
      tools: {DOCS_DB: {client: {folder: ~/notes, overview: "My notes on work and reading, 2019 to 2026."}}}

`overview` says what the documents are, in a sentence of your own; the LLM is passed it. Write `overview: ""` for
none. Then start as above. The index of your folder is made at the first question.

**An Obsidian vault** works as it is: point `folder:` at the vault. Notes are `.md` files, which are read; the
vault's own folder `.obsidian` begins with a dot and is not; a note's title is its first line that is not empty,
after its front matter (the block between two lines `---`). Links and tags are read as the text they are.

- A path relative to this project, an absolute path, or one that begins with `~`.
- Files ending `.txt` or `.md` are read (`extensions`). A file or folder whose name begins with a dot is not.
- **No step prepares the folder.** The index is made at the first question, in the state folder
  (`state/safe_rag/local/documents.sqlite`), and made again when a file is added, removed or changed.
- `tokenizer: porter` matches English words by their stem ("factions" finds "faction"). For another language write
  `tokenizer: unicode61`.
- `adds: []` on that line says you vouch for the folder. For documents you do not vouch for (mail, pages from the
  web), write `adds: [UNTRUSTED_SOURCE]`: everything displayed from them is then marked "source not trusted". It
  marks; it stops nothing.

## arXiv abstracts, with dates

    python scripts/load_arxiv.py --category cs.AI --since 2025-11-14     # about ten minutes: arXiv asks loaders to go slowly
    python -m chaperone run examples/safe_rag --setup arxiv

- The loader writes `data/arxiv/cs.AI.sqlite` (not in git): for cs.AI and one year, about 61,000 abstracts, 130 MB.
  Run it again without `--since` to add what arXiv published or changed since.
- For another category, fetch it and change `database` and `overview` of the tool DOCS_DB, in `private/local.yaml`.
- A paper's date is the day of its first version; each document's heading on the page has it.
- Two more values of DOCS_DB in `setups/arxiv.yaml`, changed the same way: `not_before: "2026-06-01"` leaves out
  older papers ("" for none); `recency_boost: 0.1` puts the later of two papers that fit about equally first (0 for
  none). For example:

      examples/safe_rag/setups/arxiv.yaml:
        tools: {DOCS_DB: {client: {not_before: "2026-06-01"}}}
- Any loader can write such a database: a table `documents(document_id, title, source, date, text)`. The client of
  kind `document_db` reads it and knows nothing of arXiv.
- A question to try here and in `just_ask_the_llm`: one about a paper newer than the LLM, by its name.

## An LLM at a vendor

    export ANTHROPIC_API_KEY="..."
    python -m chaperone run examples/safe_rag --setup hosted

The question and chunks of your documents then go to that vendor, which the rule NOTHING_LEAVES_NETWORK forbids.
The first start lists this as a possible violation with the exception that would permit it; accepting adds it for
this setup. `setups/hosted.yaml` says how to choose the model or another vendor. Not run against a vendor by the
authors' tests: they have no key.

## From the command line, with no page

    python scripts/measure_safe_rag.py "Why is the judiciary the least dangerous branch?"
    python scripts/measure_safe_rag.py

The first asks questions of your own and prints each answer and the quotes found. The second asks the 24 questions
of `questions.yaml` and counts the screens. Both run the same workflow under the same rules as the page, and are
refused until the rules and exceptions were accepted once (start the page once, or
`python -m chaperone accept examples/safe_rag --by NAME`). The script is also an example of running a workflow from
Python. It reads the shipped files only, not `private/local.yaml`: with a changed `api_base` it still asks the
shipped address, and with a changed fact it says the result is not accepted.

The last measurement, question by question: [results.md](results.md). The script judges which screen is shown, not
whether an answer is right.

## More on `single_pass` and `multi_pass`: the screen with highlighted passages

`multi_pass` is `single_pass` with a second round when no quote is found: a second search, and a call that asks the
LLM which chunks bear on the question. The bullets marked "`multi_pass` only" are about that round. On 39 questions
(the 38 of the two files, and one on arXiv abstracts) the two showed the same screens (2026-11-15, one run each).

- The screen: your question; the question as the LLM read it; the answer; then one box for each relevant document.
  Each chunk is closed on its two highest-scoring sentences, with the words that fit the question coloured. A click
  opens the whole chunk. An underlined passage is a quote the LLM gave, found in the document.
- **Before any search the LLM is asked about your question alone** (the prompt `read_question`; the state READ). It
  is passed what the documents are and how many chunks hold each word you typed, and gives the question as someone
  who knows the collection would write it (usual spellings and names), the primary concepts (worth searching for)
  and the supportive ones (for ranking). **Your question is never replaced:** it is searched as typed, it is what
  the LLM answers, and it is what the screen shows; what the LLM gave is searched as well, and the screen has it on
  the lines `also_searched_as`, `primary_concepts_of_the_llm` and `secondary_concepts_of_the_llm`.
- The first search is three searches joined: your question, the rephrased one, the primary concepts. A chunk that
  several of them found comes first; each passage says whether your question found it, what the LLM added found
  it, or both. So a misspelled question still finds its documents, and a question about something the documents do
  not hold stays about that thing and ends with no answer.
- In the scores, a primary concept counts in full and the supportive ones together count as one primary concept. A
  term in a document's title counts for each of its chunks, at 0.8 of its weight (`title_match`).
- **`multi_pass` only:** a second search is made when no quote is found, or when the first search found fewer than 5 chunks. It searches
  for the key concepts, and once more with each primary concept left out; what it finds is joined with the first search's
  chunks, and each passage says which search found it. A concept of several words ("supreme court") is searched
  whole, never one word without the others. A concept that more than a fifth of the chunks hold is not searched for,
  and the screen names it.
- **`multi_pass` only:** after the second search the LLM is asked, of each chunk found, whether it bears on your question; the documents it
  says yes for are passed first. Nothing is left out on its word (`drop: false` in the state JUDGE; `drop: true`
  leaves out what it says no for, `ask: false` does not ask).
- **`multi_pass` only:** in both rounds the LLM is passed your question as you typed it. In the second round the workflow also tells it
  that the chunks come from a broader search and are likely not relevant (the `note` of ANSWER_2).
- With no quote found (in `multi_pass`, after both searches), no answer is displayed. A passage is displayed below
  only when it holds nearly all of the LLM's key concepts (three quarters, and at least two: `unanswered`, under
  VERIFY), or, in `multi_pass` only, when the LLM judged that it bears on your question and it holds at least one of
  them; a concept in the document's title counts as held. Each says which, and at most three are displayed. A passage that only
  shares words with your question is not displayed.
- `overview`, on the DOCS_DB line of the setup, says what the documents are. The LLM is passed it and asked not to
  name the collection itself in its key concepts. Write your own for your folder, or `overview: ""`.
- How a sentence is scored is in the docstring of `verify.scored` (`modules.py`). Every number of it is written in each
  workflow's file, under VERIFY (`workflows/single_pass.yaml`, `workflows/multi_pass.yaml`): change one and start
  again.
- A frequent word weighs less, never nothing; there is no stop list.
- The text in a box is the documents'. Which sentences are displayed first and which words are coloured follow the
  question and the LLM's reply.

## Two workflows for comparison

On the page, below the other three. Ask each the same question.

| Workflow | What the LLM is passed | What is displayed | Checked |
|---|---|---|---|
| `just_ask_the_llm` | the question | the LLM's answer | nothing |
| `typical_rag` | the question and the chunks found | the LLM's answer, with whatever quotes it wrote | nothing |
| `single_pass`, `multi_pass`, `answer` | the question and the chunks found | the LLM's answer, and the documents' own text | each quote, against the documents |

- Both comparison answers are marked "untrusted" and "written by the LLM". A quote in them is the LLM's text.
- They are displayed by a module of their own, `user.show_unchecked`, which one exception permits
  (`UNCHECKED_LLM_TO_USER_EX`). Without that exception the start reports both workflows as a possible violation:
  the rules forbid displaying what an LLM wrote, unchecked.
- A question to try in all three: "Did the authors want a bill of rights added to the Constitution?"
- Harder questions, for trying changes: `questions_compound.yaml` (14: two subjects in one question, a false premise).

## The files

| File | Holds |
|---|---|
| `system.yaml` | the six labels, and which module may add or remove each |
| `setups/local.yaml`, `setups/hosted.yaml`, `setups/arxiv.yaml` | the tools (documents, LLM, page) and the facts stated for each |
| `rules.yaml` | imports the five shared rules of `examples/rules/default.yaml` |
| `exceptions.yaml` | four exceptions, each with what it permits |
| `workflows/` | `single_pass`, `multi_pass`, `answer`, and for comparison `typical_rag` and `just_ask_the_llm` |
| `modules.py` | the searches, the LLM modules, the two checks, the modules of the page |
| `prompts.yaml` | the prompts |
| `ui.yaml` | the words and colour the page displays for each label |
| `questions.yaml`, `questions_compound.yaml` | the 24 questions of the measurement, and 14 harder ones |
| `results.md` | the last measurement, question by question |
| `corpus/` | the Federalist Papers, one file each (`scripts/get_federalist.py` downloaded them) |

## Limits

- The check shows that a snippet is in the documents. It does not show that the answer follows from it, or that the
  documents are right.
- The LLM chooses which chunks are displayed and which passages are marked. One word quoted is enough for the answer
  to be displayed.
- An LLM steered by a passage planted in a document still writes the answer. The label says the answer is the
  LLM's; nothing stops it. A test with planted passages is planned and not yet run.
- That no answer is displayed when nothing is found is this workflow's branch, not a rule.
- Search is by words: a question that shares no word with a document finds nothing. Common words of a question can
  crowd out the right document.
- Ranking documents by the sum of their chunks' scores favours long documents.
- One question at a time, with no memory of the last. A run ends after 200 states: at least 30 questions in
  `single_pass`, 25 in `answer`, 20 in `multi_pass`.
- A folder changed between the search and the check of one question can renumber chunks.
