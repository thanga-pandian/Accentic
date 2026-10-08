"""
Self-assurance: the chain checks its own output before anyone relies on it.

Each check is an invariant the rest of the system assumes. When one fails, the
agent re-runs the stage responsible once (self-correction). If the invariant
still fails, the failure is quarantined for a human and the run is marked
degraded. Nothing is hidden and nothing is retried forever.
"""

from agents.base import Agent

# Downstream tables to clear before re-running a stage, in dependency order.
_CLEAR = {
    "testing": ["control_test"],
    "mapping": ["remediation", "gap", "mapping"],
    "gaps": ["remediation", "gap"],
}


def _scalar(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def run_checks(conn) -> list[dict]:
    """Return [{name, ok, detail, fix}] for every invariant."""
    out = []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.char_start, s.char_end, s.raw_text, r.full_text "
            "FROM reg_section s JOIN regulation r ON r.id = s.regulation_id")
        rows = cur.fetchall()
    bad = sum(1 for cs, ce, raw, full in rows if full[cs:ce] != raw)
    out.append({"name": "Every clause reproduces its source", "ok": bad == 0,
                "detail": f"{len(rows) - bad}/{len(rows)} clauses re-slice exactly", "fix": None})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.char_start, o.char_end, o.obligation_text, r.full_text "
            "FROM obligation o JOIN regulation r ON r.id = o.regulation_id")
        rows = cur.fetchall()
    bad = sum(1 for cs, ce, txt, full in rows if full[cs:ce] != txt)
    out.append({"name": "Every obligation reproduces its source", "ok": bad == 0,
                "detail": f"{len(rows) - bad}/{len(rows)} obligations re-slice exactly", "fix": None})

    controls = _scalar(conn, "SELECT COUNT(*) FROM control")
    untested = _scalar(conn, """
        SELECT COUNT(*) FROM control c
        WHERE (SELECT COUNT(DISTINCT test_type) FROM control_test t WHERE t.control_id = c.id) < 3
    """)
    out.append({"name": "Every control has design, operating and currency tests",
                "ok": untested == 0, "detail": f"{controls - untested}/{controls} controls fully tested",
                "fix": "testing"})

    applicable = _scalar(conn, "SELECT COUNT(*) FROM obligation WHERE applicability = 'applicable'")
    unmapped = _scalar(conn, """
        SELECT COUNT(*) FROM obligation o WHERE o.applicability = 'applicable'
          AND (SELECT COUNT(*) FROM mapping m WHERE m.obligation_id = o.id) <> 1
    """)
    out.append({"name": "Every applicable obligation has exactly one mapping",
                "ok": unmapped == 0, "detail": f"{applicable - unmapped}/{applicable} mapped once",
                "fix": "mapping"})

    stray = _scalar(conn, """
        SELECT COUNT(*) FROM mapping m JOIN obligation o ON o.id = m.obligation_id
        WHERE o.applicability <> 'applicable'
    """)
    out.append({"name": "Out-of-scope provisions are never mapped or gap-scored",
                "ok": stray == 0, "detail": f"{stray} stray mappings", "fix": "mapping"})

    overclaimed = _scalar(conn, """
        SELECT COUNT(*) FROM mapping m JOIN control c ON c.id = m.control_id
        WHERE m.coverage = 'full' AND (c.effectiveness < 0.9 OR m.scope_gap IS NOT NULL)
    """)
    out.append({"name": "No full-coverage claim rests on a failing or out-of-scope control",
                "ok": overclaimed == 0, "detail": f"{overclaimed} over-claimed mappings",
                "fix": "mapping"})

    open_maps = _scalar(conn, "SELECT COUNT(*) FROM mapping WHERE coverage <> 'full'")
    ungapped = _scalar(conn, """
        SELECT COUNT(*) FROM mapping m WHERE m.coverage <> 'full'
          AND NOT EXISTS (SELECT 1 FROM gap g WHERE g.obligation_id = m.obligation_id)
    """)
    out.append({"name": "Every incomplete mapping is raised as a gap", "ok": ungapped == 0,
                "detail": f"{open_maps - ungapped}/{open_maps} raised", "fix": "gaps"})

    gaps = _scalar(conn, "SELECT COUNT(*) FROM gap")
    orphan = _scalar(conn, """
        SELECT COUNT(*) FROM gap g WHERE NOT EXISTS
          (SELECT 1 FROM remediation r WHERE r.gap_id = g.id AND r.owner_role IS NOT NULL
             AND r.owner_role <> '' AND r.due_days IS NOT NULL)
    """)
    out.append({"name": "Every gap has an owned, dated remediation", "ok": orphan == 0,
                "detail": f"{gaps - orphan}/{gaps} owned and dated", "fix": "gaps"})
    return out


class AssuranceAgent(Agent):
    name = "AssuranceAgent"
    stage = "chain_assurance"

    def execute(self, conn, rerun: dict | None = None, **_) -> dict:
        rerun = rerun or {}
        checks = run_checks(conn)
        corrected = []
        for fix in dict.fromkeys(c["fix"] for c in checks if not c["ok"] and c["fix"]):
            if fix not in rerun:
                continue
            self.emit(event="progress", message=f"invariant failed; re-running {fix} stage")
            with conn.cursor() as cur:
                for table in _CLEAR[fix]:
                    cur.execute(f"DELETE FROM {table}")
            conn.commit()
            rerun[fix]()
            corrected.append(fix)
        if corrected:
            checks = run_checks(conn)

        failed = [c for c in checks if not c["ok"]]
        for c in failed:
            self.quarantine(conn, {"check": c["name"], "detail": c["detail"]},
                            "chain invariant failed after self-correction")
        conn.commit()

        self.log(
            conn, "degraded" if failed else "ok",
            input_summary=f"{len(checks)} chain invariants",
            output_summary=(f"{len(checks) - len(failed)}/{len(checks)} invariants hold"
                            + (f"; self-corrected: {', '.join(corrected)}" if corrected else "")),
            reasoning=(
                "Checked the invariants the rest of the chain relies on. Any failure "
                "triggers one re-run of the responsible stage; a failure that survives "
                "is quarantined for a human rather than retried indefinitely."
            ),
            tool_calls=["check_offsets", "check_test_completeness", "check_mapping_integrity",
                        "check_coverage_claims", "check_gap_remediation", "rerun_stage"],
            attempts=2 if corrected else 1,
            recovered=bool(corrected) and not failed,
            model="deterministic",
        )
        return {"summary": {"invariants": len(checks), "holding": len(checks) - len(failed),
                            "self_corrected": len(corrected)},
                "checks": checks}
