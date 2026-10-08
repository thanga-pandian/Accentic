"""
Feature 4: Bank control-framework understanding.
Feature 7: Evidence-based compliance assessment.

Controls are parsed with the same offset discipline as regulations, so a control
claim is as traceable as a regulatory one.
"""

import re
import uuid

from agents import profile as bank_profile
from agents.base import Agent

_BLOCK = re.compile(
    r"^(?P<ref>CTRL-[A-Z]+-\d+)\s*\|\s*(?P<title>.+?)$", re.M
)

_FIELDS = {
    "description": re.compile(r"^Description:\s*(.+?)(?=^\w+:)", re.M | re.S),
    "owner": re.compile(r"^Owner:\s*(.+)$", re.M),
    "frequency": re.compile(r"^Frequency:\s*(.+)$", re.M),
    "control_type": re.compile(r"^Type:\s*(.+)$", re.M),
    "automated": re.compile(r"^Automated:\s*(.+)$", re.M),
    "last_tested": re.compile(r"^Last Tested:\s*(.+)$", re.M),
    "test_result": re.compile(r"^Test Result:\s*(.+)$", re.M),
    "evidence": re.compile(r"^Evidence:\s*(.+)$", re.M),
}

_EFFECTIVENESS = {
    "effective": 1.0,
    "partially effective": 0.6,
    "needs improvement": 0.3,
    "ineffective": 0.0,
    "not tested": 0.0,
}


class ControlAgent(Agent):
    name = "ControlAgent"
    stage = "control_ingestion"

    def execute(self, conn, control_path, **_) -> dict:
        if control_path.suffix.lower() == ".pdf":
            import pdfplumber
            with pdfplumber.open(control_path) as pdf:
                full_text = "\n\n".join((p.extract_text() or "") for p in pdf.pages)
        else:
            full_text = control_path.read_text(encoding="utf-8")

        head = [ln.strip() for ln in full_text.strip().splitlines()[:6] if ln.strip()]
        title = head[1] if len(head) > 1 else (head[0] if head else control_path.stem)
        ref_match = re.search(r"^Document Ref:\s*(.+)$", full_text, re.M)

        doc_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO control_doc (id, title, owner_org, version, full_text, source_url)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    doc_id,
                    title,
                    head[0] if head else bank_profile.load().get("name"),
                    ref_match.group(1).strip() if ref_match else None,
                    full_text,
                    str(control_path),
                ),
            )

        matches = list(_BLOCK.finditer(full_text))
        if not matches:
            self.quarantine(
                conn, {"document": control_path.name, "chars": len(full_text)},
                "no controls recognised: expected 'CTRL-XXX-NNN | Title' blocks",
            )
        stored = evidence_count = 0

        for idx, m in enumerate(matches):
            start = m.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(full_text)
            block = full_text[start:end]

            fields = {}
            for key, pattern in _FIELDS.items():
                fm = pattern.search(block)
                fields[key] = fm.group(1).strip() if fm else ""

            description = " ".join(fields["description"].split())
            test_result = fields["test_result"].lower().strip()
            effectiveness = _EFFECTIVENESS.get(test_result, 0.0)

            control_id = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO control
                        (id, control_doc_id, control_ref, title, description, owner,
                         frequency, control_type, automated, last_tested, test_result,
                         effectiveness, char_start, char_end, verified)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        control_id, doc_id, m.group("ref"), m.group("title").strip(),
                        description, fields["owner"], fields["frequency"],
                        fields["control_type"],
                        1 if fields["automated"].lower().startswith("yes") else 0,
                        fields["last_tested"], fields["test_result"].strip(),
                        effectiveness, start, end,
                        1 if full_text[start:end] == block else 0,
                    ),
                )

                if fields["evidence"]:
                    status = "present" if effectiveness >= 0.6 else "deficient"
                    cur.execute(
                        """
                        INSERT INTO evidence
                            (id, control_id, evidence_type, reference, captured_on, status, note)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(uuid.uuid4()), control_id, "test_artifact",
                            fields["evidence"], fields["last_tested"], status,
                            fields["test_result"],
                        ),
                    )
                    evidence_count += 1
            stored += 1

        conn.commit()
        weak = sum(1 for m in matches if "Needs Improvement" in full_text[m.start():m.start() + 900])
        self.log(
            conn, "ok",
            input_summary=control_path.name,
            output_summary=f"{stored} controls, {evidence_count} evidence items, {weak} failing tests",
            reasoning=(
                "Parsed the internal control framework into structured controls with "
                "offsets, graded each by its latest test result, and attached evidence."
            ),
            tool_calls=["read_control_doc", "parse_controls", "grade_effectiveness", "link_evidence"],
            model="rule_engine",
        )
        return {"summary": {"controls": stored, "evidence": evidence_count},
                "control_doc_id": doc_id}
