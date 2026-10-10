"""
Tests for the chain links added beyond extraction: applicability, control testing,
mapping scope, clause references and monitoring drift.

Run: python -m tests.test_chain
"""

import os
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / f"accentic_chain_{uuid.uuid4().hex[:8]}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.as_posix()}"
os.environ["ANTHROPIC_API_KEY"] = ""   # tests never spend money; load_dotenv won't override
os.environ["ACCENTIC_LLM_CACHE"] = str(_TMP.with_suffix(".llm.json"))   # never the real cache

from agents.applicability import assess                      # noqa: E402
from agents.base import clause_ref                           # noqa: E402
from agents.contradiction import ContradictionAgent          # noqa: E402
from agents.mapping import scope_gaps                        # noqa: E402
from agents.monitoring import drift, fingerprint, source_key # noqa: E402
from agents.testing import currency_test, operating_test     # noqa: E402
from db.connection import get_connection                     # noqa: E402

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


PROFILE = {
    "name": "Test Bank", "entity_type": "Scheduled Commercial Bank", "jurisdictions": ["India"],
    "business_units": {"Retail Banking": "", "Financial Crime Unit": "", "Regulatory Reporting": "",
                       "Branch Network": ""},
    "activities": {
        "pep_relationships": {"label": "Relationships with PEPs", "enabled": True},
        "walk_in_transactions": {"label": "Walk-in transactions", "enabled": True},
        "international_money_transfer": {"label": "International money transfer", "enabled": False},
    },
}


# ---------------------------------------------------------------------------
# applicability
# ---------------------------------------------------------------------------

@test
def definitions_impose_no_duty():
    status, reason, _ = assess('(a) "Aadhaar number" shall have the same meaning as in the Act;',
                               "must", "RBI", "India", PROFILE)
    assert status == "informational", (status, reason)


@test
def title_clause_imposes_no_duty():
    status, _, _ = assess("These Directions shall be called the KYC Directions, 2016.",
                          "must", "RBI", "India", PROFILE)
    assert status == "informational", status


@test
def permissive_clause_imposes_no_duty():
    status, _, _ = assess("The RE may rely on a third party for CDD.", "may", "RBI", "India", PROFILE)
    assert status == "informational", status


@test
def definitional_limb_imposes_no_duty():
    status, _, _ = assess("(iv) gives rise to a reasonable ground of suspicion of terrorism",
                          "may", "RBI", "India", PROFILE, definitional_limb=True)
    assert status == "informational", status


@test
def inactive_business_line_is_out_of_scope():
    status, reason, _ = assess("REs shall verify identity for any international money transfer operations.",
                               "must", "RBI", "India", PROFILE)
    assert status == "not_applicable", (status, reason)
    assert "international money transfer" in reason, reason


@test
def mixed_limbs_stay_applicable_with_scope_note():
    status, reason, _ = assess(
        "Every entity shall carry out due diligence for any international money transfer operations "
        "and for occasional transactions of more than Rs. 50,000.", "must", "GoI", "India", PROFILE)
    assert status == "applicable", status
    assert "out of scope" in reason, reason


@test
def foreign_jurisdiction_is_out_of_scope():
    status, _, _ = assess("Firms shall report suspicious activity.", "must", "FCA", "United Kingdom", PROFILE)
    assert status == "not_applicable", status


@test
def obligations_are_assigned_business_units():
    _, _, units = assess("REs shall report suspicious transactions to FIU-IND within 7 days.",
                         "must", "RBI", "India", PROFILE)
    assert "Financial Crime Unit" in units and "Regulatory Reporting" in units, units


@test
def reporting_entity_is_not_a_reporting_duty():
    _, _, units = assess("Every reporting entity shall maintain records of its clients.",
                         "must", "GoI", "India", PROFILE)
    assert "Regulatory Reporting" not in units, units


# ---------------------------------------------------------------------------
# control testing
# ---------------------------------------------------------------------------

