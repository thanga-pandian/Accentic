"""
Chain link: obligations -> applicability.

Not every sentence containing "shall" binds this bank. A definition, a title
clause, or a permissive "may" imposes nothing, and a duty attached to a business
line the bank does not operate is out of scope. This agent decides, against the
bank's own profile, which obligations it must actually discharge, and why.
Only applicable obligations flow on to mapping and gap analysis.
"""

import re

from agents import feedback
from agents import profile as bank_profile
from agents.base import Agent
from extract.sectioner import NUMBERED_LINE, major

_INFORMATIONAL = [
    (r"shall be called|shall come into force|short title",
     "Title and commencement provision: names the instrument, imposes no action."),
    (r"shall have the same meaning|\"[^\"]+\"\s+means\b|“[^”]+”\s+means\b",
     "Definition: governs interpretation, imposes no action."),
    (r"shall be applicable to|these directions shall apply",
     "Scope statement: establishes who is regulated, imposes no action."),
]

_TRIGGERS = {
    "international_money_transfer": r"international money transfer|cross-border wire|wire transfer",
    "pep_relationships": r"politically exposed|\bpeps?\b",
    "non_resident_customers": r"non-resident",
    "legal_entity_accounts": r"legal entit|\bcompany\b|beneficial owner|incorporation",
    "walk_in_transactions": r"occasional transaction|not holding an account|walk-in",
    "cash_handling": r"cash transaction",
    "non_face_to_face_onboarding": r"non-face-to-face",
    "individual_accounts": r"for individuals|individual customer",
}

_UNITS = [
    ("Retail Banking", r"individual|account-based|salaried|accounts? opened|customer"),
    ("Corporate Banking", r"legal entit|company|beneficial owner|incorporation|board resolution"),
    ("Branch Network", r"cash|walk-in|occasional|not holding an account"),
    ("Financial Crime Unit", r"suspicious|politically exposed|\bpep|enhanced due diligence|"
                             r"high risk|screen|monitor|risk categor|risk classification"),
    ("Regulatory Reporting", r"\bfiu|\breport\b|reported|furnish|\bdirector\b"),
    ("Operations", r"record|retain|retention|reconstruct"),
    ("Compliance Office", r"principal officer|designated director|senior management"),
]


def _scrub(text: str) -> str:
    return re.sub(r"reporting entit(?:y|ies)", "entity", text, flags=re.I)


def _definitional_parent(full_text: str, start: int, ref: str) -> bool:
    """A lettered limb of a '"x" means ...' clause is part of a definition."""
    if "(" not in ref:
        return False
    head = full_text[max(0, start - 900):start]
    marker = list(re.finditer(NUMBERED_LINE, head))
    parent = head[marker[-1].start():] if marker else head
    return bool(re.search(r"\bmeans\b", parent))


def in_review(ref: str, regulator: str, profile: dict) -> bool:
    """
    Whether a clause falls inside this assessment's scope, written the way reviews
    cite it: {"RBI": "20, 38-46, 50-62"}. A range takes its lettered inserts with
    it ("3-5" includes 3A). A regulator with no entry is in scope in full.
    """
    spec = profile.get("assessment_scope", {}).get("paragraphs", {}).get(regulator)
    if not spec:
        return True
    head = re.match(r"\d+(?:-?[A-Z])?", ref or "")
    if not head:
        return False
    ident = head.group(0)
    for part in (p.strip() for p in spec.split(",")):
        bounds = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if bounds and int(bounds[1]) <= major(ident) <= int(bounds[2]):
            return True
        if part == ident:
            return True
    return False


