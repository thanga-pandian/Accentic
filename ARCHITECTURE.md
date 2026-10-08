# Accentic — Structural Architecture

**ET AI Hackathon: Agentic Edition — Problem 1, Banking/Financial Regulations**

Submission component 2 of 2. Component 1 is the working demo (`python -m uvicorn api.main:app --port 8000`).

---

## 1. What the system does

Accentic maintains a continuously evolving, **provably traceable** relationship across the
chain the problem statement defines:

```
Regulations → obligations → applicability → internal policies/controls → evidence
            → testing → gaps → remediation → ongoing monitoring
```

Ten agents run that chain, one per link, in an order the orchestrator plans from the current
state; an eleventh, the `AssuranceAgent`, checks the chain's own invariants before anything is
relied on, and a twelfth, the `WorkflowAgent`, closes the remediation loop on evidence. The differentiating property is that **every assertion the system makes resolves to
an exact character range in a source document**, and the system proves it by re-slicing the
original text rather than trusting its own record. The chain is continuous: the system
fingerprints its inputs, remembers what it saw, and learns from reviewer corrections, so each
run reports what changed since the last one and gets better at mapping.

---

## 2. The core architectural decisions

### 2.1 What versus where

Most LLM compliance tools ask a model to read a regulation and report what it found. The
failure mode is silent: a plausible obligation that does not exist in the document, cited to
a clause number the model invented. In regulated banking that is not a cosmetic bug — it is
an audit finding.

| Responsibility | Owner | Why |
|---|---|---|
| **What** is an obligation | The model (or rule engine) | Requires language understanding |
| **Where** it exists in the source | Python, by searching the text | Must be exact and checkable |

A model returns a quote. Python locates that quote in the source and derives
`char_start`/`char_end` itself. The stored text is **always a slice of the original
document**, never the model's transcription. If the quote cannot be located, the claim is
treated as a hallucination: it is rejected, a recovery path runs, and if that also fails the
item is quarantined to `needs_review` for a human. Implemented in `agents/base.py::anchor()`.

**Consequence:** hallucination becomes a *measurable, reported quantity* rather than a risk
to be hedged in a disclaimer.

### 2.2 Claims are not assurance

A control owner's "Test Result: Effective" is a claim. The `TestingAgent` re-performs what it
can from the evidence itself and **can only lower effectiveness, never raise it**. Coverage,
gap likelihood and every downstream number are therefore conservative by construction.

### 2.3 Not every "shall" binds this bank

Titles, definitions and permissive clauses contain modal verbs but impose no duty, and a duty
attached to a business line the bank does not operate is out of scope. The
`ApplicabilityAgent` decides this against an explicit bank profile, before any gap is scored,
and records the reason.

### 2.4 Humans correct, the system learns

A reviewer can correct any applicability or mapping decision from the traceability panel. A
decision is keyed on what the obligation *is* (regulator, clause, normalised text), so it
survives re-runs. On the next run it **overrides** the agent for that obligation (labelled
"Reviewer-confirmed", with who, when and why) and, for mappings, **teaches**: the corrected
obligation's wording joins the chosen control's vocabulary, so similar obligations map
correctly unaided. Learning is measured honestly: for each reviewer mapping the agent asks
whether the vocabulary learned from the *other* decisions alone would have reached the same
control (held-out agreement), and reports it on every run.

### 2.5 The chain checks itself

After the chain runs, the `AssuranceAgent` evaluates eight invariants the rest of the system
assumes: every clause and obligation reproduces its source; every control has design,
operating and currency tests; every applicable obligation has exactly one mapping;
out-of-scope provisions are never mapped; no full-coverage claim rests on a failing or
out-of-scope control; every incomplete mapping is a gap; every gap has an owned, dated
remediation. A violation triggers one re-run of the responsible stage; a violation that
survives is quarantined for a human and the run is marked degraded.


### 2.6 A remediation is closed by evidence, not by a checkbox

Every gap has a task (owner, status, notes) that outlives runs. The SLA clock starts at first
detection. On each run the `WorkflowAgent` opens a task for each new gap, **re-opens** a task
marked done whose gap is still detected (the fix has not taken effect), and marks a task
**verified closed** only when a fresh analysis no longer finds the gap. Monitoring raises
overdue, blocked and re-opened alerts from the same records.

### 2.7 Answers you can check

*Ask Accentic* retrieves the clauses most similar to a question and the findings attached to
them. Claude answers from those alone; every quoted citation is anchored with the same
`anchor()` the pipeline uses, a citation that cannot be found is stripped, and a paragraph left
without verified support is withheld. The counts of stripped citations and withheld paragraphs
are shown with the answer. With no key, the agent quotes the matching obligations verbatim.

