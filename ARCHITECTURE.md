# Accentic — Structural Architecture

**ET AI Hackathon: Agentic Edition — Problem 1, Banking/Financial Regulations**

Submission component 2 of 2. Component 1 is the working demo (`uvicorn api.main:app --port 8000`).

---

## 1. What the system does

Accentic maintains a continuously evolving, **provably traceable** relationship across the
chain the problem statement defines:

```
Regulations → obligations → applicability → internal controls → evidence
            → testing → gaps → remediation → ongoing monitoring
```

Eight agents run that chain autonomously. The differentiating property is that **every
assertion the system makes resolves to an exact character range in a source document**, and
the system proves it by re-slicing the original text rather than trusting its own record.

---

## 2. The core architectural decision

Most LLM compliance tools ask a model to read a regulation and report what it found. The
failure mode is silent: a plausible obligation that does not exist in the document, cited to
a clause number the model invented. In regulated banking that is not a cosmetic bug — it is
an audit finding.

Accentic separates two responsibilities that are usually conflated:

| Responsibility | Owner | Why |
|---|---|---|
| **What** is an obligation | The model (or rule engine) | Requires language understanding |
| **Where** it exists in the source | Python, by searching the text | Must be exact and checkable |

A model returns a quote. Python locates that quote in the source and derives
`char_start`/`char_end` itself. The stored text is **always a slice of the original
document**, never the model's transcription. If the quote cannot be located, the claim is
treated as a hallucination: it is rejected, a recovery path runs, and if that also fails the
item is quarantined to `needs_review` for a human.

This is implemented in `agents/base.py::anchor()` and enforced at every write.

**Consequence:** hallucination becomes a *measurable, reported quantity* rather than a risk
to be hedged in a disclaimer. The demo reports 100% verification because unverifiable
results cannot enter the database.

---

## 3. System layers

