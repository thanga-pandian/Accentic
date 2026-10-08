"""
Trainability: reviewer decisions that outlive a run.

Obligation ids are regenerated on every run, so a decision is keyed on what the
obligation *is*: its regulator, clause and normalised text. A decision does two
things on the next run:

  override  the agent's output for that obligation is replaced, and labelled
  teach     a mapping correction adds the obligation's wording to the chosen
            control's vocabulary, so similar obligations map correctly unaided
"""

import hashlib
import uuid

NO_CONTROL = "__none__"


def obligation_key(regulator: str, clause_ref: str, text: str) -> str:
    norm = " ".join((text or "").lower().split())
    raw = f"{regulator or ''}|{clause_ref or ''}|{norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def decisions(conn, target: str) -> dict:
    """Latest decision per obligation for one target ('applicability' | 'mapping')."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT obligation_key, decided_value, note, reviewer, decided_at,
                   obligation_text, system_value
            FROM review_decision WHERE target = %s ORDER BY decided_at
            """,
            (target,),
        )
        out = {}
        for key, value, note, reviewer, at, text, system in cur.fetchall():
            out[key] = {"value": value, "note": note, "reviewer": reviewer,
                        "decided_at": str(at), "text": text, "system": system}
        return out


def learned_examples(conn) -> dict:
    """{control_ref: [obligation texts reviewers mapped to it]}."""
    out = {}
    for d in decisions(conn, "mapping").values():
        if d["value"] != NO_CONTROL:
            out.setdefault(d["value"], []).append(d["text"])
    return out


def record(conn, *, target, regulator, clause_ref, text, system_value, decided_value,
           note, reviewer) -> str:
    key = obligation_key(regulator, clause_ref, text)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM review_decision WHERE obligation_key = %s AND target = %s",
                    (key, target))
        decision_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO review_decision
                (id, obligation_key, target, regulator, clause_ref, obligation_text,
                 system_value, decided_value, note, reviewer)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (decision_id, key, target, regulator, clause_ref, text, system_value,
             decided_value, note, reviewer),
        )
    conn.commit()
    return decision_id