@test
def lag_against_requirement_is_an_exception():
    t = operating_test("STR case log — mean filing lag 11.4 days against 7 day requirement", False, 5)
    assert t["result"] == "exception", t
    assert "4.4-day breach" in t["conclusion"], t["conclusion"]


@test
def lag_within_requirement_passes():
    t = operating_test("mean filing lag 5 days against 7 day requirement", False, 5)
    assert t["result"] == "pass", t


@test
def exception_rate_above_tolerance_fails():
    t = operating_test("PEP approval register — incomplete for 14 of 61 sampled relationships", False, 5)
    assert t["result"] == "exception" and "23.0%" in t["metric"], t


@test
def regulatory_filing_has_zero_tolerance():
    t = operating_test("1 of 12 submissions filed after the 15th-day deadline", True, 5)
    assert t["result"] == "exception" and t["threshold"] == "zero tolerance", t


@test
def descriptive_evidence_is_relied_not_passed():
    t = operating_test("Board minute of designation, FIU acknowledgement letter", False, 5)
    assert t["result"] == "relied", t


@test
def stale_test_lapses_assurance():
    t = currency_test("2025-05-20", False, date(2025, 12, 15), {"manual": 180})
    assert t["result"] == "overdue", t


@test
def current_test_passes():
    t = currency_test("2025-10-14", True, date(2025, 12, 15), {"automated": 365})
    assert t["result"] == "pass", t


# ---------------------------------------------------------------------------
# mapping scope and clause references
# ---------------------------------------------------------------------------

@test
def transaction_retention_does_not_cover_identity_records():
    missing = scope_gaps("The records of the identity of clients shall be maintained for five years",
                         "Transaction Record Retention. Records of all customer transactions are retained")
    assert missing == ["identity"], missing


@test
def actors_and_inclusive_phrases_are_not_scope():
    missing = scope_gaps("The Principal Officer shall report all suspicious transactions, whether or not in cash",
                         "Suspicious Transaction Reporting. Alerts confirmed as suspicious are filed")
    assert missing == [], missing


@test
def clause_ref_recovers_sub_clause_path():
    text = "13. CDD\n13.1 REs shall undertake CDD while:\n(i) opening an account;\n(ii) carrying out a transaction;\n"
    assert clause_ref(text, text.index("carrying")) == "13.1(ii)", clause_ref(text, text.index("carrying"))
    assert clause_ref(text, text.index("REs")) == "13.1"


@test
def clause_ref_reads_gazette_numbering():
    # Official text: amendment brackets, sub-rules, lettered rules and a numbered
    # explanation inside a rule that must not be read as rule 1 and 2.
    text = ("[8. Furnishing of information.—(1) The Principal Officer shall furnish monthly.\n"
            "[(2) The principal officer shall furnish suspicious transactions promptly.\n"
            "[9. Client Due Diligence.—\n(1) Every reporting entity shall identify clients.\n"
            "Explanation.—\n1. “Control” means the right to appoint directors.\n"
            "(3) Where the client is a trust, the entity shall verify trustees.\n"
            "[9-B. Inspection.—(1) The Reserve Bank may call for records.\n")
    refs = {k: clause_ref(text, text.index(k)) for k in ("promptly", "verify trustees", "call for")}
    assert refs == {"promptly": "8(2)", "verify trustees": "9(3)", "call for": "9-B"}, refs


@test
def wrapped_words_are_not_clause_numbers():
    text = "5. The bank shall keep records for\n10 years from the date of transaction.\n"
    assert clause_ref(text, text.index("years")) == "5", clause_ref(text, text.index("years"))


