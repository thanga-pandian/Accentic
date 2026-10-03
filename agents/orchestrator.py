"""
Pipeline orchestrator.

Runs the chain the problem statement asks for:
    regulation -> obligations -> mapping -> controls -> evidence
    -> gaps -> remediation -> contradictions -> monitoring

Emits events as it goes so the UI can show the agents reasoning in real time.
"""

import queue
import threading
import uuid
from pathlib import Path

from agents import llm
from agents.contradiction import ContradictionAgent
from agents.controls import ControlAgent
from agents.gaps import GapAgent
from agents.impact import ChangeAgent
from agents.mapping import MappingAgent
from agents.obligations import ObligationAgent
from db.connection import get_connection
from extract.sectioner import run as ingest_regulation

ROOT = Path(__file__).resolve().parent.parent
REG_DIR = ROOT / "data" / "regulations"
CTRL_DIR = ROOT / "data" / "controls"

PLAN = [
    ("IngestionAgent", "Parse regulations and compute verifiable character offsets"),
    ("ObligationAgent", "Extract obligations and anchor every one to its source"),
    ("ControlAgent", "Parse the bank control framework and grade effectiveness"),
    ("MappingAgent", "Map obligations to controls, discounted by test results"),
    ("GapAgent", "Score gaps by impact x likelihood and recommend remediation"),
    ("ContradictionAgent", "Detect conflicting requirements across regulations"),
    ("ChangeAgent", "Diff against the prior version and size the impact"),
]


def _reset(conn):
    tables = [
        "contradiction", "remediation", "gap", "mapping", "evidence", "control",
        "control_doc", "obligation", "reg_change", "reg_section", "regulation",
        "needs_review", "agent_run",
    ]
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"DELETE FROM {table}")
    conn.commit()


def run_pipeline(emit=None):
    """Execute the full chain. `emit(**event)` receives progress events."""
    emit = emit or (lambda **_: None)
    run_id = str(uuid.uuid4())

    emit(event="plan", run_id=run_id, mode=llm.mode(),
         steps=[{"agent": a, "goal": g} for a, g in PLAN])

    conn = get_connection()
    try:
        _reset(conn)

        # ---- Ingestion -------------------------------------------------
        emit(agent="IngestionAgent", stage="ingestion", event="start")
        sources = sorted(REG_DIR.glob("*.txt")) + sorted(REG_DIR.glob("*.pdf"))
        sources = [s for s in sources if not s.name.startswith("prior-")]
        if not sources:
            raise RuntimeError("No regulation sources found in data/regulations")

        reg_ids = []
        for src in sources:
            ingest_regulation(src, dry_run=False, conn=conn)
            emit(agent="IngestionAgent", stage="ingestion", event="progress",
                 message=f"ingested {src.name}")

        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM regulation ORDER BY ingested_at")
            regs = cur.fetchall()
            reg_ids = [r[0] for r in regs]
            cur.execute("SELECT COUNT(*) FROM reg_section")
            section_count = cur.fetchone()[0]

        emit(agent="IngestionAgent", stage="ingestion", event="done",
             regulations=len(regs), sections=section_count)

        # ---- Obligations -----------------------------------------------
        totals = {"stored": 0, "rejected": 0, "recovered": 0}
        for reg_id in reg_ids:
            result = ObligationAgent(run_id, emit).run(conn, regulation_id=reg_id)
            for key in totals:
                totals[key] += result["summary"].get(key, 0)
        emit(agent="ObligationAgent", stage="obligation_extraction",
             event="summary", **totals)

        # ---- Controls ---------------------------------------------------
        control_files = sorted(CTRL_DIR.glob("*.txt"))
        if control_files:
            ControlAgent(run_id, emit).run(conn, control_path=control_files[0])

        # ---- Mapping, gaps, contradictions ------------------------------
        MappingAgent(run_id, emit).run(conn)
        GapAgent(run_id, emit).run(conn)
        ContradictionAgent(run_id, emit).run(conn)

        # ---- Change intelligence ----------------------------------------
        # Pair each prior-*.txt with the regulation it supersedes by filename stem.
        for prior in sorted(REG_DIR.glob("prior-*.txt")):
            target_stem = prior.stem.replace("prior-", "")
            match = next((rid for rid, title in regs if title == target_stem), None)
            if match:
                ChangeAgent(run_id, emit).run(
                    conn, regulation_id=match, prior_path=prior
                )

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
                      "evidence", "reg_change", "needs_review"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM obligation WHERE verified = 1")
        counts["verified_obligations"] = cur.fetchone()[0]

        cur.execute(
            "SELECT coverage, COUNT(*) FROM mapping GROUP BY coverage"
        )
        counts["coverage"] = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT severity, COUNT(*) FROM gap GROUP BY severity")
        counts["severity"] = {row[0]: row[1] for row in cur.fetchall()}

    total_map = sum(counts["coverage"].values()) or 1
    counts["coverage_pct"] = round(
        100 * counts["coverage"].get("full", 0) / total_map, 1
    )
    counts["verification_pct"] = round(
        100 * counts["verified_obligations"] / (counts["obligation"] or 1), 1
    )
    counts["mode"] = llm.mode()
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
