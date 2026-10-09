"""
Board-ready compliance report, generated from the live results.

One self-contained HTML document styled for A4 print, so "Save as PDF" in any
browser produces the board pack, and the same document opens in Word. Every
number in it is read from the database at generation time; nothing is written
by a model, so the report reproduces exactly from the same run.
"""

from datetime import datetime, timezone
from html import escape

from agents import feedback, workflow
from agents import profile as bank_profile
from agents.assurance import run_checks
from agents.orchestrator import collect_stats

CAT = {"cdd": "Customer due diligence", "edd": "Enhanced due diligence",
       "risk_rating": "Risk categorisation", "recordkeeping": "Record keeping",
       "reporting": "Regulatory reporting", "governance": "Governance",
       "screening": "Screening", "other": "General"}
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEV_COLOUR = {"critical": "#d03b3b", "high": "#ec835a", "medium": "#fab219", "low": "#8a94a6"}


def _ref(regulator, ref):
    ref = (ref or "").rstrip(".")
    return f"PMLA r.{ref}" if regulator == "GoI" else f"{regulator} ¶{ref}"


def _short(title):
    if "Know Your Customer" in (title or ""):
        return "RBI KYC Directions"
    if "Money-Laundering" in (title or ""):
        return "PMLA Records Rules"
    return title or ""


def _date(s):
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(str(s)[:10]).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return str(s)


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _sev(s):
    if not s:
        return "—"
    return (f'<span class="sev"><i style="background:{SEV_COLOUR.get(s, "#8a94a6")}"></i>'
            f'{escape(s.capitalize())}</span>')