```
┌────────────────────────────────────────────────────────────────────────────┐
│  INPUT                                                                      │
│  Regulation sources (.pdf via pdfplumber / .txt)   Bank control framework   │
│  RBI KYC Master Direction · PMLA Maintenance Rules · prior versions         │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  INGESTION & OFFSET LAYER            extract/sectioner.py                   │
│  • pages joined into ONE immutable string  → regulation.full_text           │
│  • split on clause numbering (38. / 38.1 / (a) / (iv) / Chapter IV)         │
│    — never on token or character count                                      │
│  • page offset table maps any char position back to a physical page         │
│  • SELF-AUDIT: every section re-sliced and compared  → 100%                 │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  AGENT LAYER                         agents/                                │
│                                                                             │
│   IngestionAgent ─▶ ObligationAgent ─▶ MappingAgent ─▶ GapAgent             │
│                            │               ▲              │                 │
│                            │          ControlAgent        ▼                 │
│                            │          (+ Evidence)   RemediationEngine      │
│                            │                                               │
│                            ├─▶ ContradictionAgent   (cross-regulation)     │
│                            ├─▶ ChangeAgent          (version diff)         │
│                            └─▶ SimulationAgent      (what-if, on demand)   │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
                   ╔════════════════════════════╗
                   ║   VERIFICATION GATE        ║
                   ║   full_text[start:end]     ║
                   ║        == stored_text ?    ║
                   ╚═════════╦═══════════╦══════╝
                        PASS ║           ║ FAIL
                             ▼           ▼
                        persisted    retry → needs_review (human)
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  DATA LAYER  (SQLite dev / PostgreSQL prod — same DDL, UUID keys, real FKs) │
│  regulation · reg_section · obligation · control_doc · control · evidence   │
│  mapping · gap · remediation · contradiction · reg_change                   │
│  agent_run (observability) · needs_review (human oversight queue)           │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  PRESENTATION   FastAPI + single-file UI (no framework, no CDN, no build)   │
│  Live SSE agent stream · proof panel · provenance chain · gap board         │
│  conflicts · change impact · what-if simulation                             │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Process flow, with the decisions each agent makes

| # | Agent | Key decision it makes | Failure handling |
|---|---|---|---|
| 1 | **IngestionAgent** | Where does each clause begin and end? Which page does an offset fall on? | Self-audits all slices; any mismatch → `needs_review`, status `degraded` |
| 2 | **ObligationAgent** | Is this sentence an enforceable obligation? Who must act, under what condition, by when, at what risk tier? | Quote not locatable → rule-engine recovery → still failing → quarantine |
| 3 | **ControlAgent** | What controls exist, who owns them, and how effective is each *by its own last test result*? | Unparseable block → quarantine |
| 4 | **MappingAgent** | Which control discharges this obligation, and does it actually cover it? | Below match floor → explicitly recorded as `coverage = none`, not dropped |
| 5 | **GapAgent** | How bad is this gap? `risk = impact × likelihood` | — |
| 6 | **RemediationEngine** (in GapAgent) | What fix, which owner, what effort, due when? | Falls back to a generic control-design action |
| 7 | **ContradictionAgent** | Do two obligations on the same subject conflict on period, threshold, or modality? | Only emits above a ratio threshold to avoid false positives |
| 8 | **ChangeAgent** | What changed from the prior version, and does it move a *number*? | No prior version → logs `skipped` |
| 9 | **SimulationAgent** | If this rule changed, what is the blast radius? | No match → reports zero rather than inventing impact |

### The decision that produces the demo's headline finding

`MappingAgent` does not treat "a control exists" as "the obligation is covered". Coverage is
discounted by the control's measured effectiveness:

```python
effective_coverage = similarity × (0.4 + 0.6 × control_effectiveness)
coverage = "full" if similarity ≥ 0.20 and effectiveness ≥ 0.9 else "partial"
```

So `CTRL-RPT-041` (Suspicious Transaction Reporting) matches the STR obligation strongly —
but its own evidence records a **mean filing lag of 11.4 days against a 7-day regulatory
requirement**, so it tests as *Needs Improvement*, effectiveness drops to 0.3, and the
obligation surfaces as a **high-severity gap with a 70% residual exposure** rather than a
green tick. The bank's own evidence convicts it.

---

## 5. Model usage — where AI is central, and where it deliberately is not

| Stage | Claude Opus 5.5 | Deterministic | Rationale |
|---|---|---|---|
| Obligation identification | ✅ primary | rule engine fallback | Needs language understanding; genuinely ambiguous |
| Offset computation | ❌ never | ✅ always | Must be exact; a model cannot be trusted with arithmetic over a 100KB string |
| Quote verification | ❌ never | ✅ always | The checker must be independent of the thing it checks |
| Control parsing | ❌ | ✅ | Structured document; rules are more reliable and free |
| Mapping | ❌ | ✅ domain-weighted similarity | Deterministic, explainable, auditable score |
| Risk scoring | ❌ | ✅ impact × likelihood | A regulator must be able to reproduce the number |
| Contradiction detection | ❌ | ✅ | Numeric comparison; must not be probabilistic |

**Configuration:** `claude-opus-5-5`, adaptive thinking, `effort: medium`, streaming
(`agents/llm.py`).

**This split is intentional and is the Responsible-AI position.** AI is applied where
judgement is required and withheld from everything that must be exactly reproducible. A
compliance number that changes between runs is useless to an auditor.

**Graceful degradation:** with no `ANTHROPIC_API_KEY` the system runs end-to-end on the rule
engine — real modal-verb obligation detection, not a stub — and the UI labels the active
mode. Results are never presented as model output when rules produced them.

---

## 6. Feature coverage — 14 of 14

| # | Feature | Implementation | Demo evidence |
|---|---|---|---|
| 1 | Regulatory intelligence & knowledge ingestion | `IngestionAgent`, offset self-audit | 2 regulations, 76 sections, audit 100% |
| 2 | Regulatory change intelligence | `ChangeAgent` clause diff | 20 changes; *"period 15 days → 7 days"* |
| 3 | Regulatory obligation extraction | `ObligationAgent` + anchoring | 37 obligations, 100% verified |
| 4 | Bank control-framework understanding | `ControlAgent` | 13 controls parsed with offsets |
| 5 | Regulatory-to-control mapping | `MappingAgent` | 37 mappings: 7 full / 14 partial / 16 none |
| 6 | Control effectiveness assessment | Test-result grading, discounts coverage | 4 controls failing their last test |
| 7 | Evidence-based compliance assessment | `evidence` table linked to controls | 13 evidence artefacts, status-graded |
| 8 | Gap identification | `GapAgent` | 30 gaps |
| 9 | Risk-based gap prioritisation | impact × likelihood → severity | 4 critical, 14 high, 4 medium, 8 low |
| 10 | Remediation recommendation engine | Category playbook + owner + due date | 30 remediations, P1–P4 |
| 11 | Autonomous regulatory impact analysis | `affected_count` per change | Change → affected obligations |
| 12 | What-if / simulation | `SimulationAgent` + UI | Blast radius across the live graph |
| 13 | Cross-regulation intelligence | Multi-regulation corpus | RBI KYC + PMLA ingested together |
| 14 | Regulatory contradiction detection | `ContradictionAgent` | 3 conflicts, incl. 3-day vs 7-day STR |

---

## 7. Nine-blocker position — declared: **D2 / F3**

### Feature axis: **F3** (requires ≥ 8 of 14)

All 14 features are implemented and demonstrable in the running system. Claim F3.

### Depth axis: **D2** (textual/structured input, high demonstrable reliability)

**Why D2 and not D3.** D3 requires *"highly heterogeneous multimodal input."* Accentic
accepts PDF and plain text — both resolve to text. It does not process scanned images, OCR,
embedded tables, diagrams, or audio. Claiming D3 would be overestimation.

**Why D2 and not D1.** D1 asks only for *"acceptable outputs in a majority of situations."*
D2 requires **high demonstrable reliability** — and reliability here is measured, not
asserted:

| Reliability property | Mechanism | Measured result |
|---|---|---|
| Section offsets correct | Re-slice self-audit over every section | **100%** (76/76) |
| Obligation text authentic | Independent re-slice at read time | **100%** (37/37) |
| Unverifiable claims excluded | Anchor failure → recovery → quarantine | 0 unverified rows persisted |
| Coverage not overstated | Effectiveness discount on every mapping | 4 failing controls correctly denied full coverage |
| Every action auditable | `agent_run` log: reasoning, tools, retries, duration | 8 runs per execution |
| Human oversight path | `needs_review` queue, surfaced in the UI | Always populated on failure, never silent |

The verification is **independent of the producer** — the check re-reads the source document
rather than trusting the stored record, so it can detect corruption as well as hallucination.

**The honest path to D3** is OCR for scanned circulars, table extraction for tariff and
threshold schedules, and layout-aware parsing for multi-column gazette PDFs. The offset
contract already generalises to those inputs; only the ingestion front-end changes.

---

## 8. Responsible AI

- **Hallucination:** structurally prevented, not mitigated. Unanchorable claims cannot be stored.
- **Human oversight:** `needs_review` is a first-class table and a first-class UI surface.
- **Provenance:** every obligation renders its char range, re-sliced source text, and highlighted context.
- **Honest labelling:** the UI shows whether Claude or the rule engine produced each result.
- **Reproducibility:** scoring is deterministic; the same corpus yields the same risk numbers.
- **No silent drops:** every failure path writes a row with a reason.

---

## 9. Scalability

| Concern | Current | Production path |
|---|---|---|
| Storage | SQLite | PostgreSQL 16 — same DDL, `docker-compose.yml` included |
| Retrieval at corpus scale | Weighted lexical similarity | pgvector embeddings (extension already provisioned) |
| Throughput | Sequential agents | Agents are independent per regulation → parallel / queue |
| Cost | One model call per section | Prompt caching on the shared system prompt; batch API for bulk re-ingest |
| Re-runs | Full rebuild | Offsets are stable, so change-only re-processing is a diff against `reg_change` |

---

## 10. What is not built (stated to avoid overestimation)

- OCR / scanned-document and table ingestion (the D3 gap).
- Live regulator feed polling — change intelligence runs against a supplied prior version.
- Authentication, multi-tenancy, and workflow state transitions on remediations.
- The RBI fixture is representative sample text; the pipeline is source-agnostic and runs
  unchanged against the official PDF placed in `data/regulations/`.

---

## 11. Reproducing the demo

```bash
pip install -r requirements.txt
python -m db.migrate
uvicorn api.main:app --port 8000      # open http://localhost:8000 → "Run Analysis"
```

Independent verification of the traceability claim, outside the UI:

```bash
python -m extract.verify_offsets --audit     # expect 100%
python -m extract.verify_offsets 38.         # expect PASS
```
