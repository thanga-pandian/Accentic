"""
Feature 5: Regulatory-to-control mapping.
Feature 6: Control effectiveness assessment.

Scores every obligation against every control, then grades coverage by combining
match strength with how well the control actually tested.
"""

import math
import re
import uuid
from collections import Counter

from agents.base import Agent

_STOP = set("""
the a an and or of to in for by with on at as is are be been shall must should may
not this that these those which who whom whose it its from any all each such other
under above where when if case upon into per via than then there their them they
""".split())

_DOMAIN_TERMS = {
    "pep": 3.0, "politically": 3.0, "exposed": 2.0, "beneficial": 3.0, "owner": 1.5,
    "suspicious": 3.0, "str": 2.5, "ctr": 2.5, "cash": 2.0, "record": 2.0,
    "retain": 2.0, "retention": 2.5, "screening": 2.5, "sanction": 2.5,
    "risk": 1.8, "categor": 2.5, "diligence": 2.5, "identif": 2.0, "verif": 2.0,
    "ovd": 3.0, "document": 1.5, "report": 2.0, "fiu": 3.0, "threshold": 2.5,
    "approval": 2.0, "senior": 2.0, "management": 1.5, "principal": 2.5,
    "officer": 1.8, "wealth": 3.0, "funds": 2.0, "source": 2.0, "monitor": 2.0,
    "review": 1.8, "periodic": 2.2, "forged": 3.0, "counterfeit": 3.0,
    "incorporation": 2.5, "resolution": 2.0, "transaction": 1.5, "customer": 1.2,
}


def _tokens(text: str) -> Counter:
    words = re.findall(r"[a-z]{3,}", text.lower())
    return Counter(w for w in words if w not in _STOP)


def _weight(token: str) -> float:
    for stem, w in _DOMAIN_TERMS.items():
        if token.startswith(stem):
            return w
    return 1.0


def _similarity(a: Counter, b: Counter) -> float:
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    num = sum(_weight(t) * math.sqrt(a[t] * b[t]) for t in shared)
    norm_a = math.sqrt(sum(_weight(t) * a[t] ** 2 for t in a))
    norm_b = math.sqrt(sum(_weight(t) * b[t] ** 2 for t in b))
    return num / (norm_a * norm_b) if norm_a and norm_b else 0.0


class MappingAgent(Agent):
    name = "MappingAgent"
    stage = "regulation_to_control_mapping"

    # Calibrated against the observed similarity distribution for this corpus:
    # domain-weighted scores top out near 0.26, so a strong match sits above 0.20.
    MATCH_FLOOR = 0.15
    STRONG_MATCH = 0.20

    def execute(self, conn, **_) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, obligation_text, action, category, risk_tier FROM obligation"
            )
            obligations = cur.fetchall()
            cur.execute(
                """
                SELECT id, control_ref, title, description, effectiveness, test_result
                FROM control
                """
            )
            controls = cur.fetchall()

        control_vectors = [
            (cid, ref, title, _tokens(f"{title} {desc}"), eff, result)
            for cid, ref, title, desc, eff, result in controls
        ]

        mapped = unmapped = partial = 0

        for ob_id, ob_text, action, category, risk in obligations:
            ob_vec = _tokens(f"{ob_text} {action or ''} {category or ''}")

            scored = sorted(
                (
                    (_similarity(ob_vec, cvec), cid, ref, eff, result)
                    for cid, ref, _t, cvec, eff, result in control_vectors
                ),
                reverse=True,
                key=lambda x: x[0],
            )
            best_score, best_id, best_ref, best_eff, best_result = scored[0]

            if best_score < self.MATCH_FLOOR:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO mapping
                            (id, obligation_id, control_id, coverage, coverage_score,
                             rationale, method, confidence)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(uuid.uuid4()), ob_id, None, "none", 0.0,
                            "No control in the framework addresses this obligation.",
                            "weighted_lexical", round(1 - best_score, 2),
                        ),
                    )
                unmapped += 1
                continue

            # A control only gives full coverage if it both matches strongly and
            # passed its last effectiveness test. Anything else is partial.
            effective_coverage = best_score * (0.4 + 0.6 * best_eff)
            if best_score >= self.STRONG_MATCH and best_eff >= 0.9:
                coverage = "full"
                mapped += 1
                rationale = (
                    f"{best_ref} directly addresses this obligation "
                    f"(similarity {best_score:.2f}) and its latest test returned "
                    f"'{best_result}'."
                )
            else:
                coverage = "partial"
                partial += 1
                reason = (
                    f"its latest test returned '{best_result}'"
                    if best_eff < 0.9
                    else f"the match is indirect (similarity {best_score:.2f})"
                )
                rationale = (
                    f"{best_ref} is the closest control but coverage is incomplete "
                    f"because {reason}."
                )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mapping
                        (id, obligation_id, control_id, coverage, coverage_score,
                         rationale, method, confidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid.uuid4()), ob_id, best_id, coverage,
                        round(effective_coverage, 3), rationale,
                        "weighted_lexical", round(min(0.99, best_score + 0.3), 2),
                    ),
                )

        conn.commit()
        self.log(
            conn, "ok",
            input_summary=f"{len(obligations)} obligations x {len(controls)} controls",
            output_summary=f"{mapped} fully covered, {partial} partial, {unmapped} uncovered",
            reasoning=(
                "Scored each obligation against every control using domain-weighted "
                "term similarity, then discounted coverage by the control's own test "
                "result so an untested or failing control cannot claim full coverage."
            ),
            tool_calls=["load_obligations", "load_controls", "score_similarity",
                        "apply_effectiveness_discount"],
            model="rule_engine",
        )
        return {"summary": {"mapped": mapped, "partial": partial, "uncovered": unmapped}}
