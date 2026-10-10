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

## Run it locally (about 5 minutes)

**You need:** Python 3.10 or newer, and Git. You do **not** need an API key, a database server
or Node: it runs on SQLite, on the official regulation PDFs already in this repository.
Tested from a fresh clone on Windows with Python 3.10 and 3.14.

**1. Get the code**

```bash
git clone https://github.com/thanga-pandian/Accentic.git
cd Accentic
```

No Git? On GitHub choose **Code → Download ZIP**, unzip it, and open a terminal in the folder.

**2. Create a virtual environment and install** (about a minute)

Windows (PowerShell or Command Prompt):

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Create the database and start the app**

```bash
python -m db.migrate
python -m uvicorn api.main:app --port 8000
```

Leave this terminal running.

**4. Open the app and run the analysis**

Open <http://localhost:8000> and click **Run analysis** (top right). A short guided tour starts
on the first visit; you can skip it. The twelve agents take about 30 seconds, and you can watch
each one work. When the run finishes you should see:

| What | Expected |
|---|---|
| Clauses read from the official PDFs | 844, offset audit 100% |
| Obligations | 795, all proven from source; 133 in this review's scope |
| Obligations fully covered by a tested control | 3.8%, against a 60% target |
| Gaps | 128, of which 8 critical |
| Controls failing their tests | 4 of 13 |

**5. Things to try** (each takes under a minute)

- **Trace a finding:** on the Dashboard, click the top priority action. The drawer shows the exact
  sentence highlighted in the regulation, then the chain: applicability, control, evidence, gap
  and remediation.
- **Try to fool it:** Monitoring & proof → **Proof** → *Use a real sentence*, then *Use a tampered
  number*. The tampered sentence is rejected.
- **See an owner contradicted:** Our controls → `CTRL-RPT-040` is self-reported *Effective*, but
  its own evidence gives *Exception*.
- **See a real amendment:** Regulations → **What changed** → RBI ¶11.
- **Ask a question:** open the chat bubble (bottom right). Every citation is checked against the
  regulation before it is shown.

**Optional extras**

- **Today's UN sanctions list:** `python -m agents.sanctions` (needs internet), or **Sync now** on
  Monitoring & proof → Alerts. Until then, monitoring shows the alert *"UN sanctions list has never been synced"*.
- **Claude:** copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. Everything above works
  without it.

**If something goes wrong**

| Problem | Fix |
|---|---|
| `python` is not found, or is older than 3.10 | Windows: `py -3.12 -m venv .venv` (any installed 3.10+). macOS/Linux: use `python3`. |
| PowerShell refuses to run `activate` | Run `Set-ExecutionPolicy -Scope Process Bypass`, then activate again. |
| Windows: *"The filename or extension is too long"* during `pip install` | Clone into a short folder such as `C:\Accentic`; one dependency ships deeply nested files. |
| Port 8000 is already in use | Start with `--port 8001` and open <http://localhost:8001>. |
| You want to start again from nothing | Stop the app, delete `data/accentic.db`, and run `python -m db.migrate`. |

### Official sources

The analysis runs on the official documents, not excerpts. Each file's origin is recorded in
[data/regulations/sources.json](data/regulations/sources.json) and shown on the Documents page.

| File | Publisher | Version |
|---|---|---|
| `rbi-commercial-banks-kyc-directions-2025.pdf` | Reserve Bank of India | Updated as on October 1, 2026 |
| `prior-rbi-commercial-banks-kyc-directions-2025.pdf` | Reserve Bank of India | Updated as on September 18, 2026 (for change detection) |
| `pmla-maintenance-of-records-rules-2005.pdf` | FIU-IND, Ministry of Finance | Consolidated text |

The bank profile ([data/bank/profile.json](data/bank/profile.json)) sets the assessment scope:
RBI ¶20, 38–46, 50–62 and PMLA r.3–5, 7–8, 10. Everything else is still extracted and
traceable, marked *Not in this review*. The bank and its controls are sample data.

The **UN Security Council Consolidated List** is synced from the UN's own XML feed
(`python -m agents.sanctions`, or **Sync now** on the Monitoring page). RBI ¶58 requires daily
verification, so monitoring raises an alert when the last sync is over 24 hours old, and names
any designations added or removed since the previous sync. Schedule the command daily.

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
python -m tests.test_verification      # 16/16: anchoring, recovery, quarantine, cached replay
python -m tests.test_chain             # 37/37: applicability, testing, scope, conflicts, drift,
                                       #   gazette clause numbering, review scope, change
                                       #   detection, sanctions sync, trainability,
                                       #   self-correction, AI-draft validation,
                                       #   remediation re-open/closure, Q&A citation gate
python -m extract.verify_offsets --audit   # expect 100%
python -m extract.verify_offsets 38.       # expect PASS
```

Or attack it live: **Verification & proof → Try to break it**. Paste any sentence. A real one
resolves to its clause and offsets; change a single number and it is rejected.

To see ongoing monitoring work, replace `data/regulations/rbi-commercial-banks-kyc-directions-2025.pdf`
with another version from RBI's
[Previous Versions](https://www.rbi.org.in/Scripts/NotificationPreVersion.aspx?id=13141) page.
Within 15 seconds the UI flags drift; re-run and the Change agent reports each clause that
changed against the version it saw last time. Re-paginated text, moved page numbers and
footnotes are not reported as changes.

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

- `data/regulations/` — the official RBI KYC Directions and PMLA Records Rules PDFs, plus the
  `prior-*` RBI version for change intelligence. `.pdf` (via pdfplumber) or `.txt`.
- `data/controls/` — the bank's internal control framework.
- `data/bank/profile.json` — entity type, jurisdictions, business units, active business lines,
  risk appetite, test cycles and the fixed assessment date. Editable from the UI.
- `data/uploads/` — documents added through the UI (git-ignored).

## Production database

`docker-compose.yml` provisions Postgres 16 with pgvector. Point `DATABASE_URL` at it and run
`python -m db.migrate`; `db/schema.sql` mirrors the SQLite model table for table. (The demo and
all tests run on SQLite; the PostgreSQL path has not been exercised end to end.)
