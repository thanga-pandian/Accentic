"""
FastAPI backend for the Accentic compliance workbench.

Traceability endpoints plus the full agentic chain: obligations, applicability,
controls, control testing, mappings, gaps, remediations, contradictions, change
intelligence, simulation, ongoing monitoring, document intake and the bank
profile, and a live Server-Sent Events stream of the pipeline running.
"""

import hmac
import json
import os
import pathlib
import re
from datetime import date
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import feedback, llm, workflow
from agents import profile as bank_profile
from agents.monitoring import drift, fingerprint, previous_snapshots, source_key
from agents.orchestrator import (UPLOAD_DIR, UPLOAD_DIRS, EventStream, collect_stats,
                                 discover_sources)
from db.connection import get_connection

app = FastAPI(title="Accentic — Agentic Regulatory Compliance")

CONTEXT_CHARS = 300
WEB_DIR = pathlib.Path(__file__).parent.parent / "web"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Optional: set ACCENTIC_API_TOKEN to require a token for every state-changing call
# (uploads, profile edits, reviewer decisions, pipeline runs). Reads stay open.
API_TOKEN = os.environ.get("ACCENTIC_API_TOKEN", "").strip()


@app.middleware("http")
async def require_token(request: Request, call_next):
    path = request.url.path
    mutating = request.method in ("POST", "PUT", "DELETE") or path == "/api/run"
    if API_TOKEN and path.startswith("/api/") and mutating:
        supplied = request.headers.get("x-accentic-token") or request.query_params.get("token", "")
        if not hmac.compare_digest(supplied.encode(), API_TOKEN.encode()):
            return JSONResponse({"detail": "A valid access token is required for this action."},
                                status_code=401)
    return await call_next(request)


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
            return {"ready": True, "mode": llm.mode(), "auth_required": bool(API_TOKEN),
                    **collect_stats(conn)}
        except Exception:
            return {"ready": False, "mode": llm.mode(), "auth_required": bool(API_TOKEN)}


@app.get("/api/run")
def run_pipeline_stream():
    """Stream pipeline events as they happen."""
    stream = EventStream().start()

    def generate():
        for event in stream.events():
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Inputs: documents and the bank profile
# ---------------------------------------------------------------------------

