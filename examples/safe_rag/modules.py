"""The modules of "safe retrieval" (README.md of this folder)."""

import json
import re

import chaperone


@chaperone.module("user.describe", properties={"DESTRUCTIVE": False})
def user_describe(ctx, prompt):
    return ctx.describe(prompt)


@chaperone.module("rag.search", properties={"DESTRUCTIVE": False})
def rag_search(ctx, query, candidates, seen, max_documents, max_chunks_per_document, max_chars):
    """The chunks the LLM is passed. The store gives its `candidates` best chunks, leaving out those in `seen`. They
    are grouped by document, and the documents ranked by the sum of their chunks' scores (so a long document, with
    more chunks that fit, ranks higher). Of the first `max_documents` documents, at most `max_chunks_per_document`
    chunks each, the best first, while the texts kept stay within `max_chars` characters in all; a chunk that would go
    past it is not kept, and a later, shorter one may be. In each document the chunks kept are in the file's order.
    Returns the documents, `chunk_ids` in the order the LLM numbers them, and three counts for the screen:
    `files_searched`, `chunks_found` (what the store gave) and `chunks_passed` (what the LLM is passed)."""
    found = ctx.search(query, candidates, seen)
    kept = _select(found, max_documents, max_chunks_per_document, max_chars)
    chunk_ids = [c["chunk_id"] for d in kept for c in d["chunks"]]
    return {"documents": kept, "chunk_ids": chunk_ids, "files_searched": ctx.files(), "chunks_found": len(found), "chunks_passed": len(chunk_ids)}


def _select(found, max_documents, max_chunks_per_document, max_chars):
    """The chunks kept of `found` (best first), as documents: see rag.search."""
    documents = {}
    for chunk in found:
        documents.setdefault(chunk["document_id"], []).append(chunk)
    ranked = sorted(documents.values(), key=lambda chunks: -sum(c["score"] for c in chunks))[:max_documents]
    kept, total = [], 0
    for chunks in ranked:
        mine = []
        for chunk in chunks[:max_chunks_per_document]:                 # best first, as they were given
            if total + len(chunk["text"]) <= max_chars:
                mine.append(chunk)
                total += len(chunk["text"])
        if mine:
            mine.sort(key=lambda c: (c["location"]["first_paragraph"]))
            first = mine[0]
            kept.append({"document_id": first["document_id"], "title": first["title"], "source": first["source"],
                         "chunks": [{k: c[k] for k in ("chunk_id", "location", "text", "score")} for c in mine]})
    return kept


@chaperone.module("rag.word_counts", properties={"DESTRUCTIVE": False})
def rag_word_counts(ctx, query):
    """How many chunks of the documents hold each word of `query`, and each two words that follow each other, as a
    text for the prompt of llm.read_question: one line each, "word: count". The counts tell the LLM which words of
    the question the documents use (a name, an acronym, an old spelling), so that it leaves them as they are.
    Returns `text` and `documents`, how many documents the store has. To give the LLM no counts, the workflow
    passes llm.read_question "" in place of `text`."""
    words = re.findall(r"\w+", query.lower()) if isinstance(query, str) else []
    terms = list(dict.fromkeys(words)) + list(dict.fromkeys(f"{a} {b}" for a, b in zip(words, words[1:])))
    stats = ctx.stats(terms) if terms else {}
    lines = [f"{term}: {stats.get(term, {}).get('count', 0)}" for term in terms]
    return {"text": "\n".join(lines), "documents": ctx.files()}


@chaperone.module("rag.search_all", properties={"DESTRUCTIVE": False})
def rag_search_all(ctx, query, rephrased, primary, candidates, max_documents, max_chunks_per_document, max_chars, min_phrase_count):
    """The first search: the user's question as typed, and with it what llm.read_question gave. The typed question is
    never replaced. Up to three searches, each for the store's `candidates` best chunks: (1) the words of `query`;
    (2) the words of `rephrased`, when it is a text that differs from `query`; (3) the terms of `primary`, each whole
    (the store's `search_terms`, with `min_phrase_count`), when there is one. A chunk's score is the sum, over the
    searches that found it, of its place there counted from the end (first of 20: 1.0; last: 0.05), so a chunk that
    several searches found comes before one that a single search found. The chunks are then selected as rag.search
    selects them.

    Returns what rag.search returns, with `found_by`, for each chunk id kept: "question" (only the typed question
    found it), "concepts" (only what the LLM gave found it) or "both"; `searches`, how many were made; and
    `primary_found`, how many chunks the search for the primary concepts found (0 says the documents hold none of
    them; -1 when there was no such search)."""
    def as_terms(items):
        return [t for t in dict.fromkeys(" ".join(re.findall(r"\w+", c.lower())) for c in items or [] if isinstance(c, str)) if t]
    typed = ctx.search(query, candidates, [])
    added = []
    if isinstance(rephrased, str) and rephrased.strip() and rephrased.strip().lower() != str(query).strip().lower():
        added.append(ctx.search(rephrased, candidates, []))
    terms, primary_found = as_terms(primary), -1
    if terms:
        added.append(ctx.search_terms(terms, candidates, [], min_phrase_count))
        primary_found = len(added[-1])
    chunks, score, by_question, by_added = {}, {}, set(), set()
    for result, who in [(typed, by_question)] + [(result, by_added) for result in added]:
        for place, chunk in enumerate(result):
            chunks.setdefault(chunk["chunk_id"], chunk)
            score[chunk["chunk_id"]] = score.get(chunk["chunk_id"], 0.0) + (len(result) - place) / max(len(result), 1)
            who.add(chunk["chunk_id"])
    scored = sorted((dict(chunk, score=round(score[i], 4)) for i, chunk in chunks.items()), key=lambda c: -c["score"])
    kept = _select(scored, max_documents, max_chunks_per_document, max_chars)
    chunk_ids = [c["chunk_id"] for d in kept for c in d["chunks"]]
    found_by = {i: "both" if i in by_question and i in by_added else "question" if i in by_question else "concepts" for i in chunk_ids}
    return {"documents": kept, "chunk_ids": chunk_ids, "found_by": found_by, "searches": 1 + len(added), "primary_found": primary_found,
            "files_searched": ctx.files(), "chunks_found": len(chunks), "chunks_passed": len(chunk_ids)}


