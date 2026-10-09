"""
PDF sectioner: reads an RBI PDF and stores one regulation row + many reg_section rows.
Every char_start/char_end is an exact position into regulation.full_text.

Usage:
    python -m extract.sectioner data/regulations/rbi-commercial-banks-kyc-directions-2025.pdf
    python -m extract.sectioner data/regulations/rbi-commercial-banks-kyc-directions-2025.pdf --dry-run
    python -m extract.sectioner path/to/regulation.txt   # plain-text fallback
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from db.connection import get_connection

# Official texts mark inserted or substituted provisions with a leading "[", as in
# "[8. Furnishing of information" or "[(bb) ...". Rule numbers may carry a letter
# ("3A."). A single-level number needs its dot so a wrapped "15 days" is not clause 15.
CLAUSE_NUMBER = r"(\d{1,3}(?:-?[A-Z])?(?:\.\d{1,3})+|\d{1,3}(?:-?[A-Z])?(?=\.))\.?\s"
NUMBERED_LINE = r"(?m)^\[?[ \t]*" + CLAUSE_NUMBER


def major(number: str) -> int:
    """'9-B' -> 9, '38.3' -> 38."""
    return int(re.match(r"\d+", number).group())


def advancing(marks, number):
    """
    Paragraph numbers only advance through a document, so a lower one ("1. 'Control'
    means..." inside rule 9, or an annex list restarting at 1) is an item inside the
    current clause, not a new clause. `number(m)` is None for non-numeric marks.
    """
    out, current = [], 0
    for m in marks:
        n = number(m)
        if n is not None:
            if major(n) < current:
                continue
            current = major(n)
        out.append(m)
    return out

# ---------------------------------------------------------------------------
# Clause-split regex: matches at the start of a line
# Groups covered:
#   - "38."  "38.3."  "3A."  "[8."      (numeric dot sequences)
#   - "(a)"  "(iv)"  "(1)"  "[(bb)"     (lettered, roman and numbered sub-clauses)
#   - "Chapter IV – Title"  "Section 3" (only as headings, not "Section 2 of the Act")
# ---------------------------------------------------------------------------
_CLAUSE_RE = re.compile(
    r"""(?m)^\[?[ \t]*                      # start of line, optional amendment bracket
    (?:
        (?P<num>                            # numeric  e.g. 38.  38.3.  3A.  9-B.
            \d{1,3}(?:-?[A-Z])?(?:\.\d{1,3})*\.
        )
        |
        (?P<sub>                            # sub-clause  e.g. (a)  (iv)  (1)
            \((?:[a-z]{1,4}|\d{1,2}[A-Z]?)[)\]]
        )
        |
        (?P<chapter>                        # Chapter heading, not "Chapter IV of the Act"
            Chapter\s+[IVXLCDM\d]+(?=[ \t]*(?:$|[-–—:.](?:\s|$)))
        )
        |
        (?P<section>                        # Section heading, not "Section 11-A and ..."
            Section\s+[IVXLCDM\d]+(?=[ \t]*(?:$|[-–—:.](?:\s|$)))
        )
    )
    """,
    re.VERBOSE,
)


def _extract_text_from_pdf(pdf_path: Path) -> tuple[str, list[tuple[int, int]]]:
    """Return (full_text, page_offsets) where page_offsets[i] = (page_no, char_start)."""
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)

    page_offsets: list[tuple[int, int]] = []
    parts: list[str] = []
    offset = 0
    for page_no, text in enumerate(pages, start=1):
        page_offsets.append((page_no, offset))
        parts.append(text)
        offset += len(text) + 2  # +2 for the "\n\n" separator

    full_text = "\n\n".join(parts)
    return full_text, page_offsets


def _extract_text_from_txt(txt_path: Path) -> tuple[str, list[tuple[int, int]]]:
    """Treat the whole file as full_text; single page."""
    full_text = txt_path.read_text(encoding="utf-8")
    return full_text, [(1, 0)]


def _page_for_offset(offset: int, page_offsets: list[tuple[int, int]]) -> int:
    """Binary-search the page_offsets table to find which page contains `offset`."""
    lo, hi = 0, len(page_offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if page_offsets[mid][1] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return page_offsets[lo][0]


def _split_sections(
    full_text: str, page_offsets: list[tuple[int, int]]
) -> list[dict]:
    """
    Split full_text on _CLAUSE_RE boundaries.
    Returns list of dicts with clause_path, char_start, char_end, raw_text, page_no.
    """
    matches = advancing(_CLAUSE_RE.finditer(full_text), lambda m: m.group("num"))
    sections: list[dict] = []

    for idx, m in enumerate(matches):
        char_start = m.start()
        char_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(full_text)

        raw_text = full_text[char_start:char_end]

        # clause_path: use whichever named group matched, stripped
        clause_path = (
            m.group("num")
            or m.group("sub")
            or m.group("chapter")
            or m.group("section")
            or m.group(0)
        ).strip().replace("]", ")")

        page_no = _page_for_offset(char_start, page_offsets)

        sections.append(
            {
                "clause_path": clause_path,
                "char_start": char_start,
                "char_end": char_end,
                "raw_text": raw_text,
                "page_no": page_no,
            }
        )

    return sections


# "The Prevention of Money-Laundering (Maintenance of Records) Rules, 2005¹" -> the
# title up to its year, dropping a leading "The" and any footnote digit after it.
# No digits before the year: that is what separates a title from a gazette masthead
# ("No. 472] NEW DELHI, ... 2005") or a circular reference number.
_TITLE_RE = re.compile(
    r"^(?:The\s+)?([^\d]{10,160}?\b(?:Directions?|Rules|Regulations|Act|Circular|Guidelines),?\s+\d{4})",
    re.I,
)
_VERSION_RE = re.compile(r"Updated as on ([A-Z][a-z]+\.? \d{1,2}, \d{4})")


def _document_identity(full_text: str, source_path: Path) -> tuple[str, str, str | None]:
    """
    Read the issuing body, title and version off the document head rather than the
    filename. Gazette PDFs open with masthead lines, so the head is scanned, not
    just the first line.
    """
    lines = [ln.strip() for ln in full_text.split("\n")[:60] if ln.strip()]
    # A long title wraps ("...Directions,\n2025"), so each line is read with the next.
    pairs = [" ".join(lines[i:i + 2]) for i in range(len(lines))]
    title_match = next((m for m in map(_TITLE_RE.search, pairs) if m), None)
    title = title_match.group(1) if title_match else source_path.stem
    head = "\n".join(lines[:15])

    if re.search(r"reserve bank", title, re.I) or any(
            re.fullmatch(r"reserve bank of india", ln, re.I) for ln in lines[:3]):
        regulator = "RBI"
    elif re.search(r"government of india|ministry|gazette|extraordinary", head, re.I):
        regulator = "GoI"
    else:
        regulator = "RBI"

    version = _VERSION_RE.search("\n".join(lines[:30]))
    return regulator, title, (f"Updated as on {version.group(1)}" if version else None)


def official_source(source_path: Path) -> dict:
    """Where this file came from, per the sources.json manifest beside it."""
    manifest = source_path.parent / "sources.json"
    if not manifest.exists():
        return {}
    return json.loads(manifest.read_text(encoding="utf-8")).get(source_path.name, {})


def _insert_regulation(conn, source_path: Path, full_text: str) -> str:
    reg_id = str(uuid.uuid4())
    regulator, title, version = _document_identity(full_text, source_path)
    origin = official_source(source_path)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO regulation
                (id, regulator, jurisdiction, title, version, source_url, full_text,
                 official_url, retrieved_on)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (reg_id, regulator, "India", title, origin.get("version") or version,
             str(source_path), full_text, origin.get("official_url"),
             origin.get("retrieved_on")),
        )
    return reg_id


def _insert_section(conn, regulation_id: str, section: dict, full_text: str) -> None:
    assert full_text[section["char_start"] : section["char_end"]] == section["raw_text"], (
        f"Offset assertion failed for clause {section['clause_path']}: "
        f"slice does not match raw_text"
    )
    sec_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reg_section
                (id, regulation_id, clause_path, page_no, raw_text, char_start, char_end)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                sec_id,
                regulation_id,
                section["clause_path"],
                section["page_no"],
                section["raw_text"],
                section["char_start"],
                section["char_end"],
            ),
        )


def _insert_needs_review(conn, stage: str, payload: dict, reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO needs_review (id, stage, payload, failure_reason)
            VALUES (%s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), stage, json.dumps(payload), reason),
        )


def read_source(source_path: Path) -> tuple[str, list[tuple[int, int]]]:
    if source_path.suffix.lower() == ".txt":
        return _extract_text_from_txt(source_path)
    return _extract_text_from_pdf(source_path)


def run(source_path: Path, dry_run: bool = False, conn=None) -> None:
    full_text, page_offsets = read_source(source_path)

    sections = _split_sections(full_text, page_offsets)

    if dry_run:
        print(f"full_text length: {len(full_text):,} chars")
        print(f"Total sections found: {len(sections)}")
        print()
        for s in sections[:10]:
            preview = s["raw_text"][:80].replace("\n", "↵")
            print(
                f"  clause_path={s['clause_path']!r:20s}  "
                f"page={s['page_no']:3d}  "
                f"start={s['char_start']:7d}  "
                f"text={preview!r}"
            )
        return

    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        regulation_id = _insert_regulation(conn, source_path, full_text)

        ok = fail = 0
        for section in sections:
            try:
                _insert_section(conn, regulation_id, section, full_text)
                ok += 1
            except AssertionError as e:
                _insert_needs_review(
                    conn,
                    stage="sectioner/offset_assertion",
                    payload={
                        "clause_path": section["clause_path"],
                        "char_start": section["char_start"],
                        "char_end": section["char_end"],
                    },
                    reason=str(e),
                )
                fail += 1
            except Exception as e:
                _insert_needs_review(
                    conn,
                    stage="sectioner/insert",
                    payload={"clause_path": section.get("clause_path", "?")},
                    reason=str(e),
                )
                fail += 1

        conn.commit()
        print(f"{source_path.name}: {ok} sections, {fail} to needs_review.")
    finally:
        if owns_conn:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest an RBI PDF into the database.")
    parser.add_argument("source", type=Path, help="Path to .pdf or .txt file")
    parser.add_argument("--dry-run", action="store_true", help="Print first 10 sections, no DB write")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"File not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    run(args.source, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
