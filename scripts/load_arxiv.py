"""Fetch arXiv abstracts of one category into a database that "safe retrieval" can search, and keep it up to date.

    python scripts/load_arxiv.py --category cs.AI --since 2025-11-14      # the first fetch
    python scripts/load_arxiv.py --category cs.AI                         # later: what arXiv added or changed since

It writes `data/arxiv/<category>.sqlite` (the folder `data/` is not in git). The setup `arxiv` of
`examples/safe_rag` names that file; `python -m chaperone run examples/safe_rag --setup arxiv` then answers from it.

What it does. It asks arXiv's metadata feed (OAI-PMH, https://oaipmh.arxiv.org/oai, described at
https://info.arxiv.org/help/oa/index.html) for the records of one set, page by page, with a pause between pages,
and keeps the id, title, authors, categories, dates and abstract of each. Standard library only.

- `--category` is an arXiv category (`cs.AI`, `stat.ML`, `math.NA`) or a whole archive (`cs`). A paper cross-listed
  in the category is in it.
- `--since DATE` (year-month-day): keep papers first submitted on or after that day. The feed cannot be asked for
  papers by the day they were submitted, only by the day their record last changed, so the fetch asks for every
  record changed since DATE and keeps those whose first version is dated on or after DATE. A paper submitted since
  DATE is among them; so are older papers revised since, which are left out.
- Run again without `--since`: it asks for the records changed since the last fetch (the day is kept in the
  database), adds new papers and replaces changed ones. The first `--since` stays in force.
- A paper's date is the day of its first version (UTC), read from the feed's format `arXivRaw`, which lists every
  version. (The feed's other format has a field `created`, which for a revised paper is the day of its latest
  version, as for 2504.06438.) Its source is its address at arxiv.org.

The table it writes, `documents(document_id, title, source, date, text, more)`, is the form the client of kind
`document_db` reads (chaperone/clients.py): nothing in it is specific to arXiv. `more` is JSON: authors, categories, the
number of versions, the day the record last changed.

arXiv's terms ask automated clients to go slowly; the pause is 3 seconds, and a "retry after" answer is obeyed.
"""

import argparse
import datetime
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FEED = "https://oaipmh.arxiv.org/oai"
AGENT = "arxiv-abstract-loader/0.1 (python-urllib)"
PAUSE = 3.0
OAI, ARXIV = "{http://www.openarchives.org/OAI/2.0/}", "{http://arxiv.org/OAI/arXivRaw/}"


def set_of(category: str) -> str:
    """arXiv's set for a category: cs.AI is cs:cs:AI, an archive such as cs is cs; an archive of physics (hep-th) is
    physics:hep-th. A set written with ":" is taken as written."""
    if ":" in category:
        return category
    archive, _, sub = category.partition(".")
    group = archive if archive in ("cs", "econ", "eess", "math", "q-bio", "q-fin", "stat") else "physics"
    if group == "physics":
        return "physics" if archive == "physics" and not sub else f"physics:{archive}" + (f":{sub}" if sub else "")
    return f"{group}:{archive}:{sub}" if sub else group


def fetch(url: str) -> bytes:
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=120) as reply:
                return reply.read()
        except urllib.error.HTTPError as problem:
            if problem.code not in (429, 503) or attempt == 5:
                raise
            wait = float(problem.headers.get("Retry-After") or 30)
            print(f"  arXiv asks to wait {wait:.0f} seconds", flush=True)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as problem:
            if attempt == 5:
                raise
            print(f"  no answer ({problem}); trying again in 30 seconds", flush=True)
            time.sleep(30)
    raise RuntimeError("unreachable")