@app.get("/api/documents")
def list_documents():
    with get_connection() as conn:
        last = previous_snapshots(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.source_url, r.title, r.regulator,
                       (SELECT COUNT(*) FROM reg_section s WHERE s.regulation_id = r.id),
                       (SELECT COUNT(*) FROM obligation o WHERE o.regulation_id = r.id)
                FROM regulation r
                """
            )
            regs = {source_key(pathlib.Path(s)): (t, g, n, o) for s, t, g, n, o in cur.fetchall()}
            cur.execute(
                """
                SELECT d.source_url, d.title,
                       (SELECT COUNT(*) FROM control c WHERE c.control_doc_id = d.id)
                FROM control_doc d
                """
            )
            ctrls = {source_key(pathlib.Path(s)): (t, n) for s, t, n in cur.fetchall()}

    out = []
    for kind, path in discover_sources():
        key = source_key(path)
        sha = fingerprint(path)
        prev = last.get(key)
        doc = {
            "path": key, "name": path.name, "kind": kind,
            "format": path.suffix.lstrip(".").upper(), "bytes": path.stat().st_size,
            "sha256": sha[:12],
            "uploaded": UPLOAD_DIR in path.resolve().parents,
            "state": "new" if prev is None else
                     ("changed" if prev["sha256"] != sha else "current"),
        }
        if key in regs:
            doc.update(title=regs[key][0], regulator=regs[key][1],
                       sections=regs[key][2], obligations=regs[key][3])
        if key in ctrls:
            doc.update(title=ctrls[key][0], controls=ctrls[key][1])
        out.append(doc)
    return out


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@app.post("/api/documents")
async def upload_document(request: Request, kind: str, filename: str):
    """Raw-body upload, so no multipart dependency. kind: regulation | control."""
    if kind not in UPLOAD_DIRS:
        raise HTTPException(400, "kind must be 'regulation' or 'control'")
    name = _SAFE_NAME.sub("_", pathlib.PurePath(filename).name).lstrip(".")
    suffix = pathlib.PurePath(name).suffix.lower()
    if suffix not in (".pdf", ".txt"):
        raise HTTPException(400, "Only .pdf and .txt documents are accepted")
    if name.startswith("prior-"):
        name = "doc-" + name

    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty file")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds 20 MB")
    if suffix == ".pdf" and not body.startswith(b"%PDF"):
        raise HTTPException(400, "File is not a valid PDF")
    if suffix == ".txt":
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "Text files must be UTF-8")

    target_dir = UPLOAD_DIRS[kind]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    target.write_bytes(body)
    return {"saved": source_key(target), "kind": kind, "bytes": len(body)}


@app.delete("/api/documents")
def delete_document(path: str):
    """Only uploaded documents can be removed; the bundled corpus is read-only."""
    target = (UPLOAD_DIR.parent.parent / path).resolve()
    if UPLOAD_DIR.resolve() not in target.parents or not target.is_file():
        raise HTTPException(403, "Only uploaded documents can be removed")
    target.unlink()
    return {"removed": path}


@app.get("/api/profile")
def get_profile():
    return bank_profile.load()


class ProfileUpdate(BaseModel):
    assessment_date: Optional[str] = None
    activities: Optional[dict[str, bool]] = None
    risk_appetite: Optional[dict[str, float]] = None


@app.put("/api/profile")
def update_profile(body: ProfileUpdate):
    profile = bank_profile.load()
    if body.assessment_date is not None:
        try:
            date.fromisoformat(body.assessment_date)
        except ValueError:
            raise HTTPException(400, "assessment_date must be YYYY-MM-DD")
        profile["assessment_date"] = body.assessment_date
    for key, enabled in (body.activities or {}).items():
        if key not in profile.get("activities", {}):
            raise HTTPException(400, f"Unknown activity '{key}'")
        profile["activities"][key]["enabled"] = bool(enabled)
    for key, value in (body.risk_appetite or {}).items():
        if key not in profile.get("risk_appetite", {}):
            raise HTTPException(400, f"Unknown risk appetite setting '{key}'")
        profile["risk_appetite"][key] = value
    bank_profile.save(profile)
    return profile


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------

@app.get("/api/regulations")
def list_regulations():
    return query(
        """
        SELECT r.id, r.regulator, r.jurisdiction, r.title, r.version, r.source_url,
               r.ingested_at, LENGTH(r.full_text) AS doc_chars,
               (SELECT COUNT(*) FROM reg_section s WHERE s.regulation_id = r.id) AS sections,
               (SELECT COUNT(*) FROM obligation o WHERE o.regulation_id = r.id) AS obligations
        FROM regulation r ORDER BY r.ingested_at
        """
    )


@app.get("/api/sections")
def list_sections(regulation_id: Optional[str] = None):
    sql = """
        SELECT id, regulation_id, clause_path, page_no, char_start, char_end,
               SUBSTR(raw_text, 1, 160) AS preview
        FROM reg_section
    """
    if regulation_id:
        return query(sql + " WHERE regulation_id = %s ORDER BY char_start", (regulation_id,))
    return query(sql + " ORDER BY char_start")


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
    data["verified"] = full_text[cs:ce] == data["raw_text"]
    return data


# ---------------------------------------------------------------------------
# The compliance chain
# ---------------------------------------------------------------------------

@app.get("/api/obligations")
def list_obligations():
    return query(
        """
        SELECT o.id, o.clause_path, COALESCE(o.clause_ref, o.clause_path) AS clause_ref,
               o.obligation_text, o.actor, o.modality, o.action, o.condition, o.deadline,
               o.risk_tier, o.category, o.char_start, o.char_end, o.verified,
               o.confidence, o.extracted_by, o.page_no, o.applicability,
               o.applicability_reason, o.applies_to, o.applicability_reviewed,
               r.title AS regulation_title, r.regulator,
               m.coverage, m.coverage_score, m.scope_gap, m.rationale AS mapping_rationale,
               m.reviewed AS mapping_reviewed,
               c.control_ref, c.test_result, c.independent_result,
               g.id AS gap_id, g.severity, g.risk_score
        FROM obligation o
        JOIN regulation r ON r.id = o.regulation_id
        LEFT JOIN mapping m ON m.obligation_id = o.id
        LEFT JOIN control c ON c.id = m.control_id
        LEFT JOIN gap g ON g.obligation_id = o.id
        ORDER BY r.ingested_at, o.char_start
        """
    )


@app.get("/api/obligations/{obligation_id}/proof")
def obligation_proof(obligation_id: str):
    """Re-slice the source and prove the stored text is authentic."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, COALESCE(o.clause_ref, o.clause_path) AS clause_ref,
                       o.obligation_text, o.char_start, o.char_end,
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
               c.effectiveness, c.reported_effectiveness, c.independent_result,
               c.char_start, c.char_end, d.title AS framework, d.version AS framework_ref,
               (SELECT COUNT(*) FROM mapping m WHERE m.control_id = c.id) AS mapped_obligations,
               (SELECT reference FROM evidence e WHERE e.control_id = c.id LIMIT 1) AS evidence_ref,
               (SELECT status FROM evidence e WHERE e.control_id = c.id LIMIT 1) AS evidence_status
        FROM control c JOIN control_doc d ON d.id = c.control_doc_id
        ORDER BY c.control_ref
        """
    )


@app.get("/api/control-tests")
def list_control_tests():
    return query(
        """
        SELECT t.id, t.control_id, c.control_ref, t.test_type, t.procedure, t.result,
               t.metric, t.threshold, t.conclusion
        FROM control_test t JOIN control c ON c.id = t.control_id
        ORDER BY c.control_ref,
                 CASE t.test_type WHEN 'design' THEN 0 WHEN 'operating' THEN 1 ELSE 2 END
        """
    )


def _tasks_by_key() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.obligation_key, t.status, t.assignee, t.first_detected, t.closed_at,
                       t.resolution, t.updated_at,
                       (SELECT COUNT(*) FROM task_comment c WHERE c.obligation_key = t.obligation_key)
                FROM remediation_task t
                """
            )
            return {k: {"status": s, "assignee": a, "first_detected": f, "closed_at": c,
                        "resolution": r, "updated_at": str(u), "comments": n}
                    for k, s, a, f, c, r, u, n in cur.fetchall()}


