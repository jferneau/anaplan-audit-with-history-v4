#!/usr/bin/env python3
"""Refresh the activity-code catalog from Anaplan's authoritative Anapedia tables.

The shipped ``src/anaplan_audit/data/activity_events.csv`` is the seed message
for each audit event code (the fallback when a tenant hasn't emitted a code yet;
at runtime the live ``events.message`` is preferred). Hand-curating it drifts —
codes get the wrong message, and new Anaplan codes are missed. This script
regenerates it from Anaplan's published "tracked ... events" pages.

Merge policy (stdlib only; no runtime dependency):

* **Live stays authoritative.** An existing non-placeholder message is kept —
  it reflects what the tool has curated/observed. Anapedia is used to *fill*
  gaps, not to overwrite good values with documentation-cased wording (the
  pipeline already prefers the live message at runtime).
* **Fill placeholders.** A code whose message is the "pending Anaplan
  documentation" placeholder is filled from Anapedia when documented.
* **Add new codes.** Codes documented by Anaplan but absent from the catalog
  are appended, parented automatically by prefix at load time.
* **Reset shift-orphans.** A catalog code that Anapedia does NOT document but
  whose message is verbatim some *other* code's documented message is a
  mis-assignment (e.g. USR-60 holding USR-82's "UX worksheet page published").
  It's reset to the placeholder — honest "unknown" beats a wrong message.

Usage:
    python scripts/harvest_activity_codes.py            # fetch + report (dry run)
    python scripts/harvest_activity_codes.py --write     # fetch + rewrite the CSV
    python scripts/harvest_activity_codes.py --cache DIR # parse *.html from DIR
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

CATALOG = Path(__file__).resolve().parent.parent / "src/anaplan_audit/data/activity_events.csv"
PLACEHOLDER_MARK = "pending anaplan documentation"
CODE_RE = re.compile(r"(?:USR|AUTHZ|CONN|DSM|INT|WF|COMMENT|FRCST|PIQ|OAUTH)-[0-9A-Za-z]+")

# Anaplan's "Tracked <family> events" pages (Anapedia).
PAGES: dict[str, str] = {
    "user": "https://help.anaplan.com/tracked-user-activity-events-ef0ac1f3-fd1d-4dc2-a205-2ae4f5b22a7d",
    "authz": "https://help.anaplan.com/tracked-access-control-events-f55f4dea-bf72-4df5-808b-bcaa376b3461",
    "conn": "https://help.anaplan.com/tracked-saml-connection-management-events-c945e285-0a3b-4788-87e5-dd53ff30b70c",
    "dsm": "https://help.anaplan.com/tracked-encryption-activity-events-dc1c1e96-9159-45d0-81d6-405de46907bc",
    "int": "https://help.anaplan.com/tracked-integrations-events-4679ba26-2a3d-4fb0-bce9-47eac6f752d7",
    "wf": "https://help.anaplan.com/tracked-workflow-events-a6832db5-d137-4f47-89c2-d7645b0dfd61",
    "comment": "https://help.anaplan.com/tracked-comment-events-aa7437ab-7093-44fa-b111-73ae78b49fd8",
    "frcst": "https://help.anaplan.com/tracked-forecaster-events-74ffe96a-ae9a-4761-a606-05c8ccc5021d",
}


class _TableExtractor(HTMLParser):
    """Collect every HTML table as a list of rows of cell text."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._cur: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._buf: list[str] = []
        self._in = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "table":
            self._cur = []
        elif tag == "tr" and self._cur is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._in, self._buf = True, []
        elif tag == "br" and self._in:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._cur is not None:
            self.tables.append(self._cur)
            self._cur = None
        elif tag == "tr" and self._row is not None:
            self._cur.append(self._row)  # type: ignore[union-attr]
            self._row = None
        elif tag in ("td", "th") and self._in:
            self._row.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())  # type: ignore[union-attr]
            self._in = False

    def handle_data(self, data: str) -> None:
        if self._in:
            self._buf.append(data)


def _clean(text: str) -> str:
    """Drop the trailing ' Note: ...' / ' Display with ...' Anapedia appends."""
    return re.split(r"\s+(?:Note:|Display with|Displays as)", text, maxsplit=1)[0].strip()


