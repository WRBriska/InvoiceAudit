"""
extract.py — the messy-text / text-PDF extraction front-end.

This is the seam the README roadmap called the "real test of LLM recall." The
deterministic engine already catches every planted error given clean structured
input; the open question in any real deployment is whether the LLM can turn a
messy printed invoice line into those clean fields. This module is where that
happens, and `evaluate_extraction.py` measures exactly how well.

The boundary, kept honest:
  - LINE CHARGES arrive as messy text (what a carrier PDF actually looks like) and
    are recovered by the LLM via client.extract_line().
  - SHIPMENT CONTEXT (dock flags, residential, lane, ship_date, customer) is
    assumed to arrive structured, the way it does from an EDI 210 / manifest. We
    don't pretend to OCR it out of prose. Conflating the two would overstate the
    front-end's reach.

As everywhere in this repo: the model reads, Python computes. extract_line()
copies the stated dollar amount verbatim — it never recomputes a charge.
"""

import random
import re

from llm_client import get_client


def render_invoice_to_text(inv, seed=None):
    """Render a structured invoice's line items as the kind of messy text a
    carrier PDF would carry. Used to (a) demo the front-end and (b) generate
    inputs whose correct extraction is known, so recall is measurable.

    Mild, seeded variation in formatting keeps this from being a fixed-format
    parse — the point is that a *language* model handles the variation.
    """
    rng = random.Random(seed if seed is not None else inv["invoice_id"])
    lines = []
    for ln in inv["lines"]:
        ref = ln["ref"]
        amt = f"${ln['amount']:.2f}"
        leader = "." * rng.randint(3, 18)
        if ln["type"] == "linehaul":
            cls = ln["stated_class"].replace("class_", "")
            wt = ln["stated_weight_lb"]
            cls_tok = rng.choice([f"class {cls}", f"cls {cls}", f"cl{cls}"])
            wt_tok = rng.choice([f"{wt} lb", f"{wt}lbs", f"{wt}#", f"{wt:,} lb"])
            label = rng.choice(["LINEHAUL", "Line Haul", "Linehaul Chg"])
            lines.append(f"{ref}  {label}  {cls_tok}  {wt_tok} {leader} {amt}")
        else:
            desc = ln.get("description") or ln.get("code", "MISC")
            units = ln.get("units", 1)
            qty_tok = rng.choice([f"x{units}", f"qty {units}", f"units {units}"]) if units != 1 \
                else rng.choice(["", "x1"])
            lines.append(f"{ref}  {desc}  {qty_tok} {leader} {amt}".replace("  .", " ."))
    header = (f"{inv['invoice_id']} | {inv['customer_id']} | {inv['lane']} | "
              f"ship {inv['ship_date']}")
    return header + "\n" + "\n".join(lines)


def extract_lines_from_text(text, client=None):
    """LLM-extract the line items from a rendered invoice's text body.

    Returns a list of structured line dicts in the same shape the audit engine
    consumes. The header line is skipped (shipment context comes structured).
    """
    client = client or get_client()
    out = []
    for raw in text.splitlines():
        if not raw.strip() or "|" in raw:  # header / blank
            continue
        if not re.match(r"\s*[A-Za-z]\d+\b", raw):  # not a line-item row
            continue
        fields = client.extract_line(raw)
        if fields.get("ref"):
            out.append(fields)
    return out


def build_invoice_from_text(text, context):
    """Combine LLM-extracted line items with structured shipment context into a
    full invoice ready for the audit graph.

    `context` carries everything that isn't on the printed charge lines:
    invoice_id, customer_id, lane, ship_date, origin, dest, shipment.
    """
    lines = extract_lines_from_text(text)
    inv = dict(context)
    inv["lines"] = lines
    return inv


def _demo():
    """Show one invoice make the full trip: structured -> messy text -> LLM
    extraction -> deterministic audit. Proves the text path yields the same
    verdict as the structured path."""
    import json
    import pricing
    from audit_graph import run_one

    inv = next(json.loads(l) for l in open("synthetic_invoices.jsonl"))
    rt = pricing.load_rates()
    text = render_invoice_to_text(inv)
    context = {k: inv[k] for k in
               ("invoice_id", "customer_id", "ship_date", "lane", "origin", "dest", "shipment")}

    print("--- rendered messy invoice text (the LLM's input) ---")
    print(text)
    print("\n--- LLM-extracted structured lines ---")
    for ln in extract_lines_from_text(text):
        print(" ", ln)

    text_invoice = dict(context, raw_text=text)
    out = run_one(rt, text_invoice)
    print("\n--- deterministic audit verdict (text path) ---")
    print(" summary:", out["summary"])
    print(" total overbilled: $%.2f" % out["total_overbilled"])


if __name__ == "__main__":
    _demo()

