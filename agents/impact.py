"""
Feature 2:  Regulatory change intelligence.
Feature 11: Autonomous regulatory impact analysis.
Feature 12: What-if / simulation capability.

Change intelligence diffs two versions of a regulation by clause. Simulation
answers "if this clause tightened, what breaks?" by walking the live chain
obligation -> mapping -> control -> gap.
"""

import difflib
import re
import uuid

from agents.base import Agent
from extract.sectioner import NUMBERED_LINE, advancing, read_source

# Amendment footnotes ("2 Substituted w.e.f. October 1, 2026 ...") sit at the foot of a
# page and run to the page break; a bare number on its own line is a page number.
_FOOTNOTE = re.compile(r"^(?:\d{1,2}\s*)?(?:Substituted|Inserted|Omitted|Added|Deleted|Amended)"
                       r"\s+(?:with effect|w\.e\.f|vide|by)\b")
_PAGE_NO = re.compile(r"^\d{1,3}$")


def _body(text: str) -> str:
    """Clause text without page furniture, for comparison only. Re-paginating a
    document moves page numbers and footnotes between clauses without changing a rule."""
    pages = []
    for page in text.split("\n\n"):
        lines = [ln for ln in page.split("\n") if not _PAGE_NO.match(ln.strip())]
        cut = next((i for i, ln in enumerate(lines) if _FOOTNOTE.match(ln.strip())), len(lines))
        pages.append("\n".join(lines[:cut]))
    return "\n\n".join(pages)


class ChangeAgent(Agent):
    name = "ChangeAgent"
    stage = "change_intelligence"

    def execute(self, conn, regulation_id: str, prior_path=None, prior_text=None,
                prior_label: str = "prior published version", **_) -> dict:
        if prior_text is None:
            if prior_path is None or not prior_path.exists():
                self.log(conn, "skipped", output_summary="no prior version supplied",
                         model="rule_engine")
                return {"summary": {"changes": 0}}
            prior_text = read_source(prior_path)[0]
            source_label = prior_path.name
        else:
            source_label = prior_label

        with conn.cursor() as cur:
            cur.execute("SELECT full_text, version FROM regulation WHERE id = %s",
                        (regulation_id,))
            current_text, version = cur.fetchone()

        changes = self._diff_clauses(prior_text, current_text)

        with conn.cursor() as cur:
            for change in changes:
                # Sections store clause paths with a trailing dot ("38."), and a
                # change to "38" also touches its sub-clauses "38.1", "38.2".
                # The sectioner records sub-clause content under its parent number
                # ("13.1 ..." lands in clause_path "13."), so count against the root.
                clause = change["clause_path"]
                root = clause.split(".")[0]
                cur.execute(
                    """
                    SELECT COUNT(*) FROM obligation
                    WHERE regulation_id = %s
                      AND (clause_path = %s OR clause_path = %s OR clause_path LIKE %s)
                    """,
                    (regulation_id, root, f"{root}.", f"{root}.%"),
                )
                affected = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO reg_change
                        (id, regulation_id, prior_version, change_type, clause_path,
                         summary, before_text, after_text, impact_level, affected_count)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid.uuid4()), regulation_id, prior_label, change["type"],
                        change["clause_path"], change["summary"],
                        change["before"], change["after"],
                        change["impact"], affected,
                    ),
                )
        conn.commit()

        self.log(
            conn, "ok",
            input_summary=f"diff against {source_label}",
            output_summary=f"{len(changes)} clause-level changes",
            reasoning=(
                "Segmented both versions by clause number and diffed matching clauses, "
                "classifying each change as added, removed or amended and grading "
                "impact by whether the change tightens a numeric requirement."
            ),
            tool_calls=["load_prior_version", "segment_clauses", "diff", "grade_impact"],
            model="rule_engine",
        )
        return {"summary": {"changes": len(changes)}}

    def _diff_clauses(self, prior: str, current: str) -> list[dict]:
        def segment(text):
            out = {}
            marks = advancing(re.finditer(NUMBERED_LINE, text), lambda m: m.group(1))
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
                out[m.group(1)] = text[m.start():end].strip()
            return out

        before, after = segment(prior), segment(current)
        changes = []

        for clause in sorted(set(before) | set(after)):
            b, a = before.get(clause), after.get(clause)
            # Re-flowed lines between two printings are not a change in the rule, even
            # where a wrap splits a word or URL ("Implementation-of-" / "Implementation- of-").
            if b is not None and a is not None and \
                    "".join(_body(b).split()) == "".join(_body(a).split()):
                continue
            if b is None:
                changes.append({
                    "clause_path": clause, "type": "added",
                    "summary": f"Clause {clause} is new in this version.",
                    "before": None, "after": a[:600], "impact": "high",
                })
            elif a is None:
                changes.append({
                    "clause_path": clause, "type": "removed",
                    "summary": f"Clause {clause} was removed.",
                    "before": b[:600], "after": None, "impact": "medium",
                })
            else:
                ratio = difflib.SequenceMatcher(None, _body(b).split(), _body(a).split()).ratio()
                deltas = self._numeric_deltas(_body(b), _body(a))
                if deltas:
                    summary = f"Clause {clause} amended — " + "; ".join(deltas) + "."
                    impact = "high"
                else:
                    summary = (
                        f"Clause {clause} reworded with no change to numeric "
                        f"requirements ({ratio:.1%} of words unchanged)."
                    )
                    impact = "low"
                changes.append({
                    "clause_path": clause, "type": "amended",
                    "summary": summary,
                    "before": b[:600], "after": a[:600], "impact": impact,
                })
        return changes

    @staticmethod
    def _numeric_deltas(before: str, after: str) -> list[str]:
        """Name what actually changed: thresholds, percentages, periods."""
        words = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                 "fifteen|twenty|thirty|sixty|ninety")
        patterns = [
            (r"(?:Rs\.?|INR|₹)\s*[\d,]+(?:\s*(?:lakh|crore))?", "threshold"),
            (r"\d+\s*%|\d+\s*per\s*cent", "percentage"),
            (rf"\b(?:\d+|{words})\s+(?:working\s+)?"
             r"(?:day|days|month|months|year|years)\b", "period"),
            (rf"\b(?:\d+|{words})\s+risk categor(?:y|ies)\b", "risk tiers"),
        ]
        out = []
        for pattern, label in patterns:
            old = re.findall(pattern, before, re.I)
            new = re.findall(pattern, after, re.I)
            for o, n in zip(old, new):
                if o.strip() != n.strip():
                    out.append(f"{label} {o.strip()} → {n.strip()}")
            if len(new) > len(old):
                out.append(f"new {label} {new[-1].strip()} introduced")
        return out