@test
def assessment_scope_follows_paragraph_ranges():
    from agents.applicability import in_review
    profile = {"assessment_scope": {"paragraphs": {"RBI": "20, 38-46, 50-62", "GoI": "3-5, 10"}}}
    assert in_review("58", "RBI", profile) and in_review("44(2)(b)", "RBI", profile)
    assert not in_review("23(6)", "RBI", profile) and not in_review("", "RBI", profile)
    assert in_review("3A(2)", "GoI", profile), "a range takes its lettered inserts"
    assert not in_review("9-A", "GoI", profile)
    assert in_review("7", "SEBI", profile), "a regulator with no entry is reviewed in full"
    status, reason, _ = assess("The bank shall do x.", "must", "RBI", "India",
                               {**PROFILE, **profile}, reviewed=False)
    assert status == "not_assessed" and "20, 38-46, 50-62" in reason, (status, reason)


@test
def list_limbs_inherit_the_lead_in_duty():
    from agents.obligations import _rule_extract
    lead = "50. The bank shall take the following steps regarding maintenance, preservation"
    duty = _rule_extract("(1) maintain all necessary records of transactions for at least five years;", lead)
    item = _rule_extract("(i) the nature of the transactions and the currency in which it was denominated;", lead)
    assert [d["modality"] for d in duty] == ["must"], duty
    assert item == [], item


@test
def repagination_is_not_a_regulatory_change():
    from agents.impact import ChangeAgent
    prior = ("11. The bank shall have Board-approved policies in this\nregard.\n"
             "14. The bank shall report to the FIU-IND.\n18\n\n"
             "15. Principal Officer shall be designated.\n")
    current = ("11. The bank shall have policies approved by the Board or committee of the Board.\n"
               "14. The bank shall report to the\nFIU-IND.\n2\nSubstituted w.e.f. October 1, 2026 "
               "vide Amendment Directions, 2026\n18\n\n15. Principal Officer shall be designated.\n")
    changed = [c["clause_path"] for c in ChangeAgent("t")._diff_clauses(prior, current)]
    assert changed == ["11"], changed


# ---------------------------------------------------------------------------
# contradiction direction
# ---------------------------------------------------------------------------

def _conflict(subject, text_a, text_b):
    agent = ContradictionAgent("t")
    a = ("a", "5", text_a, "must", "PMLA Rules", "r1")
    b = ("b", "46.2", text_b, "must", "RBI KYC", "r2")
    return agent._compare(subject, a, b)


@test
def longer_retention_period_binds():
    c = _conflict("retention", "Records shall be maintained for a period of ten years.",
                  "Records shall be maintained for a period of five years.")
    assert c and "longer period governs — 10 years" in c[1], c


@test
def shorter_filing_deadline_binds():
    c = _conflict("str_filing", "Suspicious transactions shall be reported within three working days.",
                  "Suspicious transactions shall be reported within 7 days.")
    assert c and "shorter period governs — 3 days" in c[1], c


@test
def different_record_classes_do_not_conflict():
    c = _conflict("retention", "Records of transactions shall be maintained for ten years.",
                  "Records of the identity of clients shall be maintained for five years.")
    assert c is None, c


@test
def a_regulation_does_not_contradict_itself():
    agent = ContradictionAgent("t")
    a = ("a", "41(1)", "The bank shall review risk categorisation periodically.", "must", "RBI KYC", "r1")
    b = ("b", "42(3)", "The bank shall not insist on fresh documents at periodic review.", "must_not",
         "RBI KYC", "r1")
    assert agent._compare("periodic_review", a, b) is None


# ---------------------------------------------------------------------------
# monitoring drift
# ---------------------------------------------------------------------------

@test
def drift_detects_a_modified_source():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for stmt in (Path(__file__).parent.parent / "db" / "sqlite_schema.sql") \
                    .read_text(encoding="utf-8").split(";"):
                if stmt.strip():
                    cur.execute(stmt)
        src = Path(tempfile.gettempdir()) / f"accentic_src_{uuid.uuid4().hex[:6]}.txt"
        src.write_text("53.1 report within 7 days", encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source_snapshot (id, run_id, kind, path, sha256) VALUES (%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), "r1", "regulation", source_key(src), fingerprint(src)))
        conn.commit()
        assert drift(conn, [("regulation", src)]) == [], "unchanged source reported as drift"
        src.write_text("53.1 report within 5 days", encoding="utf-8")
        changed = drift(conn, [("regulation", src)])
        assert changed and changed[0]["change"] == "modified", changed
        src.unlink(missing_ok=True)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# trainability, self-assurance, AI remediation drafting (end to end on the corpus)
