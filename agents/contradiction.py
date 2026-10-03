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
                SELECT o.id, o.clause_path, o.obligation_text, o.modality,
                       r.title, o.regulation_id
                FROM obligation o JOIN regulation r ON r.id = o.regulation_id
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

        days_a, days_b = _days(text_a), _days(text_b)
        if days_a and days_b and days_a != days_b:
            ratio = max(days_a, days_b) / min(days_a, days_b)
            if ratio >= 1.5:
                return (
                    "conflicting_period",
                    f"{subject.replace('_', ' ').title()} {scope}: {clause_a} "
                    f"({reg_a}) requires {days_a} days while {clause_b} ({reg_b}) "
                    f"requires {days_b} days. The shorter period governs.",
                    "high" if cross else "medium",
                    0.86,
                )

        amt_a, amt_b = _amount(text_a), _amount(text_b)
        if amt_a and amt_b and amt_a != amt_b:
            ratio = max(amt_a, amt_b) / min(amt_a, amt_b)
            if ratio >= 1.5:
                return (
                    "conflicting_threshold",
                    f"{subject.replace('_', ' ').title()} {scope}: {clause_a} sets a "
                    f"threshold of {amt_a:,.0f} while {clause_b} sets {amt_b:,.0f}. "
                    f"Systems configured to one will breach the other.",
                    "high" if cross else "medium",
                    0.83,
                )

        if {mod_a, mod_b} == {"must", "must_not"}:
            return (
                "opposing_modality",
                f"{subject.replace('_', ' ').title()} {scope}: {clause_a} mandates an "
                f"action that {clause_b} prohibits. Requires a legal determination of "
                f"precedence.",
                "critical",
                0.74,
            )
        return None
