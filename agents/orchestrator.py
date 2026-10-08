"""
Pipeline orchestrator.

Runs the chain the problem statement defines, one agent per link:

    regulations -> obligations -> applicability -> internal controls -> evidence
    -> testing -> gaps -> remediation -> ongoing monitoring

Emits events as it goes so the UI can show the agents reasoning in real time.
"""

import queue
import threading
import uuid
from pathlib import Path

from agents import llm
from agents import profile as bank_profile
from agents import feedback
from agents.applicability import ApplicabilityAgent
from agents.assurance import AssuranceAgent
from agents.contradiction import ContradictionAgent
from agents.controls import ControlAgent
from agents.gaps import GapAgent
from agents.impact import ChangeAgent
from agents.ingestion import IngestionAgent
from agents.mapping import MappingAgent
from agents.monitoring import MonitoringAgent, fingerprint, previous_snapshots, source_key
from agents.obligations import ObligationAgent
from agents.testing import TestingAgent
from agents.workflow import WorkflowAgent
from db.connection import get_connection

ROOT = Path(__file__).resolve().parent.parent
REG_DIR = ROOT / "data" / "regulations"
CTRL_DIR = ROOT / "data" / "controls"
UPLOAD_DIR = ROOT / "data" / "uploads"
UPLOAD_DIRS = {"regulation": UPLOAD_DIR / "regulations", "control": UPLOAD_DIR / "controls"}

PLAN = [
    ("IngestionAgent", "Parse regulations and compute verifiable character offsets"),
    ("ObligationAgent", "Extract obligations and anchor every one to its source"),
    ("ApplicabilityAgent", "Decide which obligations bind this bank, and which units own them"),
    ("ControlAgent", "Parse the bank control framework and link evidence"),
    ("TestingAgent", "Re-perform control tests from evidence, not owner say-so"),
    ("MappingAgent", "Map applicable obligations to controls, discounted by test results"),
    ("GapAgent", "Score gaps by impact x likelihood and recommend remediation"),
    ("ContradictionAgent", "Detect conflicting requirements across regulations"),
    ("ChangeAgent", "Diff against the prior version and size the impact"),
    ("AssuranceAgent", "Check the chain's invariants and self-correct once"),
    ("WorkflowAgent", "Reconcile remediation tasks with this run's findings"),
    ("MonitoringAgent", "Compare with the last run, raise alerts, record KRIs"),
]


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]


def _plan_reasons(conn, sources, previous) -> dict:
    """Why each step runs the way it does on this run, derived from current state."""
    profile = bank_profile.load()
    regs = [p for k, p in sources if k == "regulation"]
    changed = [p for k, p in sources
               if k == "regulation" and previous.get(source_key(p))
               and previous[source_key(p)]["sha256"] != fingerprint(p)]
    reviews = {t: len(feedback.decisions(conn, t)) for t in ("applicability", "mapping")}
    active = sum(1 for a in profile.get("activities", {}).values() if a.get("enabled"))
    priors = [p for k, p in sources if k == "prior"]
    ai = llm.available()
    return {
        "IngestionAgent": f"{len(regs)} regulation sources; {len(changed)} changed since the last run",
        "ObligationAgent": ("Claude Opus 5.5 extraction, every quote anchored, rule-engine recovery"
                            if ai else "Rule engine (no API key); every quote anchored"),
        "ApplicabilityAgent": f"{active} active business lines; {reviews['applicability']} reviewer decisions to honour",
        "ControlAgent": f"{sum(1 for k, _ in sources if k == 'control')} control documents",
        "TestingAgent": f"Assessment date {profile.get('assessment_date')}",
        "MappingAgent": f"Learning from {reviews['mapping']} reviewer mapping decisions",
        "GapAgent": ("Claude drafts remediations; each draft validated, playbook fallback"
                     if ai else "Remediations from the control-design playbook"),
        "ContradictionAgent": f"{len(regs)} regulations cross-checked",
        "ChangeAgent": (f"{len(changed)} source(s) changed: diff against the previous run" if changed
                        else f"Diff against {len(priors)} supplied prior version(s)" if priors
                        else "No baseline yet: skipped"),
        "AssuranceAgent": "Verify invariants; re-run a failing stage once, else quarantine",
        "WorkflowAgent": f"{_count(conn, 'remediation_task')} tracked tasks to reconcile",
        "MonitoringAgent": ("Compare with the previous run's fingerprints and KRIs" if previous
                            else "First run: establish the monitoring baseline"),
    }

