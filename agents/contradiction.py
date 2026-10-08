"""
Feature 13: Cross-regulation intelligence.
Feature 14: Regulatory contradiction detection.

Looks for obligations that govern the same subject but impose different numeric
limits, retention periods, or opposite modalities — the three ways regulations
actually conflict in practice.
"""

import re
import uuid

from agents.base import Agent

_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60,
    "ninety": 90, "hundred": 100,
}

# Periods appear as digits ("7 days") or words ("three working days").
_PERIOD = re.compile(
    r"\b(\d+|" + "|".join(_WORD_NUM) + r")\s+(?:working\s+|calendar\s+)?"
    r"(day|days|month|months|year|years)\b",
    re.I,
)
_AMOUNT = re.compile(r"(?:Rs\.?|INR|₹)\s*([\d,]+)\s*(lakh|crore|thousand)?", re.I)

# Order-independent: a clause mentions the subject if the cue words appear at all.
_SUBJECTS = {
    "str_filing": r"suspicious transaction|\bstr\b",
    "ctr_filing": r"cash transaction|\bctr\b",
    "retention": r"retention|retain|preserv|(?:record).{0,60}(?:maintain)|"
                 r"(?:maintain).{0,60}(?:record)",
    "pep_approval": r"politically exposed|\bpep\b",
    "cdd_threshold": r"threshold|value of more than|transaction of (?:the value|rs)",
    "periodic_review": r"periodic|review.{0,40}risk|update.{0,40}profile",
}

_TO_DAYS = {"day": 1, "days": 1, "month": 30, "months": 30, "year": 365, "years": 365}

# str.title() would render these as "Str Filing" / "Pep Approval".
_SUBJECT_LABELS = {
    "str_filing": "STR filing deadline",
    "ctr_filing": "CTR filing deadline",
    "retention": "Record retention period",
    "pep_approval": "PEP approval requirement",
    "cdd_threshold": "CDD transaction threshold",
    "periodic_review": "Periodic review cycle",
}


# Periods that are minimums (keep for at least N), so the longer one is stricter.
_MINIMUM_PERIODS = {"retention"}

_OBJECTS = re.compile(r"identity|beneficial owner|account files|correspondence", re.I)


def _objects(text: str) -> frozenset:
    return frozenset(m.lower() for m in _OBJECTS.findall(text))


def _subject(text: str) -> str | None:
    for name, pattern in _SUBJECTS.items():
        if re.search(pattern, text, re.I | re.S):
            return name
    return None


def _days(text: str):
    m = _PERIOD.search(text)
    if not m:
        return None
    raw = m.group(1).lower()
    count = int(raw) if raw.isdigit() else _WORD_NUM.get(raw)
    if count is None:
        return None
    return count * _TO_DAYS[m.group(2).lower()]


def _short(title: str) -> str:
    """Official titles are long; cut at the first qualifier so a finding reads cleanly."""
    head = title.split("(")[0].split(",")[0].strip(" -–—")
    return head if len(head) <= 46 else head[:43].rstrip() + "…"


def _humanise(days: int) -> str:
    """'3650 days' is arithmetic; '10 years' is a finding a reviewer can act on."""
    if days % 365 == 0:
        n = days // 365
        return f"{n} year{'s' if n != 1 else ''}"
    if days % 30 == 0:
        n = days // 30
        return f"{n} month{'s' if n != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''}"


def _amount(text: str):
    m = _AMOUNT.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return value * {"lakh": 1e5, "crore": 1e7, "thousand": 1e3}.get(unit, 1)


