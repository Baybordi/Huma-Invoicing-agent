# Huma — Automated Odoo Invoicing Agent

An AI agent that turns emailed vendor-invoice PDFs into Odoo vendor bills — and
knows when to stop and ask a human. Built for the Human Agent take-home exercise.

```
  inbox (PDFs)  ──▶  extract (LLM + schema guardrail)  ──▶  validate (4 controls)
                                                                   │
                                       ┌───────────────────────────┼───────────────────────────┐
                                  POST │                     REVIEW │                     REJECT │
                                       ▼                            ▼                            ▼
                          confirm bill in Odoo          draft bill in Odoo            do NOT create in Odoo
                          (account.move, posted)        (awaiting human confirm)      (report only)
                                       └──────────── audit note written to Odoo chatter ─────────┘
```

---

## 1. What it does

For every invoice PDF the agent:

1. **Extracts** the required fields (vendor, invoice number, dates, total,
   currency, line items, payment terms, document type) using Claude, then forces
   the output through a Pydantic schema so malformed data can never reach the ERP.
2. **Validates** against the brief's finance controls — completeness,
   approved-supplier, duplicate, and receipt-vs-invoice — and produces a
   three-tier decision with human-readable reasons.
3. **Acts** on that decision:
   - **POST** — clean + approved → create the bill in Odoo *and confirm it* (status: Posted)
   - **REVIEW** — known vendor but needs a human (e.g. a missing field) → create a *draft* bill with the reason in the chatter
   - **REJECT** — receipt / duplicate / unknown vendor → nothing created in Odoo; recorded in the report only

Every Odoo action writes an audit note to the record's chatter.

---

## 2. Design in one paragraph

The core principle is a clean seam between **probabilistic** and **deterministic**
work. The LLM does the one thing it is uniquely good at — reading varied, messy
invoice layouts — and nothing else. Its output is immediately constrained by a
schema (the guardrail), after which every downstream step is ordinary, auditable
Python. Compliance decisions are made by deterministic rules a finance reviewer
can read and trust, not by the model. The agent **fails closed**: any ambiguity
becomes a human-review item rather than an automatic post.

---

## 3. The four stages — what's real vs mocked

| Stage | Status | Notes |
|---|---|---|
| 1. Email monitoring | **Built, not live in demo** | Real IMAP poller in `src/email_poller.py`. Demo runs from a folder (`data/invoices`) — see note below. |
| 2. PDF identification | Real | Filters PDF attachments (in the poller) / `.pdf` files (in the folder). |
| 3. Intelligent parsing | **Real** | Claude extraction + Pydantic guardrail + retry (`src/extract.py`). |
| 4. Odoo integration | **Real** | Posts to `account.move` (vendor bill) via XML-RPC, confirms, and audit-logs (`src/odoo_client.py`). |

**Note on the email stage.** A working IMAP poller is implemented
(`email_poller.py`) and a Gmail SMTP report-sender (`notifier.py`). Both are real
code. During this time-boxed exercise I could not get them running live against
my personal Gmail: the account rejected the app-password login for both IMAP and
SMTP (an account-level Google security restriction, not a code issue — the same
credentials failed identically at both endpoints). Rather than burn the time box
on Google account policy, I kept the demo running from a folder (which the brief
explicitly permits) and left the email code in place. Swapping the real inbox in
is a one-line change in the agent — it calls `iter_invoice_files()` today and
would call `fetch_pdf_attachments()` instead; nothing else in the pipeline changes.

---

## 4. Validation & compliance controls

All controls from the brief are implemented in `src/validate.py` (deterministic,
auditable, non-AI):

1. **Completeness** — every mandatory field present; missing → REVIEW, with the
   exact field named in the reason.
2. **Approved-supplier** — vendor matched against the Approved Supplier List
   (exact, then a guarded fuzzy match); unknown vendor → REJECT (route to
   Supplier Evaluation). Status is also checked — a listed-but-not-Approved
   vendor is rejected too.
3. **Duplicate detection** — by (vendor, invoice number), at **two** levels: an
   in-run set, and an Odoo-level check (`bill_exists`) so re-running the agent
   can't double-post.
4. **Document type** — a receipt is already paid → REJECT (never a payable bill).
5. **Audit logging** — every create writes a transparent note to the Odoo chatter.

A soft currency-consistency check (invoice currency vs the supplier's expected
currency) flags mismatches for review.

---

## 5. Extraction & accuracy (how I know it works)

I don't trust the LLM blindly — extraction is measured, not assumed.

