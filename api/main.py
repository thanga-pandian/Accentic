"""
FastAPI backend for the Accentic compliance command centre.

Traceability endpoints (Sprint 1) plus the agentic chain: obligations, controls,
mappings, gaps, remediations, contradictions, change intelligence, simulation,
and a live Server-Sent Events stream of the pipeline running.
"""

import json
import pathlib
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import llm
from agents.orchestrator import EventStream, collect_stats
from db.connection import get_connection

app = FastAPI(title="Accentic — Agentic Regulatory Compliance")

CONTEXT_CHARS = 300
WEB_DIR = pathlib.Path(__file__).parent.parent / "web"


def rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def query(sql: str, params=()):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return rows(cur)


# ---------------------------------------------------------------------------
# Pipeline control
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status():
    with get_connection() as conn:
        try:
            return {"ready": True, "mode": llm.mode(), **collect_stats(conn)}
        except Exception:
            return {"ready": False, "mode": llm.mode()}


@app.get("/api/run")
def run_pipeline_stream():
    """Stream pipeline events as they happen."""
    stream = EventStream().start()

    def generate():
        for event in stream.events():
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Traceability (Sprint 1 foundation)
# ---------------------------------------------------------------------------

@app.get("/api/regulations")
def list_regulations():
    return query(
        """
        SELECT id, regulator, jurisdiction, title, version, source_url, ingested_at,
               LENGTH(full_text) AS doc_chars
        FROM regulation ORDER BY ingested_at
        """
    )


@app.get("/api/sections")
def list_sections(regulation_id: Optional[str] = None):
    if regulation_id:
        return query(
            """
            SELECT id, clause_path, page_no, char_start, char_end,
                   SUBSTR(raw_text, 1, 120) AS preview
            FROM reg_section WHERE regulation_id = %s ORDER BY char_start
            """,
            (regulation_id,),
        )
    return query(
        """
        SELECT id, clause_path, page_no, char_start, char_end,
               SUBSTR(raw_text, 1, 120) AS preview
        FROM reg_section ORDER BY char_start
        """
    )


