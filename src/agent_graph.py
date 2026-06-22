"""
agent_graph.py — The same invoicing pipeline, expressed as a LangGraph agent.

This is an ALTERNATIVE entry point to agent.py. It reuses the exact same building
blocks (extract, validate, odoo_client) — the business logic is unchanged — but
wires them as nodes in a LangGraph StateGraph with conditional routing on the
POST / REVIEW / REJECT decision.

Why have both:
  - agent.py  : plain, explicit orchestration. Zero extra dependencies. The
                version I demo, because the control flow is auditable at a glance.
  - agent_graph.py : the same flow as a graph. Adds a typed, inspectable state
                object, a visualisable graph (nodes + conditional edges), and a
                natural place to add retries, parallelism, and a human-approval
                interrupt as the workflow grows.

The key point for review: moving to LangGraph did NOT move any decision into the
LLM or weaken the controls. Validation is still the same deterministic function;
it simply runs inside a node, and a router reads its result to pick the next node.
That keeps the safety and auditability while gaining the framework's structure.

Run:
    python -m src.agent_graph                 # process the folder inbox
    python -m src.agent_graph --dry-run        # extract + validate only
    python -m src.agent_graph --print-graph    # show the graph structure (ASCII)
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import TypedDict

from anthropic import Anthropic
from langgraph.graph import END, START, StateGraph

from .config import Settings, load_settings
from .extract import extract_invoice, read_pdf_text
from .odoo_client import OdooClient
from .schema import ExtractedInvoice
from .validate import (
    ApprovedSupplier,
    Decision,
    load_approved_suppliers,
    validate_invoice,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s"
)
logger = logging.getLogger("agent_graph")


# --- Shared state passed between nodes ---------------------------------------
class AgentState(TypedDict, total=False):
    # inputs / context
    pdf_path: str
    source_file: str
    settings: Settings
    client: Anthropic
    approved: dict[str, ApprovedSupplier]
    seen: set
    dry_run: bool
    odoo: OdooClient | None

    # produced as the graph runs
    invoice: ExtractedInvoice
    decision: str
    reasons: list[str]
    odoo_id: int
    odoo_status: str
    error: str


# --- Nodes (each reuses your existing modules) -------------------------------
def extract_node(state: AgentState) -> dict:
    """Stage 3: read the PDF and extract a validated invoice."""
    try:
        text = read_pdf_text(state["pdf_path"])
        invoice = extract_invoice(
            text, state["client"], state["settings"], source_file=state["source_file"]
        )
        return {"invoice": invoice}
    except Exception as exc:
        return {
            "decision": Decision.REVIEW.value,
            "reasons": [f"Extraction failed: {exc}"],
            "odoo_status": "not created",
            "error": str(exc),
        }


def validate_node(state: AgentState) -> dict:
    """Stage 4a: run the deterministic business rules -> POST / REVIEW / REJECT."""
    if "invoice" not in state:  # extraction already failed
        return {}
    invoice = state["invoice"]
    result = validate_invoice(invoice, state["approved"], state["seen"])
    state["seen"].add((invoice.vendor.strip().lower(), invoice.invoice_number))
    return {"decision": result.decision.value, "reasons": result.reasons}


def post_node(state: AgentState) -> dict:
    """POST: create AND confirm the bill in Odoo."""
    if state.get("dry_run"):
        return {"odoo_status": "dry-run (not sent)"}
    odoo = state["odoo"] or OdooClient(state["settings"])
    state["odoo"] = odoo
    res = odoo.post_bill(state["invoice"])
    if res.ok:
        return {"odoo_id": res.bill_id, "odoo_status": "posted"}
    # Odoo refused (e.g. already exists) -> becomes a review item.
    return {
        "decision": Decision.REVIEW.value,
        "reasons": state.get("reasons", []) + [res.error],
        "odoo_status": "not created",
    }


def review_node(state: AgentState) -> dict:
    """REVIEW: create a DRAFT bill in Odoo for a human to confirm."""
    if state.get("dry_run"):
        return {"odoo_status": "dry-run (not sent)"}
    odoo = state["odoo"] or OdooClient(state["settings"])
    state["odoo"] = odoo
    res = odoo.create_draft_for_review(state["invoice"], state.get("reasons", []))
    if res.ok:
        return {"odoo_id": res.bill_id, "odoo_status": "draft"}
    return {
        "reasons": state.get("reasons", []) + [res.error],
        "odoo_status": "not created",
    }


def reject_node(state: AgentState) -> dict:
    """REJECT: receipt / duplicate / unknown vendor -> nothing created in Odoo."""
    return {"odoo_status": "not created"}


# --- Router: reads the decision and picks the next node ----------------------
def route_decision(state: AgentState) -> str:
    """Conditional edge. Pure lookup: reads state['decision'], returns a node name.
    No computation here — the decision was already made in validate_node."""
    decision = state.get("decision", Decision.REVIEW.value)
    if decision == Decision.POST.value:
        return "post"
    if decision == Decision.REJECT.value:
        return "reject"
    return "review"  # REVIEW, or any extraction-failure fallback


def build_graph():
    """Wire the nodes and edges into a compiled, runnable graph."""
    g = StateGraph(AgentState)
    g.add_node("extract", extract_node)
    g.add_node("validate", validate_node)
    g.add_node("post", post_node)
    g.add_node("review", review_node)
    g.add_node("reject", reject_node)

    g.add_edge(START, "extract")
    g.add_edge("extract", "validate")
    g.add_conditional_edges(
        "validate",
        route_decision,
        {"post": "post", "review": "review", "reject": "reject"},
    )
    g.add_edge("post", END)
    g.add_edge("review", END)
    g.add_edge("reject", END)
    return g.compile()


def iter_invoice_files(folder: str) -> list[str]:
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".pdf")
    )


def run(invoices_dir: str, supplier_list_path: str, dry_run: bool = False) -> None:
    settings = load_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    approved = load_approved_suppliers(supplier_list_path)
    graph = build_graph()

    seen: set = set()
    odoo_holder: dict = {"odoo": None}  # share one connection across invoices

    print("\n" + "=" * 72)
    print("INVOICING AGENT (LangGraph) — RUN SUMMARY")
    print("=" * 72)

    for path in iter_invoice_files(invoices_dir):
        filename = os.path.basename(path)
        initial: AgentState = {
            "pdf_path": path,
            "source_file": filename,
            "settings": settings,
            "client": client,
            "approved": approved,
            "seen": seen,
            "dry_run": dry_run,
            "odoo": odoo_holder["odoo"],
        }
        final = graph.invoke(initial)
        odoo_holder["odoo"] = final.get("odoo")  # reuse connection next loop

        status = final.get("odoo_status", "?")
        if status == "posted":
            line = f"POSTED (Odoo #{final.get('odoo_id')}, confirmed)"
        elif status == "draft":
            line = f"DRAFT (Odoo #{final.get('odoo_id')}, awaiting review)"
        else:
            line = f"{final.get('decision')} ({status})"
        print(f"\n• {filename}")
        print(f"    vendor : {final.get('invoice').vendor if final.get('invoice') else None}")
        print(f"    result : {line}")
        for r in final.get("reasons", []):
            print(f"      - {r}")

    print("\n" + "=" * 72 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Huma invoicing agent (LangGraph)")
    parser.add_argument("--invoices", default="data/invoices")
    parser.add_argument("--suppliers", default="data/Approved_Supplier_List.xlsx")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--print-graph",
        action="store_true",
        help="Print the graph structure (ASCII) and exit.",
    )
    parser.add_argument(
        "--save-diagram",
        action="store_true",
        help="Write the graph as a Mermaid diagram to docs/agent_graph.md and exit.",
    )
    args = parser.parse_args()

    if args.save_diagram:
        graph = build_graph()
        mermaid = graph.get_graph().draw_mermaid()
        os.makedirs("docs", exist_ok=True)
        out_path = os.path.join("docs", "agent_graph.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# Invoicing Agent — LangGraph Structure\n\n")
            f.write("Generated from the compiled LangGraph. Renders automatically on GitHub.\n\n")
            f.write("```mermaid\n")
            f.write(mermaid)
            f.write("```\n")
        print(f"Wrote Mermaid diagram to {out_path}")
        return

    if args.print_graph:
        graph = build_graph()
        try:
            print(graph.get_graph().draw_ascii())
        except Exception:
            # draw_ascii needs an optional dep; fall back to a simple description.
            print("extract -> validate -> {post | review | reject} -> END")
        return

    run(args.invoices, args.suppliers, dry_run=args.dry_run)


if __name__ == "__main__":
    main()