# Monitoring history (source_snapshot, kri_snapshot) is deliberately not reset.
_RESET = [
    "monitor_alert", "contradiction", "remediation", "gap", "mapping", "control_test",
    "evidence", "control", "control_doc", "obligation", "reg_change", "reg_section",
    "regulation", "needs_review", "agent_run",
]


def _files(directory: Path, suffixes=(".txt", ".pdf")) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix.lower() in suffixes)


def discover_sources() -> list[tuple[str, Path]]:
    """Every input document, as (kind, path). kind is regulation | prior | control."""
    out = []
    for path in _files(REG_DIR) + _files(UPLOAD_DIRS["regulation"]):
        out.append(("prior" if path.name.startswith("prior-") else "regulation", path))
    for path in _files(CTRL_DIR) + _files(UPLOAD_DIRS["control"]):
        out.append(("control", path))
    return out


def _reset(conn):
    with conn.cursor() as cur:
        for table in _RESET:
            cur.execute(f"DELETE FROM {table}")
    conn.commit()


def run_pipeline(emit=None):
    """Execute the full chain. `emit(**event)` receives progress events."""
    emit = emit or (lambda **_: None)
    run_id = str(uuid.uuid4())

    conn = get_connection()
    try:
        previous = previous_snapshots(conn)
        sources = discover_sources()
        reasons = _plan_reasons(conn, sources, previous)
        emit(event="plan", run_id=run_id, mode=llm.mode(),
             steps=[{"agent": a, "goal": g, "reason": reasons.get(a, "")} for a, g in PLAN])
        _reset(conn)

        regulations = [p for kind, p in sources if kind == "regulation"]
        if not regulations:
            raise RuntimeError("No regulation sources found in data/regulations")

        IngestionAgent(run_id, emit).run(conn, sources=regulations)

        with conn.cursor() as cur:
            cur.execute("SELECT id, title, source_url FROM regulation ORDER BY ingested_at")
            regs = cur.fetchall()

        totals = {"stored": 0, "rejected": 0, "recovered": 0}
        for reg_id, _t, _s in regs:
            result = ObligationAgent(run_id, emit).run(conn, regulation_id=reg_id)
            for key in totals:
                totals[key] += result["summary"].get(key, 0)
        emit(agent="ObligationAgent", stage="obligation_extraction",
             event="summary", **totals)

        ApplicabilityAgent(run_id, emit).run(conn)

        for _kind, path in (s for s in sources if s[0] == "control"):
            ControlAgent(run_id, emit).run(conn, control_path=path)
        TestingAgent(run_id, emit).run(conn)

        MappingAgent(run_id, emit).run(conn)
        GapAgent(run_id, emit).run(conn)
        ContradictionAgent(run_id, emit).run(conn)

        # Prefer the version seen on the last run when the source has since changed:
        # that is the change a compliance officer needs to know about today.
        priors = {p.name.replace("prior-", ""): p for kind, p in sources if kind == "prior"}
        for reg_id, _title, src in regs:
            if not src:
                continue
            path = Path(src)
            prev = previous.get(source_key(path))
            with conn.cursor() as cur:
                cur.execute("SELECT full_text FROM regulation WHERE id = %s", (reg_id,))
                current = cur.fetchone()[0]
            if prev and prev.get("content") and prev["content"] != current:
                ChangeAgent(run_id, emit).run(conn, regulation_id=reg_id,
                                              prior_text=prev["content"],
                                              prior_label="previous run")
            elif path.name in priors:
                ChangeAgent(run_id, emit).run(conn, regulation_id=reg_id,
                                              prior_path=priors[path.name])

        def rerun_mapping():
            MappingAgent(run_id, emit).run(conn)
            GapAgent(run_id, emit).run(conn)

        AssuranceAgent(run_id, emit).run(conn, rerun={
            "testing": lambda: TestingAgent(run_id, emit).run(conn),
            "mapping": rerun_mapping,
            "gaps": lambda: GapAgent(run_id, emit).run(conn),
        })
        tracked = WorkflowAgent(run_id, emit).run(conn)

        stats = collect_stats(conn)
        MonitoringAgent(run_id, emit).run(conn, sources=sources, previous=previous,
                                          stats=stats, workflow_summary=tracked["summary"])

        stats = collect_stats(conn)
        emit(event="complete", run_id=run_id, **stats)
        return {"run_id": run_id, **stats}
    finally:
        conn.close()


