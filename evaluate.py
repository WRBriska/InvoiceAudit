"""
evaluate.py — the scorecard.

Scores audits.jsonl against answer_keys.jsonl and prints the table that anchors
the README. Three things it does that a naive scorer doesn't:

  1. Counts the REVIEW QUEUE as its own outcome. A finding the system routed to a
     human (low classification confidence) is neither a confident catch nor a
     silent miss — conflating it with a miss understates the system and hides the
     triage behavior, which is the point of the design.

  2. Breaks recall down BY ERROR TYPE, so a concentrated failure mode (e.g. "all
     misses are phantom_accessorial") is visible instead of averaged away.

  3. Reports DOLLAR-RECOVERY accuracy — in freight audit, dollars are the metric
     that matters, not line counts.

Matching is line-level. A planted discrepancy on line L is a true positive iff
the audit reports a confident finding on L; a review-queue entry on L is scored
separately as "deferred".
"""

import json
from collections import defaultdict


def load(path):
    return {json.loads(l)["invoice_id"]: json.loads(l) for l in open(path)}


def evaluate(keys_path="answer_keys.jsonl", audits_path="audits.jsonl"):
    keys = load(keys_path)
    auds = load(audits_path)

    tp = fp = fn = deferred = 0
    decoy_fp = 0
    per_type = defaultdict(lambda: {"planted": 0, "caught": 0, "deferred": 0, "missed": 0})
    dollars_planted = 0.0
    dollars_caught = 0.0

    for iid, k in keys.items():
        truth = {d["line_ref"]: d for d in k["discrepancies"]}
        decoys = {d["line_ref"] for d in k["decoys"]}
        a = auds[iid]
        confident = {f["line_ref"]: f for f in a["findings"]}
        review = {f["line_ref"]: f for f in a.get("review_queue", [])}

        for ref, d in truth.items():
            per_type[d["type"]]["planted"] += 1
            dollars_planted += d["dollar_impact"]
            if ref in confident:
                tp += 1
                per_type[d["type"]]["caught"] += 1
                dollars_caught += min(confident[ref]["dollar_impact"], d["dollar_impact"])
            elif ref in review:
                deferred += 1
                per_type[d["type"]]["deferred"] += 1
            else:
                fn += 1
                per_type[d["type"]]["missed"] += 1

        for ref, f in confident.items():
            if ref not in truth:
                fp += 1
                if ref in decoys:
                    decoy_fp += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    recall_incl_deferred = (tp + deferred) / (tp + fn + deferred) if (tp + fn + deferred) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    dollar_acc = dollars_caught / dollars_planted if dollars_planted else 0.0

    report = {
        "confident_precision": round(precision, 3),
        "confident_recall": round(recall, 3),
        "recall_incl_deferred": round(recall_incl_deferred, 3),
        "f1": round(f1, 3),
        "true_positives": tp, "false_positives": fp,
        "false_negatives": fn, "deferred_to_review": deferred,
        "false_positives_on_decoys": decoy_fp,
        "dollars_planted": round(dollars_planted, 2),
        "dollars_recovered": round(dollars_caught, 2),
        "dollar_recovery_accuracy": round(dollar_acc, 3),
        "per_type": {t: dict(v) for t, v in sorted(per_type.items())},
    }
    return report


def print_report(r):
    W = 64
    print("=" * W)
    print("FREIGHT INVOICE AUDIT — EVALUATION")
    print("=" * W)
    print(f"  Precision (confident)      {r['confident_precision']:.3f}")
    print(f"  Recall (confident)         {r['confident_recall']:.3f}")
    print(f"  Recall (incl. deferred)    {r['recall_incl_deferred']:.3f}")
    print(f"  F1                         {r['f1']:.3f}")
    print(f"  Dollar-recovery accuracy   {r['dollar_recovery_accuracy']:.3f}  "
          f"(${r['dollars_recovered']:.0f} / ${r['dollars_planted']:.0f})")
    print("-" * W)
    print(f"  TP={r['true_positives']}  FP={r['false_positives']}  "
          f"FN={r['false_negatives']}  deferred={r['deferred_to_review']}")
    print(f"  False positives on decoy (valid-but-unusual) lines: "
          f"{r['false_positives_on_decoys']}")
    print("-" * W)
    print(f"  {'error type':28s} {'plant':>5} {'caught':>6} {'defer':>5} {'miss':>5}")
    for t, v in r["per_type"].items():
        print(f"  {t:28s} {v['planted']:>5} {v['caught']:>6} "
              f"{v['deferred']:>5} {v['missed']:>5}")
    print("=" * W)


if __name__ == "__main__":
    r = evaluate()
    print_report(r)
    with open("eval_report.json", "w") as f:
        json.dump(r, f, indent=2)
