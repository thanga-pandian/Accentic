"""
Feature 8: Gap identification.
Feature 9: Risk-based gap prioritization.
Feature 10: Remediation recommendation engine.

Risk score is impact x likelihood, where impact comes from the obligation's risk
tier and likelihood comes from how weak the mapped control actually is.
"""

import json
import uuid
from pathlib import Path

from agents import llm
from agents.base import Agent

_IMPACT = {"high": 0.95, "medium": 0.6, "low": 0.3}

_GAP_TYPES = {
    "none": ("no_control", 1.0),
    "partial": ("weak_control", 0.6),
    "full": ("effective", 0.0),
}

_PLAYBOOK = {
    "reporting": (
        "Instrument the reporting pipeline with an SLA timer that escalates any case "
        "approaching the regulatory filing deadline, and backfill the overdue queue.",
        "Head of Financial Crime", "M",
    ),
    "edd": (
        "Make senior-management approval a hard system gate: block transacting on the "
        "relationship until the approval record exists, and remediate the open backlog.",
        "Chief Compliance Officer", "M",
    ),
    "recordkeeping": (
        "Extend the archival schema to capture the missing record class and run a "
        "retrieval test to prove reconstruction works end to end.",
        "Head of Operations", "L",
    ),
    "cdd": (
        "Add the missing verification step to the onboarding workflow and make it a "
        "blocking checkpoint before account activation.",
        "Head of Onboarding Operations", "M",
    ),
    "risk_rating": (
        "Add the attribute to the risk scoring engine inputs and re-score the existing "
        "customer book to surface any reclassification.",
        "Financial Crime Unit", "M",
    ),
    "screening": (
        "Extend screening coverage to this population and add a daily completeness "
        "reconciliation between the customer master and the screening feed.",
        "Financial Crime Unit", "S",
    ),
    "governance": (
        "Formalise the designation and build a recurring attestation so the record "
        "cannot silently go stale.",
        "Company Secretary", "S",
    ),
}

_DEFAULT_PLAY = (
    "Design and implement a control that discharges this obligation, assign a named "
    "owner, and schedule an effectiveness test within the next cycle.",
    "Chief Compliance Officer", "M",
)


def _severity(score: float) -> str:
    if score >= 0.7:
        return "critical"
    if score >= 0.45:
        return "high"
    if score >= 0.22:
        return "medium"
    return "low"