@chaperone.module("rag.overview", properties={"DESTRUCTIVE": False})
def rag_overview(ctx):
    """What the documents are, in the user's words: the `overview` of the documents tool in the setup ("" for none)."""
    return {"text": ctx.overview()}


@chaperone.module("rag.search_concepts", properties={"DESTRUCTIVE": False})
def rag_search_concepts(ctx, concepts, secondary, fallback, first_chunk_ids, candidates, max_documents, max_chunks_per_document, max_chars, min_phrase_count):
    """A second search, for the LLM's key concepts, joined with the chunks the first search passed.

    `concepts` is a list of terms, each one word or several ("supreme court"); a term of several words is searched as
    a whole, never one of its words without the others (the store's `search_terms`, with `min_phrase_count`). One
    search is made for all the concepts, and, when there are two or more, one more for each concept left out, so
    that one concept that fits very many chunks does not decide alone what is found. `secondary` is a second list of
    terms (verify.scored: the LLM's secondary key concepts): they are in the first of
    these searches, with all of `concepts`, and in none of the others, so they widen the search and never multiply
    it. With no term in either list, one search is made for the words of `fallback` (the LLM's restated question). The chunks found are joined with the chunks of
    `first_chunk_ids`, read from the store. A chunk's score is 1 when the first search passed it, plus the share of
    this module's searches that found it; the chunks are then selected as rag.search selects them.

    Returns what rag.search returns, with `found_by`, for each chunk id kept: "question" (the first search),
    "concepts" (this one) or "both"; and `searches`, how many searches were made."""
    def as_terms(items):
        return [t for t in dict.fromkeys(" ".join(re.findall(r"\w+", c.lower())) for c in items or [] if isinstance(c, str)) if t]
    terms = as_terms(concepts)
    others = [t for t in as_terms(secondary) if t not in terms]
    if terms or others:
        queries = [terms + others] + ([terms[:i] + terms[i + 1:] for i in range(len(terms))] if len(terms) > 1 else [])
        results = [ctx.search_terms(query, candidates, [], min_phrase_count) for query in queries]
    else:
        results = [ctx.search(fallback, candidates, [])]
    chunks, share, place = {}, {}, {}
    for result in results:
        for rank, chunk in enumerate(result):
            chunks.setdefault(chunk["chunk_id"], chunk)
            share[chunk["chunk_id"]] = share.get(chunk["chunk_id"], 0.0) + 1 / len(results)
            place[chunk["chunk_id"]] = min(place.get(chunk["chunk_id"], rank), rank)
    first = {chunk["chunk_id"]: chunk for chunk in ctx.get(first_chunk_ids)}
    chunks.update({i: c for i, c in first.items() if i not in chunks})
    scored = [dict(chunk, score=round((1.0 if i in first else 0.0) + share.get(i, 0.0), 4)) for i, chunk in chunks.items()]
    scored.sort(key=lambda c: (-c["score"], place.get(c["chunk_id"], len(chunks))))
    kept = _select(scored, max_documents, max_chunks_per_document, max_chars)
    chunk_ids = [c["chunk_id"] for d in kept for c in d["chunks"]]
    found_by = {i: "both" if i in first and i in share else "question" if i in first else "concepts" for i in chunk_ids}
    return {"documents": kept, "chunk_ids": chunk_ids, "found_by": found_by, "searches": len(results), "files_searched": ctx.files(),
            "chunks_found": len(chunks), "chunks_passed": len(chunk_ids)}