# ---------------------------------------------------------------------------

def _apply_schema():
    conn = get_connection()
    with conn.cursor() as cur:
        for stmt in (Path(__file__).parent.parent / "db" / "sqlite_schema.sql") \
                .read_text(encoding="utf-8").split(";"):
            if stmt.strip():
                cur.execute(stmt)
    conn.commit()
    conn.close()


def _fresh_run():
    from agents.orchestrator import run_pipeline
    _apply_schema()
    return run_pipeline()


def _un_list(entries):
    body = "".join(
        f"<INDIVIDUAL><FIRST_NAME>{name}</FIRST_NAME><UN_LIST_TYPE>{kind}</UN_LIST_TYPE>"
        f"<REFERENCE_NUMBER>{ref}</REFERENCE_NUMBER><LISTED_ON>2020-01-01</LISTED_ON>"
        f"<INDIVIDUAL_ALIAS><ALIAS_NAME>{name} ALIAS</ALIAS_NAME></INDIVIDUAL_ALIAS></INDIVIDUAL>"
        for ref, name, kind in entries)
    entity = ("<ENTITY><FIRST_NAME>DELTA TRADING</FIRST_NAME><UN_LIST_TYPE>DPRK</UN_LIST_TYPE>"
              "<REFERENCE_NUMBER>KPe.004</REFERENCE_NUMBER><LISTED_ON>2020-01-01</LISTED_ON></ENTITY>")
    return (f'<?xml version="1.0"?><CONSOLIDATED_LIST dateGenerated="2026-10-08T23:00:00Z">'
            f"<INDIVIDUALS>{body}</INDIVIDUALS><ENTITIES>{entity}</ENTITIES></CONSOLIDATED_LIST>").encode()


@test
def sanctions_sync_names_added_and_removed_designations():
    from agents import sanctions
    _apply_schema()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sanctions_entry")
            cur.execute("DELETE FROM sanctions_snapshot")
        conn.commit()
        first = sanctions.sync(conn, _un_list([("KPi.001", "ALPHA ONE", "DPRK"),
                                               ("QDi.002", "BRAVO TWO", "Al-Qaida")]))
        second = sanctions.sync(conn, _un_list([("KPi.001", "ALPHA ONE", "DPRK"),
                                                ("TAi.003", "CHARLIE THREE", "Taliban")]))
        listed = sanctions.status(conn)
    finally:
        conn.close()
    assert first["first_sync"] and first["added"] == 0, first
    assert (second["added"], second["removed"]) == (1, 1), second
    assert listed["changes"]["added"][0]["name"] == "CHARLIE THREE", listed["changes"]
    assert listed["changes"]["removed"][0]["ref"] == "QDi.002", listed["changes"]
    paras = {r["list"]: (r["rbi_para"], r["individuals"], r["entities"]) for r in listed["regimes"]}
    assert paras == {"UNSCR 1718 Sanctions List": ("58", 1, 1),
                     "Taliban Sanctions List": ("56(1)(ii)", 1, 0)}, paras
    assert not listed["stale"]


@test
def sanctions_sync_rejects_a_feed_that_is_not_the_un_list():
    from agents import sanctions
    for bad in (b"<html><body>Access denied</body></html>", b"not xml at all"):
        try:
            sanctions.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


