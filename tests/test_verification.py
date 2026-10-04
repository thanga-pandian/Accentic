"""
Tests for the verification contract — the project's central integrity claim.

Run: python -m tests.test_verification

No pytest dependency; this has to run on a locked-down demo machine.
These exist because "we reject hallucinations" is the whole argument, and an
untested claim is just a slogan.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

# Point at a throwaway database before db.connection reads the variable.
_TMP = Path(tempfile.gettempdir()) / f"accentic_test_{uuid.uuid4().hex[:8]}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.as_posix()}"

from agents import llm                                    # noqa: E402
from agents.base import VerificationError, anchor         # noqa: E402
from agents.obligations import ObligationAgent, _rule_extract  # noqa: E402
from db.connection import get_connection                  # noqa: E402

PASSED, FAILED = [], []


def test(fn):
    try:
        fn()
        PASSED.append(fn.__name__)
        print(f"  PASS  {fn.__name__}")
    except AssertionError as e:
        FAILED.append((fn.__name__, str(e)))
        print(f"  FAIL  {fn.__name__}\n        {e}")
    except Exception as e:
        FAILED.append((fn.__name__, f"{type(e).__name__}: {e}"))
        print(f"  ERROR {fn.__name__}\n        {type(e).__name__}: {e}")
    return fn


SOURCE = (
    "46.2 The records referred to in paragraph 46.1 shall be maintained for a\n"
    "period of five years from the date of the transactions between the RE and\n"
    "the customer."
)


# ---------------------------------------------------------------------------
# anchoring
# ---------------------------------------------------------------------------

@test
def anchor_finds_exact_quote():
    quote = "shall be maintained for a"
    s, e = anchor(quote, SOURCE, 0)
    assert SOURCE[s:e] == quote, f"slice {SOURCE[s:e]!r} != quote"


@test
def anchor_tolerates_collapsed_whitespace():
    """Models routinely reflow newlines when quoting. The span must still be real."""
    quote = "shall be maintained for a period of five years"   # newline removed
    s, e = anchor(quote, SOURCE, 0)
    sliced = SOURCE[s:e]
    assert "\n" in sliced, "expected the ORIGINAL text, which spans a line break"
    assert sliced.split() == quote.split(), f"word mismatch: {sliced!r}"


@test
def anchor_offsets_are_absolute():
    s, e = anchor("five years", SOURCE, 1000)
    assert (s, e) == (1000 + SOURCE.index("five years"),
                      1000 + SOURCE.index("five years") + len("five years")), (s, e)


@test
def anchor_rejects_fabricated_quote():
    try:
        anchor("records shall be destroyed after ninety days", SOURCE, 0)
    except VerificationError:
        return
    raise AssertionError("a fabricated quote was accepted")


@test
def anchor_rejects_altered_number():
    """The subtlest and most dangerous hallucination: a plausible wrong number."""
    try:
        anchor("maintained for a period of ten years", SOURCE, 0)
    except VerificationError:
        return
    raise AssertionError("an altered retention period was accepted")


@test
def anchor_rejects_paraphrase():
    try:
        anchor("records must be kept for five years", SOURCE, 0)
    except VerificationError:
        return
    raise AssertionError("a paraphrase was accepted")


@test
def anchor_rejects_empty():
    try:
        anchor("   ", SOURCE, 0)
    except VerificationError:
        return
    raise AssertionError("an empty quote was accepted")


# ---------------------------------------------------------------------------
# rule engine
# ---------------------------------------------------------------------------

@test
def rule_engine_only_returns_real_substrings():
    for cand in _rule_extract(SOURCE):
        assert cand["quote"] in SOURCE or " ".join(cand["quote"].split()) in \
            " ".join(SOURCE.split()), f"rule engine invented text: {cand['quote'][:60]!r}"


@test
def rule_engine_detects_modality():
    found = _rule_extract(SOURCE)
    assert found, "no obligation found in text containing 'shall'"
    assert found[0]["modality"] == "must", found[0]["modality"]


# ---------------------------------------------------------------------------
# end-to-end: the recovery and quarantine paths
# ---------------------------------------------------------------------------

def _seed(conn, text):
    """One regulation with one section whose offsets are correct by construction."""
    reg_id, sec_id = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        for stmt in (Path(__file__).parent.parent / "db" / "sqlite_schema.sql") \
                .read_text(encoding="utf-8").split(";"):
            if stmt.strip():
                cur.execute(stmt)
        # Tests share one file; clear it so counts measure only this test.
        for table in ("obligation", "needs_review", "reg_section",
                      "regulation", "agent_run"):
            cur.execute(f"DELETE FROM {table}")
        cur.execute(
            "INSERT INTO regulation (id,regulator,jurisdiction,title,full_text) "
            "VALUES (%s,%s,%s,%s,%s)", (reg_id, "RBI", "India", "Test Reg", text))
        cur.execute(
            "INSERT INTO reg_section "
            "(id,regulation_id,clause_path,page_no,raw_text,char_start,char_end) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (sec_id, reg_id, "46.", 1, text, 0, len(text)))
    conn.commit()
    return reg_id


@test
def hallucinated_quote_is_recovered_from_source():
    """
    The model fabricates. The anchor fails. The agent must fall back to text it
    can prove, and the stored result must still be a true slice of the source.
    """
    conn = get_connection()
    try:
        reg_id = _seed(conn, SOURCE)
        original = llm.complete_json
        llm.complete_json = lambda *a, **k: [{
            "quote": "records shall be destroyed after ninety days",   # not in source
            "actor": "RE", "modality": "must", "action": "destroy",
            "risk_tier": "high", "category": "recordkeeping",
        }]
        try:
            llm.available = lambda: True
            result = ObligationAgent("test-run").execute(conn, regulation_id=reg_id)
        finally:
            llm.complete_json = original

        assert result["summary"]["recovered"] >= 1, \
            f"recovery never fired: {result['summary']}"

        with conn.cursor() as cur:
            cur.execute("SELECT obligation_text, char_start, char_end FROM obligation")
            rows = cur.fetchall()
        assert rows, "recovery produced no obligation"
        for text, s, e in rows:
            assert SOURCE[s:e] == text, "stored text is not a slice of the source"
            assert "ninety days" not in text, "the fabricated claim leaked into storage"
    finally:
        conn.close()


@test
def unrecoverable_hallucination_is_quarantined():
    """
    When the model fabricates AND there is nothing real to fall back to, the
    claim must be routed to a human, never silently dropped or stored.
    """
    barren = "This paragraph is descriptive and imposes no requirement of any kind."
    conn = get_connection()
    try:
        reg_id = _seed(conn, barren)
        original = llm.complete_json
        llm.complete_json = lambda *a, **k: [{
            "quote": "the entity shall surrender its licence immediately",
            "actor": "RE", "modality": "must", "action": "surrender",
            "risk_tier": "high", "category": "governance",
        }]
        try:
            llm.available = lambda: True
            result = ObligationAgent("test-run").execute(conn, regulation_id=reg_id)
        finally:
            llm.complete_json = original

        assert result["summary"]["rejected"] >= 1, \
            f"fabrication was not rejected: {result['summary']}"

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM obligation")
            assert cur.fetchone()[0] == 0, "a fabricated obligation was stored"
            cur.execute("SELECT stage, failure_reason FROM needs_review")
            review = cur.fetchall()
        assert review, "rejected claim never reached the needs_review queue"
        assert "anchor" in review[0][1].lower(), review[0][1]
    finally:
        conn.close()


@test
def every_stored_obligation_reproduces_its_source():
    """The invariant the whole product rests on, checked against real text."""
    conn = get_connection()
    try:
        reg_id = _seed(conn, SOURCE)
        llm.available = lambda: False          # rule-engine path
        ObligationAgent("test-run").execute(conn, regulation_id=reg_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.obligation_text, o.char_start, o.char_end, r.full_text "
                "FROM obligation o JOIN regulation r ON r.id = o.regulation_id")
            rows = cur.fetchall()
        assert rows, "no obligations extracted"
        for text, s, e, full in rows:
            assert full[s:e] == text, f"offset drift at [{s}:{e}]"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# model output parsing
# ---------------------------------------------------------------------------

@test
def json_parser_handles_fenced_and_bare_output():
    cases = [
        '```json\n[{"quote":"x"}]\n```',
        '[{"quote":"x"}]',
        'Here you go:\n[{"quote":"x"}]\nhope that helps',
    ]
    for raw in cases:
        got = llm._parse_json(raw)
        assert got == [{"quote": "x"}], f"{raw!r} -> {got!r}"


@test
def json_parser_returns_none_on_garbage():
    for raw in ("I cannot help with that.", "", "   "):
        assert llm._parse_json(raw) is None, f"{raw!r} should not parse"


if __name__ == "__main__":
    print("\nVerification contract\n" + "-" * 52)
    total = len(PASSED) + len(FAILED)
    print("-" * 52)
    print(f"{len(PASSED)}/{total} passed")
    if FAILED:
        print("\nFailures:")
        for name, err in FAILED:
            print(f"  {name}: {err}")
    _TMP.unlink(missing_ok=True)
    sys.exit(1 if FAILED else 0)