def records(page: bytes):
    """(the records of a page, the token for the next page or "")."""
    root = ET.fromstring(page)
    error = root.find(f"{OAI}error")
    if error is not None:
        if error.get("code") == "noRecordsMatch":
            return [], ""
        raise SystemExit(f"arXiv answered an error: {error.get('code')}: {error.text}")
    found = []
    for record in root.iter(f"{OAI}record"):
        header, data = record.find(f"{OAI}header"), record.find(f"{OAI}metadata/{ARXIV}arXivRaw")
        if data is None:                                      # a deleted record has no metadata
            continue
        text = lambda name: " ".join((data.findtext(f"{ARXIV}{name}") or "").split())     # noqa: E731
        versions = []
        for version in data.iter(f"{ARXIV}version"):
            try:
                versions.append(parsedate_to_datetime(version.findtext(f"{ARXIV}date") or "").astimezone(datetime.timezone.utc).date().isoformat())
            except (TypeError, ValueError):
                pass
        found.append({"id": text("id"), "title": text("title"), "created": min(versions) if versions else "", "abstract": text("abstract"),
                      "categories": text("categories").split(), "authors": text("authors"), "versions": len(versions),
                      "changed": header.findtext(f"{OAI}datestamp") or ""})
    token = root.findtext(f"{OAI}ListRecords/{OAI}resumptionToken") or ""
    return found, token.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--category", required=True, help="an arXiv category or archive: cs.AI, stat.ML, cs")
    parser.add_argument("--since", help="year-month-day: keep papers first submitted on or after this day (required at the first fetch)")
    parser.add_argument("--data", default=str(REPO / "data" / "arxiv"), help="the folder of the database; by default data/arxiv")
    parser.add_argument("--max-pages", type=int, default=0, help="for a trial: stop after this many pages (0: all). The next run starts again from the same day")
    args = parser.parse_args(argv)

    folder = Path(args.data).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f"{args.category.replace(':', '_')}.sqlite"
    db = sqlite3.connect(file)
    db.execute("create table if not exists documents (document_id text primary key, title text, source text, date text, text text, more text)")
    db.execute("create table if not exists loader (key text primary key, value text)")
    kept = dict(db.execute("select key, value from loader"))
    since = args.since or kept.get("since")
    if not since:
        parser.error("the first fetch needs --since DATE (year-month-day)")
    datetime.date.fromisoformat(since)                        # refuses a day that is not one
    if kept.get("since") and args.since and args.since < kept["since"]:
        kept.pop("changed_through", None)                     # an earlier day than before: everything is asked for again
    start = kept.get("changed_through") or since              # the feed is asked by the day a record changed
    db.execute("insert or replace into loader values ('since', ?)", (min(since, kept.get("since", since)),))
    db.execute("insert or replace into loader values ('category', ?)", (args.category,))
    db.commit()
    since = min(since, kept.get("since", since))

    query = {"verb": "ListRecords", "metadataPrefix": "arXivRaw", "set": set_of(args.category), "from": start}
    print(f"{args.category} (set {query['set']}): records changed since {start}; kept when first submitted on or after {since}", flush=True)
    pages = seen = added = 0
    newest = start
    while True:
        found, token = records(fetch(FEED + "?" + urllib.parse.urlencode(query)))
        pages += 1
        seen += len(found)
        for record in found:
            newest = max(newest, record["changed"])
            if not record["created"] or record["created"] < since or not record["abstract"]:      # no dated version, older, or no abstract
                continue
            added += 1
            db.execute("insert or replace into documents values (?, ?, ?, ?, ?, ?)",
                       (record["id"], record["title"], f"https://arxiv.org/abs/{record['id']}", record["created"], record["abstract"],
                        json.dumps({"authors": record["authors"], "categories": record["categories"], "versions": record["versions"],
                                    "changed": record["changed"]})))
        db.commit()
        total = db.execute("select count(*) from documents").fetchone()[0]
        print(f"  page {pages}: {len(found)} records, {seen} seen, {added} kept or replaced; {total} in the database", flush=True)
        if not token or (args.max_pages and pages >= args.max_pages):
            break
        query = {"verb": "ListRecords", "resumptionToken": token}
        time.sleep(PAUSE)
    if not token:                                             # every page was read: the next run asks from the newest day seen
        db.execute("insert or replace into loader values ('changed_through', ?)", (newest,))
        db.commit()
    total, first, last = db.execute("select count(*), min(date), max(date) from documents").fetchone()
    print(f"Done: {total} abstracts, first submitted {first} to {last}, in {file}" + ("" if not token else "  (stopped at --max-pages: not complete; the next run starts again from the same day)"))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
