# Accentic — Agentic Regulatory Compliance

**ET AI Hackathon: Agentic Edition — Problem 1 (Banking/Financial Regulations)**

Eight agents maintain a traceable chain from regulation to remediation:

```
Regulations → obligations → applicability → internal controls → evidence
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
PYTHONPATH=. python -m uvicorn api.main:app --port 8000
```

Open <http://localhost:8000> and press **Run Analysis**.

> **It runs with no API key.** Obligation extraction falls back to a real rule engine
> (modal-verb detection), and the header badge says which mode is live, so rule output is
> never passed off as model output. Set `ANTHROPIC_API_KEY` to route extraction through
> `claude-opus-5-5`.

### Verify the central claim yourself

```bash
PYTHONPATH=. python -m tests.test_verification   # 14/14 — anchoring, recovery, quarantine
python -m extract.verify_offsets --audit         # expect 100%
python -m extract.verify_offsets 38.             # expect PASS
```

Or attack it live: **See the proof → Try to break it**. Paste any sentence. A real one
resolves to its clause and offsets; change a single number and it is rejected. That panel
calls the same gate the pipeline uses.

---

## The three screens

| Screen | What it answers |
|---|---|
| **① What we found** | The real problems in this bank's posture, in plain English, each linked to its proof |
| **② See the proof** | Gaps, requirements, controls, conflicts, changes, the adversarial check, what-if |
| **③ How it works** | The eight agents with their reasoning and tool calls, plus the architecture |

---

## Agents

| Agent | Responsibility |
|---|---|
| `IngestionAgent` | Parse documents, compute offsets, self-audit every slice |
| `ObligationAgent` | Extract obligations, anchor each to source, recover or quarantine |
| `ControlAgent` | Parse the bank control framework, grade effectiveness, link evidence |
| `MappingAgent` | Map obligations to controls, discounted by measured effectiveness |
| `GapAgent` | Score gaps by impact × likelihood, generate prioritised remediation |
| `ContradictionAgent` | Detect conflicting periods, thresholds and modalities across regulations |
| `ChangeAgent` | Diff against the prior version and name the numeric delta |
| `SimulationAgent` | What-if blast radius across the live obligation graph |

---

## Layout

```
agents/          the eight agents, llm client, orchestrator
extract/         sectioner (offsets) and the offset verifier
db/              schema, connection, migration    (SQLite dev · Postgres prod)
api/main.py      FastAPI + SSE stream of the live run
web/index.html   single-file UI — no framework, no CDN, no build step
prompts/         the extraction prompt, loaded at runtime
tests/           verification-contract tests
data/            regulations, prior versions, bank control framework
```

## Inputs

`data/regulations/` holds RBI KYC and PMLA extracts plus a `prior-*` version used for change
intelligence. `data/controls/` holds the bank's internal control framework. The sectioner
accepts `.pdf` (via pdfplumber) or `.txt`; drop the official PDF in and the pipeline runs on
it unchanged.

## Production database

`docker-compose.yml` provisions Postgres 16 with pgvector. Point `DATABASE_URL` at it and
`db/schema.sql` applies the same model; nothing else changes.