class GapAgent(Agent):
    name = "GapAgent"
    stage = "gap_identification"

    def execute(self, conn, **_) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, COALESCE(o.clause_ref, o.clause_path), o.risk_tier,
                       o.category, o.deadline,
                       m.coverage, m.coverage_score, c.control_ref,
                       COALESCE(c.independent_result, c.test_result), c.effectiveness,
                       m.scope_gap, o.obligation_text
                FROM obligation o
                JOIN mapping m ON m.obligation_id = o.id
                LEFT JOIN control c ON c.id = m.control_id
                """
            )
            rows = cur.fetchall()

        created = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        total = 0
        gaps = []

        for (ob_id, clause, risk_tier, category, deadline, coverage, coverage_score,
             control_ref, test_result, effectiveness, scope_gap, ob_text) in rows:

            gap_type, base_likelihood = _GAP_TYPES.get(coverage, ("weak_control", 0.5))
            if gap_type == "effective":
                continue
            if scope_gap:
                gap_type = "scope_gap"

            impact = _IMPACT.get(risk_tier, 0.6)
            if deadline:
                impact = min(1.0, impact + 0.08)

            likelihood = base_likelihood
            if effectiveness is not None and coverage == "partial":
                likelihood = max(0.2, 1.0 - float(effectiveness))
            if scope_gap:
                # Part of the obligation has no control at all, however well the
                # mapped control performs on what it does cover.
                likelihood = max(likelihood, 0.75)

            risk_score = round(impact * likelihood, 3)
            severity = _severity(risk_score)

            if gap_type == "no_control":
                rationale = (
                    f"Clause {clause} imposes a {risk_tier}-risk obligation with no "
                    f"corresponding control in the framework."
                )
            elif gap_type == "scope_gap":
                rationale = (
                    f"Clause {clause} is mapped to {control_ref}, but that control does "
                    f"not extend to {scope_gap}. That part of the obligation is "
                    f"uncontrolled, leaving residual exposure of {likelihood:.0%}."
                )
            else:
                rationale = (
                    f"Clause {clause} is mapped to {control_ref}, but independent "
                    f"testing of that control concluded '{test_result}', leaving "
                    f"residual exposure of {likelihood:.0%}."
                )

            gap_id = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gap
                        (id, obligation_id, gap_type, severity, risk_score,
                         likelihood, impact, rationale, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (gap_id, ob_id, gap_type, severity, risk_score,
                     round(likelihood, 3), round(impact, 3), rationale, "open"),
                )
            gaps.append({"gap_id": gap_id, "clause": clause, "category": category,
                         "severity": severity, "control": control_ref,
                         "test_result": test_result, "rationale": rationale,
                         "obligation": " ".join((ob_text or "").split())[:400],
                         "impact": impact, "likelihood": likelihood,
                         "risk_score": risk_score})
            created[severity] += 1
            total += 1

        drafted = draft_remediations(gaps)
        with conn.cursor() as cur:
            for g in gaps:
                d = drafted.get(g["gap_id"])
                if d:
                    action, owner, effort, source = d["action"], d["owner_role"], d["effort"], "claude"
                else:
                    action, owner, effort = _PLAYBOOK.get(g["category"], _DEFAULT_PLAY)
                    source = "rule_engine"
                sev = g["severity"]
                cur.execute(
                    """
                    INSERT INTO remediation
                        (id, gap_id, action, owner_role, effort, priority, due_days,
                         rationale, generated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid.uuid4()), g["gap_id"], action, owner, effort,
                        {"critical": 1, "high": 2, "medium": 3, "low": 4}[sev],
                        {"critical": 30, "high": 60, "medium": 90, "low": 180}[sev],
                        f"Prioritised {sev} because impact {g['impact']:.2f} x "
                        f"likelihood {g['likelihood']:.2f} = {g['risk_score']:.2f}.",
                        source,
                    ),
                )

        conn.commit()
        self.log(
            conn, "ok",
            input_summary=f"{len(rows)} mapped obligations",
            output_summary=(
                f"{total} gaps "
                f"({created['critical']} critical, {created['high']} high, "
                f"{created['medium']} medium, {created['low']} low)"
            ),
            reasoning=(
                "Converted uncovered and weakly-covered obligations into scored gaps "
                "using impact from the obligation risk tier and likelihood from the "
                "mapped control's measured effectiveness, then drafted a prioritised "
                "remediation for each"
                + (f": {len(drafted)} tailored by Claude and validated, the rest from "
                   f"the playbook." if drafted else " from the control-design playbook.")
            ),
            tool_calls=["join_obligations_controls", "score_risk", "rank_severity",
                        "draft_remediation", "validate_draft"],
            model=llm.MODEL if drafted else "rule_engine",
        )
        return {"summary": {"gaps": total, **created, "ai_drafted": len(drafted)}}


_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "remediation_drafting.md"
_EFFORTS = {"S", "M", "L"}


def draft_remediations(gaps: list[dict]) -> dict:
    """
    Ask Claude for a remediation tailored to each gap, in one call. Every draft is
    validated before use: it must name a known gap, an owner, a valid effort size,
    and an action of sensible length. Anything else falls back to the playbook.
    Severity, priority and due date are never taken from the model.
    """
    if not gaps or not llm.available():
        return {}
    payload = [{k: g[k] for k in ("gap_id", "clause", "category", "severity", "control",
                                  "test_result", "rationale", "obligation")} for g in gaps]
    result = llm.complete_json(_PROMPT_FILE.read_text(encoding="utf-8"),
                               json.dumps(payload, indent=1), max_tokens=16000)
    if not isinstance(result, list):
        return {}
    known = {g["gap_id"] for g in gaps}
    out = {}
    for item in result:
        if not isinstance(item, dict):
            continue
        gid, action = item.get("gap_id"), str(item.get("action") or "").strip()
        owner, effort = str(item.get("owner_role") or "").strip(), item.get("effort")
        if gid in known and 40 <= len(action) <= 600 and 3 <= len(owner) <= 80 \
                and effort in _EFFORTS:
            out[gid] = {"action": action, "owner_role": owner, "effort": effort}
    return out