def _attach_task(item: dict, tasks: dict, regulator, ref, text):
    t = tasks.get(feedback.obligation_key(regulator, ref, text), {})
    item["task_status"] = t.get("status", "open")
    item["assignee"] = t.get("assignee")
    item["first_detected"] = t.get("first_detected")
    item["comments"] = t.get("comments", 0)
    item["due_date"] = workflow.due_date(t.get("first_detected") or workflow._today(),
                                         item.get("due_days"))
    item["overdue"] = bool(item["due_date"] and item["due_date"] < workflow._today()
                           and item["task_status"] != "done")


@app.get("/api/gaps")
def list_gaps():
    tasks = _tasks_by_key()
    out = query(
        """
        SELECT g.id, g.gap_type, g.severity, g.risk_score, g.likelihood, g.impact,
               g.rationale, g.status,
               o.id AS obligation_id, COALESCE(o.clause_ref, o.clause_path) AS clause_ref,
               o.risk_tier, o.category, o.applies_to,
               SUBSTR(o.obligation_text, 1, 280) AS excerpt,
               o.obligation_text AS full_text_for_key,
               r.title AS regulation_title, r.regulator,
               c.control_ref, c.test_result, c.independent_result, m.scope_gap,
               rm.action, rm.owner_role, rm.effort, rm.priority, rm.due_days,
               rm.rationale AS remediation_rationale, rm.generated_by
        FROM gap g
        JOIN obligation o ON o.id = g.obligation_id
        JOIN regulation r ON r.id = o.regulation_id
        LEFT JOIN mapping m ON m.obligation_id = o.id
        LEFT JOIN control c ON c.id = m.control_id
        LEFT JOIN remediation rm ON rm.gap_id = g.id
        ORDER BY g.risk_score DESC
        """
    )
    for gap in out:
        _attach_task(gap, tasks, gap["regulator"], gap["clause_ref"], gap.pop("full_text_for_key"))
    return out