---

## 3. System layers

```
┌────────────────────────────────────────────────────────────────────────────┐
│  INPUT                                                                      │
│  Regulations (.pdf via pdfplumber / .txt) · prior versions · UI uploads     │
│  Bank control framework (.txt / .pdf) · bank profile (data/bank/profile.json)│
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  INGESTION & OFFSET LAYER            extract/sectioner.py                   │
│  • pages joined into ONE immutable string  → regulation.full_text           │
│  • split on clause numbering (38. / 38.1 / (a) / (iv) / Chapter IV)         │
│  • precise citation recovered per offset   → 13.1(ii), 53.1(b)              │
│  • SELF-AUDIT: every section re-sliced and compared  → 100%                 │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  AGENT LAYER  (agents/, run in chain order by agents/orchestrator.py)       │
│                                                                             │
│  Plan (state-derived reasons per step)                                      │
│  Ingestion → Obligation → Applicability → Control → Testing → Mapping       │
│      → Gap → Contradiction → Change → Assurance → Workflow → Monitoring     │
│                                                                             │
│  On demand: Simulation, Ask Accentic (QA), board report generation.         │
│  Reviewer decisions feed Applicability and Mapping.                         │
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
│  DATA LAYER  (SQLite dev / PostgreSQL prod — same model, UUID keys, FKs)    │
│  regulation · reg_section · obligation · control_doc · control · evidence   │
│  control_test · mapping · gap · remediation · contradiction · reg_change    │
│  monitor_alert · agent_run (observability) · needs_review (human queue)     │
│  source_snapshot · kri_snapshot · review_decision · remediation_task ·      │
│  task_comment  ← persist ACROSS runs                                        │
└───────────────────────────────┬────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  PRESENTATION   FastAPI + single-file UI (no framework, no CDN, no build)   │
│  Live SSE agent stream · traceability drawer · registers with CSV export    │
│  risk-control matrix · heatmap · change diffs · what-if · monitoring · drift│
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Process flow, with the decisions each agent makes

| # | Agent | Chain link | Key decision | Failure handling |
|---|---|---|---|---|
| 1 | **IngestionAgent** | Regulations | Where does each clause begin and end? Which page? | Self-audits all slices; mismatch → `needs_review`, status `degraded` |
| 2 | **ObligationAgent** | Obligations | Is this sentence an enforceable obligation? Who acts, under what condition, by when, at what risk? | Quote not locatable → rule-engine recovery → still failing → quarantine |
| 3 | **ApplicabilityAgent** | Applicability | Does this bind *this* bank? Title, definition, permissive, or out-of-scope business line? Which units own it? Is there a reviewer decision to honour? | Every decision stored with its reason; nothing silently excluded |
| 4 | **ControlAgent** | Controls, evidence | What controls exist, who owns them, what evidence backs them? | Unparseable document → quarantine |
| 5 | **TestingAgent** | Testing | Does the evidence support the owner's result? Design, operating and currency tests | Qualitative evidence → "relied", never "passed" |
| 6 | **MappingAgent** | Mapping | Which control discharges this obligation, and does it reach every subject the obligation names? What have reviewers taught it? | Below match floor → `coverage = none`, recorded not dropped; a reviewer's control that no longer exists is ignored |
| 7 | **GapAgent** | Gaps, remediation | How bad is this gap? `risk = impact × likelihood`. What fix, which owner, by when? | Claude draft invalid or unavailable → playbook action |
| 8 | **ContradictionAgent** | Cross-regulation | Do two obligations on the same object conflict? Which requirement binds? | Different objects are never compared |
| 9 | **ChangeAgent** | Change intelligence | What changed since the last run (or prior version), and does it move a *number*? | No baseline → logs `skipped` |
| 10 | **AssuranceAgent** | Self-assurance | Do the chain's eight invariants hold? | One re-run of the failing stage → still failing → quarantine, run marked degraded |
| 11 | **WorkflowAgent** | Remediation tracking | New gap → task; done but still detected → re-open; gone → verified closed | Every transition writes an attributed note |
| 12 | **MonitoringAgent** | Ongoing monitoring | What changed since last run? Which tests lapse, which milestones fall due, is appetite breached? | First run establishes a baseline |
| – | **SimulationAgent** | What-if | If this rule changed, what is the blast radius? | No match → reports zero rather than inventing impact |
| – | **QAAgent** | Ask Accentic | Which clauses answer this question, and what is the bank's status on them? | Unverifiable citation → stripped; unsupported paragraph → withheld; nothing relevant → says so |

### Decisions behind the headline findings

**The bank's own evidence convicts it.** `CTRL-RPT-041` (Suspicious Transaction Reporting)
matches the STR obligation strongly, but its evidence records a **mean filing lag of 11.4 days
against a 7-day requirement**. The TestingAgent re-performs that comparison, concludes
*Exception*, and caps effectiveness at 0.3. RBI ¶53.1(b) surfaces as a **critical gap with 70%
residual exposure** rather than a green tick.

**The owner says Effective; the evidence disagrees.** `CTRL-RPT-040` (Cash Transaction
Reporting) is self-reported *Effective*, but its evidence shows 1 of 12 monthly CTRs filed after
the deadline. Regulatory filings carry zero tolerance, so testing returns *Exception* and the
monitoring layer raises a critical "owner result contradicted" alert.

**A control can exist and still not cover the obligation.** PMLA Rule 6 requires *identity*
records to be kept for five years after the relationship ends. `CTRL-REC-030` retains
*transaction* records. The MappingAgent's scope check caps this at a **scope gap** instead of
claiming full coverage.

**Coverage formula:**

```python
score     = weighted_similarity(obligation + list limbs + parent lead-in, control) \
            + 0.05 if same control domain
