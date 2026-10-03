"""
PDF sectioner: reads an RBI PDF and stores one regulation row + many reg_section rows.
Every char_start/char_end is an exact position into regulation.full_text.

Usage:
    python -m extract.sectioner data/regulations/rbi-kyc.pdf
    python -m extract.sectioner data/regulations/rbi-kyc.pdf --dry-run
    python -m extract.sectioner data/regulations/rbi-kyc-extract.txt   # fallback txt
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from db.connection import get_connection

# ---------------------------------------------------------------------------
# Clause-split regex: matches at the start of a line
# Groups covered:
#   - "38."  "38.3."  "38.3.1."   (numeric dot sequences)
#   - "(a)"  "(i)"   "(iv)"       (lettered/roman sub-clauses)
#   - "Chapter IV"  "Chapter 4"
#   - "Section 3"   "Section III"
# ---------------------------------------------------------------------------
_CLAUSE_RE = re.compile(
    r"""(?m)^                               # start of line
    (?:
        (?P<num>                            # numeric  e.g. 38.  or 38.3.
            \d{1,3}(?:\.\d{1,3})*\.        # at least one dot-terminated segment
        )
        |
        (?P<sub>                            # sub-clause  e.g. (a)  (iv)
            \([a-z]{1,4}\)
        )
        |
        (?P<chapter>                        # Chapter heading
            Chapter\s+[IVXLCDM\d]+
        )
        |
        (?P<section>                        # Section heading
            Section\s+[IVXLCDM\d]+
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
    matches = list(_CLAUSE_RE.finditer(full_text))
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
        ).strip()

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


def _insert_regulation(conn, source_path: Path, full_text: str) -> str:
    reg_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO regulation
                (id, regulator, jurisdiction, title, version, source_url, full_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                reg_id,
                "RBI",
                "India",
                source_path.stem,
                None,
                str(source_path),
                full_text,
            ),
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


def run(source_path: Path, dry_run: bool = False, conn=None) -> None:
    if source_path.suffix.lower() == ".txt":
        full_text, page_offsets = _extract_text_from_txt(source_path)
    else:
        full_text, page_offsets = _extract_text_from_pdf(source_path)

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
