"""
Agent foundation: run logging, retry-with-feedback, and offset anchoring.

The anchoring contract is the project's core integrity rule. A model may only
say WHAT text is an obligation; Python decides WHERE it lives by searching the
source, and the stored text is always a slice of full_text. A model-authored
quote that cannot be located is a hallucination and is rejected, not stored.
"""

import json
import re
import time
import uuid

from db.connection import get_connection


class VerificationError(Exception):
    """Raised when a model claim cannot be anchored to the source document."""


def anchor(quote: str, haystack: str, haystack_offset: int) -> tuple[int, int]:
    """
    Locate `quote` inside `haystack` and return absolute (start, end) offsets.

    Falls back to a whitespace-flexible match because models routinely collapse
    newlines when quoting. The returned span always describes the ORIGINAL text,
    so slicing it reproduces the source byte-for-byte.
    """
    quote = quote.strip()
    if not quote:
        raise VerificationError("empty quote")

    idx = haystack.find(quote)
    if idx != -1:
        return haystack_offset + idx, haystack_offset + idx + len(quote)

    pattern = r"\s+".join(re.escape(tok) for tok in quote.split())
    match = re.search(pattern, haystack)
    if match:
        return haystack_offset + match.start(), haystack_offset + match.end()

    raise VerificationError(f"quote not found in source: {quote[:80]!r}")


_MARKER = re.compile(r"(?m)^(?:(\d{1,3}(?:\.\d{1,3})*)\.?\s|\(([a-z]{1,4})\))")


def clause_ref(full_text: str, offset: int) -> str:
    """
    Precise citation for an offset, e.g. '13.1(ii)'. The sectioner groups '13.1' and
    '13.2' under '13.', so the fine-grained reference is recovered from the source.
    """
    num = sub = None
    for m in _MARKER.finditer(full_text):
        if m.start() > offset:
            break
        if m.group(1):
            num, sub = m.group(1), None
        else:
            sub = m.group(2)
    if num is None:
        return f"({sub})" if sub else ""
    return f"{num}({sub})" if sub else num


class Agent:
    name = "agent"
    stage = "stage"

    def __init__(self, run_id: str, emit=None):
        self.run_id = run_id
        self._emit = emit or (lambda **_: None)

    def emit(self, **payload):
        self._emit(agent=self.name, stage=self.stage, **payload)

    def log(
        self,
        conn,
        status: str,
        input_summary: str = "",
        output_summary: str = "",
        reasoning: str = "",
        tool_calls: list | None = None,
        attempts: int = 1,
        recovered: bool = False,
        duration_ms: int = 0,
        model: str = "",
    ):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_run
                    (id, run_id, agent_name, stage, status, input_summary,
                     output_summary, reasoning, tool_calls, attempts, recovered,
                     duration_ms, model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid.uuid4()), self.run_id, self.name, self.stage, status,
                    input_summary, output_summary, reasoning,
                    json.dumps(tool_calls or []), attempts, 1 if recovered else 0,
                    duration_ms, model,
                ),
            )
        # Commit here: agents call log() after their own commit, so without this
        # the final agent in the pipeline would lose its run record on close.
        conn.commit()

    def quarantine(self, conn, payload: dict, reason: str):
        """Route an unverifiable result to needs_review for human oversight."""
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO needs_review (id, stage, payload, failure_reason)
                VALUES (%s,%s,%s,%s)
                """,
                (str(uuid.uuid4()), f"{self.name}/{self.stage}",
                 json.dumps(payload), reason),
            )

    def run(self, conn, **kwargs):
        started = time.time()
        self.emit(event="start")
        try:
            result = self.execute(conn, **kwargs)
            duration = int((time.time() - started) * 1000)
            self.emit(event="done", duration_ms=duration, **result.get("summary", {}))
            return result
        except Exception as exc:
            duration = int((time.time() - started) * 1000)
            self.log(conn, "failed", output_summary=str(exc), duration_ms=duration)
            self.emit(event="failed", error=str(exc), duration_ms=duration)
            raise

    def execute(self, conn, **kwargs) -> dict:
        raise NotImplementedError