coverage  = "none"    if score < 0.13
          = "partial" if scope_gap(obligation, control)          # names a subject the control misses
          = "full"    if score >= 0.20 and tested_effectiveness >= 0.9
          = "partial" otherwise
```

**Conflicts name the binding requirement correctly.** For a filing deadline the shorter period
binds (3 working days under PMLA Rule 9, not 7 days under RBI ¶53.1(b)). For record retention
the *longer* period binds (10 years under PMLA Rule 5, not 5 years under RBI ¶46.2), because
keeping records for longer satisfies both rules.

---

## 5. Model usage — where AI is central, and where it deliberately is not

| Stage | Claude Opus 5.5 | Deterministic | Rationale |
|---|---|---|---|
| Obligation identification | ✅ primary | rule engine fallback | Needs language understanding; genuinely ambiguous |
| Question answering | ✅ primary | verbatim obligation quotes | Synthesis across clauses is a language task; every quoted citation is anchored or stripped |
| Board report | ❌ | ✅ generated from the database | A board pack must reproduce exactly from the same run |
| Remediation drafting | ✅ primary | control-design playbook | Tailored advice is a language task; each draft must name a real gap, an owner, a valid effort size and a sensible-length action, or it is discarded. Severity, priority and due date are never taken from the model |
| Offset computation | ❌ never | ✅ always | Must be exact; a model cannot be trusted with arithmetic over a long string |
| Quote verification | ❌ never | ✅ always | The checker must be independent of the thing it checks |
| Applicability | ❌ | ✅ profile rules | A scoping decision must be auditable and reproducible |
| Control testing | ❌ | ✅ re-performance | Assurance must be reproducible by an auditor |
| Mapping | ❌ | ✅ domain-weighted similarity + scope check | Deterministic, explainable score |
| Risk scoring | ❌ | ✅ impact × likelihood | A regulator must be able to reproduce the number |
| Contradiction detection | ❌ | ✅ numeric / modal comparison | Must not be probabilistic |

**Configuration:** `claude-opus-5-5`, adaptive thinking, `effort: medium`, streaming, and
server-side refusal fallback (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`)
in `agents/llm.py`. Prompts live in `prompts/` so they can be reviewed without touching code.
A refusal or unparseable response returns `None` and the deterministic path takes over.

**Honest labelling is computed, not configured.** The header reports Claude only when stored
results were actually produced by Claude; a key that fails at the API is reported as
"rule engine (API unreachable)". The `.env.example` placeholder is treated as no key.

**This split is the Responsible-AI position.** AI is applied where judgement is required and
withheld from everything that must be exactly reproducible. A compliance number that changes
between runs is useless to an auditor; the fixed assessment date in the bank profile makes
test currency, due dates and scores reproduce exactly.

**Graceful degradation:** with no `ANTHROPIC_API_KEY` the system runs end to end on the rule
engine — real modal-verb obligation detection, not a stub — and the UI labels the active mode.

---

## 6. Feature coverage — 14 of 14

Figures are from the bundled corpus on the rule engine.

