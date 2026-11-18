"""The workflow `multi_pass` of "safe retrieval": the scores of verify.scored, the two methods of the documents
client it uses, and the page's layout `passages` (specification 2.13)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.test_engine import CHUNKS, EXAMPLES, SHOWN, Browser, Scripted, chunk, entry, permits, start
from chaperone.web import render_report

WORKFLOW = "multi_pass"
QUESTION = "Where does power come from?"
REPLY = {"restated_query": "Where does power come from?", "primary_concepts": ["people", 7], "secondary_concepts": ["legitimate fountain"], "answer_found": True,
         "relevant": [2, 1], "snippets": [{"id": 1, "snippet": "the only legitimate fountain of power"}], "answer": "From the people."}
FILLERS = [chunk(60 + n, 1, f"Filler text number {n}.", 1.0 - n / 10) for n in range(3)]     # so that a search finds five chunks: no second search for few
BOTH, BY_QUESTION, BY_CONCEPTS = "found by your question and by what the LLM added to it", "found by your question", "found by what the LLM added to your question"


def store(chunks: list[dict[str, Any]] = CHUNKS + FILLERS, shares: dict[str, float] | None = None, by_terms: list[dict[str, Any]] | None = None) -> Scripted:
    """A store. `search` gives `chunks`; `find` matches a term where the text has its words, in order; `stats` gives
    `shares` (0 for a term not named); `search_terms` gives `by_terms` (the first two chunks when not given), whatever
    the terms; `get` knows the chunks of both."""
    known = chunks + [c for c in by_terms or [] if c not in chunks]
    def find(texts: list[str], terms: list[str]) -> list[list[list[Any]]]:
        return [sorted([m.start(), m.end(), term] for term in terms for m in re.finditer(r"\b" + r"\s+".join(map(re.escape, term.split())) + r"\b", text.lower()))
                for text in texts]
    return Scripted(search=lambda query, limit, exclude: [c for c in chunks if c["chunk_id"] not in exclude][:limit],
                    get=lambda ids: [dict(c, score=0) for i in ids for c in known if c["chunk_id"] == i], files=lambda: 2,
                    stats=lambda terms: {t: {"count": int(100 * (shares or {}).get(t, 0.0)), "share": (shares or {}).get(t, 0.0)} for t in terms},
                    find=find, overview=lambda: "Made-up papers.",
                    search_terms=lambda terms, limit, exclude, min_phrase_count: list(chunks[:2] if by_terms is None else by_terms))


def tools(*replies: dict[str, Any], docs: Scripted | None = None, question: str = QUESTION, judge: str | None = None,
          read: dict[str, Any] | None = None) -> dict[str, Any]:
    """The LLM gives `replies` in turn, the last one again after that (REPLY when none is given)."""
    left = [json.dumps(reply) for reply in replies or (REPLY,)]
    return {"BROWSER": Browser(question), "DOCS_DB": docs or store(),
            "LLM": Scripted(ask_with_concepts=lambda **named: left.pop(0) if len(left) > 1 else left[0],
                            read_question=lambda **named: dict(read or {}),        # before the search: by default no rephrased question and no concepts
                            judge_relevance=judge if judge is not None else '{"1": true}')}     # by default no chunk is judged not to bear on the question


def calls_of(tool: Scripted, method: str) -> list[Any]:
    return [arguments for name, arguments in tool.calls if name == method]


def shows(run: Any) -> list[str]:
    return [e["state"] for e in run.log if e["call"] == "user.show"]


def test_the_screen_is_the_question_the_llms_words_then_the_documents_text_from_the_store(tmp_path: Path) -> None:
    used = tools()
    run = start(EXAMPLES / "safe_rag", tmp_path, used)
    assert run.run(WORKFLOW).state == "DONE" and shows(run) == ["SHOW"] * 3                    # five chunks found, a quote found: one search
    assert entry(run, "verify.scored")["requests"] == [[f, "CHECKED", True] for f in (
        "restated_query", "answer", "found", "supported", "search_again", "key_concepts", "primary_concepts", "secondary_concepts", "search_secondary", "searched_as",
        "search_concepts", "searched_again_for", "not_searched", "left_out")] + [["documents", "UNTRUSTED", False], ["documents", "LLM_WRITTEN", False], ["documents", "ORIGINAL_SOURCE", True]]
    (said, said_marks), (shown, marks), (closing, closing_marks) = used["BROWSER"].shown
    # the LLM's two lists of key concepts are on the screen, as it gave them (an item that is not a text is left out), marked as its own
    assert (said.pop("primary_concepts_of_the_llm"), said.pop("secondary_concepts_of_the_llm")) == (["people"], ["legitimate fountain"])
    assert all(m == SHOWN for m in said_marks.pop("primary_concepts_of_the_llm") + said_marks.pop("secondary_concepts_of_the_llm"))
    assert said.pop("also_searched_as") == "" and said_marks.pop("also_searched_as") is not None      # the scripted LLM rephrased nothing before the search
    assert said == {"your_question": QUESTION, "question_as_the_llm_read_it": "Where does power come from?", "answer": "From the people."}
    assert said_marks == {"your_question": [], "question_as_the_llm_read_it": SHOWN, "answer": SHOWN}       # the user's words carry no label
    assert permits(run, "user.show", 0) == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "SHOW_CHECKED_EX"} and permits(run, "user.show", 1) == {}   # the documents: no rule forbids
    # the chunk with the verified snippet outranks the one the LLM put first; each chunk's pieces, joined, are the store's text
    assert [(d["title"], d["source"]) for d in shown] == [("FEDERALIST No. 49", "federalist_49.txt"), ("FEDERALIST No. 51", "federalist_51.txt")]
    first, second = (d["passages"][0] for d in shown)
    assert "".join(p["text"] for p in first["pieces"]) == CHUNKS[0]["text"] and "".join(p["text"] for p in second["pieces"]) == CHUNKS[1]["text"]
    assert (first["where"], first["llm"], first["found_by"]) == ("paragraph 2", "a quote of the LLM's was found here", BY_QUESTION)
    assert second["llm"] == "named relevant by the LLM; no quote of it was found here"
    assert "".join(p["text"] for p in first["pieces"] if p["quoted"]) == "the only legitimate fountain of power"
    assert not any(p["quoted"] for p in second["pieces"])
    levels = {p["text"]: p["level"] for p in first["pieces"] if p["level"]}
    assert levels == {"people": 2, "legitimate fountain": 2, "power": 3}      # a key concept of two words is one term, coloured as one piece; one primary concept, and one secondary that has the whole of the secondary weight; power: the user's word and the restated one
    assert all(set(labels) == {"ORIGINAL_SOURCE"} for d in marks for p in d["passages"] for piece in p["pieces"] for labels in piece.values())
    assert marks[0]["title"] == ["ORIGINAL_SOURCE"]
    assert closing["left_out"] == {"numbers_that_name_no_chunk": 0, "snippets_not_in_their_chunk": 0} and closing_marks["note"] == []
    (asked,), = calls_of(used["LLM"], "ask_with_concepts") and [calls_of(used["LLM"], "ask_with_concepts")]
    assert asked[0] == QUESTION and asked[2] == "Made-up papers." and asked[3] == ""        # the setup's overview reaches the prompt; no note in the first round


def test_before_the_search_the_llm_reads_the_question_and_what_it_gives_is_searched_as_well_never_instead(tmp_path: Path) -> None:
    read = {"rephrased_query": "Where does power originate?", "primary_concepts": ["people", 7], "supportive_concepts": ["legitimate fountain"]}
    used = tools(read=read)
    run = start(EXAMPLES / "safe_rag", tmp_path, used)
    assert run.run(WORKFLOW).state == "DONE" and shows(run) == ["SHOW"] * 3
    # three searches: the question as typed, the rephrased one, the primary concepts that are texts; the supportive one is not searched
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search")] == [QUESTION, "Where does power originate?"]
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search_terms")] == [["people"]]
    (answer_call,) = calls_of(used["LLM"], "ask_with_concepts")
    assert QUESTION in repr(answer_call) and "originate" not in repr(answer_call)          # the LLM answers the question as typed
    (said, said_marks), (shown, _), _ = used["BROWSER"].shown
    assert said["your_question"] == QUESTION and said_marks["your_question"] == []         # the typed question is displayed, unchanged and unlabelled
    assert said["also_searched_as"] == "Where does power originate?" and said_marks["also_searched_as"] == SHOWN     # the LLM's, marked so
    assert (said["primary_concepts_of_the_llm"], said["secondary_concepts_of_the_llm"]) == (["people"], ["legitimate fountain"])
    assert {p["found_by"] for d in shown for p in d["passages"]} <= {BOTH, BY_QUESTION, BY_CONCEPTS}
    # a reply that is no object: no rephrased question, no concepts, one search, and the run goes on
    used = tools(read={"rephrased_query": 3, "primary_concepts": "people"})
    start(EXAMPLES / "safe_rag", tmp_path / "bad", used).run(WORKFLOW)
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search")] == [QUESTION] and used["BROWSER"].shown[0][0]["also_searched_as"] == ""


def test_single_pass_is_multi_pass_without_the_second_round(tmp_path: Path) -> None:
    used = tools()
    run = start(EXAMPLES / "safe_rag", tmp_path / "found", used)
    assert run.run("single_pass").state == "DONE" and shows(run) == ["SHOW"] * 3                   # a quote found: the answer and the documents
    assert used["BROWSER"].shown[0][0]["answer"] == "From the people."
    # no quote found: no answer, one LLM call for the answer, no second search, no relevance call
    used = tools(dict(REPLY, snippets=[{"id": 1, "snippet": "not in any chunk"}]), question="power people")
    run = start(EXAMPLES / "safe_rag", tmp_path / "none", used)
    assert run.run("single_pass").state == "DONE" and shows(run) == ["NOTHING_FOUND"] * 2
    assert "answer" not in used["BROWSER"].shown[0][0]
    assert len(calls_of(used["LLM"], "ask_with_concepts")) == 1 and calls_of(used["LLM"], "judge_relevance") == []
    assert all(e["state"] not in ("SEARCH_2", "JUDGE", "ANSWER_2") for e in run.log)


def test_a_frequent_word_weighs_less_and_never_nothing_and_a_sentence_the_llm_did_not_quote_can_be_first(tmp_path: Path) -> None:
    text = "The power of the purse is here. Power comes from the people and power returns to the people. The rest is detail."
    chunks = [chunk(1, 1, text, 5.0)] + CHUNKS[1:] + FILLERS
    reply = dict(REPLY, primary_concepts=[], secondary_concepts=[], relevant=[1], snippets=[{"id": 1, "snippet": "The rest is detail."}])
    used = tools(reply, docs=store(chunks, shares={"power": 0.5, "people": 0.02}), question="power people")
    start(EXAMPLES / "safe_rag", tmp_path, used).run(WORKFLOW)
    pieces = used["BROWSER"].shown[1][0][0]["passages"][0]["pieces"]
    assert "".join(p["text"] for p in pieces) == text
    # shown first: the quoted sentence (4.0) and the one with both words (0.15 + 0.6); the first sentence (0.15) is not
    assert "".join(p["text"] for p in pieces if p["first"]) == "Power comes from the people and power returns to the people. The rest is detail."
    by_word = {p["text"].lower(): p["level"] for p in pieces if p["text"].lower() in ("power", "people")}
    assert by_word == {"power": 0, "people": 2}       # power: in half the chunks, 1.5 x 0.1, scored and not coloured; people: 1.0 x 0.6
    # the counts for the LLM's first call (the question's words and each two that follow each other), then the scores' (the user's words, then the restated ones)
    assert calls_of(used["DOCS_DB"], "stats") == [(["power", "people", "power people"],), (["power", "people", "where", "does", "come", "from"],)]


def test_with_no_quote_found_the_key_concepts_are_searched_each_left_out_in_turn_and_a_passage_needs_the_llms_yes_or_the_concepts(tmp_path: Path) -> None:
    reply = dict(REPLY, snippets=[{"id": 1, "snippet": "not in any chunk"}], relevant=[1])
    used = tools(reply, question="power people")
    run = start(EXAMPLES / "safe_rag", tmp_path / "two", used)
    assert run.run(WORKFLOW).state == "DONE" and shows(run) == ["NOTHING_FOUND", "NOTHING_FOUND"]
    # one search for the key concepts that are texts, the primary one first; a concept of two words stays whole.
    # One primary concept: no search with a concept left out, and the secondary one is never searched alone
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search_terms")] == [["people", "legitimate fountain"]]
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search")] == ["power people"] and permits(run, "rag.search_concepts") == {}
    (said, _), (shown, marks) = used["BROWSER"].shown
    assert "answer" not in said and said["your_question"] == "power people" and said["searched_again_for"] == "people, legitimate fountain"   # with commas
    # no answer; the one chunk the LLM judged to bear on the question ({"1": true}) is displayed, and says so; the
    # words coloured are the user's own: "power" is, the LLM's concepts are not
    (document,) = shown
    (passage,) = document["passages"]
    assert document["title"] == "FEDERALIST No. 49" and passage["found_by"] == BOTH
    assert passage["llm"] == "judged by the LLM to bear on your question, and holds one or more of its key concepts; no quote of the LLM's was found here"
    # the LLM's yes alone is not enough: a chunk judged to bear on the question that holds none of the key concepts is not displayed
    used = tools(dict(reply, primary_concepts=["rayleigh scattering"], secondary_concepts=["sky blue"]), question="power people")
    start(EXAMPLES / "safe_rag", tmp_path / "yes_alone", used).run(WORKFLOW)
    assert used["BROWSER"].shown[1][0] == []
    assert {p["text"]: p["level"] for p in passage["pieces"] if p["level"]} == {"power": 2, "people": 2} and not any(p["quoted"] for p in passage["pieces"])
    assert all(set(labels) == {"ORIGINAL_SOURCE"} for p in marks[0]["passages"] for piece in p["pieces"] for labels in piece.values())
    # two primary concepts: one search for all the concepts, the secondary one with them, then one for each primary one left out
    both = dict(reply, primary_concepts=["power", "people"])
    used = tools(both, question="power people", judge='{"1": false, "2": false}')
    start(EXAMPLES / "safe_rag", tmp_path / "concepts", used).run(WORKFLOW)
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search_terms")] == [["power", "people", "legitimate fountain"], ["people"], ["power"]]
    # judged "no", but the chunk holds both primary concepts: displayed, and says why. The secondary one is not required
    ((passage,),) = [d["passages"] for d in used["BROWSER"].shown[1][0]]
    assert passage["llm"] == "holds nearly all of the LLM's key concepts; no quote of the LLM's was found here"
    # judged "no", and the chunks hold none of the key concepts: nothing is displayed, though they hold the question's words
    used = tools(dict(reply, primary_concepts=["rayleigh scattering"], secondary_concepts=["sky blue"]), question="power people", judge='{"1": false, "2": false}')
    start(EXAMPLES / "safe_rag", tmp_path / "none", used).run(WORKFLOW)
    assert used["BROWSER"].shown[1][0] == []
    # a concept in the document's title counts for its chunks: "federalist" is in no chunk's text, "power" is in the chunk
    used = tools(dict(reply, primary_concepts=["federalist"], secondary_concepts=["power"]), question="power people", judge='{"1": false, "2": false}')
    start(EXAMPLES / "safe_rag", tmp_path / "title", used).run(WORKFLOW)
    assert [p["llm"] for d in used["BROWSER"].shown[1][0] for p in d["passages"]][:1] == ["holds nearly all of the LLM's key concepts; no quote of the LLM's was found here"]
    # one key concept held of one is not enough (`min_concepts: 2`)
    used = tools(dict(reply, primary_concepts=["people"], secondary_concepts=[]), question="power people", judge='{"1": false, "2": false}')
    start(EXAMPLES / "safe_rag", tmp_path / "one", used).run(WORKFLOW)
    assert used["BROWSER"].shown[1][0] == []


def test_a_reply_with_no_key_concept_is_searched_again_by_its_restated_question(tmp_path: Path) -> None:
    for concepts in ({}, {"primary_concepts": []}, {"primary_concepts": "people"}, {"secondary_concepts": [3, None]}, {"key_concepts": []}):
        reply = {k: v for k, v in REPLY.items() if k not in ("primary_concepts", "secondary_concepts")} | concepts | {"snippets": [], "restated_query": "What is the source of power?"}
        used = tools(reply)
        start(EXAMPLES / "safe_rag", tmp_path / str(len(str(concepts))), used).run(WORKFLOW)
        assert [a[0] for a in calls_of(used["DOCS_DB"], "search")] == [QUESTION, "What is the source of power?"], concepts
        assert calls_of(used["DOCS_DB"], "search_terms") == []


def test_few_chunks_found_is_a_second_search_and_each_passage_says_which_search_found_it(tmp_path: Path) -> None:
    extra = chunk(70, 3, "A single executive is the people's own fountain.", 2.0)
    again = dict(REPLY, relevant=[1, 2, 3], snippets=[{"id": 1, "snippet": "the only legitimate fountain of power"}, {"id": 2, "snippet": "A single executive"}])
    used = tools(REPLY, again, docs=store(CHUNKS, by_terms=[CHUNKS[0], extra]))             # two chunks found, below five: a second search, though a quote was found
    run = start(EXAMPLES / "safe_rag", tmp_path / "a", used)
    assert run.run(WORKFLOW).state == "DONE" and shows(run) == ["SHOW_2"] * 3
    (said, _), (shown, _), _ = used["BROWSER"].shown
    assert said["searched_again_for"] == "people, legitimate fountain" and said["key_concepts_not_searched_as_too_common"] == ""
    first_call, second_call = calls_of(used["LLM"], "ask_with_concepts")
    assert second_call[0] == QUESTION == first_call[0] and "likely not relevant" in second_call[3]     # the user's own question again, and the workflow's note
    found_by = {d["title"]: [p["found_by"] for p in d["passages"]] for d in shown}
    assert found_by == {"FEDERALIST No. 49": [BOTH], "FEDERALIST No. 51": [BY_QUESTION], "FEDERALIST No. 70": [BY_CONCEPTS]}
    # the second round finds no quote: the first round's answer is displayed, since it had one
    nothing = dict(REPLY, snippets=[{"id": 1, "snippet": "not in any chunk"}])
    used = tools(REPLY, nothing, docs=store(CHUNKS))
    run = start(EXAMPLES / "safe_rag", tmp_path / "b", used)
    assert run.run(WORKFLOW).state == "DONE" and shows(run) == ["SHOW"] * 3 and used["BROWSER"].shown[0][0]["answer"] == "From the people."


def test_a_key_concept_that_most_chunks_hold_is_not_searched_for_and_the_screen_says_so(tmp_path: Path) -> None:
    reply = dict(REPLY, primary_concepts=["legitimate fountain", "people"], secondary_concepts=[], snippets=[])
    used = tools(reply, docs=store(shares={"people": 0.5}))                                     # in half the chunks, above drop_above (0.20)
    run = start(EXAMPLES / "safe_rag", tmp_path, used)
    run.run(WORKFLOW)
    assert [a[0] for a in calls_of(used["DOCS_DB"], "search_terms")] == [["legitimate fountain"]]      # one concept left: one search
    said = used["BROWSER"].shown[0][0]
    assert said["searched_again_for"] == "legitimate fountain" and said["key_concepts_not_searched_as_too_common"] == "people"


def test_the_documents_client_counts_terms_finds_them_and_never_searches_part_of_a_phrase(tmp_path: Path) -> None:
    from chaperone.clients import Documents
    folder = tmp_path / "law"
    folder.mkdir()
    (folder / "a.txt").write_text("Title\n\nThe supreme court decides. A court of law.\n\nThe supreme being; the court.\n\nNothing here.")
    docs = Documents("DOCS_DB", folder, tmp_path / "law.sqlite", [".txt"], 30, "porter", "  Notes on   courts. ")
    assert docs.overview() == "Notes on courts."
    assert docs.stats(["supreme court", "courts", "zebra", "a-b", "court"]) == {
        "supreme court": {"count": 1, "share": 0.25}, "courts": {"count": 2, "share": 0.5}, "zebra": {"count": 0, "share": 0.0},
        "court": {"count": 2, "share": 0.5}}                                                    # "a-b" is no term; with porter, "courts" is "court"
    assert docs.find(["The Supreme Courts and a court.", "no match"], ["supreme court", "court"]) == [
        [[4, 18, "supreme court"], [12, 18, "court"], [25, 30, "court"]], []]
    ids = lambda found: [c["chunk_id"] for c in found]                                          # noqa: E731
    assert ids(docs.search_terms(["supreme court"], 5, [], 1)) == ["a.txt#2"]                   # as a phrase: one chunk holds it
    assert ids(docs.search_terms(["supreme court"], 5, [], 5)) == ["a.txt#3", "a.txt#2"]        # too rare as a phrase: both words, anywhere in a chunk
    assert ids(docs.search_terms(["supreme zebra"], 5, [], 5)) == []                            # never "supreme" without the other word
    assert ids(docs.search_terms(["supreme court", "law"], 5, ["a.txt#2"], 1)) == [] and docs.search_terms(['"; drop table chunks; --'], 5, [], 1) == []
    import pytest
    from chaperone.values import Problem
    with pytest.raises(Problem, match="`overview` is a text"):
        Documents("DOCS_DB", folder, tmp_path / "law.sqlite", [".txt"], 30, "porter", None)


UI = {"labels": {"UNTRUSTED": {"words": "untrusted", "colour": "#b3261e"}, "ORIGINAL_SOURCE": {"words": "from the documents", "colour": "#1e7a1e"}},
      "styles": {"fields": {"layout": "one_part_per_line"}, "passages": {"layout": "passages"}}, "default_style": "fields"}
HOSTILE = '<script>alert(1)</script><img src=x onerror=alert(2)>'


def passages(pieces: list[dict[str, Any]], **group: Any) -> list[dict[str, Any]]:
    return [{"title": "Paper", "source": "paper.txt", **group, "passages": [{"where": "paragraph 2", "pieces": pieces}]}]


def marked(value: Any, labels: list[str]) -> Any:
    return ({k: marked(v, labels) for k, v in value.items()} if isinstance(value, dict)
            else [marked(v, labels) for v in value] if isinstance(value, list) else list(labels))


def test_the_layout_passages_draws_a_box_per_group_closed_on_the_first_pieces_and_escapes_every_text() -> None:
    report = passages([{"text": "Hidden start. ", "level": 0, "quoted": False, "first": False},
                       {"text": "The ", "level": 0, "quoted": True, "first": True}, {"text": HOSTILE, "level": 3, "quoted": True, "first": True},
                       {"text": " Hidden end.", "level": 9, "quoted": "yes", "first": False}], title=HOSTILE)
    drawn = render_report(report, marked(report, ["ORIGINAL_SOURCE"]), UI, "passages")
    assert "<script>" not in drawn and "<img" not in drawn and drawn.count("&lt;script&gt;") == 3      # the title; the piece, closed and opened
    assert 'class="group lab1"' in drawn and drawn.count("from the documents") == 1                    # the labels once, in the group's colour
    closed = drawn.split('<span class="first">')[1].split("</summary>")[0]
    assert closed.startswith('<span class="gap">[…]</span> <span class="em0 quoted">The </span><span class="em3 quoted">')
    assert closed.rstrip("</span>").endswith('<span class="gap">[…]') and "Hidden" not in closed
    whole = drawn.split('<div class="whole">')[1]
    assert '<span class="em0 rest">Hidden start. </span>' in whole and '<span class="em0 rest"> Hidden end.</span>' in whole   # a level that is not 0 to 3 is 0; "yes" is not true
    assert "paragraph 2" in drawn and "<details" in drawn and "<script" not in drawn


def test_the_layout_passages_draws_any_other_value_as_any_value() -> None:
    report = passages([{"text": "Some text.", "level": 1, "quoted": False, "first": True}])
    marks = marked(report, ["ORIGINAL_SOURCE"])
    marks[0]["passages"][0]["pieces"][0]["text"] = ["UNTRUSTED"]                                   # the parts of a group carry different labels
    drawn = render_report(report, marks, UI, "passages")
    assert "<details" not in drawn and 'class="chip lab0">untrusted' in drawn and "Some text." in drawn   # each part with its own labels
    for other in ([], [{"title": "t", "passages": [{"pieces": [{"text": 5}]}]}], {"note": "x"}, [{"title": {"nested": 1}, "passages": []}]):
        assert "<details" not in render_report(other, {}, UI, "passages")


# ---- the two workflows for comparison: the LLM's answer, unchecked ---------------------------------------------------

UNCHECKED = ["LLM_WRITTEN", "UNTRUSTED"]


def test_just_ask_the_llm_displays_the_llms_answer_unchecked_and_reads_no_document(tmp_path: Path) -> None:
    used = {"BROWSER": Browser("Where does power come from?"), "LLM": Scripted(ask_alone="From the people."), "DOCS_DB": store()}
    run = start(EXAMPLES / "safe_rag", tmp_path, used)
    assert run.run("just_ask_the_llm").state == "DONE"
    (screen, marks), = used["BROWSER"].shown
    assert screen["your_question"] == "Where does power come from?" and screen["answer"] == "From the people."
    assert marks["your_question"] == [] and marks["answer"] == UNCHECKED and marks["note"] == []      # no CHECKED: nothing read it
    assert permits(run, "user.show_unchecked") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "UNCHECKED_LLM_TO_USER_EX"}
    assert used["DOCS_DB"].calls == [] and used["LLM"].calls == [("ask_alone", ("Where does power come from?",))]


def test_typical_rag_passes_the_chunks_to_the_llm_and_displays_only_its_answer(tmp_path: Path) -> None:
    answer = 'Power comes from the people: "the people are the sole fountain of power" (FEDERALIST No. 49).'   # a quote that is not in the chunk
    used = {"BROWSER": Browser("Where does power come from?"), "LLM": Scripted(ask_plainly=answer), "DOCS_DB": store(CHUNKS)}
    run = start(EXAMPLES / "safe_rag", tmp_path, used)
    assert run.run("typical_rag").state == "DONE"
    (screen, marks), = used["BROWSER"].shown
    assert screen["answer"] == answer and marks["answer"] == UNCHECKED            # displayed as written, marked the LLM's and untrusted
    assert set(screen) == {"your_question", "answer", "chunks_passed_to_the_llm", "note"} and screen["chunks_passed_to_the_llm"] == 2
    (_, (question, chunks)), = used["LLM"].calls
    assert question == "Where does power come from?" and "[1] the people are the only legitimate fountain of power" in chunks
    assert [method for method, _ in used["DOCS_DB"].calls] == ["search", "files"]     # the search; the check's `get` is never called
    assert permits(run, "user.show_unchecked") == {"NO_UNTRUSTED_DATA_TO_OUTPUT": "UNCHECKED_LLM_TO_USER_EX"}


def test_the_exception_for_the_comparison_names_one_module_and_user_show_still_needs_the_check(tmp_path: Path) -> None:
    from chaperone import load_project, prerun
    from tests.test_engine import copy_of
    exception, = [e for e in load_project(EXAMPLES / "safe_rag").exceptions if e["id"] == "UNCHECKED_LLM_TO_USER_EX"]
    assert exception["except"] == {"call": ["user.show_unchecked"]}
    result = prerun.check(load_project(EXAMPLES / "safe_rag"))
    assert result.permits["UNCHECKED_LLM_TO_USER_EX"] == ["just_ask_the_llm.SHOW: user.show_unchecked", "typical_rag.SHOW: user.show_unchecked"]
    edited = copy_of(tmp_path, "safe_rag", ("workflows/typical_rag.yaml", "{call: user.show_unchecked, with", "{call: user.show, with"))
    refused = prerun.check(load_project(edited)).refused
    assert [(r["call"], r["rule"]) for r in refused] == [("typical_rag.SHOW: user.show", "NO_UNTRUSTED_DATA_TO_OUTPUT")]


# ---- the second round: the LLM is asked which chunks bear on the question ---------------------------------------------

def test_the_llm_is_asked_which_chunks_of_the_second_search_bear_on_the_question_and_those_documents_go_first(tmp_path: Path) -> None:
    extra = chunk(70, 3, "A single executive is the people's own fountain.", 2.0)
    again = dict(REPLY, relevant=[1], snippets=[{"id": 1, "snippet": "ambition must be made to counteract ambition"}])
    used = tools(REPLY, again, docs=store(CHUNKS, by_terms=[CHUNKS[0], extra]), judge='Sure: {"1": false, "2": false, "3": true}')   # of 49, 70, 51: only 51 bears on it
    run = start(EXAMPLES / "safe_rag", tmp_path, used)
    assert run.run(WORKFLOW).state == "DONE" and shows(run) == ["SHOW_2"] * 3
    (judged,) = calls_of(used["LLM"], "judge_relevance")
    assert judged[0] == QUESTION and "[2] A single executive" in judged[1] and judged[2] == "Made-up papers."     # the user's own question, the chunks, the overview
    passed = calls_of(used["LLM"], "ask_with_concepts")[1][1]                                                     # the answer call: 51 first, and nothing left out
    assert passed.index("[1] ambition must be made") < passed.index("[2] the people are") < passed.index("[3] A single executive")
    assert [d["title"] for d in used["BROWSER"].shown[1][0]] == ["FEDERALIST No. 51"]                              # the quote the LLM then gave is in chunk 1
    assert "the documents it named were put first" in used["BROWSER"].shown[2][0]["note"]


def test_a_relevance_reply_that_cannot_be_read_keeps_every_chunk_and_ask_false_asks_nothing() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("safe_rag_modules_for_test", EXAMPLES / "safe_rag" / "modules.py")
    modules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modules)
    documents = [{"title": "A", "chunks": [{"chunk_id": "a#1", "text": "one"}, {"chunk_id": "a#2", "text": "two"}]}, {"title": "B", "chunks": [{"chunk_id": "b#1", "text": "three"}]}]
    ids, by = ["a#1", "a#2", "b#1"], {"a#1": "both", "a#2": "question", "b#1": "concepts"}
    judge = lambda reply, ask=True, drop=True: modules.llm_judge_relevance(Scripted(judge_relevance=reply), "q", documents, ids, by, "", ask, drop)   # noqa: E731
    kept = judge('{"relevant": [3, 1]}')
    assert (kept["chunk_ids"], kept["found_by"], kept["judged"], kept["passed"], kept["kept"]) == (["a#1", "b#1"], {"a#1": "both", "b#1": "concepts"}, "asked", 3, 2)
    assert set(kept["relevant"]) <= {"a#1", "b#1"}                                   # only chunks whose entry is true; one the reply does not name is kept and is not in it
    assert [[c["chunk_id"] for c in d["chunks"]] for d in kept["documents"]] == [["a#1"], ["b#1"]]               # in the order they had
    assert judge('{"relevant": []}')["chunk_ids"] == [] and judge('{"relevant": []}')["documents"] == []
    assert judge('```json\n{"1": true, "2": false, "3": true}\n```')["chunk_ids"] == ["a#1", "b#1"]      # the form asked for: an entry for each chunk
    assert judge('{"1": false}')["chunk_ids"] == ["a#2", "b#1"]                                          # a chunk the reply does not name is kept
    for unreadable in ("I think 1 and 3.", '{"chunks": [1]}', '{"relevant": 2}', "{}"):
        assert (judge(unreadable)["chunk_ids"], judge(unreadable)["judged"]) == (ids, "reply not readable"), unreadable
    moved = judge('{"1": false, "2": false, "3": true}', drop=False)                                     # not dropped: the document with a yes comes first
    assert (moved["chunk_ids"], moved["kept"], moved["passed"], moved["found_by"]) == (["b#1", "a#1", "a#2"], 1, 3, by)
    tool = Scripted(judge_relevance='{"relevant": [1]}')
    not_asked = modules.llm_judge_relevance(tool, "q", documents, ids, by, "", False, True)
    assert (not_asked["chunk_ids"], not_asked["judged"], tool.calls) == (ids, "not asked", [])


# ---- documents in a database a loader wrote: dates, a filter and a boost for the later ones ---------------------------

def loaded(tmp_path: Path, rows: list[tuple[str, str, str, str, str]]) -> Path:
    import sqlite3
    file = tmp_path / "loaded.sqlite"
    db = sqlite3.connect(file)
    db.execute("create table if not exists documents (document_id text primary key, title text, source text, date text, text text, more text)")
    db.executemany("insert or replace into documents values (?, ?, ?, ?, ?, '{}')", rows)
    db.commit()
    db.close()
    return file


ROWS = [("2501.00001", "Agents that plan", "https://arxiv.org/abs/2501.00001", "2025-01-10", "We study planning agents. Agents plan with tools."),
        ("2506.00002", "Planning agents, again", "https://arxiv.org/abs/2506.00002", "2025-06-10", "We study planning agents. Agents plan with tools!"),
        ("2511.00003", "Retrieval", "https://arxiv.org/abs/2511.00003", "2025-11-10", "A study of retrieval.\n\nSecond paragraph on agents."),
        ("old/9901001", "No date", "somewhere", "", "Planning agents of long ago.")] + [
    (f"2503.{n:05}", f"Filler {n}", "s", "2025-03-01", f"Unrelated text number {n} about something else entirely.") for n in range(40)]     # enough documents for scores above zero


def test_a_database_of_documents_is_searched_with_its_dates_a_filter_and_a_boost(tmp_path: Path) -> None:
    from chaperone.clients import DocumentDb
    file = loaded(tmp_path, ROWS)
    client = lambda not_before="", boost=0.0, block=2500: DocumentDb("DOCS_DB", file, tmp_path / "index.sqlite", block, "porter", "Papers.", not_before, boost)   # noqa: E731
    docs = client()
    assert docs.files() == 44 and docs.overview() == "Papers."
    found = docs.search("planning agents tools", 5, [])
    assert {c["document_id"] for c in found[:2]} == {"2501.00001", "2506.00002"} and found[0]["score"] > 0
    assert [c["document_id"] for c in docs.search("planning agents tools", 2, [])] == ["2501.00001", "2506.00002"]            # without a boost: the earlier one fits a little better
    first = next(c for c in found if c["document_id"] == "2501.00001")
    assert first == {"document_id": "2501.00001", "title": "Agents that plan", "source": "https://arxiv.org/abs/2501.00001", "chunk_id": "2501.00001#1",
                     "location": {"first_paragraph": 1, "last_paragraph": 1}, "text": "We study planning agents. Agents plan with tools.",
                     "score": first["score"], "date": "2025-01-10"}
    assert "date" not in next(c for c in docs.search("long ago", 5, []))                              # a document with no date
    assert [c["document_id"] for c in docs.search("retrieval", 5, [])] == ["2511.00003"]               # its title is searched too ("Retrieval")
    assert docs.get(["2511.00003#1"])[0]["date"] == "2025-11-10"
    # the boost: of two chunks that fit equally, the later comes first; without it, the order is the store's
    assert [c["document_id"] for c in client(boost=0.5).search("planning agents tools", 2, [])] == ["2506.00002", "2501.00001"]
    # the filter: nothing dated before the day, and nothing with no date
    assert {c["document_id"] for c in client(not_before="2025-06-01").search("planning agents", 5, [])} == {"2506.00002", "2511.00003"}
    assert {c["document_id"] for c in client(not_before="2025-06-01").search_terms(["planning agents"], 5, [], 1)} == {"2506.00002"}
    # a text is split into chunks as a file's is, linked by the document's id and their order
    assert [c["chunk_id"] for c in client(block=25).get(["2511.00003#1", "2511.00003#2"])] == ["2511.00003#1", "2511.00003#2"]
    # the index is made again when the database changed: one file is looked at
    loaded(tmp_path, [("2512.00004", "New", "s", "2025-12-01", "A new paper on planning agents and tools.")])
    assert docs.files() == 45 and "2512.00004" in {c["document_id"] for c in docs.search("planning agents tools", 5, [])}


def test_a_database_client_refuses_what_it_cannot_use(tmp_path: Path) -> None:
    import pytest
    from chaperone.clients import DocumentDb
    from chaperone.values import Problem
    file = loaded(tmp_path, ROWS)
    make = lambda *a: DocumentDb("DOCS_DB", *a)                                                       # noqa: E731
    with pytest.raises(Problem, match="does not exist; a loader script writes it"):
        make(tmp_path / "none.sqlite", tmp_path / "i.sqlite", 2500, "porter", "", "", 0.0)
    with pytest.raises(Problem, match="`not_before` is a day written year-month-day"):
        make(file, tmp_path / "i.sqlite", 2500, "porter", "", "last year", 0.0)
    with pytest.raises(Problem, match="`recency_boost` is a number, 0 or above"):
        make(file, tmp_path / "i.sqlite", 2500, "porter", "", "", -1)
    (tmp_path / "empty.sqlite").write_bytes(b"")
    with pytest.raises(Problem, match="has no table `documents`"):
        make(tmp_path / "empty.sqlite", tmp_path / "i.sqlite", 2500, "porter", "", "", 0.0).files()


def test_safe_retrieval_under_the_setup_arxiv_loads_and_no_call_would_be_refused() -> None:
    from chaperone import load_project, prerun
    project = load_project(EXAMPLES / "safe_rag", "arxiv")
    assert project.tools["DOCS_DB"]["client"]["kind"] == "document_db"
    assert prerun.check(project).refused == []


def test_the_arxiv_loader_reads_a_page_and_dates_a_paper_by_its_first_version() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("load_arxiv_for_test", EXAMPLES.parent / "scripts" / "load_arxiv.py")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    assert [loader.set_of(c) for c in ("cs.AI", "cs", "stat.ML", "hep-th", "physics", "astro-ph.GA", "math:math:NA")] == [
        "cs:cs:AI", "cs", "stat:stat:ML", "physics:hep-th", "physics", "physics:astro-ph:GA", "math:math:NA"]
    page = b"""<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>
      <record><header><identifier>oai:arXiv.org:2504.06438</identifier><datestamp>2026-02-18</datestamp></header><metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>2504.06438</id>
          <version version="v1"><date>Tue, 08 Apr 2025 21:14:48 GMT</date></version><version version="v2"><date>Mon, 16 Feb 2026 23:34:15 GMT</date></version>
          <title>A  title\n over two lines</title><authors>A. One, B. Two</authors><categories>cs.CL cs.AI</categories><abstract>  An abstract.\n More. </abstract>
        </arXivRaw></metadata></record>
      <record><header status="deleted"><identifier>oai:arXiv.org:0000.00000</identifier><datestamp>2026-02-18</datestamp></header></record>
      <resumptionToken>next-page</resumptionToken></ListRecords></OAI-PMH>"""
    found, token = loader.records(page)
    assert token == "next-page" and found == [{"id": "2504.06438", "title": "A title over two lines", "created": "2025-04-08", "abstract": "An abstract. More.",
                                               "categories": ["cs.CL", "cs.AI"], "authors": "A. One, B. Two", "versions": 2, "changed": "2026-02-18"}]
    assert loader.records(b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch">none</error></OAI-PMH>') == ([], "")


def test_a_reply_that_copies_a_backslash_of_a_document_is_read_and_one_that_is_no_object_is_not() -> None:
    """An abstract with a formula holds backslashes ("5\\%"); copied into the LLM's JSON reply they are no JSON escape, and
    the reply was once refused whole."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("safe_rag_modules_for_json", EXAMPLES / "safe_rag" / "modules.py")
    modules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modules)
    assert modules._json_object('Here: {"snippet": "up 5\\% and \\alpha", "answer": "line\\nnext \\"q\\""} Done.') == {
        "snippet": "up 5\\% and \\alpha", "answer": 'line\nnext "q"'}                      # the backslashes as copied; JSON's own escapes as JSON reads them
    assert modules._json_object('{"a": 1}') == {"a": 1}
    try:
        modules._json_object("no object here")
    except ValueError:
        pass
    else:
        raise AssertionError("a text with no object was read as one")