@chaperone.module("llm.answer", properties={"DESTRUCTIVE": False})
def llm_answer(ctx, query, documents):
    """Asks the LLM the question, passing the chunks as a numbered list under each document's title (the numbers
    follow `chunk_ids` of rag.search). Returns the reply as text: the prompt `ask` of prompts.yaml."""
    return {"text": ctx.ask(query=query, chunks=_numbered(documents))}


@chaperone.module("llm.answer_with_concepts", properties={"DESTRUCTIVE": False})
def llm_answer_with_concepts(ctx, query, documents, overview, note):
    """As llm.answer, with the prompt `ask_with_concepts` of prompts.yaml: it asks for no key concepts (llm.read_question gives them, before the search), and
    `relevant` is asked in order of usefulness. `overview` says what the documents are (rag.overview); the prompt
    asks the LLM not to name the collection itself in its key concepts or its restated question. `note` is a text the
    workflow writes about these chunks, placed before them in the prompt ("" for none): the second round says there
    that the chunks come from a broader search and are likely not relevant."""
    return {"text": ctx.ask_with_concepts(query=query, chunks=_numbered(documents), overview=overview or "(not given)", note=note)}


@chaperone.module("llm.read_question", properties={"DESTRUCTIVE": False})
def llm_read_question(ctx, query, overview, counts):
    """Asks the LLM about the question alone, before any search: the prompt `read_question` of prompts.yaml, passed
    the user's question, what the documents are, and `counts` (rag.word_counts' text; "" for none). The reply is one
    JSON object: `rephrased_query`, the question as someone who knows the collection would write it;
    `primary_concepts`, the concepts of the question worth searching for; `supportive_concepts`, its other concepts,
    for ranking. None of them replaces the question: rag.search_all searches for them as well as for it.

    Read generously: a reply that is not such an object gives no rephrased question and no concepts; a key that is
    absent or of another form is empty; an item of a list that is not a text is left out. Returns `rephrased`
    (a text, "" for none), `primary` and `supportive` (lists of texts)."""
    reply = ctx.read_question(query=query, overview=overview or "(not given)", counts=counts or "(not given)")
    if not isinstance(reply, dict):
        try:
            reply = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
        except (ValueError, TypeError, AttributeError):
            reply = {}
    reply = reply if isinstance(reply, dict) else {}
    def texts(key):
        return [c.strip() for c in reply[key] if isinstance(c, str) and c.strip()] if isinstance(reply.get(key), list) else []
    rephrased = reply.get("rephrased_query")
    return {"rephrased": rephrased.strip() if isinstance(rephrased, str) else "", "primary": texts("primary_concepts"), "supportive": texts("supportive_concepts")}


@chaperone.module("llm.judge_relevance", properties={"DESTRUCTIVE": False})
def llm_judge_relevance(ctx, query, documents, chunk_ids, found_by, overview, ask, drop):
    """Asks the LLM which of the chunks bear on the question: the prompt `judge_relevance` of prompts.yaml, passed the
    user's question and the chunks, numbered as llm.answer numbers them. With `drop: false`, the documents with a
    chunk judged to bear on the question are put first, and nothing is left out: a wrong "no" costs a place, not the
    chunk. With `drop: true`, the chunks judged not to bear on it are left out. With `ask: false` the LLM is not
    asked and nothing changes.

    The reply is one JSON object with an entry for each chunk, its number and true or false: {"1": true, "2": false}.
    A chunk is left out only when its entry is false: one the reply does not name is kept, so a reply that stops
    early loses nothing. Read generously: text around the object is taken away; {"relevant": [numbers]} is read as
    true for those numbers and false for the others. A reply that cannot be read so keeps every chunk, and `judged`
    says it.

    Returns `documents`, `chunk_ids` and `found_by` (with `drop: true`, of the chunks kept; else of all, the documents
    reordered); `judged`: "asked", "not asked" or "reply not readable"; `passed`, how many chunks were judged;
    `kept`, how many were not judged "no"; and `relevant`, the ids of the chunks whose entry is true (none when the
    LLM was not asked or its reply was not readable: a chunk the reply does not name is kept, and is not in it)."""
    yes = set()
    if ask is True:
        text = ctx.judge_relevance(query=query, chunks=_numbered(documents), overview=overview or "(not given)")
        def number(n):
            return int(n.strip()) if isinstance(n, str) and n.strip().isdigit() else n if isinstance(n, int) and not isinstance(n, bool) else 0
        try:
            reply = json.loads(text[text.index("{"):text.rindex("}") + 1])
            if isinstance(reply.get("relevant"), list):                 # the other form: the numbers that bear on the question
                named = {number(n) for n in reply["relevant"]}
                verdicts = {n: n in named for n in range(1, len(chunk_ids) + 1)}
            else:
                verdicts = {number(n): verdict for n, verdict in reply.items() if isinstance(verdict, bool)}
                if not verdicts:
                    raise ValueError("no entry is true or false")
            keep = {chunk_id for n, chunk_id in enumerate(chunk_ids, 1) if verdicts.get(n) is not False}
            yes = {chunk_id for n, chunk_id in enumerate(chunk_ids, 1) if verdicts.get(n) is True}
            judged = "asked"
        except (ValueError, KeyError, TypeError, AttributeError):
            keep, judged = set(chunk_ids), "reply not readable"
    else:
        keep, judged = set(chunk_ids), "not asked"
    if drop is True:
        kept = [dict(document, chunks=[c for c in document["chunks"] if c["chunk_id"] in keep]) for document in documents]
        kept = [document for document in kept if document["chunks"]]
    else:                                                       # nothing is left out: the documents with a chunk that bears on the question come first
        kept = sorted(documents, key=lambda document: not any(c["chunk_id"] in keep for c in document["chunks"]))
    ids = [c["chunk_id"] for document in kept for c in document["chunks"]]
    return {"documents": kept, "chunk_ids": ids, "judged": judged, "passed": len(chunk_ids), "kept": len(keep),
            "relevant": [i for i in ids if i in yes],
            "found_by": {i: by for i, by in found_by.items() if i in ids} if isinstance(found_by, dict) else {}}


