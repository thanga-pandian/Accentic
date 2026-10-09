"""
Chain link: remediation -> ongoing monitoring.

A compliance assessment is stale the day after it runs. This agent gives the
system a memory: it fingerprints every source document and stores KRIs per run
(both survive the per-run reset), so each run can say what changed since the
last one. It then raises the alerts a compliance officer would otherwise have
to chase by hand: changed regulations, lapsed or failing control tests,
remediation milestones, and risk-appetite breaches.
"""

import hashlib
import uuid
from datetime import date, timedelta
from pathlib import Path

from agents import feedback, sanctions, workflow
from agents import profile as bank_profile
from agents.base import Agent

ROOT = Path(__file__).resolve().parent.parent
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_REMOVED = "removed"  # tombstone fingerprint: a removed source is reported once, not forever


def _human(d: date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


def source_key(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def previous_snapshots(conn) -> dict:
    """Latest snapshot per source path, as {path: {sha256, content, captured_at}}."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.path, s.sha256, s.content, s.captured_at
            FROM source_snapshot s
            WHERE s.captured_at = (SELECT MAX(captured_at) FROM source_snapshot t
                                   WHERE t.path = s.path)
            """
        )
        return {p: {"sha256": h, "content": c, "captured_at": t}
                for p, h, c, t in cur.fetchall() if h != _REMOVED}


def drift(conn, sources) -> list[dict]:
    """Which source files differ from what the last run saw. Read-only."""
    previous = previous_snapshots(conn)
    if not previous:
        return []
    current = {source_key(p): (kind, p) for kind, p in sources}
    out = []
    for key, (kind, path) in current.items():
        prev = previous.get(key)
        if prev is None:
            out.append({"path": key, "kind": kind, "change": "added"})
        elif prev["sha256"] != fingerprint(path):
            out.append({"path": key, "kind": kind, "change": "modified"})
    for key in previous:
        if key not in current:
            out.append({"path": key, "kind": "unknown", "change": "removed"})
    return out


class MonitoringAgent(Agent):
    name = "MonitoringAgent"
    stage = "ongoing_monitoring"

    def execute(self, conn, sources, previous: dict, stats: dict,
                workflow_summary: dict | None = None, **_) -> dict:
        profile = bank_profile.load()
        as_of = bank_profile.as_of(profile)
        appetite = profile.get("risk_appetite", {})
        cycles = profile.get("test_cycle_days", {})
        alerts = []

        def alert(kind, severity, title, detail="", due=None, ref_type=None, ref_id=None):
            alerts.append((kind, severity, title, detail,
                           due.isoformat() if isinstance(due, date) else due,
                           ref_type, ref_id))

        # ---- 1. Source watch ------------------------------------------------
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, source_url, full_text FROM regulation")
            regs = {source_key(Path(src)): (rid, title, text)
                    for rid, title, src, text in cur.fetchall() if src}
            cur.execute("SELECT title, source_url, full_text FROM control_doc")
            ctrl_docs = {source_key(Path(src)): (title, text)
                         for title, src, text in cur.fetchall() if src}

        current_keys = set()
        for kind, path in sources:
            key = source_key(path)
            current_keys.add(key)
            sha = fingerprint(path)
            if kind == "regulation" and key in regs:
                title, content = regs[key][1], regs[key][2]
            elif kind == "control" and key in ctrl_docs:
                title, content = ctrl_docs[key]
            else:
                title, content = path.name, None
            prev = previous.get(key)

            if prev is None and previous:
                alert("source_added", "medium", f"New document in corpus: {title}",
                      f"{path.name} was not present on the previous run and has been "
                      f"ingested.", ref_type=kind)
            elif prev is not None and prev["sha256"] != sha:
                changed = 0
                if kind == "regulation" and key in regs:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) FROM reg_change WHERE regulation_id = %s "
                            "AND prior_version = 'previous run'", (regs[key][0],))
                        changed = cur.fetchone()[0]
                alert("source_changed", "high", f"Source changed since last run: {title}",
                      f"Content fingerprint changed since {prev['captured_at']}. "
                      + (f"{changed} clause-level changes re-assessed automatically."
                         if changed else "Re-assessed in full on this run."),
                      ref_type=kind)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO source_snapshot (id, run_id, kind, path, sha256, content)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (str(uuid.uuid4()), self.run_id, kind, key, sha, content),
                )

        for key in set(previous) - current_keys:
            alert("source_removed", "medium", f"Document removed from corpus: {key}",
                  "Obligations sourced from it are no longer assessed.")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO source_snapshot (id, run_id, kind, path, sha256) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), self.run_id, "removed", key, _REMOVED),
                )

        if not previous:
            alert("baseline", "info", "Monitoring baseline established",
                  f"Fingerprinted {len(sources)} source documents. Later runs report "
                  f"changes against this baseline.")

        # ---- 2. Control assurance ------------------------------------------
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, control_ref, title, last_tested, automated, test_result,
                       independent_result, reported_effectiveness
                FROM control
                """
            )
            controls = cur.fetchall()
        for cid, ref, title, last, automated, reported, independent, rep_eff in controls:
            if independent in ("Exception", "Design deficient"):
                contradicted = (rep_eff or 0) >= 0.9
                alert("control_failing", "critical" if contradicted else "high",
                      (f"{ref} reported '{reported}' but evidence shows an exception"
                       if contradicted else f"{ref} failing independent testing"),
                      f"{title}. Independent result: {independent}.",
                      ref_type="control", ref_id=cid)
            try:
                due = date.fromisoformat((last or "").strip()) + timedelta(
                    days=cycles.get("automated" if automated else "manual", 365))
            except ValueError:
                continue
            if due < as_of:
                alert("test_overdue", "medium", f"{ref} retest overdue",
                      f"{title}. Retest was due {_human(due)}.", due,
                      "control", cid)
            elif (due - as_of).days <= 60:
                alert("test_due", "low", f"{ref} retest due within 60 days",
                      f"{title}.", due, "control", cid)

        # ---- 3. Remediation milestones (real SLA clocks from first detection) --
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.severity, COALESCE(o.clause_ref, o.clause_path), r.regulator,
                       rm.owner_role, rm.due_days, o.id, o.obligation_text
                FROM gap g JOIN obligation o ON o.id = g.obligation_id
                JOIN regulation r ON r.id = o.regulation_id
                LEFT JOIN remediation rm ON rm.gap_id = g.id
                """
            )
            open_gaps = cur.fetchall()
            cur.execute("SELECT obligation_key, status, first_detected, assignee FROM remediation_task")
            tasks = {k: (s, f, a) for k, s, f, a in cur.fetchall()}
        today = date.fromisoformat(workflow._today())
        high_due, blocked = [], 0
        for sev, clause, regulator, owner, due_days, ob_id, text in open_gaps:
            status, first, assignee = tasks.get(
                feedback.obligation_key(regulator, clause, text), ("open", today.isoformat(), None))
            due = date.fromisoformat(workflow.due_date(first, due_days or 30))
            who = assignee or owner
            if status == "blocked":
                blocked += 1
            if due < today and status != "done":
                alert("remediation_overdue", "critical" if sev in ("critical", "high") else "high",
                      f"Remediation overdue since {_human(due)}: {regulator} {clause}",
                      f"{sev.capitalize()} gap. Assigned to {who}. Status: {status.replace('_', ' ')}.",
                      due, "obligation", ob_id)
            elif sev == "critical":
                alert("remediation_due", "critical",
                      f"P1 remediation due {_human(due)}: {regulator} {clause}",
                      f"Assigned to {who}. Status: {status.replace('_', ' ')}.", due,
                      "obligation", ob_id)
            elif sev == "high":
                high_due.append(due)
        if high_due:
            alert("remediation_due", "high",
                  f"{len(high_due)} P2 remediations due by {_human(max(high_due))}",
                  "High-severity gaps on a 60-day remediation clock.", max(high_due))
        if blocked:
            alert("remediation_blocked", "high", f"{blocked} remediation task(s) blocked",
                  "Blocked tasks need an escalation decision from the owner's line manager.")
        reopened = (workflow_summary or {}).get("reopened", 0)
        if reopened:
            alert("remediation_reopened", "high",
                  f"{reopened} remediation(s) marked done but the gap is still detected",
                  "The tasks were re-opened automatically. The fix has not taken effect.")

        # ---- 4. Regulatory change and conflicts ----------------------------
        with conn.cursor() as cur:
            # A moved number is what breaks a configured control, so each one is an
            # alert in its own right. Added and removed clauses roll up into one.
            cur.execute(
                "SELECT change_type, clause_path, summary, affected_count FROM reg_change"
            )
            structural = 0
            for kind, clause, summary, affected in cur.fetchall():
                if kind == "amended" and "→" in summary:
                    alert("regulatory_change", "high", summary,
                          f"{affected} obligation(s) under clause {clause} need impact "
                          f"review, and any control configured to the old value must "
                          f"be re-parameterised.")
                elif kind in ("added", "removed"):
                    structural += 1
            if structural:
                alert("regulatory_change", "medium",
                      f"{structural} clauses added or removed versus the prior version",
                      "Newly introduced clauses have been extracted, mapped and gap-scored "
                      "on this run.")
            cur.execute("SELECT COUNT(*) FROM contradiction")
            conflicts = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM needs_review")
            review = cur.fetchone()[0]
        if conflicts:
            alert("conflict", "high",
                  f"{conflicts} conflicting requirements need a precedence ruling",
                  "Apply the stricter requirement until Legal confirms precedence.")
        if review:
            alert("human_review", "medium", f"{review} item(s) awaiting human review",
                  "Results the system could not verify were quarantined, not stored.")

        # ---- 5. Official sanctions list currency (RBI para 56, 58) ----------
        listed = sanctions.status(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT o.id FROM obligation o JOIN regulation r ON r.id = o.regulation_id "
                        "WHERE r.regulator = 'RBI' AND o.clause_ref = '58' ORDER BY o.char_start")
            daily_check = (cur.fetchone() or [None])[0]
        link = ("obligation", daily_check) if daily_check else (None, None)
        if listed is None:
            alert("sanctions_list", "high", "UN sanctions list has never been synced",
                  "RBI para 58 requires the UNSCR 1718 list to be verified every day, and para 56 "
                  "requires screening against the UNSC lists. Run the sanctions sync.", None, *link)
        elif listed["stale"]:
            alert("sanctions_list", "high",
                  f"UN sanctions list last verified {listed['age_hours']:.0f} hours ago",
                  f"RBI para 58 requires daily verification. Last UN list on record was generated "
                  f"{listed['date_generated'][:10]}.", None, *link)
        if listed and (listed["added"] or listed["removed"]):
            names = [f"{c['name']} ({c['ref']})" for c in listed["changes"].get("added", [])[:5]]
            alert("sanctions_list", "high",
                  f"UN sanctions list changed: {listed['added']} designation(s) added, "
                  f"{listed['removed']} removed",
                  (f"New: {'; '.join(names)}. " if names else "")
                  + "Screen customers against the change the same day (RBI paras 56 and 58).",
                  None, *link)

        # ---- 6. Risk appetite ----------------------------------------------
        critical = stats.get("severity", {}).get("critical", 0)
        if stats.get("coverage_pct", 0) < appetite.get("min_coverage_pct", 0):
            alert("appetite_breach", "high",
                  f"Full coverage {stats['coverage_pct']}% is below appetite "
                  f"({appetite['min_coverage_pct']}%)",
                  "Share of applicable obligations fully discharged by a tested control.")
        if critical > appetite.get("max_critical_gaps", 99):
            alert("appetite_breach", "critical",
                  f"{critical} critical gaps against an appetite of "
                  f"{appetite['max_critical_gaps']}",
                  "Board-level risk appetite breached. Escalate to the Risk Committee.")
        if stats.get("verification_pct", 100) < appetite.get("min_verification_pct", 0):
            alert("appetite_breach", "critical", "Unverified obligations in the register",
                  "Every stored obligation must reproduce from its source.")

        alerts.sort(key=lambda a: _SEVERITY_ORDER.get(a[1], 9))
        with conn.cursor() as cur:
            for a in alerts:
                cur.execute(
                    """
                    INSERT INTO monitor_alert
                        (id, alert_type, severity, title, detail, due_date, ref_type, ref_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (str(uuid.uuid4()), *a),
                )
            cur.execute(
                """
                INSERT INTO kri_snapshot
                    (id, run_id, obligations, applicable, coverage_pct, gaps, critical,
                     high, verification_pct, failing_controls, alerts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (str(uuid.uuid4()), self.run_id, stats.get("obligation", 0),
                 stats.get("applicable", 0), stats.get("coverage_pct", 0),
                 stats.get("gap", 0), critical,
                 stats.get("severity", {}).get("high", 0),
                 stats.get("verification_pct", 0), stats.get("failing_controls", 0),
                 len(alerts)),
            )
        conn.commit()

        by_sev = {}
        for a in alerts:
            by_sev[a[1]] = by_sev.get(a[1], 0) + 1
        self.log(
            conn, "ok",
            input_summary=f"{len(sources)} sources, {len(previous)} prior fingerprints",
            output_summary=f"{len(alerts)} alerts ("
                           + ", ".join(f"{v} {k}" for k, v in by_sev.items()) + ")",
            reasoning=(
                "Compared every source fingerprint with the previous run, checked control "
                "test currency and independent results, scheduled remediation milestones, "
                "and tested this run's KRIs against the bank's risk appetite."
            ),
            tool_calls=["fingerprint_sources", "diff_against_last_run", "check_test_cycles",
                        "schedule_milestones", "check_sanctions_list_currency",
                        "evaluate_risk_appetite", "record_kris"],
            model="rule_engine",
        )
        return {"summary": {"alerts": len(alerts), **by_sev}}
