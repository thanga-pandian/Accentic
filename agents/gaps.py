"""
Feature 8: Gap identification.
Feature 9: Risk-based gap prioritization.
Feature 10: Remediation recommendation engine.

Risk score is impact x likelihood, where impact comes from the obligation's risk
tier and likelihood comes from how weak the mapped control actually is.
"""

import uuid

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
                SELECT o.id, o.clause_path, o.risk_tier, o.category, o.deadline,
                       m.coverage, m.coverage_score, c.control_ref, c.test_result,
                       c.effectiveness
                FROM obligation o
                JOIN mapping m ON m.obligation_id = o.id
                LEFT JOIN control c ON c.id = m.control_id
                """
            )
            rows = cur.fetchall()

        created = Counter_ = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        total = 0

        for (ob_id, clause, risk_tier, category, deadline, coverage,
             coverage_score, control_ref, test_result, effectiveness) in rows:

            gap_type, base_likelihood = _GAP_TYPES.get(coverage, ("weak_control", 0.5))
            if gap_type == "effective":
                continue

            impact = _IMPACT.get(risk_tier, 0.6)
            if deadline:
                impact = min(1.0, impact + 0.08)

            likelihood = base_likelihood
            if effectiveness is not None and coverage == "partial":
                likelihood = max(0.2, 1.0 - float(effectiveness))

            risk_score = round(impact * likelihood, 3)
            severity = _severity(risk_score)

            if gap_type == "no_control":
                rationale = (
                    f"Clause {clause} imposes a {risk_tier}-risk obligation with no "
                    f"corresponding control in the framework."
                )
            else:
                rationale = (
                    f"Clause {clause} is mapped to {control_ref}, but that control's "
                    f"latest test result is '{test_result}', leaving residual exposure "
                    f"of {likelihood:.0%}."
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

                action, owner, effort = _PLAYBOOK.get(category, _DEFAULT_PLAY)
                priority = {"critical": 1, "high": 2, "medium": 3, "low": 4}[severity]
                due = {"critical": 30, "high": 60, "medium": 90, "low": 180}[severity]
                cur.execute(
                    """
                    INSERT INTO remediation
                        (id, gap_id, action, owner_role, effort, priority, due_days,
                         rationale, generated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid.uuid4()), gap_id, action, owner, effort, priority, due,
                        f"Prioritised {severity} because impact {impact:.2f} x "
                        f"likelihood {likelihood:.2f} = {risk_score:.2f}.",
                        "rule_engine",
                    ),
                )

            created[severity] += 1
            total += 1

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
                "mapped control's measured effectiveness, then generated a prioritised "
                "remediation for each."
            ),
            tool_calls=["join_obligations_controls", "score_risk", "rank_severity",
                        "generate_remediation"],
            model="rule_engine",
        )
        return {"summary": {"gaps": total, **created}}