@chaperone.module("llm.answer_plainly", properties={"DESTRUCTIVE": False})
def llm_answer_plainly(ctx, query, documents):
    """Asks the LLM the question with the chunks, as llm.answer does, and for an answer in plain text: the prompt
    `ask_plainly`. Nothing reads or checks the reply."""
    return {"text": ctx.ask_plainly(query=query, chunks=_numbered(documents))}


@chaperone.module("llm.answer_alone", properties={"DESTRUCTIVE": False})
def llm_answer_alone(ctx, query):
    """Asks the LLM the question and nothing else: no document is searched or passed. The prompt `ask_alone`."""
    return {"text": ctx.ask_alone(query=query)}


def _numbered(documents):
    lines, number = [], 0
    for document in documents:
        lines.append(f"Document: {document['title']}")
        for chunk in document["chunks"]:
            number += 1
            lines.append(f"[{number}] {chunk['text']}")
        lines.append("")
    return "\n".join(lines).strip() or "(no chunks were passed)"


@chaperone.module("verify.response", properties={"DESTRUCTIVE": False},
              requests={"restated_query": {"CHECKED": True}, "answer": {"CHECKED": True},
                        "found": {"CHECKED": True}, "supported": {"CHECKED": True},
                        "documents": {"UNTRUSTED": False, "LLM_WRITTEN": False, "ORIGINAL_SOURCE": True},
                        "left_out": {"CHECKED": True}})
def verify_response(ctx, chunk_ids, response):
    """Reads the LLM's reply and checks each snippet against its chunk's text, which it reads from the store by the
    chunk's id: what is displayed as from the documents is the store's text, whatever the reply says.

    The reply is one JSON object: `restated_query` (a text), `answer_found` (true or false), `relevant` (chunk
    numbers), `snippets` ([{id: a chunk number, snippet: a text}]) and `answer` (a text). The last three may be
    absent, as when nothing was found, and are then empty; they are read whatever `answer_found` says. Anything else
    is the error `bad_reply`. Read generously: text around the object (a code fence) is taken away; a number given
    as a text ("3") is taken as the number; a snippet is compared with a run of spaces or line breaks as one space.
    A snippet is found only as whole words of its chunk: "ambit" is not found in "ambition". A number that names no
    chunk the LLM was passed, and a snippet that is not in its chunk, are left out and counted in `left_out`. A
    snippet found more than once in its chunk is marked where it is first.

    Returns `restated_query` and `answer` as the LLM wrote them (`answer` is "" when the reply has none); `found`, the
    LLM's `answer_found`; `supported`, whether at least one snippet was found in its chunk; `documents`, each chunk
    that holds a verified snippet or that the LLM named relevant, whole, as `pieces` ({text, marked}, marked for a
    verified snippet), with `verified`, whether any snippet of it was found; and `left_out`."""
    read = _read(ctx, chunk_ids, response)
    documents = {}
    for chunk_id in chunk_ids:                                  # in the order the LLM numbered them
        if chunk_id not in read["chunks"] or (chunk_id not in read["marks"] and chunk_id not in read["named"]):
            continue
        chunk = read["chunks"][chunk_id]
        document = documents.setdefault(chunk["document_id"], {"title": chunk["title"], "source": chunk["source"], "chunks": []})
        document["chunks"].append({"chunk_id": chunk_id, "location": chunk["location"],
                                   "pieces": _pieces(chunk["text"], read["marks"].get(chunk_id, [])), "verified": chunk_id in read["marks"]})
    result = {"restated_query": read["restated"], "answer": read["answer"], "found": read["found"], "supported": bool(read["marks"]),
              "documents": list(documents.values()), "left_out": read["left_out"]}
    return result, [("restated_query", "CHECKED", True), ("answer", "CHECKED", True), ("found", "CHECKED", True),
                    ("supported", "CHECKED", True), ("documents", "UNTRUSTED", False), ("documents", "LLM_WRITTEN", False),
                    ("documents", "ORIGINAL_SOURCE", True), ("left_out", "CHECKED", True)]