def collect_stats(conn) -> dict:
    counts = {}
    with conn.cursor() as cur:
        for table in ("regulation", "reg_section", "obligation", "control",
                      "mapping", "gap", "remediation", "contradiction",
                      "evidence", "reg_change", "needs_review", "control_test",
                      "monitor_alert"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM obligation WHERE verified = 1")
        counts["verified_obligations"] = cur.fetchone()[0]

        cur.execute("SELECT applicability, COUNT(*) FROM obligation GROUP BY applicability")
        counts["applicability"] = {row[0]: row[1] for row in cur.fetchall()}
        counts["applicable"] = counts["applicability"].get("applicable", 0)

        cur.execute("SELECT coverage, COUNT(*) FROM mapping GROUP BY coverage")
        counts["coverage"] = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT severity, COUNT(*) FROM gap GROUP BY severity")
        counts["severity"] = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT COUNT(*) FROM control WHERE effectiveness < 0.6")
        counts["failing_controls"] = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM control WHERE reported_effectiveness >= 0.9 "
            "AND effectiveness < 0.9"
        )
        counts["contradicted_controls"] = cur.fetchone()[0]

        cur.execute("SELECT severity, COUNT(*) FROM monitor_alert GROUP BY severity")
        counts["alerts"] = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT MAX(created_at) FROM agent_run")
        counts["last_run"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM review_decision")
        decisions = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM obligation WHERE applicability_reviewed = 1")
        applied = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mapping WHERE reviewed = 1")
        applied += cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM remediation WHERE generated_by = 'claude'")
        counts["ai_remediations"] = cur.fetchone()[0]
        counts["reviews"] = {"decisions": decisions, "applied": applied}

    total_map = sum(counts["coverage"].values()) or 1
    counts["coverage_pct"] = round(
        100 * counts["coverage"].get("full", 0) / total_map, 1
    )
    counts["verification_pct"] = round(
        100 * counts["verified_obligations"] / (counts["obligation"] or 1), 1
    )
    # Report what actually produced the results, not what is configured: a key that
    # fails at the API falls back to the rule engine and must be labelled as such.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM obligation WHERE extracted_by = 'claude'")
        by_claude = cur.fetchone()[0]
    counts["ai_obligations"] = by_claude
    counts["mode"] = "claude" if by_claude else ("rule_engine" if counts["obligation"] else llm.mode())
    counts["llm_configured"] = llm.available()
    profile = bank_profile.load()
    counts["bank"] = profile.get("name")
    counts["assessment_date"] = profile.get("assessment_date")
    return counts


class EventStream:
    """Thread-backed pipeline run that yields events for Server-Sent Events."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.done = threading.Event()

    def emit(self, **event):
        self.q.put(event)

    def start(self):
        def worker():
            try:
                run_pipeline(self.emit)
            except Exception as exc:
                self.q.put({"event": "error", "error": str(exc)})
            finally:
                self.done.set()
                self.q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        return self

    def events(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            yield item
