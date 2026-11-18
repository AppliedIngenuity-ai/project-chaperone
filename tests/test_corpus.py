"""The shipped corpus of "safe retrieval": the Federalist Papers, written by scripts/get_federalist.py."""

from __future__ import annotations

import re
from pathlib import Path

from chaperone.clients import Documents

CORPUS = Path(__file__).resolve().parents[1] / "examples" / "safe_rag" / "corpus"


def test_the_corpus_is_the_85_papers_each_once() -> None:
    files = sorted(CORPUS.glob("*"))
    assert [f.name for f in files] == [f"federalist_{n:02d}.txt" for n in range(1, 86)]
    first_lines = [f.read_text(encoding="utf-8").splitlines()[0] for f in files]
    numbers = [int(re.match(r"FEDERALIST No\. (\d+): \S", line).group(1)) for line in first_lines]
    assert numbers == list(range(1, 86))               # a contents list, or a paper printed twice, would break this


def test_no_gutenberg_text_is_in_the_corpus() -> None:
    for file in CORPUS.glob("*.txt"):
        text = file.read_text(encoding="utf-8")
        assert "gutenberg" not in text.lower() and "*** " not in text, file.name


def test_the_papers_are_searched_by_their_words(tmp_path: Path) -> None:
    docs = Documents("DOCS_DB", CORPUS, tmp_path / "index.sqlite", [".txt", ".md"], 1200, "porter")
    assert docs.files() == 85
    assert docs.search("factions", 5, [])[0]["chunk_id"].startswith("federalist_10.txt#")