def _json_object(text):
    """The JSON object in `text`, the text around it taken away. Read generously, for a reply that copies passages of
    documents: when the object does not parse, a backslash that begins no JSON escape (documents with formulas hold
    them: "5\\%", "\\alpha") is read as that backslash, and the object is parsed once more. A backslash that does begin
    a JSON escape ("\\times" begins with a tab's) is read as JSON reads it: the snippet then is not found in its chunk
    and is counted as left out. Raises ValueError when there is still no object."""
    inner = text[text.index("{"):text.rindex("}") + 1]
    try:
        return json.loads(inner)
    except ValueError:
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", inner))


def _read(ctx, chunk_ids, response):
    """The LLM's reply, read and checked against the store, for verify.response and verify.scored: `restated`, `found`,
    `answer`; `primary` and `secondary`, the texts of `primary_concepts` and `secondary_concepts`, and `concepts`, both (a reply's `key_concepts`, the earlier form, is read as primary; absent, or not a list: none; an item that is not a text is
    left out); `chunks`, the store's chunks by id; `marks`, for each chunk id the (start, end) of its verified snippets;
    `named`, the ids of the chunks named relevant, in the reply's order; `left_out`."""
    try:
        text = response["text"]
        reply = _json_object(text)
        restated, found = reply["restated_query"], reply["answer_found"]
        relevant, snippets, answer = reply.get("relevant", []), reply.get("snippets", []), reply.get("answer", "")
        pairs = [(item["id"], item["snippet"]) for item in snippets]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise chaperone.ModuleError("bad_reply", "the reply is not a JSON object with `restated_query`, `answer_found` and, "
                                             "if any, `relevant`, `snippets` ({id, snippet} each) and `answer`") from None
    if (not isinstance(restated, str) or not isinstance(found, bool) or not isinstance(answer, str) or not isinstance(relevant, list)
            or not all(isinstance(s, str) for _, s in pairs)):
        raise chaperone.ModuleError("bad_reply", "`restated_query`, `answer` and each snippet are texts, `answer_found` is true or "
                                             "false, `relevant` is a list")
    chunks = {c["chunk_id"]: c for c in ctx.get(chunk_ids)}

    def chunk_of(number):
        if isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(chunk_ids):
            return chunks.get(chunk_ids[number - 1])
        return None

    left_out = {"numbers_that_name_no_chunk": 0, "snippets_not_in_their_chunk": 0}
    marks = {}                                                  # chunk id -> [(start, end)] of its verified snippets
    for number, snippet in pairs:
        chunk = chunk_of(number)
        if chunk is None:
            left_out["numbers_that_name_no_chunk"] += 1
            continue
        snippet = " ".join(snippet.split())
        start = _find_words(chunk["text"], snippet)
        if start < 0:
            left_out["snippets_not_in_their_chunk"] += 1
            continue
        marks.setdefault(chunk["chunk_id"], []).append((start, start + len(snippet)))
    named = []
    for number in relevant:
        chunk = chunk_of(number)
        if chunk is None:
            left_out["numbers_that_name_no_chunk"] += 1
        elif chunk["chunk_id"] not in named:
            named.append(chunk["chunk_id"])
    def texts(key):
        return [c for c in reply[key] if isinstance(c, str)] if isinstance(reply.get(key), list) else []
    primary, secondary = texts("primary_concepts") + texts("key_concepts"), texts("secondary_concepts")    # `key_concepts`, the earlier form of the reply, is read as primary
    return {"restated": restated, "found": found, "answer": answer, "concepts": primary + secondary, "primary": primary, "secondary": secondary, "chunks": chunks, "marks": marks,
            "named": named, "left_out": left_out}


@chaperone.module("verify.scored", properties={"DESTRUCTIVE": False},
              requests={"restated_query": {"CHECKED": True}, "answer": {"CHECKED": True}, "found": {"CHECKED": True},
                        "supported": {"CHECKED": True}, "search_again": {"CHECKED": True}, "key_concepts": {"CHECKED": True},
                        "primary_concepts": {"CHECKED": True}, "secondary_concepts": {"CHECKED": True}, "search_secondary": {"CHECKED": True},
                        "searched_as": {"CHECKED": True},
                        "search_concepts": {"CHECKED": True}, "searched_again_for": {"CHECKED": True}, "not_searched": {"CHECKED": True},
                        "documents": {"UNTRUSTED": False, "LLM_WRITTEN": False, "ORIGINAL_SOURCE": True},
                        "left_out": {"CHECKED": True}})