| # | Feature | Implementation | Demo evidence |
|---|---|---|---|
| 1 | Regulatory intelligence & knowledge ingestion | `IngestionAgent`, offset self-audit, UI upload | 2 regulations, 76 clauses, audit 100% |
| 2 | Regulatory change intelligence | `ChangeAgent`, `MonitoringAgent` source fingerprints | 20 clause changes, 5 numeric (e.g. *period 15 days → 7 days*); drift between runs |
| 3 | Regulatory obligation extraction | `ObligationAgent` + anchoring | 37 obligations, 100% verified |
| 4 | Bank control-framework understanding | `ControlAgent` | 13 controls parsed with offsets |
| 5 | Regulatory-to-control mapping | `MappingAgent` | 32 mappings: 5 full, 26 partial, 1 scope gap |
| 6 | Control effectiveness assessment | `TestingAgent` | 39 tests: 4 exceptions, 1 lapsed, 2 contradict the owner |
| 7 | Evidence-based compliance assessment | evidence linked and re-performed | 13 artefacts; operating tests from their figures |
| 8 | Gap identification | `GapAgent` | 27 gaps (weak control, scope gap, no control) |
| 9 | Risk-based gap prioritisation | impact × likelihood → severity | 2 critical, 8 high, 7 medium, 10 low |
| 10 | Remediation recommendation engine | Claude-drafted per gap (validated) or playbook; owner, effort, due date | 27 remediations, P1–P4 |
| 11 | Autonomous regulatory impact analysis | affected obligations per change, alerts | numeric moves raised automatically |
| 12 | What-if / simulation | `SimulationAgent` + UI | blast radius across the live graph |
| 13 | Cross-regulation intelligence | multi-regulation graph, one bank profile | RBI KYC + PMLA assessed together |
| 14 | Regulatory contradiction detection | `ContradictionAgent` | 2 conflicts, binding requirement named |

### Supporting features (the second half of the feature axis)

| Supporting feature | Implementation |
|---|---|
| Observability | `agent_run` log per agent (input, output, reasoning, tool calls, attempts, duration, model); live SSE stream; state-derived plan reason per step |
| Trainability | `review_decision` store; overrides applied on the next run; learned control vocabulary; held-out agreement reported per run |
| Fault tolerance | anchor recovery → quarantine; `AssuranceAgent` re-runs a failing stage once; degraded status; refusal fallback on model calls |
| Human oversight | `needs_review` queue; attributed reviewer corrections; risk-appetite breach alerts for escalation |
| Configurability | bank profile, business lines, risk appetite, test cycles and assessment date, editable in the UI |
| Security | optional `ACCENTIC_API_TOKEN` on every state-changing call (constant-time compare); uploads checked for type, size, PDF signature and path traversal; bundled corpus read-only |
| Explainability and audit | rationale and source span on every decision; CSV export of registers; printable submission pages; board report from live data; attributed task notes |
| Usability | guided tour of the demo story; Ctrl+K search over pages, obligations, controls, tasks and alerts; plain-English Q&A with verified citations; remediation board with drag and drop |

---

## 7. Nine-blocker position — declared: **D2 / F3**

### Feature axis: **F3** (requires ≥ 8 of 14)

All 14 features are implemented and demonstrated in the running system.

### Depth axis: **D2** (textual/structured input, high demonstrable reliability)

**Why D2 and not D3.** D3 requires *"highly heterogeneous multimodal input."* Accentic
accepts PDF and plain text — both resolve to text. It does not process scanned images, OCR,
embedded tables, diagrams, or audio. Claiming D3 would be overestimation.

**Why D2 and not D1.** D1 asks only for *"acceptable outputs in a majority of situations."*
D2 requires **high demonstrable reliability** — and reliability here is measured:

| Reliability property | Mechanism | Measured result |
|---|---|---|
| Section offsets correct | Re-slice self-audit over every section | **100%** (76/76) |
| Obligation text authentic | Independent re-slice at read time | **100%** (37/37) |
| Unverifiable claims excluded | Anchor failure → recovery → quarantine | 0 unverified rows persisted |
| Non-duties excluded | Applicability screening with stated reason | 5 provisions correctly excluded |
| Coverage not overstated | Independent testing + scope check | 4 failing controls and 1 scope gap denied full coverage |
| Conflicts resolved correctly | Direction-aware binding rule, object check | Tested in `tests/test_chain.py` |
| The chain is internally consistent | `AssuranceAgent`, eight invariants, self-correction | 8/8 hold; self-correction tested |
| Model drafts cannot corrupt the register | Draft validation; deterministic severity and dates | Invalid and invented drafts rejected (tested) |
| Answers cannot cite what the source does not say | Citation anchoring in `QAAgent` | Fabricated citations stripped (tested) |
| A fix is not declared done on say-so | `WorkflowAgent` re-open / verified closure | Tested |
| Every action auditable | `agent_run`: reasoning, tools, retries, duration | 12 agents per run |
| Human oversight path | `needs_review` queue, reviewer corrections | Populated on failure, never silent |
| Behaviour is tested | `tests/test_verification.py`, `tests/test_chain.py` | 14/14 and 29/29 |