@app.get("/api/contradictions")
def list_contradictions():
    return query(
        """
        SELECT ct.id, ct.conflict_type, ct.explanation, ct.severity, ct.confidence,
               COALESCE(a.clause_ref, a.clause_path) AS clause_a,
               SUBSTR(a.obligation_text,1,260) AS text_a,
               a.id AS obligation_a_id, ra.title AS regulation_a, ra.regulator AS regulator_a,
               COALESCE(b.clause_ref, b.clause_path) AS clause_b,
               SUBSTR(b.obligation_text,1,260) AS text_b,
               b.id AS obligation_b_id, rb.title AS regulation_b, rb.regulator AS regulator_b
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
        SELECT ch.id, ch.change_type, ch.clause_path, ch.summary, ch.before_text,
               ch.after_text, ch.impact_level, ch.affected_count, ch.prior_version,
               r.title AS regulation_title, r.regulator
        FROM reg_change ch JOIN regulation r ON r.id = ch.regulation_id
        ORDER BY CASE ch.impact_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 ch.clause_path
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


@app.get("/api/monitoring")
def monitoring():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alert_type, severity, title, detail, due_date, ref_type, ref_id
                FROM monitor_alert
                ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                         WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END
                """
            )
            alerts = rows(cur)
            cur.execute(
                """
                SELECT run_id, obligations, applicable, coverage_pct, gaps, critical, high,
                       verification_pct, failing_controls, alerts, captured_at
                FROM kri_snapshot ORDER BY captured_at DESC LIMIT 20
                """
            )
            history = list(reversed(rows(cur)))
        pending = drift(conn, discover_sources())
    return {"alerts": alerts, "kri_history": history, "drift": pending}


@app.get("/api/assurance")
def assurance():
    """The chain invariants, evaluated now against the stored results. Read-only."""
    from agents.assurance import run_checks
    with get_connection() as conn:
        return run_checks(conn)


@app.get("/api/monitoring/drift")
def monitoring_drift():
    """Cheap poll: has any source changed since the last analysis?"""
    with get_connection() as conn:
        return {"drift": drift(conn, discover_sources())}


