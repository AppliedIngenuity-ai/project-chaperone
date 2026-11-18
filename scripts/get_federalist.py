"""Downloads the Federalist Papers from Project Gutenberg and writes each paper as a text file, for the example
"safe retrieval" (examples/safe_rag).

    python scripts/get_federalist.py [FOLDER]

FOLDER is examples/safe_rag/corpus unless named. The script writes `federalist_01.txt` to `federalist_85.txt`. The
first line of each is the paper's heading and its title as the book prints them, joined into one line; the rest is
the paper as the book prints it, footnotes included, with Unix line endings. Gutenberg's header, licence and the book's
own title page are not written.

The script stops, and writes nothing, when the book is not the one expected, or when its papers are not numbered 1 to
85 in order, each once. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://www.gutenberg.org/cache/epub/1404/pg1404.txt"
TITLE = "Title: The Federalist Papers"
START = "*** START OF THE PROJECT GUTENBERG EBOOK THE FEDERALIST PAPERS ***"
END = "*** END OF THE PROJECT GUTENBERG EBOOK THE FEDERALIST PAPERS ***"
HEADING = re.compile(r"(?m)^FEDERALIST No\. (\d+)$")
PAPERS = 85
FOLDER = Path(__file__).resolve().parents[1] / "examples" / "safe_rag" / "corpus"


def papers(book: str) -> list[str]:
    """The text of each paper, in order: the heading and the title paragraph on the first line, then the rest."""
    book = book.replace("\r\n", "\n")
    lines = book.splitlines()
    if TITLE not in lines or START not in lines or END not in lines:
        sys.exit(f"Stopped: the file at {URL} is not the book expected (no line `{TITLE}`, or no Gutenberg start or end "
                 f"marker). Nothing written.")
    body = book[book.index(START) + len(START):book.index(END)]
    numbers = [int(found.group(1)) for found in HEADING.finditer(body)]
    if numbers != list(range(1, PAPERS + 1)):
        sys.exit(f"Stopped: the book has {len(numbers)} paper headings, numbered {numbers[:3]}...{numbers[-3:]}; "
                 f"expected 1 to {PAPERS}, in order, each once. Nothing written.")
    written = []
    for text in re.split(r"(?m)^(?=FEDERALIST No\. \d+$)", body)[1:]:
        heading, title, rest = re.split(r"\n\s*\n", text.strip(), maxsplit=2)
        written.append(f"{heading}: {' '.join(title.split())}\n\n{rest.rstrip()}\n")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Downloads the Federalist Papers from Project Gutenberg, one text file "
                                                 "per paper, for examples/safe_rag. The folder corpus is shipped already.")
    parser.add_argument("folder", nargs="?", default=FOLDER, type=Path,
                        help="where the 85 files are written; by default examples/safe_rag/corpus")
    folder = parser.parse_args().folder
    print(f"Downloading {URL}")
    with urllib.request.urlopen(URL, timeout=60) as answer:
        book = answer.read().decode("utf-8")
    texts = papers(book)
    folder.mkdir(parents=True, exist_ok=True)
    replaced = 0
    for number, text in enumerate(texts, 1):
        file = folder / f"federalist_{number:02d}.txt"
        if file.is_file() and file.read_text(encoding="utf-8") != text:
            replaced += 1
        file.write_text(text, encoding="utf-8")
    others = sorted(f.name for f in folder.iterdir() if not re.fullmatch(r"federalist_\d\d\.txt", f.name))
    print(f"Wrote {len(texts)} papers to {folder}: federalist_01.txt to federalist_{len(texts):02d}.txt, "
          f"{sum(len(t) for t in texts):,} characters; {replaced} replaced a file with other text.")
    if others:
        print(f"The folder also holds {len(others)} other file(s), left as they are: {', '.join(others[:5])}")


if __name__ == "__main__":
    main()