- **Golden dataset** (`evaluation/golden_dataset.json`) — human-verified ground
  truth for all 8 sample invoices, read directly from the PDFs (deliberately
  *not* generated by the model, so I'm not grading the model against itself).
- **Evaluation harness** (`evaluation/run_eval.py`) — reports **field-level
  extraction accuracy**, **pipeline decision accuracy**, and **latency per
  invoice**. Current results: ~98% extraction, 7/8 decisions (the one mismatch is
  a genuine edge case — AWS is marked PAID/card-charged, so the model reads it as
  a receipt; a good example of the eval surfacing real ambiguity).
- **LLM-as-judge** (`evaluation/llm_judge.py`) — for text fields like vendor
  name, when exact-match fails a second model judges semantic equivalence
  ("Atlassian Pty Ltd" ≈ "Atlassian Pty. Limited"), so the score is fair, not
  brittle. Used only on text fields; numbers/dates stay on exact match (cheaper
  and correct). `--judge` flag.
- **HTML report** (`evaluation/html_report.py`) — a standalone, finance-styled
  results page (accuracy cards, regression-gate verdict, per-invoice table).
  `--html` flag.

```bash
python -m evaluation.run_eval --judge --min-extraction 95 --min-decision 80 --html
```

---

## 6. Regression gate & CI

The eval doubles as a **regression gate**: pass `--min-extraction` /
`--min-decision` and it exits non-zero if accuracy drops below threshold. A
GitHub Actions workflow (`.github/workflows/eval.yml`) runs this on every push,
so a prompt or model change that regresses quality **fails the build**. (Requires
an `ANTHROPIC_API_KEY` repository secret.)

This is CI, deliberately not CD — auto-deploying an agent that writes to a
finance system without a human gate would be inappropriate.

---

## 7. Tracing & monitoring

The pipeline is also implemented as a **LangGraph** agent (`src/agent_graph.py`):
the same modules become nodes (extract → validate → conditional route to
post/review/reject), with a typed state object and a visualisable graph
(`docs/agent_graph.md`, rendered Mermaid). The business logic is unchanged — the
LLM still only extracts; validation is still deterministic — so the safety and
auditability are identical, but the structure adds a natural place for retries,
parallelism, and a human-approval interrupt.

**LangSmith (next step, not wired in):** the LangGraph path is structured for
LangSmith tracing — setting `LANGCHAIN_TRACING_V2` and `LANGCHAIN_API_KEY` would
stream per-node traces (inputs, outputs, latency, token cost) to the LangSmith
dashboard with no code change. I scoped it as the next observability step rather
than adding another account/credential dependency inside the time box.

---

## 8. Repository layout

```
src/
  config.py            secrets & settings, loaded from .env (nothing hardcoded)
  schema.py            Pydantic models — the structured-output guardrail
  extract.py           PDF -> Claude -> validated ExtractedInvoice (with retry)
  validate.py          the compliance controls -> POST / REVIEW / REJECT
  odoo_client.py       all Odoo XML-RPC logic (post, draft, audit), isolated
  agent.py             the orchestrator / entry point (plain version)
  agent_graph.py       the same pipeline as a LangGraph agent
  email_poller.py      real IMAP inbox poller (stage 1, built; folder used in demo)
  notifier.py          emails the CSV report via Gmail SMTP (built)
  seed_odoo_vendors.py one-off: load approved suppliers into Odoo (idempotent)
evaluation/
  golden_dataset.json  human-verified expected outputs for all 8 samples
  run_eval.py          accuracy + decision + latency, regression gate, HTML
  llm_judge.py         LLM-as-judge for semantic text-field comparison
  html_report.py       standalone HTML results page
.github/workflows/
  eval.yml             CI regression gate (runs the eval on every push)
data/
  invoices/            sample invoice PDFs (the mocked inbox)
  Approved_Supplier_List.xlsx
docs/
  architecture.md      design, decisions, edge cases, next steps
  agent_graph.md       rendered LangGraph diagram
docker-compose.yml     local Odoo + Postgres
```

---

## 9. Quick start

```bash
# 1. Bring up Odoo locally (http://localhost:8069)
docker compose up -d
#    create a database, install the Invoicing app, generate an API key
#    (Preferences > Account Security).

# 2. Install + configure
pip install -r requirements.txt
cp .env.example .env          # then fill in your real keys

# 3. Seed approved vendors into Odoo (idempotent — safe to re-run)
python -m src.seed_odoo_vendors

# 4. Run the agent on the sample pack
python -m src.agent                    # posts/drafts into Odoo, writes run_report.csv
python -m src.agent --dry-run          # extract + validate only, no Odoo
python -m src.agent_graph              # same pipeline via LangGraph

# 5. Evaluate (accuracy + decision + latency + gate + HTML report)
python -m evaluation.run_eval --judge --min-extraction 95 --min-decision 80 --html
```

Expected on the sample pack: Google / Atlassian / Communere → POST; AWS, GitHub →
REVIEW (missing field); Sentry (receipt), Northwind (new vendor), Atlassian
resend (duplicate) → REJECT. The interesting work is in the holds, not the happy path.

---

## 10. Edge cases & what's next

See `docs/architecture.md` for the full treatment. In brief — handled today:
receipts, duplicates (two levels), unknown vendors, missing fields, vendor naming
variants, currency mismatch, and a junk row in the supplier sheet. Known gaps /
next steps, in priority order: OCR fallback for scanned PDFs; line-item-to-total
reconciliation as an extra control; live email + report delivery (code built,
blocked only by a Google account setting); retry/backoff around Odoo; a real
review queue UI; LangSmith tracing; and the CI gate as a required merge check.

---

## 11. Where I used AI

Extraction is Claude (Anthropic API). I also used Claude as a pair to scaffold and
review this codebase; every design decision, the validation logic, the three-tier
model, and the test cases I drove and verified myself — including catching a
supplier-list parsing bug through the test suite (a trailing note row was being
read as a 12th vendor).