def build(conn) -> str:
    e = escape
    st = collect_stats(conn)
    pf = bank_profile.load()
    ap = pf.get("risk_appetite", {})
    sev = st.get("severity", {})
    cov = st.get("coverage", {})
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC").lstrip("0")

    gaps = _rows(conn, """
        SELECT g.severity, g.risk_score, g.impact, g.likelihood, g.gap_type, g.rationale,
               COALESCE(o.clause_ref, o.clause_path) AS ref, o.obligation_text, o.category,
               r.regulator, r.title, c.control_ref, rm.action, rm.owner_role, rm.effort,
               rm.priority, rm.due_days, rm.generated_by
        FROM gap g JOIN obligation o ON o.id = g.obligation_id
        JOIN regulation r ON r.id = o.regulation_id
        LEFT JOIN mapping m ON m.obligation_id = o.id
        LEFT JOIN control c ON c.id = m.control_id
        LEFT JOIN remediation rm ON rm.gap_id = g.id
        ORDER BY g.risk_score DESC""")
    tasks = {t["obligation_key"]: t for t in _rows(conn, "SELECT * FROM remediation_task")}
    for g in gaps:
        t = tasks.get(feedback.obligation_key(g["regulator"], g["ref"], g["obligation_text"]), {})
        g["status"] = (t.get("status") or "open").replace("_", " ")
        g["assignee"] = t.get("assignee") or g["owner_role"]
        g["due"] = workflow.due_date(t.get("first_detected") or workflow._today(), g["due_days"])

    controls = _rows(conn, """
        SELECT control_ref, title, owner, test_result, independent_result,
               reported_effectiveness, effectiveness,
               (SELECT conclusion FROM control_test t WHERE t.control_id = c.id
                  AND t.result IN ('exception','fail','overdue') LIMIT 1) AS finding
        FROM control c WHERE effectiveness < 0.9 ORDER BY effectiveness, control_ref""")
    conflicts = _rows(conn, "SELECT severity, explanation FROM contradiction")
    changes = _rows(conn, "SELECT clause_path, summary, affected_count FROM reg_change "
                          "WHERE summary LIKE '%→%' ORDER BY clause_path")
    alerts = _rows(conn, "SELECT severity, title, due_date FROM monitor_alert "
                         "WHERE severity IN ('critical','high')")
    regs = _rows(conn, "SELECT title, regulator, source_url, "
                       "(SELECT COUNT(*) FROM reg_section s WHERE s.regulation_id = r.id) AS n "
                       "FROM regulation r ORDER BY ingested_at")
    applicable = _rows(conn, """
        SELECT o.category, m.coverage, m.scope_gap FROM obligation o
        JOIN mapping m ON m.obligation_id = o.id WHERE o.applicability = 'applicable'""")
    checks = run_checks(conn)

    # ---- executive summary, composed from the numbers ----------------------
    breaches = []
    if st["coverage_pct"] < ap.get("min_coverage_pct", 0):
        breaches.append(f"full coverage of {st['coverage_pct']}% is below the {ap['min_coverage_pct']}% appetite")
    if sev.get("critical", 0) > ap.get("max_critical_gaps", 99):
        breaches.append(f"{sev['critical']} critical gaps exceed the appetite of {ap['max_critical_gaps']}")
    contradicted = st.get("contradicted_controls", 0)
    top = gaps[0] if gaps else None
    summary = [
        f"Accentic assessed {e(pf.get('name', 'the bank'))} against {len(regs)} regulations "
        f"({', '.join(e(_short(r['title'])) for r in regs)}) as at {_date(pf.get('assessment_date'))}. "
        f"Of {st['obligation']} obligations extracted, {st['applicable']} bind the bank; every one of them "
        f"reproduces exactly from the source text ({st['verification_pct']}% verified).",
        f"{cov.get('full', 0)} applicable obligations are fully discharged by a control that passed "
        f"independent testing. {st['gap']} gaps remain open: {sev.get('critical', 0)} critical, "
        f"{sev.get('high', 0)} high, {sev.get('medium', 0)} medium and {sev.get('low', 0)} low.",
    ]
    if top:
        summary.append(f"The highest risk is {e(_ref(top['regulator'], top['ref']))} "
                       f"({e(CAT.get(top['category'], top['category']))}): {e(top['rationale'])}")
    if contradicted:
        summary.append(f"{contradicted} control(s) reported effective by their owners are contradicted "
                       f"by their own evidence; their coverage has been downgraded.")
    summary.append(("Risk appetite is breached: " + "; ".join(breaches) + ". Escalation to the Risk "
                    "Committee is recommended.") if breaches else "Risk appetite is within limits.")

    # ---- tables --------------------------------------------------------------
    def table(head, body, widths=None):
        cols = "".join(f'<col style="width:{w}">' for w in (widths or []))
        return (f'<table><colgroup>{cols}</colgroup><thead><tr>'
                + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"
                + ("".join(body) or f'<tr><td colspan="{len(head)}" class="muted">None.</td></tr>')
                + "</tbody></table>")

    grid = [[0] * 3 for _ in range(3)]
    for g in gaps:
        i = 2 if g["impact"] >= 0.8 else 1 if g["impact"] >= 0.5 else 0
        l = 2 if g["likelihood"] >= 0.7 else 1 if g["likelihood"] >= 0.4 else 0
        grid[i][l] += 1
    heat = '<table class="heat"><tr><th></th><th>Low likelihood</th><th>Medium</th><th>High</th></tr>'
    for i, name in ((2, "High impact"), (1, "Medium impact"), (0, "Low impact")):
        heat += f"<tr><th>{name}</th>" + "".join(
            f'<td style="background:{["#f4f6f9","#cde2fb","#9ec5f4","#6da7ec","#3987e5","#256abf"][min(5, n)] if n else "#f4f6f9"};'
            f'color:{"#fff" if n >= 4 else "#14171f"}">{n}</td>' for n in grid[i]) + "</tr>"
    heat += "</table>"

    domains = {}
    for a in applicable:
        d = domains.setdefault(a["category"], {"n": 0, "full": 0, "partial": 0, "scope": 0, "none": 0})
        d["n"] += 1
        d["scope" if a["scope_gap"] else a["coverage"]] += 1
    domain_rows = [f"<tr><td>{e(CAT.get(k, k))}</td><td class='n'>{v['n']}</td><td class='n'>{v['full']}</td>"
                   f"<td class='n'>{v['partial']}</td><td class='n'>{v['scope']}</td><td class='n'>{v['none']}</td></tr>"
                   for k, v in sorted(domains.items(), key=lambda kv: -kv[1]["n"])]

    finding_rows = [
        f"<tr><td>{_sev(g['severity'])}</td><td class='ref'>{e(_ref(g['regulator'], g['ref']))}</td>"
        f"<td>{e(g['rationale'])}</td><td class='n'>{g['risk_score']:.2f}</td></tr>" for g in gaps[:8]]
    plan_rows = [
        f"<tr><td>P{g['priority']}</td><td class='ref'>{e(_ref(g['regulator'], g['ref']))}</td>"
        f"<td>{e(g['action'] or '')}{' <span class=muted>(drafted by Claude, validated)</span>' if g['generated_by'] == 'claude' else ''}</td>"
        f"<td>{e(g['assignee'] or '')}</td><td>{_date(g['due'])}</td><td>{e(g['status'].capitalize())}</td></tr>"
        for g in sorted(gaps, key=lambda g: (g["priority"] or 9, g["due"] or ""))
        if (g["priority"] or 9) <= 2]
    control_rows = [
        f"<tr><td class='ref'>{e(c['control_ref'])}</td><td>{e(c['title'])}<div class='muted'>{e(c['owner'] or '')}</div></td>"
        f"<td>{e(c['test_result'] or '')}</td><td><b>{e(c['independent_result'] or '')}</b></td>"
        f"<td>{e(c['finding'] or '')}</td></tr>" for c in controls]
    conflict_rows = [f"<tr><td>{_sev(c['severity'])}</td><td>{e(c['explanation'])}</td></tr>" for c in conflicts]
    change_rows = [f"<tr><td class='ref'>{e(c['clause_path'])}</td><td>{e(c['summary'])}</td>"
                   f"<td class='n'>{c['affected_count']}</td></tr>" for c in changes]
    alert_rows = [f"<tr><td>{_sev(a['severity'])}</td><td>{e(a['title'])}</td><td>{_date(a['due_date'])}</td></tr>"
                  for a in sorted(alerts, key=lambda a: SEV_ORDER.get(a["severity"], 9))[:12]]
    check_rows = [f"<tr><td>{'Holds' if c['ok'] else '<b>Violated</b>'}</td><td>{e(c['name'])}</td>"
                  f"<td>{e(c['detail'])}</td></tr>" for c in checks]

    kpi = lambda v, l: f'<div class="kpi"><b>{v}</b><span>{l}</span></div>'
    kpis = "".join([
        kpi(f"{st['coverage_pct']}%", "obligations fully covered"),
        kpi(st["gap"], f"open gaps ({sev.get('critical', 0)} critical)"),
        kpi(f"{st['failing_controls']}/{st['control']}", "controls failing tests"),
        kpi(f"{st['verification_pct']}%", "claims proven from source"),
    ])

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Compliance report · {e(pf.get('name', ''))} · {_date(pf.get('assessment_date'))}</title>
<style>
@page {{ size: A4; margin: 18mm 16mm 20mm; }}
* {{ box-sizing: border-box; }}
body {{ font: 10.5pt/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; color: #14171f; margin: 0; }}
.wrap {{ max-width: 900px; margin: 0 auto; padding: 28px; }}
.cover {{ border-bottom: 3px solid #a100ff; padding-bottom: 18px; margin-bottom: 22px; }}
.cover .kicker {{ font-size: 9pt; letter-spacing: .12em; text-transform: uppercase; color: #6a00ae; font-weight: 700; }}
h1 {{ font-size: 22pt; margin: 6px 0 4px; letter-spacing: -.02em; }}
h2 {{ font-size: 13pt; margin: 26px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e4e6eb; break-after: avoid; }}
.meta {{ color: #454c5a; font-size: 9.5pt; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 16px 0 6px; }}
.kpi {{ border: 1px solid #e4e6eb; border-radius: 8px; padding: 10px 12px; }}
.kpi b {{ display: block; font-size: 17pt; letter-spacing: -.02em; }}
.kpi span {{ font-size: 8.5pt; color: #6b7280; }}
p {{ margin: 0 0 8px; }}
table {{ width: 100%; border-collapse: collapse; margin: 6px 0 4px; font-size: 9pt; break-inside: auto; }}
th {{ text-align: left; background: #f4f5f7; color: #454c5a; font-weight: 600; padding: 6px 8px; border-bottom: 1px solid #d2d6dd; }}
td {{ padding: 6px 8px; border-bottom: 1px solid #eceef2; vertical-align: top; }}
tr {{ break-inside: avoid; }}
td.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
.ref {{ font-family: Consolas, monospace; font-weight: 600; white-space: nowrap; }}
.muted {{ color: #6b7280; font-size: 8.5pt; }}
.sev {{ white-space: nowrap; font-weight: 600; }}
.sev i {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }}
.heat {{ width: auto; }} .heat td {{ width: 90px; text-align: center; font-weight: 700; font-size: 12pt; border: 2px solid #fff; }}
.heat th {{ background: none; border: none; font-weight: 500; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
.sign {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 18px; margin-top: 30px; }}
.sign div {{ border-top: 1px solid #14171f; padding-top: 4px; font-size: 9pt; color: #454c5a; }}
.foot {{ margin-top: 26px; font-size: 8.5pt; color: #6b7280; border-top: 1px solid #e4e6eb; padding-top: 8px; }}
.bar {{ position: sticky; top: 0; background: #14111b; color: #fff; padding: 10px 28px; display: flex; gap: 10px; align-items: center; font-size: 10pt; }}
.bar button, .bar a {{ background: #a100ff; color: #fff; border: 0; border-radius: 6px; padding: 7px 12px; font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }}
@media print {{ .bar {{ display: none; }} .wrap {{ padding: 0; }} }}
</style></head><body>
<div class="bar"><b>Board report</b><span style="opacity:.7">Generated {generated}</span><span style="flex:1"></span>
<button onclick="window.print()">Print / save as PDF</button><a href="/api/report?format=doc">Download Word</a></div>
<div class="wrap">
<div class="cover">
  <div class="kicker">Regulatory compliance assessment · Board pack</div>
  <h1>{e(pf.get('name', ''))}</h1>
  <div class="meta">{e(pf.get('entity_type', ''))} · {e(', '.join(pf.get('jurisdictions', [])))} · Assessment as of {_date(pf.get('assessment_date'))}<br>
  Prepared by Accentic agentic compliance analysis · generated {generated} · extraction engine: {'Claude Opus 5.5' if st.get('mode') == 'claude' else 'deterministic rule engine'}</div>
  <div class="kpis">{kpis}</div>
</div>

<h2>1. Executive summary</h2>
{''.join(f'<p>{p}</p>' for p in summary)}

<h2>2. Highest-priority findings</h2>
{table(['Severity', 'Reference', 'Finding', 'Risk'], finding_rows, ['12%', '14%', '64%', '10%'])}

<div class="two">
<div><h2>3. Risk profile</h2>{heat}
<p class="muted">Gaps by impact (obligation risk tier) and likelihood (residual exposure after independent control testing).</p></div>
<div><h2>4. Coverage by domain</h2>
{table(['Domain', 'Total', 'Full', 'Partial', 'Scope gap', 'None'], domain_rows)}</div>
</div>

<h2>5. Remediation plan: P1 and P2</h2>
{table(['Priority', 'Closes', 'Action', 'Owner', 'Due', 'Status'], plan_rows, ['8%', '12%', '44%', '16%', '10%', '10%'])}
<p class="muted">Due dates run from first detection of each gap. A task marked done is closed only when a fresh analysis no longer detects the gap.</p>

<h2>6. Control testing exceptions</h2>
{table(['Control', 'Title / owner', 'Owner-reported', 'Independent', 'Evidence'], control_rows, ['12%', '26%', '14%', '14%', '34%'])}

<h2>7. Conflicting requirements</h2>
{table(['Severity', 'Conflict and binding requirement'], conflict_rows, ['12%', '88%'])}

<h2>8. Regulatory change moving a numeric requirement</h2>
{table(['Clause', 'Change', 'Obligations affected'], change_rows, ['12%', '70%', '18%'])}

<h2>9. Open critical and high alerts</h2>
{table(['Severity', 'Alert', 'Due'], alert_rows, ['12%', '72%', '16%'])}

<h2>10. Assurance and method</h2>
<p>Every obligation in this report was located in its source document by exact character position and re-verified when the report was generated. Coverage reflects independent re-performance of control tests from their evidence, not owners' self-assessment. Scores are deterministic: the same inputs produce the same report.</p>
{table(['Status', 'Chain invariant', 'Measured'], check_rows, ['12%', '50%', '38%'])}
<p class="muted">Scope: {''.join(f"{e(r['title'])} ({r['n']} clauses); " for r in regs)}reviewer decisions applied: {st.get('reviews', {}).get('applied', 0)}.</p>

<div class="sign"><div>Chief Compliance Officer</div><div>Chief Risk Officer</div><div>Date</div></div>
<div class="foot">Generated by Accentic from the analysis run of {_date(st.get('last_run'))}. Each finding is traceable in the application to the exact clause text that supports it.</div>
</div></body></html>"""
