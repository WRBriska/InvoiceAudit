"""run_audit.py — audit every invoice through the LangGraph pipeline."""

import json
import pricing
from audit_graph import build_graph


def main(invoices_path="synthetic_invoices.jsonl", out_path="audits.jsonl"):
    rt = pricing.load_rates()
    graph = build_graph()
    n = 0
    with open(invoices_path) as fin, open(out_path, "w") as fout:
        for line in fin:
            inv = json.loads(line)
            out = graph.invoke({"rt": rt, "invoice": inv, "classified": {},
                                "audit": {}, "review_queue": [], "summary": ""})
            rec = {
                "invoice_id": inv["invoice_id"],
                "findings": out["audit"]["findings"],
                "total_overbilled": out["audit"]["total_overbilled"],
                "review_queue": out["review_queue"],
                "summary": out["summary"],
            }
            fout.write(json.dumps(rec) + "\n")
            n += 1
    print(f"audited {n} invoices -> {out_path}")


if __name__ == "__main__":
    main()
