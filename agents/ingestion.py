"""
Feature 1: Regulatory intelligence and knowledge ingestion.

Wraps the sectioner so ingestion reports through the same observability path as
every other agent, and self-audits its own offsets before handing off.
"""

from agents.base import Agent
from extract.sectioner import run as sectionise


class IngestionAgent(Agent):
    name = "IngestionAgent"
    stage = "ingestion"

    def execute(self, conn, sources, **_) -> dict:
        for src in sources:
            sectionise(src, dry_run=False, conn=conn)
            self.emit(event="progress", message=f"ingested {src.name}")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM regulation")
            regs = cur.fetchone()[0]
            cur.execute(
                """
                SELECT s.char_start, s.char_end, s.raw_text, r.full_text
                FROM reg_section s JOIN regulation r ON r.id = s.regulation_id
                """
            )
            rows = cur.fetchall()

        # Self-audit: re-slice every section and confirm it reproduces the source.
        passed = sum(1 for cs, ce, raw, full in rows if full[cs:ce] == raw)
        total = len(rows)
        pct = round(100 * passed / total, 1) if total else 0.0

        if passed != total:
            self.quarantine(
                conn,
                {"passed": passed, "total": total},
                f"offset self-audit failed for {total - passed} sections",
            )

        conn.commit()
        self.log(
            conn, "ok" if passed == total else "degraded",
            input_summary=f"{len(sources)} source documents",
            output_summary=f"{regs} regulations, {total} sections, offset audit {pct}%",
            reasoning=(
                "Joined pages into a single immutable document string, split on "
                "clause numbering rather than token count, then re-sliced every "
                "section to confirm the stored text reproduces the source exactly."
            ),
            tool_calls=["read_source", "join_pages", "split_on_clause_numbering",
                        "compute_offsets", "self_audit_slices"],
            model="deterministic",
        )
        return {"summary": {"regulations": regs, "sections": total,
                            "offset_audit_pct": pct}}