class ContradictionAgent(Agent):
    name = "ContradictionAgent"
    stage = "contradiction_detection"

    def execute(self, conn, **_) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, COALESCE(o.clause_ref, o.clause_path), o.obligation_text,
                       o.modality,
                       r.title, o.regulation_id
                FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                WHERE o.applicability = 'applicable'
                """
            )
            rows = cur.fetchall()

        buckets: dict[str, list] = {}
        for ob_id, clause, text, modality, reg_title, reg_id in rows:
            subj = _subject(text)
            if subj:
                buckets.setdefault(subj, []).append(
                    (ob_id, clause, text, modality, reg_title, reg_id)
                )

        found = 0
        for subject, items in buckets.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a, b = items[i], items[j]
                    conflict = self._compare(subject, a, b)
                    if not conflict:
                        continue
                    kind, explanation, severity, confidence = conflict
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO contradiction
                                (id, obligation_a, obligation_b, conflict_type,
                                 explanation, severity, confidence)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (str(uuid.uuid4()), a[0], b[0], kind, explanation,
                             severity, confidence),
                        )
                    found += 1

        conn.commit()
        self.log(
            conn, "ok",
            input_summary=f"{len(rows)} obligations across {len(buckets)} subjects",
            output_summary=f"{found} conflicts detected",
            reasoning=(
                "Grouped obligations by regulated subject, then compared numeric "
                "periods, monetary thresholds and modalities within each group to "
                "surface requirements that cannot both be satisfied."
            ),
            tool_calls=["group_by_subject", "compare_periods", "compare_thresholds",
                        "compare_modality"],
            model="rule_engine",
        )
        return {"summary": {"conflicts": found}}

    def _compare(self, subject, a, b):
        _, clause_a, text_a, mod_a, reg_a, reg_id_a = a
        _, clause_b, text_b, mod_b, reg_b, reg_id_b = b

        cross = reg_id_a != reg_id_b
        scope = "across regulations" if cross else "within the same regulation"
        reg_a, reg_b = _short(reg_a), _short(reg_b)
        subject_label = _SUBJECT_LABELS.get(subject, subject.replace("_", " ").title())
        clause_a = f"clause {clause_a.rstrip('.')}"
        clause_b = f"clause {clause_b.rstrip('.')}"

        # Two rules about different objects (identity records vs transaction
        # records) do not conflict, however different their numbers are.
        if _objects(text_a) != _objects(text_b):
            return None

        days_a, days_b = _days(text_a), _days(text_b)
        if days_a and days_b and days_a != days_b:
            ratio = max(days_a, days_b) / min(days_a, days_b)
            if ratio >= 1.5:
                # A deadline is met by the earlier date; a retention duty is met
                # by keeping records for the longer period. Either way the
                # stricter requirement satisfies both rules.
                longer_binds = subject in _MINIMUM_PERIODS
                a_binds = (days_a > days_b) if longer_binds else (days_a < days_b)
                binding = days_a if a_binds else days_b
                src = f"{reg_a} {clause_a}" if a_binds else f"{reg_b} {clause_b}"
                rule = "The longer period governs" if longer_binds else "The shorter period governs"
                return (
                    "conflicting_period",
                    f"{subject_label} {scope}. {reg_a} {clause_a} requires "
                    f"{_humanise(days_a)}; {reg_b} {clause_b} requires "
                    f"{_humanise(days_b)}. {rule} — "
                    f"{_humanise(binding)} per {src} is the binding requirement.",
                    "high" if cross else "medium",
                    0.86,
                )

        amt_a, amt_b = _amount(text_a), _amount(text_b)
        if amt_a and amt_b and amt_a != amt_b:
            ratio = max(amt_a, amt_b) / min(amt_a, amt_b)
            if ratio >= 1.5:
                return (
                    "conflicting_threshold",
                    f"{subject_label} {scope}. {reg_a} {clause_a} sets a threshold of "
                    f"Rs {amt_a:,.0f}; {reg_b} {clause_b} sets Rs {amt_b:,.0f}. "
                    f"A system configured to one will breach the other.",
                    "high" if cross else "medium",
                    0.83,
                )

        if {mod_a, mod_b} == {"must", "must_not"}:
            return (
                "opposing_modality",
                f"{subject_label} {scope}. {reg_a} {clause_a} mandates an action that "
                f"{reg_b} {clause_b} prohibits. This needs a legal determination of "
                f"precedence before either can be automated.",
                "critical",
                0.74,
            )
        return None