def verify_scored(ctx, question, rephrased, primary_concepts, secondary_concepts, chunk_ids, response, chunks_found, search_again_below,
                  drop_above, found_by, judged_relevant, weights, adjustment, levels, shown_first, unanswered, captions):
    """The check of verify.response, and a score for each sentence of each chunk it displays, for the page's layout
    `passages`. The text is the store's, read by the chunk's id; the scores decide only which sentences are displayed
    before a chunk is opened, which words are coloured, and the order of the documents.

    Terms. A term is a word of `question` (the user's), a word of the LLM's `restated_query`, or one of its
    `key_concepts`, whole: a key concept of several words ("supreme court") is one term, matched where its words
    follow each other, and its words are never used apart. The key concepts are the arguments `primary_concepts`
    and `secondary_concepts`, the LLM's two lists of llm.read_question, given before any search (what the question
    is about, in its usual spelling, so a primary concept need not be a word of the question; and its other
    concepts, for ranking). `rephrased` is that call's rephrased question, returned as `searched_as` for the screen.
    A term's weight is the sum of `weights.user_word`,
    `weights.restated_word` and, for a key concept, `weights.key_concept` when it is primary; the secondary ones share
    `weights.key_concept` times `weights.secondary_concepts_total` equally (with 1.0, all of them together weigh
    as one primary concept). That sum is multiplied by the term's adjustment: `adjustment`
    is a list of {above, weight}, read in order; the first whose `above` is less than the share of the store's chunks
    that hold the term gives the weight, and the last one does when none is. No term is left out and none weighs
    nothing unless the workflow writes 0. Terms are matched as the store matches them.

    Scores. A sentence's score is the sum of the weights of the terms in it, each once, plus
    `weights.verified_snippet` when it holds part of a verified snippet. A chunk's score is the sum of its
    `weights.top_sentences` best sentences, plus `weights.llm_rank` divided by the chunk's place in the LLM's
    `relevant` list (first, second, ...), plus, for each term that the title of the chunk's document holds and the
    chunk's text does not, the term's weight times `weights.title_match` (as shipped 0.8: a term in the title counts
    for every chunk of the document, a little less than one in the chunk itself). A document's score is its best chunk's; the documents are returned best
    first, and a document's chunks in the file's order. A sentence ends at ".", "!" or "?" followed by a space and a
    capital letter: a rule for English text, and a wrong cut changes only which words are displayed first.

    Which chunks. When a snippet was verified (`supported`): each chunk that holds a verified snippet or that the LLM
    named relevant. When none was, a chunk is returned only when (1) its id is in `judged_relevant`, the chunks the
    LLM judged to bear on the question (llm.judge_relevance; `[]` for none), and it holds at least
    `unanswered.judged_min_concepts` of the LLM's key concepts, primary or secondary, each whole (as shipped 1: the
    LLM's yes alone is not enough, so with no key concept given nothing is returned on it), or (2) it holds, each
    whole, at least `unanswered.concepts_share` of the key concepts, primary and secondary together, and at least
    `unanswered.min_concepts` of them. For both, a concept in the title of the chunk's document counts as held by
    the chunk. At most
    `unanswered.show` are returned, those of (1) first, then by score; the scores and the colours there are of the
    words of `question` alone. With neither, nothing is returned: a chunk that only shares words with the question
    is not displayed.

    The second search. `search_again` is true when no snippet was verified, or when `chunks_found` (how many chunks
    the search found) is below `search_again_below`. `search_concepts` is what to search for: the key concepts,
    without those that more than `drop_above` of the chunks hold (a concept that common says what the collection is
    about, not what the question is about); `search_concepts` has the primary ones and `search_secondary` the
    secondary ones (rag.search_concepts). `searched_again_for` and `not_searched` are the lists as texts, with
    commas, for the screen.

    Returns the fields of verify.response, those five, `key_concepts` (the LLM's, as texts), `primary_concepts` and
    `secondary_concepts` (the same, split) and `documents` in
    another form: [{title, source, date (when the store gives one), passages: [{where, llm, found_by, pieces}]}].
    `where` is the chunk's paragraphs;
    `llm` is `captions.verified`, `captions.not_verified` or, when no snippet was verified,
    `captions.not_answered.judged` or `captions.not_answered.concepts`, by which of (1) and (2) returned the chunk;
    `found_by` is the text `captions.found_by` gives for the chunk's entry in the argument `found_by` ("question"
    when it has none: `{}` for a first search); `pieces` is the chunk's text in order, each {text, level, quoted,
    first}: `level` is 0, or the `level` of the first of `levels` ({at_least, level}) whose `at_least` the weight of
    the piece's term reaches; `quoted`, whether the piece is part of a verified snippet; `first`, whether its
    sentence is one of the chunk's `shown_first` best."""
    read = _read(ctx, chunk_ids, response)
    supported = bool(read["marks"])
    concepts = [t for t in dict.fromkeys(" ".join(re.findall(r"\w+", c.lower())) for c in read["concepts"]) if t]
    user_words = list(dict.fromkeys(re.findall(r"\w+", question.lower()))) if isinstance(question, str) else []
    def as_terms(items):
        return [t for t in dict.fromkeys(" ".join(re.findall(r"\w+", c.lower())) for c in items) if t]
    def given(items):
        return [c for c in items if isinstance(c, str)] if isinstance(items, list) else []
    # the LLM's two lists, as it gave them before the search (llm.read_question); a reply's own lists, the earlier form, are read too
    primary = as_terms(given(primary_concepts) + read["primary"])
    secondary = [c for c in as_terms(given(secondary_concepts) + read["secondary"]) if c not in primary]
    concepts = primary + secondary
    sources = [("user_word", user_words)]
    if supported:
        sources += [("restated_word", list(dict.fromkeys(re.findall(r"\w+", read["restated"].lower())))), ("key_concept", primary)]
    weight = {}
    for source, terms in sources:
        for term in terms:
            weight[term] = weight.get(term, 0.0) + weights[source]
    if supported:
        for term in secondary:                                  # together, they weigh as `secondary_concepts_total` primary concepts
            weight[term] = weight.get(term, 0.0) + weights["key_concept"] * weights["secondary_concepts_total"] / len(secondary)
    stats = ctx.stats(list(dict.fromkeys(list(weight) + concepts)))

    def share(term):
        return stats.get(term, {}).get("share", 0.0)
    for term in weight:
        bucket = next((b for b in adjustment if share(term) > b["above"]), adjustment[-1])
        weight[term] = round(weight[term] * bucket["weight"], 4)
    search_concepts = [c for c in primary if share(c) <= drop_above]
    search_secondary = [c for c in secondary if share(c) <= drop_above]
    not_searched = [c for c in concepts if share(c) > drop_above]

    if supported:
        kept = [read["chunks"][i] for i in chunk_ids if i in read["chunks"] and (i in read["marks"] or i in read["named"])]
    else:
        kept = [read["chunks"][i] for i in chunk_ids if i in read["chunks"]]
    parts = [_sentences(chunk["text"]) for chunk in kept]
    titled = ctx.find([chunk["title"] for chunk in kept], list(dict.fromkeys(list(weight) + concepts))) if kept else []
    in_title = {chunk["chunk_id"]: {term for _, _, term in spans} for chunk, spans in zip(kept, titled)}     # the terms each chunk's document has in its title
    found = iter(ctx.find([part for chunk_parts in parts for part in chunk_parts], list(weight)))
    scored = []
    for chunk, chunk_parts in zip(kept, parts):
        quoted = read["marks"].get(chunk["chunk_id"], [])
        rows, start = [], 0
        for part in chunk_parts:
            spans = [(a, b, term) for a, b, term in next(found)]
            score = sum(weight[term] for term in {term for _, _, term in spans})
            if any(a < start + len(part) and b > start for a, b in quoted):
                score += weights["verified_snippet"]
            rows.append({"text": part, "start": start, "spans": spans, "score": score})
            start += len(part)
        best = sorted(range(len(rows)), key=lambda i: -rows[i]["score"])
        place = read["named"].index(chunk["chunk_id"]) + 1 if supported and chunk["chunk_id"] in read["named"] else 0
        in_text = {term for row in rows for _, _, term in row["spans"]}
        from_title = sum(weight[term] for term in in_title[chunk["chunk_id"]] - in_text if term in weight) * weights["title_match"]
        score = sum(rows[i]["score"] for i in best[:weights["top_sentences"]]) + (weights["llm_rank"] / place if place else 0.0) + from_title
        scored.append({"chunk": chunk, "rows": rows, "first": set(best[:shown_first]), "quoted": quoted, "score": score,
                       "top": rows[best[0]]["score"] if rows else 0.0})
    if not supported:                                           # no quote found: only chunks the LLM judged relevant, or that hold nearly all its key concepts
        judged_yes = set(judged_relevant) if isinstance(judged_relevant, list) else set()
        held = ctx.find([chunk["text"] for chunk in kept], concepts) if concepts else [[] for _ in kept]
        needed = max(unanswered["min_concepts"], -int(-unanswered["concepts_share"] * len(concepts) // 1))
        for one, spans in zip(scored, held):
            terms = ({term for _, _, term in spans} | in_title[one["chunk"]["chunk_id"]]) & set(concepts)     # held by the chunk, or by its document's title
            one["why"] = ("judged" if one["chunk"]["chunk_id"] in judged_yes and len(terms) >= unanswered["judged_min_concepts"]
                          else "concepts" if len(terms) >= needed else None)
        best = sorted((one for one in scored if one["why"]), key=lambda one: (one["why"] != "judged", -one["score"]))[:unanswered["show"]]
        scored = [one for one in scored if any(one is b for b in best)]

    documents = {}
    for one in scored:
        chunk, quoted, pieces = one["chunk"], one["quoted"], []
        for i, row in enumerate(one["rows"]):
            edges = {0, len(row["text"])}
            for a, b, _ in row["spans"]:
                edges |= {a, b}
            for a, b in quoted:
                edges |= {min(max(a - row["start"], 0), len(row["text"])), min(max(b - row["start"], 0), len(row["text"]))}
            edges = sorted(edges)
            for a, b in zip(edges, edges[1:]):
                heaviest = max((weight[term] for x, y, term in row["spans"] if x <= a and b <= y), default=None)
                pieces.append({"text": row["text"][a:b],
                               "level": 0 if heaviest is None else next((lv["level"] for lv in levels if heaviest >= lv["at_least"]), 0),
                               "quoted": any(x - row["start"] <= a and b <= y - row["start"] for x, y in quoted), "first": i in one["first"]})
        location = chunk["location"]
        where = (f"paragraph {location['first_paragraph']}" if location["first_paragraph"] == location["last_paragraph"]
                 else f"paragraphs {location['first_paragraph']} to {location['last_paragraph']}")
        said = captions["not_answered"][one["why"]] if not supported else captions["verified" if quoted else "not_verified"]
        by = captions["found_by"][found_by.get(chunk["chunk_id"], "question") if isinstance(found_by, dict) else "question"]
        document = documents.setdefault(chunk["document_id"], {"title": chunk["title"], "source": chunk["source"]} | (
            {"date": chunk["date"]} if chunk.get("date") else {}) | {"passages": [], "score": 0.0})      # a document's date, when the store gives one
        document["passages"].append({"where": where, "llm": said, "found_by": by, "pieces": pieces})
        document["score"] = max(document["score"], one["score"])
    ranked = sorted(documents.values(), key=lambda document: -document["score"])
    result = {"restated_query": read["restated"], "answer": read["answer"], "found": read["found"], "supported": supported,
              "search_again": not supported or chunks_found < search_again_below,
              "key_concepts": primary + secondary, "primary_concepts": primary, "secondary_concepts": secondary,
              "searched_as": rephrased if isinstance(rephrased, str) else "",
              "search_concepts": search_concepts, "search_secondary": search_secondary,
              "searched_again_for": ", ".join(search_concepts + search_secondary), "not_searched": ", ".join(not_searched), "left_out": read["left_out"],
              "documents": [{k: v for k, v in document.items() if k != "score"} for document in ranked]}
    return result, [(name, "CHECKED", True) for name in ("restated_query", "answer", "found", "supported", "search_again", "key_concepts",
                                                         "primary_concepts", "secondary_concepts", "search_secondary", "searched_as",
                                                         "search_concepts", "searched_again_for", "not_searched", "left_out")] + [
        ("documents", "UNTRUSTED", False), ("documents", "LLM_WRITTEN", False), ("documents", "ORIGINAL_SOURCE", True)]


def _sentences(text):
    """The text as consecutive sentences: joined, they are the text."""
    cuts = [m.end() for m in re.finditer(r"[.!?][\"')\]]*\s+(?=[\"'(\[]?[A-Z])", text)]
    return [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)]) if b > a]


