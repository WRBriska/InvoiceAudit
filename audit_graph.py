"""
audit_graph.py — the LangGraph pipeline.

Nodes (LLM boundary marked):
  extract   [LLM*] structured invoice -> working lines      (*passthrough for JSON input)
  classify  [LLM ] normalize each line description -> code + confidence
  audit     [det ] deterministic pricing & rule checks  (pricing.audit_invoice)
  triage    [det ] downgrade/divert low-confidence findings to human review
  report    [LLM ] human-readable summary

State flows as a typed dict. The graph is intentionally linear and legible;
the value is in WHICH node owns money (only `audit`, and it never calls the LLM).
"""

from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, START, END

import pricing
from llm_client import get_client

CONFIDENCE_FLOOR = 0.6  # below this, a line's classification is not trusted


class AuditState(TypedDict):
    rt: Dict[str, Any]
    invoice: Dict[str, Any]
    classified: Dict[str, float]     # line_ref -> classification confidence
    audit: Dict[str, Any]
    review_queue: List[Dict[str, Any]]
    summary: str


def node_extract(state: AuditState) -> AuditState:
    # Synthetic invoices are already structured, so extraction is a passthrough.
    # A real text/PDF front-end would populate state["invoice"] here via the LLM.
    return state


def node_classify(state: AuditState) -> AuditState:
    llm = get_client()
    inv = state["invoice"]
    conf = {}
    catalog = set(state["rt"]["accessorials"].keys())
    for line in inv["lines"]:
        if line["type"] != "accessorial":
            conf[line["ref"]] = 1.0
            continue
        code, c = llm.normalize_line(line.get("description"), line.get("code"))
        line["code"] = code            # the normalized code the audit engine will use
        # A line that maps to NO catalog code isn't ambiguous — it's a confident
        # phantom (charge with no contractual basis). Don't bury it in review.
        if code not in catalog:
            conf[line["ref"]] = 1.0
        else:
            conf[line["ref"]] = c
    state["classified"] = conf
    return state


def node_audit(state: AuditState) -> AuditState:
    state["audit"] = pricing.audit_invoice(state["rt"], state["invoice"])
    return state


def node_triage(state: AuditState) -> AuditState:
    """Findings resting on low-confidence classification go to human review,
    not to the customer as an assertion. Precision protection."""
    conf = state["classified"]
    keep, review = [], []
    for f in state["audit"]["findings"]:
        low_classify = conf.get(f["line_ref"], 1.0) < CONFIDENCE_FLOOR
        low_finding = f.get("confidence") == "low"
        if low_classify or low_finding:
            f["confidence"] = "low"
            review.append(f)
        else:
            keep.append(f)
    state["audit"]["findings"] = keep
    state["audit"]["total_overbilled"] = round(
        sum(f["dollar_impact"] for f in keep if f["dollar_impact"] > 0), 2)
    state["review_queue"] = review
    return state


def node_report(state: AuditState) -> AuditState:
    llm = get_client()
    state["summary"] = llm.summarize_findings(
        state["invoice"]["invoice_id"], state["audit"]["findings"])
    return state


def build_graph():
    g = StateGraph(AuditState)
    g.add_node("extract", node_extract)
    g.add_node("classify", node_classify)
    g.add_node("audit", node_audit)
    g.add_node("triage", node_triage)
    g.add_node("report", node_report)
    g.add_edge(START, "extract")
    g.add_edge("extract", "classify")
    g.add_edge("classify", "audit")
    g.add_edge("audit", "triage")
    g.add_edge("triage", "report")
    g.add_edge("report", END)
    return g.compile()


def run_one(rt, invoice):
    graph = build_graph()
    out = graph.invoke({"rt": rt, "invoice": invoice, "classified": {},
                        "audit": {}, "review_queue": [], "summary": ""})
    return {
        "invoice_id": invoice["invoice_id"],
        "findings": out["audit"]["findings"],
        "total_overbilled": out["audit"]["total_overbilled"],
        "review_queue": out["review_queue"],
        "summary": out["summary"],
    }