def assess(text: str, modality: str, regulator: str, jurisdiction: str,
           profile: dict, definitional_limb: bool = False,
           reviewed: bool = True) -> tuple[str, str, list[str]]:
    """Return (status, reason, business_units). Pure function so it is testable."""
    if jurisdiction not in profile.get("jurisdictions", []):
        return ("not_applicable",
                f"Out of scope: {jurisdiction} regulation, bank operates in "
                f"{', '.join(profile.get('jurisdictions', []))}.", [])
    if not reviewed:
        spec = profile["assessment_scope"]["paragraphs"][regulator]
        return ("not_assessed",
                f"Not in this review, which covers {regulator} {spec}. Recorded and "
                f"traceable to the source, but not assessed against controls.", [])

    for pattern, reason in _INFORMATIONAL:
        if re.search(pattern, text, re.I):
            return "informational", reason, []
    if definitional_limb:
        return ("informational",
                "Limb of a definition: describes what qualifies, imposes no action.", [])
    if modality == "may":
        return ("informational",
                "Permissive provision: grants discretion, not a mandatory requirement.", [])

    scrubbed = _scrub(text)
    units = [u for u, pat in _UNITS if re.search(pat, scrubbed, re.I)
             and u in profile.get("business_units", {})]

    activities = profile.get("activities", {})
    hits = [k for k, pat in _TRIGGERS.items() if re.search(pat, text, re.I)]
    on = [k for k in hits if activities.get(k, {}).get("enabled", True)]
    off = [k for k in hits if k not in on]

    def labels(keys):
        # Lower only the leading letter so acronyms such as PEP survive.
        names = (activities.get(k, {}).get("label", k) for k in keys)
        return ", ".join(n[:1].lower() + n[1:] for n in names)

    name = profile.get("name", "The bank")
    if hits and not on:
        return ("not_applicable",
                f"Out of scope: {name} does not undertake {labels(off)} (bank profile).", [])
    if off:
        return ("applicable",
                f"Applies through {labels(on)}. The {labels(off)} limb is out of scope "
                f"for {name}.", units)
    if on:
        return "applicable", f"Applies: {name} undertakes {labels(on)}.", units

    addressee = "Reporting Entity under PMLA" if regulator == "GoI" else \
        f"RBI-regulated {profile.get('entity_type', 'entity')}"
    return "applicable", f"Applies to every addressee. {name} is a {addressee}.", units


class ApplicabilityAgent(Agent):
    name = "ApplicabilityAgent"
    stage = "applicability"

    def execute(self, conn, **_) -> dict:
        profile = bank_profile.load()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.obligation_text, o.modality, o.char_start, o.clause_ref,
                       r.regulator, r.jurisdiction, r.full_text
                FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                """
            )
            rows = cur.fetchall()

        reviews = feedback.decisions(conn, "applicability")
        counts = {"applicable": 0, "not_applicable": 0, "informational": 0, "not_assessed": 0}
        overridden = agreed = 0
        with conn.cursor() as cur:
            for ob_id, text, modality, start, ref, regulator, jurisdiction, full in rows:
                status, reason, units = assess(
                    text, modality, regulator, jurisdiction, profile,
                    _definitional_parent(full, start, ref or ""),
                    in_review(ref, regulator, profile),
                )
                review = reviews.get(feedback.obligation_key(regulator, ref, text))
                if review and review["value"] in counts:
                    if review["value"] == status:
                        agreed += 1
                    else:
                        overridden += 1
                    status = review["value"]
                    reason = (f"Reviewer decision by {review['reviewer']}"
                              + (f": {review['note']}" if review["note"] else ".")
                              + f" (system proposed: {reason})")
                    if status == "applicable" and not units:
                        units = assess(text, "must", regulator, jurisdiction,
                                       {**profile, "activities": {}})[2]
                cur.execute(
                    """
                    UPDATE obligation
                    SET applicability = %s, applicability_reason = %s, applies_to = %s,
                        applicability_reviewed = %s
                    WHERE id = %s
                    """,
                    (status, reason, ", ".join(units), 1 if review else 0, ob_id),
                )
                counts[status] += 1
        conn.commit()
        if reviews:
            self.emit(event="progress",
                      message=f"honoured {overridden + agreed} reviewer decisions "
                              f"({overridden} overrides)")

        self.log(
            conn, "ok",
            input_summary=f"{len(rows)} obligations vs profile of {profile.get('name')}",
            output_summary=(
                f"{counts['applicable']} applicable, {counts['informational']} "
                f"informational, {counts['not_applicable']} out of scope, "
                f"{counts['not_assessed']} not in this review"
            ),
            reasoning=(
                "Set aside clauses outside the assessment's declared scope, screened out "
                "titles, definitions and permissive clauses that impose no duty, then "
                "tested each remaining obligation against the bank's jurisdiction and "
                "active business lines, and assigned the business units that must "
                "discharge it."
            ),
            tool_calls=["load_bank_profile", "apply_assessment_scope", "classify_provision",
                        "match_activity_triggers", "assign_business_units",
                        "apply_reviewer_decisions"],
            model="rule_engine",
        )
        return {"summary": {**counts, "reviewer_overrides": overridden}}