class SimulationAgent(Agent):
    name = "SimulationAgent"
    stage = "impact_simulation"

    def execute(self, conn, scenario: str = "", **_) -> dict:
        """Walk the live chain to compute blast radius for a hypothetical change."""
        terms = [t for t in re.findall(r"[a-z]{4,}", (scenario or "").lower())][:8]
        if not terms:
            return {"summary": {"affected": 0}, "impact": []}

        like = " OR ".join(["LOWER(o.obligation_text) LIKE %s"] * len(terms))
        params = [f"%{t}%" for t in terms]

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT o.id, COALESCE(o.clause_ref, o.clause_path), o.risk_tier,
                       o.obligation_text, m.coverage, c.control_ref,
                       COALESCE(c.independent_result, c.test_result), g.severity,
                       g.risk_score
                FROM obligation o
                LEFT JOIN mapping m ON m.obligation_id = o.id
                LEFT JOIN control c ON c.id = m.control_id
                LEFT JOIN gap g ON g.obligation_id = o.id
                WHERE o.applicability = 'applicable' AND ({like})
                """,
                params,
            )
            rows = cur.fetchall()

        impact = [
            {
                "obligation_id": r[0], "clause_path": r[1], "risk_tier": r[2],
                "excerpt": (r[3] or "")[:200],
                "coverage": r[4] or "none", "control_ref": r[5],
                "test_result": r[6], "severity": r[7],
                "risk_score": r[8] or 0.0,
            }
            for r in rows
        ]
        impact.sort(key=lambda x: x["risk_score"], reverse=True)

        controls_hit = {i["control_ref"] for i in impact if i["control_ref"]}
        uncovered = sum(1 for i in impact if i["coverage"] == "none")

        self.log(
            conn, "ok",
            input_summary=scenario[:200],
            output_summary=(
                f"{len(impact)} obligations, {len(controls_hit)} controls, "
                f"{uncovered} already uncovered"
            ),
            reasoning=(
                "Matched the scenario to live obligations, then traversed the mapping "
                "and gap chain to report which controls would need to change and where "
                "exposure already exists."
            ),
            tool_calls=["parse_scenario", "match_obligations", "traverse_chain",
                        "rank_by_risk"],
            model="rule_engine",
        )
        conn.commit()
        return {
            "summary": {
                "affected": len(impact),
                "controls": len(controls_hit),
                "uncovered": uncovered,
            },
            "impact": impact[:40],
        }