**The honest path to D3** is OCR for scanned circulars, table extraction for threshold
schedules, and layout-aware parsing for multi-column gazette PDFs. The offset contract already
generalises to those inputs; only the ingestion front-end changes.

---

## 8. Responsible AI

- **Hallucination:** structurally prevented, not mitigated. Unanchorable claims cannot be stored.
- **Human oversight:** `needs_review` is a first-class table and a first-class UI surface.
- **Provenance:** every obligation renders its char range, re-sliced source text and highlighted
  context, and its full chain through to monitoring.
- **Honest labelling:** the UI shows whether Claude or the rule engine produced each result.
- **Conservatism:** independent testing can only lower a control's effectiveness.
- **Reproducibility:** scoring is deterministic and the assessment date is fixed.
- **No silent drops:** every failure path writes a row with a reason.
- **Privacy:** no customer personal data is processed. Only public regulatory text is sent to
  Claude; the bank's control framework, evidence and profile never leave the machine. Uploaded
  documents stay in `data/uploads/` (git-ignored).
- **Bias and fairness:** no individual or customer is scored. Rules apply identically to every
  obligation, and every exclusion or downgrade states its reason, so a reviewer can contest it.
- **Security:** optional access token on all state-changing calls; validated uploads; the
  bundled corpus cannot be deleted through the API. Not built: user accounts and role-based
  access (see §11).
- **Accountability:** reviewer decisions record who, when and why, and the system's original
  proposal is kept alongside the override.

---

## 9. Business impact

Measured on the bundled corpus (rule engine), per run:

| Measure | Value |
|---|---|
| Obligation × control pairs assessed | 32 × 13 = 416 |
| Control tests re-performed from evidence | 39 |
| Clauses audited against the source | 76 |
| Whole chain, end to end | under one second |

What that buys a bank:

- **Risk reduction.** Critical and high gaps are found, owned and dated before an inspection
  finds them, including an STR filing lag that breaches the 7-day deadline, and the stricter
  3-working-day PMLA requirement that actually binds.
- **Assurance quality.** Controls reported effective by their owners are tested against their own
  evidence; two are contradicted on this corpus.
- **Speed to impact.** A changed regulation is diffed, sized and alerted on the next run rather
  than at the next audit cycle.
- **Productivity (illustrative, assumption stated).** At an assumed 10 minutes per manual
  obligation-to-control assessment and 1 hour per control-test re-performance, one run replaces
  roughly 18 analyst hours on this small corpus; the effort scales linearly with corpus size
  while the run time stays in seconds.

---

## 10. Scalability

| Concern | Current | Production path |
|---|---|---|
| Storage | SQLite | PostgreSQL 16 — `db/schema.sql` mirrors the model, `docker-compose.yml` included |
| Retrieval at corpus scale | Weighted lexical similarity | pgvector embeddings (extension already provisioned) |
| Throughput | Sequential agents | Agents are independent per regulation → parallel / queue |
| Cost | One model call per section | Prompt caching on the shared system prompt; batch API for bulk re-ingest |
| Re-runs | Full rebuild | Source fingerprints already identify what changed; re-process only those clauses |

---

## 11. What is not built or not verified (stated to avoid overestimation)

- OCR / scanned-document and table ingestion (the D3 gap).
- The Claude path is implemented and its contracts are tested with stubbed model output
  (hallucinated quotes, invalid drafts), but the demo figures above come from the rule engine.
- The PostgreSQL schema mirrors SQLite but has not been run end to end; the demo and every test
  use SQLite.
- User accounts and role-based access: there is one optional shared token, not per-user identity.
- Live polling of regulator websites. Change detection runs against files in the corpus:
  edits and new uploads are detected automatically between runs, and the UI polls for drift.
- Authentication, multi-tenancy, and workflow state transitions on remediations.
- Control testing re-performs only what the evidence states numerically; descriptive evidence
  is marked "relied" pending a sample test, not passed.
- The RBI and PMLA files are representative extracts; the pipeline is source-agnostic and runs
  unchanged against the official PDF placed in `data/regulations/` or uploaded in the UI.

---

## 12. Reproducing the demo

```bash
pip install -r requirements.txt
python -m db.migrate
python -m uvicorn api.main:app --port 8000      # open http://localhost:8000 → "Run analysis"
```

Independent verification, outside the UI:

```bash
python -m tests.test_verification
python -m tests.test_chain
python -m extract.verify_offsets --audit     # expect 100%
python -m extract.verify_offsets 38.         # expect PASS
```