def _codes_in_cell(cell: str) -> list[str]:
    """Codes in a code cell — handles DSM's 'DSM-071 displays as: DSM-DAO0071I'."""
    return CODE_RE.findall(cell)


def parse_page(html: str) -> dict[str, tuple[str, str]]:
    """Map code -> (message, associated_object) for every event table on a page."""
    ext = _TableExtractor()
    ext.feed(html)
    out: dict[str, tuple[str, str]] = {}
    for table in ext.tables:
        if not table:
            continue
        header = [c.lower() for c in table[0]]

        def idx(subs: tuple[str, ...], default: int, hdr: list[str] = header) -> int:
            return next((i for i, h in enumerate(hdr) if any(s in h for s in subs)), default)

        ci = idx(("code",), 0)
        mi = idx(("message", "event/"), 1)
        ai = idx(("associated", "object"), 2)
        for row in table:
            if ci >= len(row):
                continue
            codes = _codes_in_cell(row[ci])
            if not codes:
                continue
            msg = _clean(row[mi]) if mi < len(row) else ""
            assoc = _clean(row[ai]) if ai < len(row) else ""
            for code in codes:
                out.setdefault(code, (msg, assoc))
    return out


def _is_placeholder(msg: str) -> bool:
    return PLACEHOLDER_MARK in msg.lower()


def harvest(pages: dict[str, str], cache: Path | None) -> dict[str, tuple[str, str]]:
    merged: dict[str, tuple[str, str]] = {}
    per_page: dict[str, int] = {}
    for name, url in pages.items():
        if cache:
            html = (cache / f"{name}.html").read_text(encoding="utf-8")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        got = parse_page(html)
        per_page[name] = len(got)
        for code, val in got.items():
            merged.setdefault(code, val)
    print(f"harvested per page: {per_page}  (total {len(merged)} codes)")
    return merged


def merge(rows: list[list[str]], doc: dict[str, tuple[str, str]]) -> tuple[list[list[str]], dict]:
    """Apply the merge policy. Returns (new_data_rows, report)."""
    existing = {r[0] for r in rows}
    doc_messages = {m for m, _ in doc.values()}
    placeholder = "User activity event (message pending Anaplan documentation)"
    filled, reset, added, kept = [], [], [], 0

    for r in rows:
        code, msg = r[0], r[1]
        if code in doc:
            if _is_placeholder(msg):
                new_msg, assoc = doc[code]
                filled.append((code, msg, new_msg))
                r[1] = new_msg
                if assoc and len(r) > 2:
                    r[2] = assoc
            else:
                kept += 1
        else:
            # Not documented. If it's verbatim another code's documented
            # message, it's a shift-orphan -> honest placeholder.
            if not _is_placeholder(msg) and msg.strip() in doc_messages:
                reset.append((code, msg))
                r[1] = placeholder
            else:
                kept += 1

    for code, (msg, assoc) in sorted(doc.items()):
        if code not in existing:
            added.append(code)
            rows.append([code, msg, assoc, ""])

    return rows, {"filled": filled, "reset": reset, "added": added, "kept": kept}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="rewrite the catalog CSV")
    ap.add_argument("--cache", type=Path, help="parse *.html from this dir instead of fetching")
    args = ap.parse_args()

    doc = harvest(PAGES, args.cache)
    rows = list(csv.reader(CATALOG.open(newline="")))
    hdr, data = rows[0], rows[1:]
    data, rep = merge(data, doc)

    print(f"\nfilled placeholders from Anapedia: {len(rep['filled'])}")
    for c, old, new in rep["filled"]:
        print(f"  {c:12} {old[:40]!r} -> {new!r}")
    print(f"\nshift-orphans reset to placeholder: {len(rep['reset'])}")
    for c, old in rep["reset"]:
        print(f"  {c:12} was {old!r}")
    print(f"\nnew codes added from Anapedia: {len(rep['added'])}  {rep['added']}")
    print(f"unchanged (kept): {rep['kept']}")

    if args.write:
        # lineterminator="\n" (csv default is "\r\n") so unchanged rows stay
        # byte-identical and the diff shows only real content changes.
        with CATALOG.open("w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(hdr)
            w.writerows(data)
        print(f"\nWROTE {CATALOG}")
    else:
        print("\n(dry run — pass --write to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
