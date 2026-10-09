"""
Verify that stored offsets in reg_section correctly point into regulation.full_text.

Usage:
    python -m extract.verify_offsets 38         # check clause_path '38'
    python -m extract.verify_offsets "38(a)"    # sub-clause
    python -m extract.verify_offsets --audit    # check every section, report pass rate
"""

import argparse
import difflib
import sys

from db.connection import get_connection

CONTEXT_CHARS = 150


def _check_section(cur, section_row, full_text: str) -> bool:
    """
    Print details for one section and return True if offsets are correct.
    """
    sec_id, clause_path, page_no, char_start, char_end, raw_text = section_row

    resliced = full_text[char_start:char_end]
    passed = resliced == raw_text

    print(f"\n{'='*70}")
    print(f"clause_path : {clause_path}")
    print(f"page_no     : {page_no}")
    print(f"char_start  : {char_start:,}")
    print(f"char_end    : {char_end:,}")
    print(f"length      : {char_end - char_start:,}")
    print()
    print("--- stored raw_text ---")
    print(raw_text)
    print()
    print("--- re-sliced full_text[char_start:char_end] ---")
    print(resliced)
    print()

    if passed:
        print("PASS")
    else:
        print("FAIL — diff:")
        diff = difflib.unified_diff(
            raw_text.splitlines(keepends=True),
            resliced.splitlines(keepends=True),
            fromfile="raw_text",
            tofile="resliced",
        )
        sys.stdout.writelines(diff)

    # Context window
    ctx_before = full_text[max(0, char_start - CONTEXT_CHARS) : char_start]
    ctx_after = full_text[char_end : char_end + CONTEXT_CHARS]
    print()
    print(f"--- {CONTEXT_CHARS} chars BEFORE ---")
    print(repr(ctx_before))
    print(f"--- {CONTEXT_CHARS} chars AFTER ---")
    print(repr(ctx_after))

    return passed


def spot_check(clause_path: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rs.id, rs.clause_path, rs.page_no, rs.char_start, rs.char_end,
                       rs.raw_text, r.full_text
                FROM reg_section rs
                JOIN regulation r ON r.id = rs.regulation_id
                WHERE rs.clause_path IN (%s, %s)
                ORDER BY rs.created_at
                LIMIT 1
                """,
                (clause_path, clause_path.rstrip(".") + "."),
            )
            row = cur.fetchone()
            if row is None:
                print(f"No section found with clause_path={clause_path!r}", file=sys.stderr)
                sys.exit(1)

            *section_row, full_text = row
            _check_section(cur, section_row, full_text)
    finally:
        conn.close()


def audit() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rs.id, rs.clause_path, rs.page_no, rs.char_start, rs.char_end,
                       rs.raw_text, r.full_text
                FROM reg_section rs
                JOIN regulation r ON r.id = rs.regulation_id
                ORDER BY rs.char_start
                """
            )
            rows = cur.fetchall()

        if not rows:
            print("No sections in database.", file=sys.stderr)
            sys.exit(1)

        total = len(rows)
        failures: list[tuple[str, str]] = []

        for row in rows:
            *section_row, full_text = row
            _, clause_path, _, char_start, char_end, raw_text = section_row
            resliced = full_text[char_start:char_end]
            if resliced != raw_text:
                failures.append((clause_path, f"char_start={char_start} char_end={char_end}"))

        passed = total - len(failures)
        pct = 100 * passed / total if total else 0

        print(f"\nAudit results: {passed}/{total} PASS  ({pct:.1f}%)")
        if failures:
            print(f"\nFailed sections ({len(failures)}):")
            for clause_path, info in failures:
                print(f"  FAIL  {clause_path!r}  {info}")
        else:
            print("All sections verified.")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify reg_section offset correctness.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("clause_path", nargs="?", help="Clause path to spot-check, e.g. '38'")
    group.add_argument("--audit", action="store_true", help="Audit all sections")
    args = parser.parse_args()

    if args.audit:
        audit()
    else:
        spot_check(args.clause_path)


if __name__ == "__main__":
    main()
