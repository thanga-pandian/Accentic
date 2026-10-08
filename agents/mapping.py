"""
Feature 5: Regulatory-to-control mapping.
Feature 6: Control effectiveness assessment.

Scores every applicable obligation against every control, then grades coverage by
combining match strength with how well the control actually tested. Reviewer
corrections are applied to the obligation they name and, through learned
vocabulary, improve the match for every similar obligation.
"""

import math
import re
import uuid
from collections import Counter

from agents import feedback
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
    "reconstruct": 3.0, "archiv": 2.0, "designat": 2.0,
}

_SYNONYMS = {"client": "customer", "clients": "customer", "kyc": "diligence",
             "fiu": "financial intelligence unit", "cdd": "customer due diligence",
             "edd": "enhanced due diligence"}

# Control refs encode their domain (CTRL-KYC-001). Same-domain matches get a
# small, fixed bonus: deterministic, and stated in the rationale.
_CONTROL_DOMAIN = {"KYC": "cdd", "RISK": "risk_rating", "EDD": "edd",
                   "REC": "recordkeeping", "RPT": "reporting", "GOV": "governance"}
DOMAIN_BONUS = 0.05

# Subjects that narrow what an obligation's object is. A retention control for
# transaction records does not discharge a duty to retain identity records.
# Actors ("the Principal Officer shall") and inclusive phrases ("whether or not
# in cash") are deliberately not here: they do not narrow the object.
_SCOPE_TERMS = ["identity", "beneficial", "politically exposed", "reconstruct",
                "occasional", "non-resident"]

_SUFFIXES = ("ations", "ation", "ions", "ion", "ings", "ing", "ed", "es", "s")
_NEXT_CLAUSE = re.compile(r"(?m)^\d{1,3}(?:\.\d{1,3})*\.?\s")


def scope_gaps(obligation_text: str, control_text: str) -> list[str]:
    ob, ctrl = obligation_text.lower(), control_text.lower()
    return [t for t in _SCOPE_TERMS if t in ob and t not in ctrl]


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _tokens(text: str) -> Counter:
    text = re.sub(r"reporting entit(?:y|ies)", "entity", text.lower())
    words = []
    for w in re.findall(r"[a-z]{3,}", text):
        words.extend(_SYNONYMS.get(w, w).split())
    return Counter(_stem(w) for w in words if w not in _STOP)


_STEMMED_TERMS = [(_stem(k), w) for k, w in _DOMAIN_TERMS.items()]


def _context(full_text: str, char_start: int, char_end: int, text: str, ref: str) -> str:
    """
    A clause ending in ':' introduces a list, and its limbs are what a control must
    cover. A limb such as 13.1(ii) only makes sense with its parent's lead-in.
    """
    out = ""
    if text.rstrip().endswith(":"):
        tail = full_text[char_end:char_end + 1200]
        nxt = _NEXT_CLAUSE.search(tail)
        out += tail[: nxt.start()] if nxt else tail
    if "(" in (ref or ""):
        head = full_text[max(0, char_start - 900):char_start]
        marks = list(_NEXT_CLAUSE.finditer(head))
        if marks:
            out += " " + head[marks[-1].start():].split("\n", 1)[0]
    return out


def _domain(control_ref: str) -> str | None:
    parts = control_ref.split("-")
    return _CONTROL_DOMAIN.get(parts[1]) if len(parts) > 2 else None


