"""
FastAPI backend for the Accentic section viewer.

Endpoints:
    GET /api/regulations                  list all regulations
    GET /api/sections?regulation_id=...   list sections (truncated text)
    GET /api/sections/{id}                full section + surrounding context

Static files served from web/ at /.
"""

import pathlib
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from db.connection import get_connection

app = FastAPI(title="Accentic Compliance Viewer")

CONTEXT_CHARS = 300
WEB_DIR = pathlib.Path(__file__).parent.parent / "web"


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/api/regulations")
def list_regulations():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, regulator, jurisdiction, title, version, effective_from,
                       source_url, ingested_at
                FROM regulation
                ORDER BY ingested_at DESC
                """
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/api/sections")
def list_sections(regulation_id: Optional[str] = None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if regulation_id:
                cur.execute(
                    """
                    SELECT id, clause_path, page_no, char_start, char_end,
                           SUBSTR(raw_text, 1, 120) AS preview
                    FROM reg_section
                    WHERE regulation_id = %s
                    ORDER BY char_start
                    """,
                    (regulation_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, clause_path, page_no, char_start, char_end,
                           SUBSTR(raw_text, 1, 120) AS preview
                    FROM reg_section
                    ORDER BY char_start
                    """
                )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/api/sections/{section_id}")
def get_section(section_id: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rs.id, rs.clause_path, rs.page_no, rs.char_start, rs.char_end,
                       rs.raw_text, rs.created_at, r.full_text
                FROM reg_section rs
                JOIN regulation r ON r.id = rs.regulation_id
                WHERE rs.id = %s
                """,
                (section_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Section not found")
            cols = [d[0] for d in cur.description]

    data = dict(zip(cols, row))
    full_text = data.pop("full_text")

    char_start = data["char_start"]
    char_end = data["char_end"]
    ctx_start = max(0, char_start - CONTEXT_CHARS)
    ctx_end = min(len(full_text), char_end + CONTEXT_CHARS)

    data["context_before"] = full_text[ctx_start:char_start]
    data["context_after"] = full_text[char_end:ctx_end]
    return data


# ---------------------------------------------------------------------------
# Static files (web/ served at /)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
