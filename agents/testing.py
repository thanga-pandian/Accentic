"""
Chain link: evidence -> testing.

A control owner's "Test Result: Effective" is a claim, not assurance. This agent
re-performs what it can from the evidence itself:

  design      is the control fully specified (owner, frequency, type, evidence)?
  operating   does the evidence's own data meet the requirement or tolerance?
  currency    was it tested within the bank's test cycle as at the assessment date?

The independent result can only lower a control's effectiveness, never raise it,
so the downstream coverage figures are conservative by construction.
"""

import re
import uuid
from datetime import date

from agents import profile as bank_profile
from agents.base import Agent

_LAG = re.compile(
    r"([\d.]+)\s*days?\s+against\s+(?:a\s+)?(\d+)[\s-]*days?\s+requirement", re.I
)
_RATIO = re.compile(r"(\d[\d,]*)\s+of\s+(\d[\d,]*)")
_EXCEPTION_CUE = re.compile(
    r"incomplete|late|overdue|missing|exception|after the|failed|breach", re.I
)


def _num(raw: str) -> float:
    return float(raw.replace(",", ""))


def operating_test(evidence: str, zero_tolerance: bool, tolerance_pct: float) -> dict:
    """Re-perform the operating test from figures stated in the evidence. Pure."""
    lag = _LAG.search(evidence or "")
    if lag:
        actual, required = _num(lag.group(1)), _num(lag.group(2))
        ok = actual <= required
        return {
            "result": "pass" if ok else "exception",
            "metric": f"{actual:g} days",
            "threshold": f"≤ {required:g} days",
            "conclusion": (
                f"Evidence shows {actual:g} days against a {required:g}-day requirement"
                + ("." if ok else f", a {actual - required:g}-day breach.")
            ),
        }

    ratio = _RATIO.search(evidence or "")
    if ratio and _EXCEPTION_CUE.search(evidence):
        exceptions, sample = _num(ratio.group(1)), _num(ratio.group(2))
        rate = 100 * exceptions / sample if sample else 0.0
        limit = 0.0 if zero_tolerance else tolerance_pct
        ok = rate <= limit
        return {
            "result": "pass" if ok else "exception",
            "metric": f"{exceptions:g} of {sample:g} ({rate:.1f}%)",
            "threshold": "zero tolerance" if zero_tolerance else f"≤ {limit:g}%",
            "conclusion": (
                f"{exceptions:g} exceptions in a population of {sample:g} is a "
                f"{rate:.1f}% exception rate"
                + (" — within tolerance." if ok else
                   (" against zero tolerance for a regulatory filing." if zero_tolerance
                    else f", above the {limit:g}% tolerance."))
            ),
        }

    return {
        "result": "relied",
        "metric": None,
        "threshold": None,
        "conclusion": (
            "Evidence is descriptive, with no figures to re-perform. The reported "
            "result is relied upon pending a sample test."
        ),
    }


def currency_test(last_tested: str, automated: bool, as_of: date, cycles: dict) -> dict:
    cycle = cycles.get("automated" if automated else "manual", 365)
    try:
        tested = date.fromisoformat((last_tested or "").strip())
    except ValueError:
        return {"result": "exception", "metric": "no test date", "threshold": f"{cycle} days",
                "conclusion": "No valid last-tested date: assurance cannot be evidenced."}
    age = (as_of - tested).days
    ok = age <= cycle
    return {
        "result": "pass" if ok else "overdue",
        "metric": f"{age} days since test",
        "threshold": f"≤ {cycle} days",
        "conclusion": (
            f"Last tested {tested.isoformat()}, {age} days before the assessment date"
            + (f", inside the {cycle}-day cycle." if ok else
               f", outside the {cycle}-day cycle. Assurance has lapsed.")
        ),
    }