def _find_words(text, snippet):
    """Where `snippet` is first in `text` as whole words (neither end inside a word), or -1."""
    start = text.find(snippet) if snippet else -1
    while start >= 0:
        end = start + len(snippet)
        if (start == 0 or not (text[start - 1].isalnum() and snippet[0].isalnum())) and \
                (end == len(text) or not (text[end].isalnum() and snippet[-1].isalnum())):
            return start
        start = text.find(snippet, start + 1)
    return -1


def _pieces(text, spans):
    """The text as consecutive pieces, each marked or not; overlapping marks are joined."""
    pieces, at = [], 0
    for start, end in sorted(spans):
        start = max(start, at)
        if end <= start:
            continue
        if start > at:
            pieces.append({"text": text[at:start], "marked": False})
        if pieces and pieces[-1]["marked"] and start == at:
            pieces[-1]["text"] += text[start:end]
        else:
            pieces.append({"text": text[start:end], "marked": True})
        at = end
    if at < len(text):
        pieces.append({"text": text[at:], "marked": False})
    return pieces


@chaperone.module("user.show", properties={"DESTRUCTIVE": False})
def user_show(ctx, report, marks, style=None):
    ctx.show(report, marks, style)


@chaperone.module("user.show_unchecked", properties={"DESTRUCTIVE": False})
def user_show_unchecked(ctx, report, marks, style=None):
    """As user.show, under its own name, so that one exception (UNCHECKED_LLM_TO_USER_EX) can permit the two
    workflows for comparison, `just_ask_the_llm` and `typical_rag`, to display what the LLM wrote, unchecked, and
    permit that to no other call: user.show still displays the LLM's words only after the check."""
    ctx.show(report, marks, style)
