"""
Ask Accentic: plain-English questions over the regulations and the bank's findings.

Retrieval picks the clauses most relevant to the question and the findings that
hang off them. Claude writes the answer, but every regulatory citation it gives
must quote the source exactly: the quote is anchored with the same anchor() the
pipeline uses, and a citation that cannot be found is stripped. A paragraph left
with no verified support is withheld, and the count is reported. With no API key
the agent answers by quoting the matching obligations verbatim, which is true by
construction.
"""

import time
from pathlib import Path

from agents import llm
from agents.base import Agent, VerificationError, anchor, clause_ref
from agents.mapping import _similarity, _tokens

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "qa_answer.md"
TOP_SECTIONS = 8
MIN_RELEVANCE = 0.05   # below this a clause shares only incidental words with the question


def _ref(regulator: str, ref: str) -> str:
    ref = (ref or "").rstrip(".")
    prefix = "PMLA" if regulator == "GoI" else regulator
    if ref[:1].isalpha():                      # "Chapter II", "Section 3"
        return f"{prefix} {ref}"
    return f"PMLA r.{ref}" if regulator == "GoI" else f"{regulator} ¶{ref}"


def retrieve(conn, question: str) -> tuple[list[dict], list[dict]]:
    q = _tokens(question)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.regulation_id, s.char_start, s.raw_text, r.regulator, r.title,
                   s.clause_path
            FROM reg_section s JOIN regulation r ON r.id = s.regulation_id
            """
        )
        sections = cur.fetchall()
        cur.execute("SELECT id, full_text FROM regulation")
        texts = dict(cur.fetchall())
        cur.execute(
            """
            SELECT o.id, o.section_id, COALESCE(o.clause_ref, o.clause_path), r.regulator,
                   o.obligation_text, o.applicability, o.char_start, o.char_end,
                   c.control_ref, COALESCE(c.independent_result, c.test_result),
                   m.coverage, m.scope_gap, g.severity, rm.owner_role, r.title
            FROM obligation o JOIN regulation r ON r.id = o.regulation_id
            LEFT JOIN mapping m ON m.obligation_id = o.id
            LEFT JOIN control c ON c.id = m.control_id
            LEFT JOIN gap g ON g.obligation_id = o.id
            LEFT JOIN remediation rm ON rm.gap_id = g.id
            """
        )
        obligations = cur.fetchall()

    ranked = sorted(
        ((_similarity(q, _tokens(raw)), sid, rid, start, raw, reg, title, path)
         for sid, rid, start, raw, reg, title, path in sections if len(raw.strip()) > 40),
        reverse=True,
    )
    picked = [r for r in ranked[:TOP_SECTIONS] if r[0] >= MIN_RELEVANCE]

    sources, chosen = [], set()
    for i, (score, sid, rid, start, raw, reg, title, path) in enumerate(picked, 1):
        chosen.add(sid)
        sources.append({"id": f"S{i}", "section_id": sid, "regulation_id": rid,
                        "regulator": reg, "title": title, "char_start": start, "text": raw,
                        "full_text": texts[rid], "score": round(score, 3),
                        "ref": _ref(reg, clause_ref(texts[rid], start) or path)})

    findings = []
    for (oid, sid, ref, reg, text, app, cs, ce, ctrl, result, cov, scope, sev, owner,
         title) in obligations:
        if sid not in chosen:
            continue
        status = ("imposes no duty on this bank" if app != "applicable" else
                  f"mapped to {ctrl or 'no control'} (coverage {('scope gap: ' + scope) if scope else (cov or 'none')}"
                  f"{', independent test: ' + result if result else ''})"
                  + (f"; open {sev} gap, owner {owner}" if sev else "; no gap"))
        findings.append({"id": f"F{len(findings) + 1}", "obligation_id": oid, "regulator": reg,
                         "ref": _ref(reg, ref), "text": " ".join(text.split()), "raw": text,
                         "status": status,
                         "severity": sev, "char_start": cs, "char_end": ce, "title": title,
                         "applicable": app == "applicable"})
    return sources, findings


def _verify(answer: list, sources: list[dict], findings: list[dict]) -> tuple[list, int, int]:
    by_id = {s["id"]: s for s in sources}
    f_by_id = {f["id"]: f for f in findings}
    out, rejected, withheld = [], 0, 0
    for para in answer if isinstance(answer, list) else []:
        if not isinstance(para, dict) or not str(para.get("text", "")).strip():
            continue
        cites = []
        for c in para.get("citations") or []:
            if not isinstance(c, dict):
                continue
            src = str(c.get("source", ""))
            if src in f_by_id:
                f = f_by_id[src]
                cites.append({"kind": "finding", "ref": f["ref"], "obligation_id": f["obligation_id"],
                              "status": f["status"], "verified": True})
                continue
            s = by_id.get(src)
            if s is None:
                rejected += 1
                continue
            try:
                start, end = anchor(str(c.get("quote", "")), s["text"], s["char_start"])
            except VerificationError:
                rejected += 1
                continue
            cites.append({"kind": "source", "ref": _ref(s["regulator"], clause_ref(s["full_text"], start)),
                          "regulator": s["regulator"], "title": s["title"],
                          "quote": s["full_text"][start:end], "char_start": start, "char_end": end,
                          "verified": True})
        if any(c["kind"] == "source" for c in cites) or para.get("kind") == "caveat":
            out.append({"text": str(para["text"]).strip(), "citations": cites})
        else:
            withheld += 1
    return out, rejected, withheld


def _rule_answer(question: str, findings: list[dict]) -> list[dict]:
    """No model: answer with the matching obligations, quoted verbatim, and their status."""
    q = _tokens(question)
    scored = sorted(((_similarity(q, _tokens(f["text"])), f) for f in findings if f["applicable"]),
                    key=lambda x: -x[0])
    if not scored:
        return []
    # Keep obligations that are genuinely about the question, not incidental matches.
    floor = max(MIN_RELEVANCE, 0.5 * scored[0][0])
    picked = [f for s, f in scored if s >= floor][:5]
    # A clause that introduces a list ("relating to:") is answered by its limbs.
    for parent in list(picked):
        if parent["raw"].rstrip().endswith(":"):
            stem = parent["ref"] + "("
            picked += [f for _s, f in scored if f["ref"].startswith(stem) and f not in picked]
    paras = []
    for f in picked[:7]:
        paras.append({
            "text": f"{f['ref']} requires: “{f['text']}” Status: {f['status']}.",
            "citations": [
                {"kind": "source", "ref": f["ref"], "regulator": f["regulator"], "quote": f["raw"],
                 "char_start": f["char_start"], "char_end": f["char_end"], "verified": True,
                 "title": f["title"]},
                {"kind": "finding", "ref": f["ref"], "obligation_id": f["obligation_id"],
                 "status": f["status"], "verified": True},
            ],
        })
    return paras


class QAAgent(Agent):
    name = "QAAgent"
    stage = "question_answering"

    def execute(self, conn, question: str = "", **_) -> dict:
        started = time.time()
        sources, findings = retrieve(conn, question)
        mode, rejected, withheld = "rule_engine", 0, 0

        if not sources:
            answer = []
        elif llm.available():
            context = "\n\n".join(f"[{s['id']}] {s['ref']} ({s['title']})\n{s['text'].strip()}" for s in sources)
            facts = "\n".join(f"[{f['id']}] {f['ref']}: {f['status']}" for f in findings)
            raw = llm.complete_json(
                _PROMPT.read_text(encoding="utf-8"),
                f"QUESTION:\n{question}\n\nSOURCES:\n{context}\n\nFINDINGS:\n{facts or '(none)'}",
                max_tokens=16000,
            )
            if isinstance(raw, dict) and isinstance(raw.get("answer"), list):
                answer, rejected, withheld = _verify(raw["answer"], sources, findings)
                mode = "claude"
            else:
                answer = _rule_answer(question, findings)
        else:
            answer = _rule_answer(question, findings)

        self.log(
            conn, "ok",
            input_summary=question[:200],
            output_summary=(f"{len(answer)} paragraphs from {len(sources)} sources; "
                            f"{rejected} citations rejected, {withheld} paragraphs withheld"),
            reasoning=("Retrieved the clauses most similar to the question and the findings "
                       "attached to them, answered from those alone, and verified every quoted "
                       "citation against the source text before showing it."),
            tool_calls=["retrieve_clauses", "attach_findings", "answer", "anchor_citations"],
            duration_ms=int((time.time() - started) * 1000),
            model=llm.MODEL if mode == "claude" else "rule_engine",
        )
        conn.commit()
        return {
            "question": question, "mode": mode, "answer": answer,
            "rejected_citations": rejected, "withheld_paragraphs": withheld,
            "sources": [{"id": s["id"], "ref": s["ref"], "title": s["title"], "score": s["score"]}
                        for s in sources],
        }