class TestingAgent(Agent):
    name = "TestingAgent"
    stage = "control_testing"

    def execute(self, conn, **_) -> dict:
        profile = bank_profile.load()
        as_of = bank_profile.as_of(profile)
        cycles = profile.get("test_cycle_days", {})
        tolerance = float(profile.get("risk_appetite", {}).get("exception_tolerance_pct", 5))

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.control_ref, c.owner, c.frequency, c.control_type,
                       c.automated, c.last_tested, c.test_result,
                       COALESCE(c.reported_effectiveness, c.effectiveness),
                       (SELECT reference FROM evidence e WHERE e.control_id = c.id LIMIT 1)
                FROM control c
                """
            )
            controls = cur.fetchall()

        tally = {"confirmed": 0, "exceptions": 0, "lapsed": 0, "relied": 0,
                 "contradicted": 0, "tests": 0}

        for (cid, ref, owner, freq, ctype, automated, last_tested, reported,
             eff, evidence) in controls:
            tests = []

            missing = [n for n, v in (("owner", owner), ("frequency", freq),
                                      ("type", ctype), ("evidence", evidence)) if not v]
            tests.append(("design", "Inspect control specification for owner, frequency, "
                          "type and named evidence",
                          {"result": "fail" if missing else "pass", "metric": None,
                           "threshold": None,
                           "conclusion": ("Missing: " + ", ".join(missing)) if missing
                           else "Control is fully specified."}))

            op = operating_test(evidence or "", "-RPT-" in ref, tolerance)
            tests.append(("operating", "Re-perform from figures stated in the evidence", op))

            cur_t = currency_test(last_tested, bool(automated), as_of, cycles)
            tests.append(("currency", "Compare last test date to the test cycle", cur_t))

            reported_eff = float(eff or 0)
            if op["result"] == "exception":
                independent, new_eff = "Exception", min(reported_eff, 0.3)
                tally["exceptions"] += 1
            elif missing:
                independent, new_eff = "Design deficient", min(reported_eff, 0.3)
                tally["exceptions"] += 1
            elif cur_t["result"] != "pass":
                independent, new_eff = "Assurance lapsed", min(reported_eff, 0.6)
                tally["lapsed"] += 1
            elif op["result"] == "pass":
                independent, new_eff = "Confirmed", reported_eff
                tally["confirmed"] += 1
            else:
                independent, new_eff = "Relied on owner result", reported_eff
                tally["relied"] += 1

            if reported_eff >= 0.9 and new_eff < 0.9:
                tally["contradicted"] += 1
                self.emit(event="progress",
                          message=f"{ref}: owner reports '{reported}', testing finds "
                                  f"{independent.lower()}")

            with conn.cursor() as cur:
                for test_type, procedure, t in tests:
                    cur.execute(
                        """
                        INSERT INTO control_test
                            (id, control_id, test_type, procedure, result, metric,
                             threshold, conclusion)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (str(uuid.uuid4()), cid, test_type, procedure, t["result"],
                         t["metric"], t["threshold"], t["conclusion"]),
                    )
                    tally["tests"] += 1
                cur.execute(
                    """
                    UPDATE control SET reported_effectiveness = %s, effectiveness = %s,
                           independent_result = %s
                    WHERE id = %s
                    """,
                    (reported_eff, new_eff, independent, cid),
                )
                if new_eff < 0.6:
                    cur.execute(
                        "UPDATE evidence SET status = 'deficient' WHERE control_id = %s",
                        (cid,),
                    )
        conn.commit()

        self.log(
            conn, "ok",
            input_summary=f"{len(controls)} controls, as at {as_of.isoformat()}",
            output_summary=(
                f"{tally['tests']} tests: {tally['confirmed']} confirmed, "
                f"{tally['exceptions']} exceptions, {tally['lapsed']} lapsed, "
                f"{tally['contradicted']} contradict the owner's result"
            ),
            reasoning=(
                "Re-performed design, operating and currency tests from the control "
                "specification and its evidence rather than accepting the owner's "
                "reported result, and lowered effectiveness wherever the evidence "
                "does not support it."
            ),
            tool_calls=["load_controls", "inspect_design", "reperform_from_evidence",
                        "check_test_currency", "downgrade_effectiveness"],
            model="rule_engine",
        )
        return {"summary": {k: v for k, v in tally.items()}}
