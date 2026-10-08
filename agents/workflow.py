"""
Remediation workflow, with the loop closed by evidence rather than by a checkbox.

Every gap has a task that outlives the run (keyed on the obligation, like
reviewer decisions). Each run the agent reconciles tasks with what it found:

  new gap                       → task opened, SLA clock starts now
  gap still present, task done  → re-opened: the fix did not hold
  gap no longer present         → resolved automatically, with the run as evidence

A human can move a task to in progress or blocked, but only the absence of the
gap on a fresh run can close it for good.
"""

import uuid
from datetime import date, datetime, timezone

from agents import feedback
from agents.base import Agent

STATUSES = ("open", "in_progress", "blocked", "done")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def comment(cur, key: str, author: str, body: str, system: bool = False):
    cur.execute(
        "INSERT INTO task_comment (id, obligation_key, author, body, system) "
        "VALUES (%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), key, author, body, 1 if system else 0),
    )


def due_date(first_detected: str, due_days) -> str | None:
    if not first_detected or due_days is None:
        return None
    return date.fromordinal(date.fromisoformat(first_detected[:10]).toordinal()
                            + int(due_days)).isoformat()


class WorkflowAgent(Agent):
    name = "WorkflowAgent"
    stage = "remediation_tracking"

    def execute(self, conn, **_) -> dict:
        today = _today()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.obligation_text, COALESCE(o.clause_ref, o.clause_path), r.regulator,
                       g.severity
                FROM gap g JOIN obligation o ON o.id = g.obligation_id
                JOIN regulation r ON r.id = o.regulation_id
                """
            )
            gaps = {feedback.obligation_key(reg, ref, text): (reg, ref, text, sev)
                    for text, ref, reg, sev in cur.fetchall()}
            cur.execute("SELECT obligation_key, status FROM remediation_task")
            tasks = dict(cur.fetchall())

        opened = reopened = resolved = 0
        with conn.cursor() as cur:
            for key, (reg, ref, text, sev) in gaps.items():
                title = " ".join(text.split())[:160]
                if key not in tasks:
                    cur.execute(
                        """
                        INSERT INTO remediation_task
                            (id, obligation_key, regulator, clause_ref, title, status,
                             first_detected, last_detected)
                        VALUES (%s,%s,%s,%s,%s,'open',%s,%s)
                        """,
                        (str(uuid.uuid4()), key, reg, ref, title, today, today),
                    )
                    comment(cur, key, "Accentic", f"Task opened: {sev} gap first detected.", True)
                    opened += 1
                    continue
                if tasks[key] == "done":
                    cur.execute(
                        "UPDATE remediation_task SET status = 'open', closed_at = NULL, "
                        "resolution = NULL, last_detected = %s, updated_at = %s "
                        "WHERE obligation_key = %s",
                        (today, now_ts(), key),
                    )
                    comment(cur, key, "Accentic",
                            "Re-opened: the task was marked done, but this run still detects "
                            "the gap. The remediation has not yet taken effect.", True)
                    reopened += 1
                else:
                    cur.execute(
                        "UPDATE remediation_task SET last_detected = %s WHERE obligation_key = %s",
                        (today, key),
                    )

            for key, status in tasks.items():
                if key in gaps or status == "done":
                    continue
                cur.execute(
                    "UPDATE remediation_task SET status = 'done', closed_at = %s, "
                    "resolution = %s, updated_at = %s WHERE obligation_key = %s",
                    (today, "Verified closed: the gap is no longer detected.", now_ts(), key),
                )
                comment(cur, key, "Accentic",
                        "Resolved automatically: a fresh analysis no longer detects this gap.", True)
                resolved += 1
        conn.commit()

        self.log(
            conn, "ok",
            input_summary=f"{len(gaps)} gaps, {len(tasks)} existing tasks",
            output_summary=f"{opened} opened, {reopened} re-opened, {resolved} verified closed",
            reasoning=(
                "Reconciled the remediation tracker with this run's findings: opened a task "
                "for every new gap, re-opened tasks marked done whose gap is still detected, "
                "and closed tasks whose gap a fresh analysis no longer finds."
            ),
            tool_calls=["load_gaps", "load_tasks", "open_new_tasks", "reopen_failed_fixes",
                        "verify_closures"],
            model="deterministic",
        )
        return {"summary": {"opened": opened, "reopened": reopened, "resolved": resolved}}