def _weight(token: str) -> float:
    for stem, w in _STEMMED_TERMS:
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

    # Calibrated against the observed score distribution for this corpus:
    # correct matches score 0.13-0.33 once the domain bonus applies.
    MATCH_FLOOR = 0.13
    STRONG_MATCH = 0.20

    def execute(self, conn, **_) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id, o.obligation_text, o.action, o.category, o.risk_tier,
                       o.char_start, o.char_end, o.clause_ref, r.full_text, r.regulator
                FROM obligation o JOIN regulation r ON r.id = o.regulation_id
                WHERE o.applicability = 'applicable'
                """
            )
            obligations = cur.fetchall()
            cur.execute(
                """
                SELECT id, control_ref, title, description, effectiveness,
                       COALESCE(independent_result, test_result)
                FROM control
                """
            )
            controls = cur.fetchall()

        reviews = feedback.decisions(conn, "mapping")
        learned = {ref: [_tokens(t) for t in texts]
                   for ref, texts in feedback.learned_examples(conn).items()}

        # The title states what a control is for, so it counts twice. Reviewer
        # examples extend the control's vocabulary: that is the learning step.
        base = {ref: _tokens(f"{title} {title} {desc}") for _c, ref, title, desc, _e, _r in controls}
        vectors = {}
        for ref, vec in base.items():
            vec = Counter(vec)
            for example in learned.get(ref, []):
                vec.update(example)
            vectors[ref] = vec
        info = {ref: (cid, eff, result) for cid, ref, _t, _d, eff, result in controls}
        control_text = {ref: f"{title} {desc}" for _c, ref, title, desc, _e, _x in controls}

        mapped = unmapped = partial = 0
        overrides = reproduced = reviewed = 0

        for (ob_id, ob_text, action, category, risk, char_start, char_end, ref_,
             full_text, regulator) in obligations:
            context = _context(full_text, char_start, char_end, ob_text, ref_)
            ob_vec = _tokens(f"{ob_text} {context} {action or ''} {category or ''}")
            own = _tokens(ob_text)

            def score(ref, held_out=False):
                vec = vectors[ref]
                if held_out:
                    # Rebuild without this obligation's own reviewer example.
                    vec, skipped = Counter(base[ref]), False
                    for example in learned.get(ref, []):
                        if not skipped and example == own:
                            skipped = True
                            continue
                        vec.update(example)
                same = category and _domain(ref) == category
                return _similarity(ob_vec, vec) + (DOMAIN_BONUS if same else 0.0)

            def best(held_out=False):
                ranked = sorted(((score(r, held_out), r) for r in vectors), reverse=True)
                return ranked[0] if ranked else (0.0, None)

            review = reviews.get(feedback.obligation_key(regulator, ref_, ob_text))
            if review and review["value"] != feedback.NO_CONTROL and review["value"] not in info:
                review = None   # the control the reviewer chose no longer exists

            best_score, best_ref = best()
            if review:
                reviewed += 1
                # Held-out check: would learning from the *other* decisions alone
                # have reached the reviewer's answer? Its own example is removed.
                held_score, held_ref = best(held_out=True)
                auto = held_ref if held_score >= self.MATCH_FLOOR else feedback.NO_CONTROL
                if auto == review["value"]:
                    reproduced += 1
                else:
                    overrides += 1
                if review["value"] == feedback.NO_CONTROL:
                    best_score, best_ref = 0.0, None
                else:
                    # The reviewer has confirmed relevance; coverage is still graded
                    # on the control's tested effectiveness and scope.
                    best_ref = review["value"]
                    best_score = max(score(best_ref), self.STRONG_MATCH)

            prefix = (f"Reviewer-confirmed mapping ({review['reviewer']}). "
                      if review else "")

            if best_ref is None or best_score < self.MATCH_FLOOR:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO mapping
                            (id, obligation_id, control_id, coverage, coverage_score,
                             rationale, method, confidence, reviewed)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(uuid.uuid4()), ob_id, None, "none", 0.0,
                            prefix + "No control in the framework addresses this obligation.",
                            "reviewer" if review else "weighted_lexical",
                            1.0 if review else round(1 - best_score, 2),
                            1 if review else 0,
                        ),
                    )
                unmapped += 1
                continue

            best_id, best_eff, best_result = info[best_ref]

            # A control only gives full coverage if it matches strongly, passed its
            # independent test, and reaches every subject the obligation names.
            missing = scope_gaps(ob_text, control_text.get(best_ref, ""))
            effective_coverage = best_score * (0.4 + 0.6 * best_eff)
            if missing:
                effective_coverage *= 0.5
                coverage = "partial"
                partial += 1
                rationale = (
                    f"{best_ref} is the closest control, but its scope does not extend to "
                    f"{', '.join(repr(m) for m in missing)}, which this obligation "
                    f"specifically requires."
                )
            elif best_score >= self.STRONG_MATCH and best_eff >= 0.9:
                coverage = "full"
                mapped += 1
                rationale = (
                    f"{best_ref} directly addresses this obligation "
                    f"(match score {best_score:.2f}) and independent testing "
                    f"concluded '{best_result}'."
                )
            else:
                coverage = "partial"
                partial += 1
                reason = (
                    f"independent testing concluded '{best_result}'"
                    if best_eff < 0.9
                    else f"the match is indirect (match score {best_score:.2f})"
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
                         rationale, method, confidence, scope_gap, reviewed)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid.uuid4()), ob_id, best_id, coverage,
                        round(effective_coverage, 3), prefix + rationale,
                        "reviewer" if review else "weighted_lexical",
                        1.0 if review else round(min(0.99, best_score + 0.3), 2),
                        ", ".join(missing) or None, 1 if review else 0,
                    ),
                )

        conn.commit()
        if reviewed:
            self.emit(event="progress",
                      message=f"learned from {sum(len(v) for v in learned.values())} reviewer "
                              f"mappings; {reproduced} of {reviewed} reproduced held-out")
        self.log(
            conn, "ok",
            input_summary=f"{len(obligations)} obligations x {len(controls)} controls, "
                          f"{len(reviews)} reviewer decisions",
            output_summary=f"{mapped} fully covered, {partial} partial, {unmapped} uncovered"
                           + (f"; held-out agreement {reproduced}/{reviewed}" if reviewed else ""),
            reasoning=(
                "Scored each applicable obligation, with its list limbs and parent "
                "lead-in as context, against every control using domain-weighted term "
                "similarity extended with vocabulary learned from reviewer corrections, "
                "applied reviewer overrides, then discounted coverage by the control's "
                "independent test result so a failing control cannot claim full coverage."
            ),
            tool_calls=["load_obligations", "load_controls", "learn_from_reviewers",
                        "score_similarity", "apply_reviewer_overrides",
                        "apply_effectiveness_discount"],
            model="rule_engine",
        )
        return {"summary": {"mapped": mapped, "partial": partial, "uncovered": unmapped,
                            "reviewer_overrides": overrides,
                            "held_out_agreement": f"{reproduced}/{reviewed}" if reviewed else "n/a"}}
