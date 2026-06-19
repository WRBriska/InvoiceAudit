"""
evaluate_golden.py — the INDEPENDENT oracle.

Why this exists
---------------
The main scorecard (evaluate.py) scores the audit engine against answer_keys.jsonl,
which generate_invoices.py produces using the SAME pricing arithmetic the engine
itself runs (compute_linehaul == pricing.expected_linehaul, etc.). That makes the
dollar metrics circular: a 1.000 there means "my two copies of the formula agree,"
not "the engine is correct." A shared bug — fuel applied before discount, a dropped
minimum-charge floor — would corrupt the planted invoices and the auditor identically
and still score a perfect 1.000.

This file breaks the circle. golden_invoices.jsonl is a small set of invoices crafted
by hand; golden_truth.jsonl carries the expected outcome and expected DOLLARS for every
line as literals — derived by a human (see each entry's "derivation"), cross-checked
with a deliberately different Decimal/datetime recompute, and importing NOTHING from
pricing.py or generate_invoices.py. We then run the REAL production pipeline
(audit_graph) over the golden invoices and assert it matches those literals.

Here, 1.000 means "the engine matches independently-derived ground truth."

What each golden case pins
--------------------------
  * exact expected-dollar value on a flagged line  (e.g. GOLD-002 expects 126.14)
  * the right outcome bucket: confident finding vs. deferred-to-review vs. clean
  * NO spurious finding on a correct line. This is how clean cases stay honest even
    though the engine emits no `expected` for them: GOLD-008 (minimum-charge floor)
    and the clean linehauls would draw a phantom over/under-charge finding the moment
    a shared formula bug shifted the engine's number off the hand-derived charge.

Run:  python evaluate_golden.py     (exit 0 = all golden lines match; nonzero = drift)
"""

import json
import sys

from audit_graph import build_graph
import pricing

CENT = 0.01  # dollar tolerance for an exact-value assertion (float vs. hand-rounded)


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def run_pipeline(rt, invoice):
    """Run the real production graph over one invoice; return confident + deferred maps."""
    graph = build_graph()
    out = graph.invoke({"rt": rt, "invoice": invoice, "classified": {},
                        "audit": {}, "review_queue": [], "summary": ""})
    confident = {f["line_ref"]: f for f in out["audit"]["findings"]}
    deferred = {f["line_ref"]: f for f in out["review_queue"]}
    return confident, deferred


def check_invoice(rt, invoice, truth):
    """Compare the engine's verdict to the hand-labeled truth. Returns a list of failures."""
    confident, deferred = run_pipeline(rt, invoice)
    fails = []
    expected_lines = truth["lines"]

    # 1. The engine must not flag any line the truth doesn't account for as flagged.
    for ref in confident:
        if expected_lines.get(ref, {}).get("outcome") != "confident":
            f = confident[ref]
            fails.append(f"{ref}: engine raised an UNEXPECTED confident "
                         f"{f['type']} (+${f['dollar_impact']:.2f}); truth says "
                         f"'{expected_lines.get(ref, {}).get('outcome', 'no such line')}'")
    for ref in deferred:
        if expected_lines.get(ref, {}).get("outcome") != "deferred":
            fails.append(f"{ref}: engine UNEXPECTEDLY deferred this line; truth says "
                         f"'{expected_lines.get(ref, {}).get('outcome', 'no such line')}'")

    # 2. Every truth line must land in its expected bucket with the expected dollars.
    for ref, want in expected_lines.items():
        outcome = want["outcome"]
        if outcome == "clean":
            if ref in confident:
                fails.append(f"{ref}: expected CLEAN but engine flagged it confidently")
            elif ref in deferred:
                fails.append(f"{ref}: expected CLEAN but engine deferred it to review")
        elif outcome == "confident":
            if ref not in confident:
                where = "deferred to review" if ref in deferred else "not flagged at all"
                fails.append(f"{ref}: expected a CONFIDENT finding but it was {where}")
            else:
                fails.extend(_check_dollars(ref, confident[ref], want))
        elif outcome == "deferred":
            if ref not in deferred:
                where = "flagged confidently" if ref in confident else "not flagged at all"
                fails.append(f"{ref}: expected DEFERRAL to review but it was {where}")
            else:
                fails.extend(_check_dollars(ref, deferred[ref], want))
        else:
            fails.append(f"{ref}: golden_truth has unknown outcome '{outcome}'")

    return fails


def _check_dollars(ref, finding, want):
    fails = []
    if "expected" in want and abs(finding["expected"] - want["expected"]) > CENT:
        fails.append(f"{ref}: engine expected ${finding['expected']:.2f}, "
                     f"hand-derived truth is ${want['expected']:.2f}")
    if "dollar_impact" in want and abs(finding["dollar_impact"] - want["dollar_impact"]) > CENT:
        fails.append(f"{ref}: engine impact ${finding['dollar_impact']:.2f}, "
                     f"hand-derived truth is ${want['dollar_impact']:.2f}")
    return fails


def main(invoices_path="golden_invoices.jsonl", truth_path="golden_truth.jsonl"):
    rt = pricing.load_rates()
    invoices = {inv["invoice_id"]: inv for inv in load_jsonl(invoices_path)}
    truths = load_jsonl(truth_path)

    total_lines = passed_lines = 0
    all_fails = []
    W = 64
    print("=" * W)
    print("GOLDEN SET — INDEPENDENT ORACLE (hand-derived ground truth)")
    print("=" * W)

    for truth in truths:
        iid = truth["invoice_id"]
        n_lines = len(truth["lines"])
        total_lines += n_lines
        fails = check_invoice(rt, invoices[iid], truth)
        if fails:
            all_fails.extend(f"{iid} {msg}" for msg in fails)
            print(f"  FAIL  {iid:10s} {truth['summary']}")
            for msg in fails:
                print(f"          - {msg}")
        else:
            passed_lines += n_lines
            print(f"  ok    {iid:10s} {truth['summary']}")

    acc = passed_lines / total_lines if total_lines else 0.0
    print("-" * W)
    print(f"  golden line accuracy   {acc:.3f}  ({passed_lines}/{total_lines} lines)")
    print(f"  invoices               {len(truths)}")
    print("=" * W)

    report = {
        "golden_line_accuracy": round(acc, 3),
        "lines_passed": passed_lines,
        "lines_total": total_lines,
        "invoices": len(truths),
        "failures": all_fails,
    }
    with open("golden_report.json", "w") as f:
        json.dump(report, f, indent=2)

    if all_fails:
        print(f"\n{len(all_fails)} mismatch(es) — the engine drifted from hand-labeled "
              f"ground truth. This is NOT a self-consistency check; investigate pricing.")
        return 1
    print("\nAll golden lines match independently-derived ground truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