@test
def reviewer_decisions_override_and_teach():
    from agents import feedback
    # PMLA rule 10(1): keep the physical record of client identity. RBI 20(2): a
    # permissive "may" the system calls informational.
    identity_rule = """SELECT o.obligation_text, o.clause_ref, r.regulator, c.control_ref,
                              m.reviewed, m.rationale
                       FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                       JOIN mapping m ON m.obligation_id = o.id JOIN control c ON c.id = m.control_id
                       WHERE r.regulator = 'GoI' AND o.clause_ref = '10(1)'"""
    permissive = """SELECT o.obligation_text, o.clause_ref, r.regulator, o.applicability,
                           o.applicability_reviewed
                    FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                    WHERE r.regulator = 'RBI' AND o.clause_ref = '20(2)' ORDER BY o.char_start"""
    _fresh_run()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(identity_rule)
            text, ref, regulator, current = cur.fetchone()[:4]
            cur.execute(permissive)
            may_row = cur.fetchone()
        assert current == "CTRL-REC-030", current
        assert may_row[3] == "informational", may_row
        feedback.record(conn, target="mapping", regulator=regulator, clause_ref=ref, text=text,
                        system_value=current, decided_value="CTRL-KYC-001",
                        note="identity records sit with onboarding", reviewer="Test Reviewer")
        feedback.record(conn, target="applicability", regulator=may_row[2], clause_ref=may_row[1],
                        text=may_row[0], system_value="informational", decided_value="applicable",
                        note="", reviewer="Test Reviewer")
    finally:
        conn.close()

    stats = _fresh_run()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(identity_rule)
            ref_now, reviewed, rationale = cur.fetchone()[3:]
            cur.execute(permissive)
            app_now = cur.fetchone()[3:]
            cur.execute("SELECT output_summary FROM agent_run WHERE agent_name = 'MappingAgent'")
            summary = cur.fetchone()[0]
    finally:
        conn.close()
    assert ref_now == "CTRL-KYC-001" and reviewed == 1, (ref_now, reviewed)
    assert rationale.startswith("Reviewer-confirmed"), rationale
    assert app_now == ("applicable", 1), app_now
    assert stats["reviews"]["applied"] == 2, stats["reviews"]
    assert "held-out agreement" in summary, summary


@test
def assurance_self_corrects_a_broken_chain():
    from agents.assurance import AssuranceAgent
    from agents.gaps import GapAgent
    _fresh_run()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM remediation")
        conn.commit()
        result = AssuranceAgent("t").execute(conn, rerun={"gaps": lambda: GapAgent("t").execute(conn)})
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gap g WHERE NOT EXISTS (SELECT 1 FROM remediation r WHERE r.gap_id = g.id)")
            orphans = cur.fetchone()[0]
    finally:
        conn.close()
    assert result["summary"]["self_corrected"] == 1, result["summary"]
    assert result["summary"]["holding"] == result["summary"]["invariants"], result["checks"]
    assert orphans == 0, orphans


@test
def ai_remediation_drafts_are_validated():
    from agents import gaps, llm
    sample = [{"gap_id": "g1", "clause": "53.1(b)", "category": "reporting", "severity": "critical",
               "control": "CTRL-RPT-041", "test_result": "Exception", "rationale": "x", "obligation": "y"},
              {"gap_id": "g2", "clause": "6", "category": "recordkeeping", "severity": "high",
               "control": "CTRL-REC-030", "test_result": "Relied", "rationale": "x", "obligation": "y"}]
    original_complete, original_available = llm.complete_json, llm.available
    llm.available = lambda: True
    llm.complete_json = lambda *a, **k: [
        {"gap_id": "g1", "action": "Add an SLA timer to CTRL-RPT-041 that escalates any STR case at day 2.",
         "owner_role": "Head of Financial Crime", "effort": "M"},
        {"gap_id": "g2", "action": "short", "owner_role": "Ops", "effort": "XL"},   # invalid
        {"gap_id": "invented", "action": "A remediation for a gap that does not exist anywhere.",
         "owner_role": "Nobody", "effort": "S"},
    ]
    try:
        drafted = gaps.draft_remediations(sample)
    finally:
        llm.complete_json, llm.available = original_complete, original_available
    assert set(drafted) == {"g1"}, drafted


