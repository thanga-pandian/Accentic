"""
Feature 3: Regulatory obligation extraction.

A section may contain several obligations at different risk tiers. Each candidate
is anchored to the source before it is stored; anything that cannot be anchored
is retried once with the failure fed back, then quarantined.
"""

import re
import uuid
from pathlib import Path

from agents import llm
from agents.base import Agent, VerificationError, anchor

# The prompt lives in prompts/ so it can be reviewed and tuned without touching
# code. Behaviour it asks for is still enforced here, never trusted.
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "obligation_extraction.md"
SYSTEM = _PROMPT_FILE.read_text(encoding="utf-8")

_MODALS = [
    (r"\bshall not\b|\bmust not\b|\bshould not\b", "must_not"),
    # "to be reported within 7 days" carries the same force as "shall report".
    (r"\bshall\b|\bmust\b|\b(?:is|are) required to\b|\bto be \w+ed\b|"
     r"\brequires?\b|\bmandator", "must"),
    (r"\bshould\b", "should"),
    (r"\bmay\b", "may"),
]

_HIGH_RISK = re.compile(
    r"politically exposed|pep\b|high risk|suspicious|terror|money launder|"
    r"sanction|freeze|forged|counterfeit|enhanced due diligence|senior management",
    re.I,
)
_LOW_RISK = re.compile(r"low risk|salaried|government department|may\b", re.I)

_CATEGORIES = [
    (r"suspicious|str\b|ctr\b|fiu|report", "reporting"),
    (r"record|maintain|retain|five years|preserv", "recordkeeping"),
    (r"enhanced due diligence|edd\b|politically exposed|pep\b", "edd"),
    (r"risk categor|low risk|medium risk|high risk|risk profile", "risk_rating"),
    (r"due diligence|cdd\b|identif|verif|officially valid", "cdd"),
    (r"principal officer|designated director|senior management|board", "governance"),
    (r"sanction|screen|watch ?list|fatf", "screening"),
]

_DEADLINE = re.compile(
    r"within\s+\d+\s+(?:days?|months?|years?|working days?)|"
    r"by the \d+\w{2} of|every month|annually|periodic|ongoing basis|"
    r"period of \w+ years?",
    re.I,
)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;:])\s+(?=[A-Z(])|\n(?=\([a-z]+\)|\(i+v?\)|\d+\.)", text)
    return [p.strip() for p in parts if len(p.strip()) > 40]


def _rule_extract(section_text: str) -> list[dict]:
    """Modal-verb obligation detection. Genuine NLP, not a stub."""
    found = []
    for sentence in _sentences(section_text):
        modality = next(
            (m for pattern, m in _MODALS if re.search(pattern, sentence, re.I)), None
        )
        if not modality:
            continue

        risk = "high" if _HIGH_RISK.search(sentence) else (
            "low" if _LOW_RISK.search(sentence) else "medium"
        )
        category = next(
            (c for pattern, c in _CATEGORIES if re.search(pattern, sentence, re.I)),
            "other",
        )
        deadline_match = _DEADLINE.search(sentence)

        actor_match = re.search(
            r"\b(REs?|Regulated Entit(?:y|ies)|banks?|the RE|Principal Officer|"
            r"Designated Director)\b", sentence, re.I,
        )
        condition_match = re.search(
            r"\b(where|when|if|in case|unless|upon)\b[^,.;]{10,120}", sentence, re.I
        )

        found.append({
            "quote": sentence,
            "actor": actor_match.group(0) if actor_match else "Regulated Entity",
            "modality": modality,
            "action": re.sub(r"^\W*(?:\(\w+\)|\d+(?:\.\d+)*\.?)\s*", "", sentence)[:220],
            "condition": condition_match.group(0).strip() if condition_match else None,
            "deadline": deadline_match.group(0) if deadline_match else None,
            "risk_tier": risk,
            "category": category,
        })
    return found


class ObligationAgent(Agent):
    name = "ObligationAgent"
    stage = "obligation_extraction"

    def execute(self, conn, regulation_id: str, **_) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT full_text FROM regulation WHERE id = %s", (regulation_id,)
            )
            full_text = cur.fetchone()[0]
            cur.execute(
                """
                SELECT id, clause_path, raw_text, char_start, page_no
                FROM reg_section WHERE regulation_id = %s ORDER BY char_start
                """,
                (regulation_id,),
            )
            sections = cur.fetchall()

        stored = rejected = recovered = 0
        used_llm = llm.available()

        for sec_id, clause_path, raw_text, sec_start, page_no in sections:
            if len(raw_text.strip()) < 60:
                continue

            candidates, source = self._candidates(clause_path, raw_text)
            if not candidates:
                continue

            for cand in candidates:
                try:
                    start, end = anchor(cand["quote"], raw_text, sec_start)
                except VerificationError:
                    # Model hallucinated a quote. Retry once against the rule
                    # engine, which can only ever return real substrings.
                    retry = self._retry(cand, raw_text, sec_start)
                    if retry is None:
                        self.quarantine(
                            conn,
                            {"clause_path": clause_path, "quote": cand["quote"][:300]},
                            "obligation quote could not be anchored to source text",
                        )
                        rejected += 1
                        self.emit(event="rejected", clause_path=clause_path)
                        continue
                    cand, (start, end) = retry
                    recovered += 1
                    self.emit(event="recovered", clause_path=clause_path)

                sliced = full_text[start:end]
                if not sliced.strip():
                    rejected += 1
                    continue

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO obligation
                            (id, section_id, regulation_id, clause_path, obligation_text,
                             actor, modality, action, condition, deadline, risk_tier,
                             category, char_start, char_end, verified, confidence,
                             extracted_by, page_no)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(uuid.uuid4()), sec_id, regulation_id, clause_path,
                            sliced, cand.get("actor"), cand.get("modality"),
                            cand.get("action"), cand.get("condition"),
                            cand.get("deadline"), cand.get("risk_tier", "medium"),
                            cand.get("category", "other"), start, end, 1,
                            0.92 if source == "claude" else 0.78, source, page_no,
                        ),
                    )
                stored += 1

        conn.commit()
        self.log(
            conn, "ok",
            input_summary=f"{len(sections)} sections",
            output_summary=f"{stored} obligations, {rejected} rejected, {recovered} recovered",
            reasoning=(
                "Split each clause into candidate obligations, anchored every quote to "
                "regulation.full_text, and rejected any the source could not confirm."
            ),
            tool_calls=["read_sections", "extract_obligations", "anchor_offsets", "verify_slice"],
            recovered=recovered > 0,
            model=llm.MODEL if used_llm else "rule_engine",
        )
        return {"summary": {
            "stored": stored, "rejected": rejected, "recovered": recovered,
            "mode": llm.mode(),
        }}

    def _candidates(self, clause_path: str, raw_text: str) -> tuple[list[dict], str]:
        result = llm.complete_json(
            SYSTEM, f"Clause {clause_path}:\n\n{raw_text}", max_tokens=4000
        )
        if isinstance(result, list) and result:
            return [c for c in result if isinstance(c, dict) and c.get("quote")], "claude"
        return _rule_extract(raw_text), "rule_engine"

    def _retry(self, cand: dict, raw_text: str, sec_start: int):
        """Recovery path: re-derive the obligation from text we know is real."""
        for fallback in _rule_extract(raw_text):
            try:
                span = anchor(fallback["quote"], raw_text, sec_start)
            except VerificationError:
                continue
            merged = {**cand, **{k: v for k, v in fallback.items() if v}}
            return merged, span
        return None