@app.get("/api/trace/{obligation_id}")
def trace(obligation_id: str):
    """Full provenance chain for one obligation, regulation to monitoring."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, COALESCE(o.clause_ref, o.clause_path) AS clause_ref,
                       o.obligation_text, o.risk_tier, o.category, o.actor, o.modality,
                       o.deadline, o.char_start, o.char_end, o.page_no, o.extracted_by,
                       o.confidence, o.applicability, o.applicability_reason, o.applies_to,
                       o.applicability_reviewed,
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
                SELECT m.coverage, m.coverage_score, m.rationale, m.confidence, m.scope_gap,
                       m.reviewed,
                       c.id AS control_id, c.control_ref, c.title, c.owner, c.frequency,
                       c.test_result, c.independent_result, c.effectiveness,
                       c.reported_effectiveness, c.last_tested
                FROM mapping m LEFT JOIN control c ON c.id = m.control_id
                WHERE m.obligation_id = %s
                """,
                (obligation_id,),
            )
            mapping = rows(cur)

            cur.execute(
                """
                SELECT g.id, g.gap_type, g.severity, g.risk_score, g.likelihood, g.impact,
                       g.rationale, rm.action, rm.owner_role, rm.effort, rm.due_days,
                       rm.priority, rm.rationale AS remediation_rationale, rm.generated_by
                FROM gap g LEFT JOIN remediation rm ON rm.gap_id = g.id
                WHERE g.obligation_id = %s
                """,
                (obligation_id,),
            )
            gaps = rows(cur)

            evidence, tests, alerts = [], [], []
            control_id = mapping[0].get("control_id") if mapping else None
            if control_id:
                cur.execute(
                    "SELECT evidence_type, reference, captured_on, status, note "
                    "FROM evidence WHERE control_id = %s", (control_id,))
                evidence = rows(cur)
                cur.execute(
                    "SELECT test_type, procedure, result, metric, threshold, conclusion "
                    "FROM control_test WHERE control_id = %s", (control_id,))
                tests = rows(cur)
            cur.execute(
                "SELECT severity, title, detail, due_date FROM monitor_alert "
                "WHERE ref_id = %s OR ref_id = %s",
                (obligation_id, control_id or ""),
            )
            alerts = rows(cur)

    key = feedback.obligation_key(obligation["regulator"], obligation["clause_ref"],
                                  obligation["obligation_text"])
    tasks = _tasks_by_key()
    for gap in gaps:
        _attach_task(gap, tasks, obligation["regulator"], obligation["clause_ref"],
                     obligation["obligation_text"])
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT author, body, system, created_at FROM task_comment "
                        "WHERE obligation_key = %s ORDER BY created_at", (key,))
            comments = rows(cur)

    return {
        "obligation": obligation,
        "mapping": mapping,
        "evidence": evidence,
        "tests": tests,
        "gaps": gaps,
        "alerts": alerts,
        "task": tasks.get(key),
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Remediation workflow
# ---------------------------------------------------------------------------

class TaskUpdate(BaseModel):
    obligation_id: str
    status: Optional[Literal["open", "in_progress", "blocked", "done"]] = None
    assignee: Optional[str] = None
    author: str


class TaskComment(BaseModel):
    obligation_id: str
    author: str
    body: str


def _obligation_key(conn, obligation_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.obligation_text, COALESCE(o.clause_ref, o.clause_path), r.regulator "
            "FROM obligation o JOIN regulation r ON r.id = o.regulation_id WHERE o.id = %s",
            (obligation_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Obligation not found")
    return feedback.obligation_key(row[2], row[1], row[0])


def _author(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(400, "Your name is required (max 80 characters)")
    return name


@app.put("/api/tasks")
def update_task(body: TaskUpdate):
    author = _author(body.author)
    with get_connection() as conn:
        key = _obligation_key(conn, body.obligation_id)
        with conn.cursor() as cur:
            cur.execute("SELECT status, assignee FROM remediation_task WHERE obligation_key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "No remediation task for this obligation")
            status, assignee = row
            notes = []
            if body.status and body.status != status:
                notes.append(f"Status {status.replace('_', ' ')} → {body.status.replace('_', ' ')}"
                             + (". Closure is confirmed only when a fresh analysis no longer "
                                "detects the gap." if body.status == "done" else "."))
                status = body.status
            if body.assignee is not None and body.assignee.strip() != (assignee or ""):
                new = body.assignee.strip()[:80] or None
                notes.append(f"Assigned to {new}." if new else "Unassigned.")
                assignee = new
            if not notes:
                return {"changed": False}
            cur.execute(
                "UPDATE remediation_task SET status = %s, assignee = %s, updated_at = %s, "
                "closed_at = %s WHERE obligation_key = %s",
                (status, assignee, workflow.now_ts(),
                 workflow._today() if status == "done" else None, key))
            workflow.comment(cur, key, author, " ".join(notes))
    return {"changed": True, "status": status, "assignee": assignee}


@app.post("/api/tasks/comment")
def add_comment(body: TaskComment):
    author, text = _author(body.author), (body.body or "").strip()
    if not 1 <= len(text) <= 2000:
        raise HTTPException(400, "Comment must be 1–2000 characters")
    with get_connection() as conn:
        key = _obligation_key(conn, body.obligation_id)
        with conn.cursor() as cur:
            workflow.comment(cur, key, author, text)
    return {"added": True}


# ---------------------------------------------------------------------------
# Ask Accentic and the board report
# ---------------------------------------------------------------------------

class Question(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: Question):
    from agents.qa import QAAgent
    question = (body.question or "").strip()
    if not 3 <= len(question) <= 1000:
        raise HTTPException(400, "Ask a question of 3–1000 characters")
    with get_connection() as conn:
        return QAAgent(run_id="on-demand").execute(conn, question=question)


@app.get("/api/report")
def report(format: str = "html"):
    from agents.report import build
    with get_connection() as conn:
        html = build(conn)
    if format == "doc":
        name = f"accentic-compliance-report-{workflow._today()}.doc"
        return HTMLResponse(html, media_type="application/msword",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Trainability: reviewer decisions
# ---------------------------------------------------------------------------

_APPLICABILITY = {"applicable", "informational", "not_applicable"}


class ReviewIn(BaseModel):
    obligation_id: str
    target: Literal["applicability", "mapping"]
    value: str
    note: str = ""
    reviewer: str


@app.get("/api/reviews")
def list_reviews():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, obligation_key, target, regulator, clause_ref, obligation_text,
                       system_value, decided_value, note, reviewer, decided_at
                FROM review_decision ORDER BY decided_at DESC
                """
            )
            decisions = rows(cur)
            cur.execute(
                "SELECT o.obligation_text, COALESCE(o.clause_ref, o.clause_path), r.regulator, o.id "
                "FROM obligation o JOIN regulation r ON r.id = o.regulation_id")
            live = {feedback.obligation_key(g, c, t): oid for t, c, g, oid in cur.fetchall()}
    for d in decisions:
        d["obligation_id"] = live.get(d["obligation_key"])
        d["corrected"] = d["system_value"] != d["decided_value"]
    return decisions


@app.post("/api/reviews")
def create_review(body: ReviewIn):
    reviewer, note = body.reviewer.strip(), body.note.strip()
    if not 1 <= len(reviewer) <= 80:
        raise HTTPException(400, "Reviewer name is required (max 80 characters)")
    if len(note) > 500:
        raise HTTPException(400, "Note must be 500 characters or fewer")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.obligation_text, COALESCE(o.clause_ref, o.clause_path), r.regulator,
                       o.applicability, c.control_ref, m.reviewed, o.applicability_reviewed
                FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                LEFT JOIN mapping m ON m.obligation_id = o.id
                LEFT JOIN control c ON c.id = m.control_id
                WHERE o.id = %s
                """,
                (body.obligation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Obligation not found")
            text, ref, regulator, applicability, control_ref, map_rev, app_rev = row
            if body.target == "applicability":
                if body.value not in _APPLICABILITY:
                    raise HTTPException(400, "Unknown applicability value")
                current = applicability
            else:
                cur.execute("SELECT control_ref FROM control")
                if body.value != feedback.NO_CONTROL and body.value not in {r[0] for r in cur.fetchall()}:
                    raise HTTPException(400, "Unknown control")
                current = control_ref or feedback.NO_CONTROL

        # When the current value is itself a reviewer's, keep the system's original.
        prior = feedback.decisions(conn, body.target).get(
            feedback.obligation_key(regulator, ref, text))
        reviewed_now = app_rev if body.target == "applicability" else map_rev
        system_value = prior["system"] if (prior and reviewed_now) else current
        decision_id = feedback.record(
            conn, target=body.target, regulator=regulator, clause_ref=ref, text=text,
            system_value=system_value, decided_value=body.value, note=note, reviewer=reviewer)
    return {"id": decision_id, "applies": "on the next analysis run"}


@app.delete("/api/reviews/{decision_id}")
def delete_review(decision_id: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM review_decision WHERE id = %s", (decision_id,))
    return {"removed": decision_id}


class Claim(BaseModel):
    quote: str


@app.post("/api/verify-claim")
def verify_claim(body: Claim):
    """
    Adversarial check: hand the system any sentence and see whether it can be
    proved from the source corpus. Uses the same anchor() the pipeline uses, so
    this is the real gate, not a demonstration of one.
    """
    from agents.base import VerificationError, anchor, clause_ref

    quote = (body.quote or "").strip()
    if not quote:
        raise HTTPException(400, "empty claim")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, regulator, full_text FROM regulation")
            regs = cur.fetchall()

        for reg_id, title, regulator, full_text in regs:
            try:
                start, end = anchor(quote, full_text, 0)
            except VerificationError:
                continue

            sliced = full_text[start:end]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT page_no FROM reg_section
                    WHERE regulation_id = %s AND char_start <= %s AND char_end >= %s
                    ORDER BY char_start DESC LIMIT 1
                    """,
                    (reg_id, start, end),
                )
                row = cur.fetchone()

            return {
                "verified": True,
                "regulation": title,
                "regulator": regulator,
                "clause_path": clause_ref(full_text, start),
                "page_no": row[0] if row else None,
                "char_start": start,
                "char_end": end,
                "resliced": sliced,
                "exact": sliced == quote,
                "context_before": full_text[max(0, start - 220):start],
                "context_after": full_text[end:end + 220],
            }

    return {
        "verified": False,
        "reason": "This text does not appear in any ingested regulation.",
        "action": "Rejected at the verification gate. Inside the pipeline an "
                  "unanchorable claim is quarantined to needs_review for a human and "
                  "is never stored as a finding.",
        "searched": len(regs),
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