@app.get("/api/sections/{section_id}")
def get_section(section_id: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rs.id, rs.clause_path, rs.page_no, rs.char_start, rs.char_end,
                       rs.raw_text, r.full_text, r.title
                FROM reg_section rs JOIN regulation r ON r.id = rs.regulation_id
                WHERE rs.id = %s
                """,
                (section_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Section not found")
            cols = [d[0] for d in cur.description]

    data = dict(zip(cols, row))
    full_text = data.pop("full_text")
    cs, ce = data["char_start"], data["char_end"]
    data["context_before"] = full_text[max(0, cs - CONTEXT_CHARS):cs]
    data["context_after"] = full_text[ce:min(len(full_text), ce + CONTEXT_CHARS)]
    return data


# ---------------------------------------------------------------------------
# The compliance chain
# ---------------------------------------------------------------------------

@app.get("/api/obligations")
def list_obligations(risk: Optional[str] = None, category: Optional[str] = None):
    sql = """
        SELECT o.id, o.clause_path, o.obligation_text, o.actor, o.modality, o.action,
               o.condition, o.deadline, o.risk_tier, o.category, o.char_start,
               o.char_end, o.verified, o.confidence, o.extracted_by, o.page_no,
               r.title AS regulation_title,
               m.coverage, m.coverage_score, c.control_ref, c.test_result,
               g.id AS gap_id, g.severity, g.risk_score
        FROM obligation o
        JOIN regulation r ON r.id = o.regulation_id
        LEFT JOIN mapping m ON m.obligation_id = o.id
        LEFT JOIN control c ON c.id = m.control_id
        LEFT JOIN gap g ON g.obligation_id = o.id
    """
    where, params = [], []
    if risk:
        where.append("o.risk_tier = %s")
        params.append(risk)
    if category:
        where.append("o.category = %s")
        params.append(category)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE o.risk_tier WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, o.char_start"
    return query(sql, tuple(params))


@app.get("/api/obligations/{obligation_id}/proof")
def obligation_proof(obligation_id: str):
    """Re-slice the source and prove the stored text is authentic."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.clause_path, o.obligation_text, o.char_start, o.char_end,
                       o.page_no, o.extracted_by, o.confidence, o.risk_tier,
                       r.full_text, r.title, r.regulator
                FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                WHERE o.id = %s
                """,
                (obligation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Obligation not found")
            cols = [d[0] for d in cur.description]

    data = dict(zip(cols, row))
    full_text = data.pop("full_text")
    cs, ce = data["char_start"], data["char_end"]

    resliced = full_text[cs:ce]
    data["resliced"] = resliced
    data["verified"] = resliced == data["obligation_text"]
    data["context_before"] = full_text[max(0, cs - CONTEXT_CHARS):cs]
    data["context_after"] = full_text[ce:min(len(full_text), ce + CONTEXT_CHARS)]
    data["doc_length"] = len(full_text)
    return data


@app.get("/api/controls")
def list_controls():
    return query(
        """
        SELECT c.id, c.control_ref, c.title, c.description, c.owner, c.frequency,
               c.control_type, c.automated, c.last_tested, c.test_result,
               c.effectiveness, c.char_start, c.char_end,
               (SELECT COUNT(*) FROM mapping m WHERE m.control_id = c.id) AS mapped_obligations,
               (SELECT reference FROM evidence e WHERE e.control_id = c.id LIMIT 1) AS evidence_ref,
               (SELECT status FROM evidence e WHERE e.control_id = c.id LIMIT 1) AS evidence_status
        FROM control c ORDER BY c.control_ref
        """
    )


@app.get("/api/gaps")
def list_gaps():
    return query(
        """
        SELECT g.id, g.gap_type, g.severity, g.risk_score, g.likelihood, g.impact,
               g.rationale, g.status,
               o.id AS obligation_id, o.clause_path, o.risk_tier, o.category,
               SUBSTR(o.obligation_text, 1, 260) AS excerpt,
               r.title AS regulation_title,
               c.control_ref, c.test_result,
               rm.action, rm.owner_role, rm.effort, rm.priority, rm.due_days,
               rm.rationale AS remediation_rationale
        FROM gap g
        JOIN obligation o ON o.id = g.obligation_id
        JOIN regulation r ON r.id = o.regulation_id
        LEFT JOIN mapping m ON m.obligation_id = o.id
        LEFT JOIN control c ON c.id = m.control_id
        LEFT JOIN remediation rm ON rm.gap_id = g.id
        ORDER BY g.risk_score DESC
        """
    )


@app.get("/api/contradictions")
def list_contradictions():
    return query(
        """
        SELECT ct.id, ct.conflict_type, ct.explanation, ct.severity, ct.confidence,
               a.clause_path AS clause_a, SUBSTR(a.obligation_text,1,240) AS text_a,
               a.id AS obligation_a_id, ra.title AS regulation_a,
               b.clause_path AS clause_b, SUBSTR(b.obligation_text,1,240) AS text_b,
               b.id AS obligation_b_id, rb.title AS regulation_b
        FROM contradiction ct
        JOIN obligation a ON a.id = ct.obligation_a
        JOIN obligation b ON b.id = ct.obligation_b
        JOIN regulation ra ON ra.id = a.regulation_id
        JOIN regulation rb ON rb.id = b.regulation_id
        ORDER BY CASE ct.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END
        """
    )


@app.get("/api/changes")
def list_changes():
    return query(
        """
        SELECT id, change_type, clause_path, summary, before_text, after_text,
               impact_level, affected_count
        FROM reg_change
        ORDER BY CASE impact_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END
        """
    )


@app.get("/api/agent-runs")
def list_agent_runs():
    return query(
        """
        SELECT id, run_id, agent_name, stage, status, input_summary, output_summary,
               reasoning, tool_calls, attempts, recovered, duration_ms, model, created_at
        FROM agent_run ORDER BY created_at
        """
    )


@app.get("/api/needs-review")
def list_needs_review():
    return query(
        "SELECT id, stage, payload, failure_reason, created_at FROM needs_review "
        "ORDER BY created_at DESC"
    )


@app.get("/api/trace/{obligation_id}")
def trace(obligation_id: str):
    """Full provenance chain for one obligation."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.clause_path, o.obligation_text, o.risk_tier, o.category,
                       o.char_start, o.char_end, o.page_no, o.extracted_by,
                       r.title AS regulation_title, r.regulator,
                       s.id AS section_id, s.clause_path AS section_clause
                FROM obligation o
                JOIN regulation r ON r.id = o.regulation_id
                JOIN reg_section s ON s.id = o.section_id
                WHERE o.id = %s
                """,
                (obligation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Obligation not found")
            obligation = dict(zip([d[0] for d in cur.description], row))

            cur.execute(
                """
                SELECT m.coverage, m.coverage_score, m.rationale, m.confidence,
                       c.id AS control_id, c.control_ref, c.title, c.owner,
                       c.test_result, c.effectiveness
                FROM mapping m LEFT JOIN control c ON c.id = m.control_id
                WHERE m.obligation_id = %s
                """,
                (obligation_id,),
            )
            mapping = rows(cur)

            cur.execute(
                """
                SELECT g.id, g.gap_type, g.severity, g.risk_score, g.rationale,
                       rm.action, rm.owner_role, rm.effort, rm.due_days
                FROM gap g LEFT JOIN remediation rm ON rm.gap_id = g.id
                WHERE g.obligation_id = %s
                """,
                (obligation_id,),
            )
            gaps = rows(cur)

            evidence = []
            if mapping and mapping[0].get("control_id"):
                cur.execute(
                    """
                    SELECT evidence_type, reference, captured_on, status, note
                    FROM evidence WHERE control_id = %s
                    """,
                    (mapping[0]["control_id"],),
                )
                evidence = rows(cur)

    return {
        "obligation": obligation,
        "mapping": mapping,
        "evidence": evidence,
        "gaps": gaps,
    }


class Scenario(BaseModel):
    scenario: str


@app.post("/api/simulate")
def simulate(body: Scenario):
    from agents.impact import SimulationAgent
    with get_connection() as conn:
        agent = SimulationAgent(run_id="simulation")
        result = agent.execute(conn, scenario=body.scenario)
        conn.commit()
    return result


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
