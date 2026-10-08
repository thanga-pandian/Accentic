# Accentic — Agentic Regulatory Compliance

**ET AI Hackathon: Agentic Edition — Problem 1 (Banking/Financial Regulations)**

Twelve agents maintain a continuously evolving, traceable chain from regulation to remediation:
one agent per link the problem statement defines, a self-assurance agent that checks the
chain's own invariants, and a workflow agent that closes the remediation loop on evidence:

```
Regulations → obligations → applicability → internal policies/controls → evidence
            → testing → gaps → remediation → ongoing monitoring
```

The differentiator is that **every claim resolves to an exact character range in a source
document**, and the system proves it by re-slicing the original rather than trusting its own
record. A model may decide *what* is an obligation; Python decides *where* it exists. A quote
that cannot be located is rejected as a hallucination, retried, then quarantined for a human.

Submission components: this demo, and [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # optional: add ANTHROPIC_API_KEY
python -m db.migrate
python -m uvicorn api.main:app --port 8000
```

Open <http://localhost:8000> and press **Run analysis**.

> **It runs with no API key.** Obligation extraction falls back to a real rule engine
> (modal-verb detection) and remediations come from a control-design playbook. The header
> badge reports what *actually* produced the results, so rule output is never passed off as
> model output. Set `ANTHROPIC_API_KEY` to route obligation extraction and remediation drafting
> through `claude-opus-5-5` (with server-side refusal fallback). Every model output is still
> verified before it is stored.
>
> **Optional access control.** Set `ACCENTIC_API_TOKEN` and every state-changing call
> (uploads, profile edits, reviewer decisions, runs) requires it; the UI asks for it once.

### Verify the claims yourself

```bash
python -m tests.test_verification      # 14/14: anchoring, recovery, quarantine
python -m tests.test_chain             # 29/29: applicability, testing, scope, conflicts, drift,
                                       #   trainability, self-correction, AI-draft validation,
                                       #   remediation re-open/closure, Q&A citation gate
python -m extract.verify_offsets --audit   # expect 100%
python -m extract.verify_offsets 38.       # expect PASS
```

Or attack it live: **Verification & proof → Try to break it**. Paste any sentence. A real one
resolves to its clause and offsets; change a single number and it is rejected.

To see ongoing monitoring work, edit a regulation in `data/regulations/` (for example change
`within 7 days` to `within 5 days`). Within 15 seconds the UI flags drift; re-run and the
Change agent reports `period 7 days → 5 days` against the version it saw last time.

---

## The workbench

The sidebar has six places, each named for the question it answers. The active place expands to
show its pages, which also appear as tabs at the top of the screen.

| Place | Answers | Pages (tabs) |
|---|---|---|
| Dashboard | Our compliance at a glance | Summary: "What would you like to do?" launcher, KPIs, the nine-link chain, key findings, risk heatmap |
| Ask Accentic | Ask in plain English | Q&A with every quoted citation verified against the regulation |
| Issues & actions | What's wrong, who's fixing it | Issues (gap register) · Action board (owner, status, SLA, notes) · What-if |
| Regulations | What the rules require | Requirements · What changed · Conflicts · Documents (upload, fingerprints, clause index) |
| Our controls | How well we meet the rules | Controls & tests · Coverage map · Bank profile (applicability) |
| Monitoring & proof | Alerts, proof, audit trail | Alerts (KRIs, drift) · Proof (adversarial check, invariants, review queue) · Reviewer feedback · Agent activity |
| *For judges:* Hackathon submission | Problem statement coverage | Problem coverage (chain, 14 features, nine-blocker, evaluation criteria) · Architecture |

**Help & glossary** (the ? button) explains every place and every term in plain words, and
hovering any status chip (Scope gap, Exception, Applicable…) shows what it means. The sidebar
collapses to an icon rail.

Selecting any obligation, gap or finding opens its full traceability chain: source text with the
exact span highlighted, applicability, control, evidence, independent tests, gap, remediation
and monitoring alerts. A reviewer can correct the applicability or mapping decision right there;
the decision is attributed, applied on the next run, and learned for similar obligations.
Registers export to CSV, the submission pages print to PDF, and there are light and dark themes.

- **Board report:** the header's report button opens an executive compliance pack built from
  live data (`/api/report`): print or save as PDF, or download for Word (`?format=doc`).
- **Guided tour:** runs on first visit (or press the compass icon) through the demo story.
- **Ctrl+K:** search every page, obligation, control, task and alert; recently opened items first.

---

## Agents

| Agent | Chain link | Responsibility |
|---|---|---|
| `IngestionAgent` | Regulations | Parse documents, compute offsets, self-audit every slice |
| `ObligationAgent` | Obligations | Extract obligations, anchor each to source, recover or quarantine |
| `ApplicabilityAgent` | Applicability | Screen out definitions and permissive clauses; scope to the bank's profile; assign business units |
| `ControlAgent` | Controls, evidence | Parse the control framework (text or PDF), link evidence |
| `TestingAgent` | Testing | Re-perform design, operating and currency tests from the evidence, not the owner's result |
| `MappingAgent` | Mapping | Map obligations to controls, discounted by tested effectiveness, capped on scope gaps; apply and learn from reviewer corrections |
| `GapAgent` | Gaps, remediation | Score gaps by impact × likelihood; Claude drafts a tailored remediation per gap (validated), playbook fallback |
| `ContradictionAgent` | Cross-regulation | Detect conflicting periods, thresholds and modalities; name the binding requirement |
| `ChangeAgent` | Change intelligence | Diff against the previous run or a prior version; name the numeric delta |
| `AssuranceAgent` | Self-assurance | Check eight chain invariants; re-run a failing stage once, else quarantine |
| `WorkflowAgent` | Remediation tracking | Open a task per new gap; re-open tasks marked done whose gap persists; verify closures |
| `MonitoringAgent` | Ongoing monitoring | Fingerprint sources across runs, raise alerts, record KRIs, check risk appetite |
| `SimulationAgent` | What-if | Blast radius across the live obligation graph, on demand |
| `QAAgent` | Ask Accentic | Retrieve clauses and findings, answer, anchor every quoted citation or strip it, on demand |

---

## Layout

```
agents/          the agents, llm client, bank profile loader, orchestrator
extract/         sectioner (offsets) and the offset verifier
db/              schema, connection, migration    (SQLite dev · Postgres prod, same model)
api/main.py      FastAPI + SSE stream of the live run
web/index.html   single-file UI — no framework, no CDN, no build step
prompts/         the extraction prompt, loaded at runtime
tests/           verification-contract and chain tests (no pytest needed)
data/            regulations, prior versions, control framework, bank profile, uploads
```

## Inputs

- `data/regulations/` — RBI KYC and PMLA extracts plus a `prior-*` version for change
  intelligence. `.pdf` (via pdfplumber) or `.txt`.
- `data/controls/` — the bank's internal control framework.
- `data/bank/profile.json` — entity type, jurisdictions, business units, active business lines,
  risk appetite, test cycles and the fixed assessment date. Editable from the UI.
- `data/uploads/` — documents added through the UI (git-ignored).

## Production database

`docker-compose.yml` provisions Postgres 16 with pgvector. Point `DATABASE_URL` at it and run
`python -m db.migrate`; `db/schema.sql` mirrors the SQLite model table for table. (The demo and
all tests run on SQLite; the PostgreSQL path has not been exercised end to end.)