@test
def workflow_reopens_unfixed_and_verifies_closure():
    from agents.workflow import WorkflowAgent
    _fresh_run()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), SUM(status = 'open') FROM remediation_task")
            total, open_ = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM gap")
            gaps = cur.fetchone()[0]
            # A human marks every task done, but nothing has actually been fixed.
            cur.execute("UPDATE remediation_task SET status = 'done'")
            # And one task belongs to a gap that has genuinely gone away.
            cur.execute("INSERT INTO remediation_task (id, obligation_key, status, first_detected) "
                        "VALUES ('x', 'gone-key', 'in_progress', '2026-01-01')")
        conn.commit()
        result = WorkflowAgent("t").execute(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM remediation_task WHERE obligation_key = 'gone-key'")
            gone = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM task_comment WHERE body LIKE 'Re-opened%'")
            notes = cur.fetchone()[0]
    finally:
        conn.close()
    assert open_ == gaps and total >= gaps, (total, open_, gaps)   # earlier tests leave closed tasks
    assert result["summary"]["reopened"] == gaps, result["summary"]
    assert result["summary"]["resolved"] == 1 and gone == "done", (result["summary"], gone)
    assert notes == gaps, notes


@test
def qa_strips_unverifiable_citations():
    from agents import llm, qa
    _fresh_run()
    conn = get_connection()
    original_complete, original_available = llm.complete_json, llm.available
    llm.available = lambda: True

    def fake(system, prompt, **_):
        import re
        # Cite whichever retrieved source really holds PMLA rule 8(2).
        block = next(b for b in re.split(r"\n\n(?=\[S\d+\])", prompt.split("SOURCES:\n", 1)[1])
                     if "furnish the information promptly" in b)
        sid = re.match(r"\[(S\d+)\]", block).group(1)
        return {"answer": [
            {"text": "STRs must be furnished promptly.",
             "citations": [{"source": sid, "quote": "furnish the information promptly"}]},
            {"text": "STRs must be furnished within 2 days.",                   # fabricated
             "citations": [{"source": sid, "quote": "furnish the information within 2 days"}]},
            {"text": "An invented source.", "citations": [{"source": "S99", "quote": "x"}]},
        ]}
    llm.complete_json = fake
    try:
        result = qa.QAAgent("t").execute(conn, question="When must suspicious transactions be reported?")
    finally:
        llm.complete_json, llm.available = original_complete, original_available
        conn.close()
    texts = [p["text"] for p in result["answer"]]
    assert result["mode"] == "claude", result["mode"]
    assert texts == ["STRs must be furnished promptly."], texts
    assert result["rejected_citations"] == 2 and result["withheld_paragraphs"] == 2, result
    cite = result["answer"][0]["citations"][0]
    assert cite["quote"] == "furnish the information promptly", cite


@test
def qa_without_a_model_quotes_obligations_verbatim():
    from agents import qa
    conn = get_connection()
    try:
        result = qa.QAAgent("t").execute(conn, question="How long must transaction records be kept?")
        with conn.cursor() as cur:
            cur.execute("SELECT full_text FROM regulation")
            corpus = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert result["mode"] == "rule_engine" and result["answer"], result
    for para in result["answer"]:
        for c in para["citations"]:
            if c["kind"] == "source":
                assert any(full[c["char_start"]:c["char_end"]] == c["quote"] for full in corpus), c


if __name__ == "__main__":
    print("\nChain links\n" + "-" * 52)
    total = len(PASSED) + len(FAILED)
    print("-" * 52)
    print(f"{len(PASSED)}/{total} passed")
    if FAILED:
        print("\nFailures:")
        for name, err in FAILED:
            print(f"  {name}: {err}")
    _TMP.unlink(missing_ok=True)
    sys.exit(1 if FAILED else 0)
