# Huma — Automated Odoo Invoicing Agent

An AI agent that turns emailed vendor-invoice PDFs into Odoo vendor bills — and
knows when to stop and ask a human. Built for the Human Agent take-home.

```
  inbox (PDFs)  ──▶  extract (LLM + schema guardrail)  ──▶  validate (4 controls)
                                                                   │
                                              ┌────────────────────┴───────────────┐
                                          POST │                                    │ FLAG
                                              ▼                                     ▼
                                  create bill in Odoo                    queue for human review
                                  (account.move) + audit note            with a clear reason
```

## What it does

For every invoice PDF it:

1. **Extracts** the required fields (vendor, invoice number, dates, total,
   currency, line items, payment terms, document type) using Claude, then forces
   the output through a Pydantic schema so malformed data can never reach the ERP.
2. **Validates** against the brief's four finance controls — completeness,
   approved-supplier, duplicate, and receipt-vs-invoice — and produces a
   `POST` or `FLAG` decision with human-readable reasons.
3. **Acts**: posts approved, complete invoices to Odoo as vendor bills with an
   audit note in the chatter; everything else is flagged for a human, never
   silently posted.

## Design in one paragraph

The core principle is a clean seam between *probabilistic* and *deterministic*
work. The LLM does the one thing it is uniquely good at — reading varied,
messy invoice layouts — and nothing else. Its output is immediately constrained
by a schema (the guardrail), after which every downstream step is ordinary,
auditable Python. Compliance decisions are made by deterministic rules a finance
reviewer can read and trust, not by the model. The agent **fails closed**: any
ambiguity becomes a human-review FLAG rather than an automatic post.

## Repository layout

```
src/
  config.py            secrets & settings, loaded from .env (nothing hardcoded)
  schema.py            Pydantic models — the structured-output guardrail
  extract.py           PDF → Claude → validated ExtractedInvoice (with retry)
  validate.py          the 4 business-rule controls → POST / FLAG decision
  odoo_client.py       all Odoo XML-RPC logic, isolated behind one class
  agent.py             the orchestrator / control loop (entry point)
  seed_odoo_vendors.py one-off: load approved suppliers into Odoo
evaluation/
  golden_dataset.json  human-verified ground truth for all 8 samples
  run_eval.py          field-level + pipeline accuracy vs. the golden set
data/
  invoices/            sample invoice PDFs (the mocked inbox)
  Approved_Supplier_List.xlsx
docs/
  architecture.md      diagram, decisions, edge cases, next steps
docker-compose.yml     local Odoo + Postgres
```

## Quick start

```bash
# 1. Bring up Odoo locally (Odoo at http://localhost:8069)
docker compose up -d
#    create a database named 'huma_demo', install the Invoicing app,
#    and generate an API key under Preferences → Account Security.

# 2. Install + configure
pip install -r requirements.txt
cp .env.example .env          # then fill in your real keys

# 3. Seed approved vendors into Odoo (idempotent)
python -m src.seed_odoo_vendors

# 4. Run the agent on the sample pack
python -m src.agent                  # posts to Odoo
python -m src.agent --dry-run        # extract + validate only, no Odoo

# 5. Measure extraction & decision accuracy against the golden set
python -m evaluation.run_eval
```

## Expected behaviour on the sample pack

| Invoice | Outcome | Why |
|---|---|---|
| Google Cloud, Atlassian, Communere | **POST** | approved, complete |
| AWS | **FLAG** | no due date printed (auto-charged card) |
| Sentry | **FLAG** | it's a receipt — already paid |
| Northwind | **FLAG** | vendor not on Approved Supplier List → SE review |
| Atlassian (resend) | **FLAG** | duplicate invoice number |
| GitHub | **FLAG** | no invoice number on the document |

The pack is deliberately adversarial; the interesting work is in the flags, not
the happy path.

## What is real vs. mocked

- **Real**: Odoo instance, vendor-bill creation via API, audit-log note,
  Claude extraction, all validation logic, the evaluation harness.
- **Mocked**: the email inbox is a folder of PDFs (`data/invoices`). The brief
  explicitly permits this. Only `iter_invoice_files` in `agent.py` would change
  to swap in a real IMAP/Microsoft-Graph poller — the rest of the pipeline is
  untouched. That isolation is intentional.

See [`docs/architecture.md`](docs/architecture.md) for the deeper design,
edge-case handling, and what I'd build next with more time.

## Where I used AI

The extraction stage is Claude (Anthropic API). I also used Claude as a pair to
scaffold and review this codebase; every design decision, the validation logic,
and the test cases I drove and verified myself — including catching a supplier-
list parsing bug via the test suite (a trailing note row was being read as a
12th vendor).
