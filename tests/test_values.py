"""Values, labels and the store (specification 2.1, 2.9, P1 to P6)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from chaperone import Store, Value, load_project
from chaperone.values import Problem
from chaperone.values import apply_requests, equals, error, parse_path, result, show_path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SPREADING = {"UNTRUSTED", "CONFIDENTIAL", "LLM_WRITTEN", "PERSONAL"}          # VERIFIED, CHECKED, MONITORED_SENDER do not


def labels(value: Value, path: str = "") -> set[str]:
    return set(value.carried(parse_path(path)))


# ---- 2.1 and 2.9: values, parts, where labels are -------------------------------------------------------------------

def test_a_path_is_field_names_and_list_indices() -> None:
    assert parse_path("a.b[2]") == ("a", "b", 2) and parse_path("") == () and parse_path("[0].x") == (0, "x")
    assert show_path(("a", "b", 2)) == "a.b[2]" and show_path(()) == "the whole value"
    for bad in ("a..b", ".a", "a[b]", "a b"):
        with pytest.raises(Problem):
            parse_path(bad)


def test_labels_are_on_scalar_parts_and_a_container_carries_what_its_parts_carry() -> None:
    record = Value.build({"id": 7, "note": Value.of("call me", ["UNTRUSTED"]), "tags": [Value.of("x", ["PERSONAL"]), "y"]})
    assert labels(record, "id") == set() and labels(record, "note") == {"UNTRUSTED"}
    assert labels(record, "tags") == {"PERSONAL"} and labels(record, "tags[1]") == set()
    assert labels(record) == {"UNTRUSTED", "PERSONAL"}
    assert record.data == {"id": 7, "note": "call me", "tags": ["x", "y"]}       # the data is never changed by a label


def test_a_part_written_in_a_workflow_carries_no_label_and_a_referenced_part_keeps_its_own() -> None:
    terms = Value.of(["UPS"], ["UNTRUSTED", "LLM_WRITTEN"])
    screen = Value.build({"calling": "mail.read.search_senders", "terms": terms})
    assert labels(screen, "calling") == set() and labels(screen, "terms") == {"UNTRUSTED", "LLM_WRITTEN"}


def test_an_empty_list_has_no_scalar_part_and_carries_nothing() -> None:
    assert labels(Value.of([], ["UNTRUSTED"])) == set() and labels(Value.of({}, ["UNTRUSTED"])) == set()


def test_what_is_not_a_scalar_a_list_or_a_dictionary_is_not_a_value() -> None:
    for bad in ({"when": object()}, [(1, 2)], {1: "a number as a key"}, {"x": {3, 4}}):
        with pytest.raises(Problem) as refused:
            Value.of(bad)
        assert refused.value.type == "not_a_value"


# ---- P1 and P2 -------------------------------------------------------------------------------------------------------

def test_p1_every_part_of_a_result_carries_the_modules_adds() -> None:
    record = result({"id": 7, "lines": ["a", "b"]}, ["CONFIDENTIAL"], [], SPREADING)
    assert all(record.labels[p] == {"CONFIDENTIAL"} for p in (("id",), ("lines", 0), ("lines", 1)))


def test_p2_a_spreading_label_on_any_part_of_any_argument_is_on_every_part_of_the_result() -> None:
    argument = Value.build({"clean": 1, "dirty": Value.of("x", ["UNTRUSTED", "VERIFIED"])})
    out = result({"a": 1, "b": [2]}, [], [Value.of("plain"), argument], SPREADING)
    assert labels(out, "a") == {"UNTRUSTED"} and labels(out, "b[0]") == {"UNTRUSTED"}       # VERIFIED does not spread


# ---- P3: engine operations -------------------------------------------------------------------------------------------

def test_p3_get_and_pick_move_parts_with_their_labels() -> None:
    report = Value.build({"summary": Value.of("s", ["UNTRUSTED", "CHECKED"]), "results": [{"id": 1}], "extra": 0})
    assert labels(report.get(parse_path("summary"))) == {"UNTRUSTED", "CHECKED"}
    assert labels(report.get(parse_path("results[0].id"))) == set()
    picked = report.pick(["summary", "results"])
    assert set(picked.data) == {"summary", "results"} and labels(picked, "summary") == {"UNTRUSTED", "CHECKED"}
    with pytest.raises(Problem) as missing:
        report.get(parse_path("results[4]"))
    assert missing.value.type == "no_such_part"


def test_p3_equals_computes_a_value_with_the_spreading_labels_of_both_operands() -> None:
    same = equals(Value.of("a", ["UNTRUSTED", "VERIFIED"]), Value.of("a", ["PERSONAL"]), SPREADING)
    assert same.data is True and labels(same) == {"UNTRUSTED", "PERSONAL"}


def test_p3_labels_of_returns_the_labels_of_each_part_and_carries_none() -> None:
    report = Value.build({"summary": Value.of("s", ["UNTRUSTED", "CHECKED"]), "results": [{"id": 1}]})
    marks = report.marks()
    assert marks.data == {"summary": ["CHECKED", "UNTRUSTED"], "results": [{"id": []}]} and labels(marks) == set()


# ---- P4: requests ----------------------------------------------------------------------------------------------------

def test_p4_a_declared_request_applies_to_the_part_and_every_part_within_it() -> None:
    items = result(["a@x.example", "b@x.example"], [], [Value.of("found", ["UNTRUSTED", "LLM_WRITTEN"])], SPREADING)
    whole = {"UNTRUSTED": False, "LLM_WRITTEN": False, "MONITORED_SENDER": True}
    out = apply_requests(items, [("", "UNTRUSTED", False), ("", "LLM_WRITTEN", False), ("[0]", "MONITORED_SENDER", True)],
                         whole, {})
    assert labels(out, "[0]") == {"MONITORED_SENDER"} and labels(out, "[1]") == set()


def test_p4_requests_follow_p1_so_a_module_whose_adds_lists_a_label_may_return_a_result_without_it() -> None:
    out = apply_requests(result({"n": 3}, ["UNTRUSTED"], [], SPREADING), [("", "UNTRUSTED", False)], {"UNTRUSTED": False}, {})
    assert labels(out) == set()


def test_p4_a_request_the_declaration_does_not_state_refuses_the_call() -> None:
    value = result({"summary": "s", "results": []}, ["UNTRUSTED"], [], SPREADING)
    for request, whole, by_part in (
            (("", "CHECKED", True), {}, {}),                                       # nothing declared
            (("", "UNTRUSTED", True), {"UNTRUSTED": False}, {}),                   # the other value is declared
            (("summary", "UNTRUSTED", False), {}, {"results": {"UNTRUSTED": False}}),   # declared for another field
            (("", "UNTRUSTED", False), {}, {"results": {"UNTRUSTED": False}})):    # declared for a field, asked for all
        with pytest.raises(Problem) as refused:
            apply_requests(value, [request], whole, by_part)
        assert refused.value.type == "bad_request" and "not stated" in str(refused.value)


def test_p4_a_request_whose_path_names_no_part_refuses_the_call() -> None:
    with pytest.raises(Problem, match="names no part of the result"):
        apply_requests(Value.of({"a": 1}), [("b", "CHECKED", True)], {"CHECKED": True}, {})


def test_safe_retrieval_the_llms_words_keep_their_labels_and_the_documents_text_loses_them() -> None:
    """The row VERIFY: P1 (the store adds nothing), P2, then the requests of P4, each stated in `verify.response`'s
    declaration for its own field."""
    project = load_project(EXAMPLES / "safe_rag")
    verify = project.declarations["verify.response"]
    assert verify.requests == {} and set(verify.part_requests) == {"restated_query", "answer", "found", "supported", "documents", "left_out"}
    spreading = {name for name, spec in project.labels.items() if spec["spreads"]}
    response = Value.of({"text": "{...}"}, project.facts["llm.answer"].adds)
    raw = {"restated_query": "Why?", "answer": "Publius argues ...", "found": True, "supported": True,
           "documents": [{"title": "FEDERALIST No. 10", "source": "federalist_10.txt", "chunks": [{"chunk_id": "federalist_10.txt#3",
                          "location": {"first_paragraph": 6, "last_paragraph": 9}, "pieces": [{"text": "the people", "marked": True}], "verified": True}]}],
           "left_out": {"names_no_chunk": 0, "not_in_its_chunk": 0}}
    report = result(raw, project.facts["verify.response"].adds, [Value.of({"chunk_ids": ["federalist_10.txt#3"]}), response], spreading)
    assert labels(report, "documents") == {"UNTRUSTED", "LLM_WRITTEN"}                     # P2, before the requests
    report = apply_requests(report, [("answer", "CHECKED", True), ("documents", "UNTRUSTED", False), ("documents", "LLM_WRITTEN", False),
                                     ("documents", "ORIGINAL_SOURCE", True)], verify.requests, verify.part_requests)
    assert labels(report, "answer") == {"UNTRUSTED", "LLM_WRITTEN", "CHECKED"} and labels(report, "documents") == {"ORIGINAL_SOURCE"}
    with pytest.raises(Problem):                                                           # not for the LLM's answer
        apply_requests(report, [("answer", "UNTRUSTED", False)], verify.requests, verify.part_requests)
    with pytest.raises(Problem):                                                           # nor the documents' label on it
        apply_requests(report, [("answer", "ORIGINAL_SOURCE", True)], verify.requests, verify.part_requests)


def test_the_example_of_section_3() -> None:
    """db.read on LOCAL_DB, llm.summarize on LLM, code.verify with two requests: the copy carries CONFIDENTIAL and
    VERIFIED and not UNTRUSTED."""
    spreading = {"UNTRUSTED", "CONFIDENTIAL"}
    record = result({"id": 7, "text": "..."}, ["CONFIDENTIAL"], [Value.of({"id": 7})], spreading)
    summary = result({"text": "short"}, ["UNTRUSTED"], [record], spreading)
    assert labels(summary) == {"UNTRUSTED", "CONFIDENTIAL"}
    copy = apply_requests(result({"text": "short"}, [], [summary], spreading),
                          [("", "UNTRUSTED", False), ("", "VERIFIED", True)], {"UNTRUSTED": False, "VERIFIED": True}, {})
    assert labels(copy) == {"CONFIDENTIAL", "VERIFIED"}


# ---- P6: errors ------------------------------------------------------------------------------------------------------

def test_p6_an_error_is_a_value_with_the_modules_adds_and_the_arguments_spreading_labels() -> None:
    message = Value.of({"body": "..."}, ["UNTRUSTED", "PERSONAL", "MONITORED_SENDER"])
    raised = error("bad_values", "no date", state="COPY", call="check.delivery_values", adds=["LLM_WRITTEN"],
                   arguments=[message], spreading=SPREADING, detail={"python": "ValueError"})
    assert raised.data == {"type": "bad_values", "message": "no date", "state": "COPY", "call": "check.delivery_values",
                           "detail": {"python": "ValueError"}}
    assert labels(raised, "type") == {"LLM_WRITTEN", "UNTRUSTED", "PERSONAL"}


# ---- P5 and 2.12: the store -------------------------------------------------------------------------------------------

DECLARED = {"MONITORED_SENDERS": [], "TURNS": 0}


def test_a_store_starts_with_its_declared_value_and_only_declared_names_exist(tmp_path: Path) -> None:
    store = Store(tmp_path / "store.json", DECLARED)
    assert store.get("MONITORED_SENDERS").data == [] and store.get("TURNS").data == 0
    for call in (lambda: store.get("WATCHED"), lambda: store.put("WATCHED", Value.of(1))):
        with pytest.raises(Problem) as refused:
            call()
        assert refused.value.type == "store_not_declared"


def test_p5_a_stored_value_keeps_its_labels_between_runs(tmp_path: Path) -> None:
    Store(tmp_path / "store.json", DECLARED).put("MONITORED_SENDERS", Value.build(
        [Value.of("a@x.example", ["PERSONAL", "MONITORED_SENDER"]), Value.of("b@x.example", ["MONITORED_SENDER"])]))
    again = Store(tmp_path / "store.json", DECLARED).get("MONITORED_SENDERS")
    assert again.data == ["a@x.example", "b@x.example"]
    assert labels(again, "[0]") == {"PERSONAL", "MONITORED_SENDER"} and labels(again, "[1]") == {"MONITORED_SENDER"}


def test_store_add_adds_what_is_not_there_by_content_and_says_what_was(tmp_path: Path) -> None:
    store = Store(tmp_path / "store.json", DECLARED)
    first = store.add("MONITORED_SENDERS", Value.of(["a@x.example", "b@x.example", "a@x.example"], ["MONITORED_SENDER"]))
    assert first.data == {"added": ["a@x.example", "b@x.example"], "present": ["a@x.example"]}
    second = store.add("MONITORED_SENDERS", Value.of(["b@x.example", "c@x.example"], ["PERSONAL"]))
    assert second.data == {"added": ["c@x.example"], "present": ["b@x.example"]}
    assert labels(second, "present") == {"MONITORED_SENDER"}             # the stored item, with the labels it was stored with
    kept = store.get("MONITORED_SENDERS")
    assert kept.data == ["a@x.example", "b@x.example", "c@x.example"] and labels(kept, "[2]") == {"PERSONAL"}


def test_store_remove_removes_by_content_and_says_what_was_absent(tmp_path: Path) -> None:
    store = Store(tmp_path / "store.json", DECLARED)
    store.add("MONITORED_SENDERS", Value.of(["a@x.example", "b@x.example"], ["MONITORED_SENDER"]))
    out = store.remove("MONITORED_SENDERS", Value.of(["b@x.example", "z@x.example"]))
    assert out.data == {"removed": ["b@x.example"], "absent": ["z@x.example"]} and labels(out, "removed") == {"MONITORED_SENDER"}
    assert store.get("MONITORED_SENDERS").data == ["a@x.example"]
    with pytest.raises(Problem) as refused:
        store.add("TURNS", Value.of([1]))
    assert refused.value.type == "not_a_list"


def test_a_kept_name_that_is_no_longer_declared_is_refused_not_dropped(tmp_path: Path) -> None:
    Store(tmp_path / "store.json", DECLARED).put("TURNS", Value.of(3))
    with pytest.raises(Problem, match="keeps a value for TURNS"):
        Store(tmp_path / "store.json", {"MONITORED_SENDERS": []})


def test_two_runs_at_once_lose_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path / "store.json", DECLARED)

    def run(prefix: str) -> None:
        for number in range(40):
            store.add("MONITORED_SENDERS", Value.of([f"{prefix}{number}@x.example"]))
    threads = [threading.Thread(target=run, args=(p,)) for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.get("MONITORED_SENDERS").data) == 120
    assert len(Store(tmp_path / "store.json", DECLARED).get("MONITORED_SENDERS").data) == 120
