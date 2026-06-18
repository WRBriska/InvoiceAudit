"""
evaluate_extraction.py — the scorecard for the LLM extraction front-end.

The main eval (evaluate.py) proves the rule engine is correct given clean input.
This one measures the other half: how reliably the LLM turns a messy printed line
back into the correct structured fields. That is where real-world recall actually
lives, so it gets its own honest number instead of being folded into the audit score.

Method: take each structured synthetic invoice (the ground truth), render its lines
to messy text via extract.render_invoice_to_text, extract them back through the
active LLM client, and compare field-by-field against the original. Offline (mock
client) this is a deterministic baseline; set ANTHROPIC_API_KEY and the same harness
scores a real model with no other change.

Scored fields:
  - line recall      : did we recover a line for each ref that exists?
  - type             : linehaul vs accessorial
  - amount           : the dollar figure (copied, never recomputed) matches
  - stated_class     : linehaul only
  - stated_weight_lb : linehaul only
  - units            : accessorial only
A line is "fully correct" iff every field applicable to its type matches.
"""

import json
from collections import defaultdict

from extract import render_invoice_to_text, extract_lines_from_text
from llm_client import get_client

FIELDS_BY_TYPE = {
    "linehaul": ["type", "amount", "stated_class", "stated_weight_lb"],
    "accessorial": ["type", "amount", "units"],
}


def _truth_fields(line):
    f = {"type": line["type"], "amount": round(line["amount"], 2)}
    if line["type"] == "linehaul":
        f["stated_class"] = line["stated_class"]
        f["stated_weight_lb"] = line["stated_weight_lb"]
    else:
        f["units"] = line.get("units", 1)
    return f


def evaluate_extraction(invoices_path="synthetic_invoices.jsonl", limit=None):
    client = get_client()
    invoices = [json.loads(l) for l in open(invoices_path)]
    if limit:
        invoices = invoices[:limit]

    total_lines = recovered = fully_correct = 0
    field_correct = defaultdict(int)
    field_total = defaultdict(int)

    for inv in invoices:
        text = render_invoice_to_text(inv)
        got = {g.get("ref"): g for g in extract_lines_from_text(text, client)}
        for line in inv["lines"]:
            total_lines += 1
            truth = _truth_fields(line)
            g = got.get(line["ref"])
            if not g:
                for fld in FIELDS_BY_TYPE[line["type"]]:
                    field_total[fld] += 1
                continue
            recovered += 1
            line_ok = True
            for fld in FIELDS_BY_TYPE[line["type"]]:
                field_total[fld] += 1
                if g.get(fld) == truth.get(fld):
                    field_correct[fld] += 1
                else:
                    line_ok = False
            if line_ok:
                fully_correct += 1

    report = {
        "client": type(client).__name__,
        "invoices": len(invoices),
        "lines": total_lines,
        "line_recall": round(recovered / total_lines, 3) if total_lines else 0.0,
        "line_full_accuracy": round(fully_correct / total_lines, 3) if total_lines else 0.0,
        "field_accuracy": {
            f: round(field_correct[f] / field_total[f], 3)
            for f in sorted(field_total) if field_total[f]
        },
    }
    return report


def print_report(r):
    W = 64
    print("=" * W)
    print(f"EXTRACTION FRONT-END — RECALL  (client: {r['client']})")
    print("=" * W)
    print(f"  invoices {r['invoices']:>5}   lines {r['lines']:>5}")
    print(f"  line recall            {r['line_recall']:.3f}")
    print(f"  line full-field acc.   {r['line_full_accuracy']:.3f}")
    print("-" * W)
    print(f"  {'field':20s} {'accuracy':>8}")
    for f, v in r["field_accuracy"].items():
        print(f"  {f:20s} {v:>8.3f}")
    print("=" * W)


if __name__ == "__main__":
    r = evaluate_extraction()
    print_report(r)
    with open("extraction_report.json", "w") as f:
        json.dump(r, f, indent=2)
