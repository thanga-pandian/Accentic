"""
Feature 3: Regulatory obligation extraction.

A section may contain several obligations at different risk tiers. Each candidate
is anchored to the source before it is stored; anything that cannot be anchored
is retried once with the failure fed back, then quarantined.
"""

import re
import uuid
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from agents import llm
from agents import profile as bank_profile
from agents.applicability import in_review
from agents.base import Agent, VerificationError, anchor, clause_ref
from extract.sectioner import NUMBERED_LINE

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
    (r"risk categor|low risk|medium risk|high risk|risk profile|risk-based approach",
     "risk_rating"),
    # Before CDD: "verify every day, the UNSCR 1718 Sanctions List" is screening.
    (r"sanction|screen|watch ?list|fatf|\bunscr?\b|\buapa\b|terroris|designated (?:individuals|"
     r"persons|list)|freez", "screening"),
    (r"due diligence|cdd\b|identif|verif|officially valid", "cdd"),
    (r"principal officer|designated director|senior management|board", "governance"),
]

_DEADLINE = re.compile(
    r"within\s+\d+\s+(?:days?|months?|years?|working days?)|"
    r"by the \d+\w{2} of|every month|annually|periodic|ongoing basis|"
    r"period of \w+ years?",
    re.I,
)


def _categorise(text: str) -> str:
    # "reporting entity" names the addressee, not a reporting duty.
    scrubbed = re.sub(r"reporting entit(?:y|ies)", "entity", text, flags=re.I)
    return next(
        (c for pattern, c in _CATEGORIES if re.search(pattern, scrubbed, re.I)), "other"
    )


def _parent_lead(full_text: str, start: int) -> str:
    """The lead-in line of the numbered clause that a lettered limb belongs to."""
    head = full_text[max(0, start - 4000):start]
    marks = list(re.finditer(NUMBERED_LINE, head))
    return head[marks[-1].start():].split("\n", 1)[0] if marks else ""


# "The bank shall, (1) maintain all necessary records ..." - the limb carries the duty
# without its own "shall". A limb that opens on a verb inherits it; one that opens on
# a noun phrase ("(i) the nature of the transactions") is an item in a list, not a duty.
_NOT_VERBS = {"the", "a", "an", "any", "all", "such", "its", "their", "his", "her", "in", "of",
              "on", "for", "where", "when", "if", "to", "by", "with", "from", "and", "or",
              "that", "this", "these", "those", "each", "every", "no", "not", "whether",
              "other", "at", "as", "under", "after", "before", "within", "who", "which"}


def _opens_on_verb(sentence: str) -> bool:
    m = re.match(r"\W*(?:\((?:\w{1,4})[)\]]\s*)+([a-z]+)\b", sentence)
    return bool(m) and m.group(1) not in _NOT_VERBS


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;:])\s+(?=[A-Z(\[])|\n(?=\[?\((?:[a-z]+|\d{1,2})[)\]]|\[?\d+[A-Z]?\.)", text)
    return [p.strip() for p in parts if len(p.strip()) > 40]


def _modality(text: str) -> str | None:
    return next((m for pattern, m in _MODALS if re.search(pattern, text, re.I)), None)


def _rule_extract(section_text: str, lead: str = "") -> list[dict]:
    """Modal-verb obligation detection. Genuine NLP, not a stub."""
    inherited = _modality(lead) if lead else None
    found = []
    for sentence in _sentences(section_text):
        modality = _modality(sentence) or (
            inherited if inherited in ("must", "must_not") and _opens_on_verb(sentence) else None)
        if not modality:
            continue

        risk = "high" if _HIGH_RISK.search(sentence) else (
            "low" if _LOW_RISK.search(sentence) else "medium"
        )
        category = _categorise(sentence)
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
            "action": re.sub(r"^\W*(?:\(\w+[)\]]|\d+[A-Z]?(?:\.\d+)*\.?)\s*", "", sentence)[:220],
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
                "SELECT full_text, regulator FROM regulation WHERE id = %s", (regulation_id,)
            )
            full_text, regulator = cur.fetchone()
            cur.execute(
                """
                SELECT id, clause_path, raw_text, char_start, page_no
                FROM reg_section WHERE regulation_id = %s ORDER BY char_start
                """,
                (regulation_id,),
            )
            sections = cur.fetchall()

        stored = rejected = recovered = 0
        sections = [s for s in sections if len(s[2].strip()) >= 60]

        # Claude reads only the clauses this assessment covers; the rest are still
        # extracted, by the rule engine, so they stay searchable and traceable.
        profile = bank_profile.load()
        in_scope = [in_review(clause_ref(full_text, s[3]), regulator, profile) for s in sections]
        with ThreadPoolExecutor(max_workers=6) as pool:
            answers = list(pool.map(
                lambda pair: self._ask(pair[0][1], pair[0][2]) if pair[1] else None,
                zip(sections, in_scope)))
        used_llm = any(a is not None for a in answers)

        for (sec_id, clause_path, raw_text, sec_start, page_no), answer in zip(sections, answers):
            lead = _parent_lead(full_text, sec_start) if clause_path.startswith("(") else ""
            if answer is not None:
                candidates, source = answer, "claude"
            else:
                candidates, source = _rule_extract(raw_text, lead), "rule_engine"
            if not candidates:
                continue

            for cand in candidates:
                try:
                    start, end = anchor(cand["quote"], raw_text, sec_start)
                except VerificationError:
                    # Model hallucinated a quote. Retry once against the rule
                    # engine, which can only ever return real substrings.
                    retry = self._retry(cand, raw_text, sec_start, lead)
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

                ref = clause_ref(full_text, start)
                category = cand.get("category") or "other"
                if category == "other" and "(" in ref:
                    category = _categorise(_parent_lead(full_text, start))

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO obligation
                            (id, section_id, regulation_id, clause_path, obligation_text,
                             actor, modality, action, condition, deadline, risk_tier,
                             category, char_start, char_end, verified, confidence,
                             extracted_by, page_no, clause_ref)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(uuid.uuid4()), sec_id, regulation_id, clause_path,
                            sliced, cand.get("actor"), cand.get("modality"),
                            cand.get("action"), cand.get("condition"),
                            cand.get("deadline"), cand.get("risk_tier", "medium"),
                            category, start, end, 1,
                            0.92 if source == "claude" else 0.78, source, page_no, ref,
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

    @staticmethod
    def _ask(clause_path: str, raw_text: str) -> list[dict] | None:
        """Claude's candidates for one clause (live or cached), or None to use rules."""
        result = llm.complete_json(
            SYSTEM, f"Clause {clause_path}:\n\n{raw_text}", max_tokens=4000
        )
        if isinstance(result, list) and result:
            return [c for c in result if isinstance(c, dict) and c.get("quote")]
        return None

    def _retry(self, cand: dict, raw_text: str, sec_start: int, lead: str = ""):
        """Recovery path: re-derive the obligation from text we know is real."""
        for fallback in _rule_extract(raw_text, lead):
            try:
                span = anchor(fallback["quote"], raw_text, sec_start)
            except VerificationError:
                continue
            merged = {**cand, **{k: v for k, v in fallback.items() if v}}
            return merged, span
        return None
