# Freight Invoice Auditor

[![eval](https://github.com/WRBriska/InvoiceAudit/actions/workflows/eval.yml/badge.svg)](https://github.com/WRBriska/InvoiceAudit/actions/workflows/eval.yml)

A production-shaped AI system that audits LTL freight invoices against contract
terms, flags overbilling with exact dollar impact, and **routes what it isn't sure
about to a human instead of guessing**. Built with LangGraph, with a deliberate
architectural rule: the language model never touches a number.

It ships with a synthetic-data generator, a labeled evaluation set, an eval harness,
and an **independent hand-labeled oracle** that catches even bugs shared between the
generator and the engine — so every claim below is reproducible with one command, and
**CI re-runs the whole pipeline on every push and fails the build if quality regresses.**

```bash
make install   # langgraph + anthropic
make all       # generate data -> audit -> score -> check vs independent oracle
make golden    # run just the independent hand-labeled golden-set check
```

## See it run

A real invoice making the full trip — structured fields in, deterministic verdict
out. `INV-2026-00000` is billed `$731.14` of linehaul on lane CHI-DEN; the engine
recomputes the contractually correct figure (rate → minimum → 55% discount →
28.5% fuel) at `$618.79` and flags the difference:

```json
{
  "line_ref": "L1",
  "type": "linehaul_overcharge",
  "charged": 731.14,
  "expected": 618.79,
  "dollar_impact": 112.35,
  "explanation": "Linehaul exceeds contracted rate/discount/fuel for stated class & weight.",
  "confidence": "high"
}
```

And the part most "anomaly detectors" get wrong — declining to assert when a charge
is genuinely ambiguous. On another invoice, a second detention line *could* be a
duplicate or a real second event, so it goes to the review queue instead of to the
customer:

```json
{
  "line_ref": "A2",
  "type": "duplicate_charge",
  "charged": 255.0,
  "dollar_impact": 255.0,
  "explanation": "DETENTION already billed on A1; repeat charge — verify it reflects a distinct event.",
  "confidence": "low"
}
```

`make extract-demo` shows the same invoice rendered as messy carrier-style text,
parsed back to these structured fields by the LLM, and audited to the identical
verdict — the [extraction front-end](#extraction-front-end) below.

---

## The problem

Freight and parcel invoices are overbilled constantly, and the expensive errors
aren't the obvious ones. A charge can look completely normal and still be wrong
because *this customer's contract* waives it, or because a discount wasn't applied,
or because fuel was rated against the wrong week. Catching those requires checking
each line against a contractual source of truth — not eyeballing it for anomalies.
An anomaly detector flags a weird-looking charge and waves through a normal-looking
one that violates the contract. That's backwards.

This system audits against the contract.

## Why hybrid (the one design decision that matters)

LLMs are good at reading messy language and bad at arithmetic. So the boundary is
drawn precisely:

| Stage | Owner | Why |
|---|---|---|
| extract messy line text → structured fields | **LLM** | language, not math |
| map free-text descriptions → catalog codes | **LLM** | fuzzy classification |
| compute expected charges, compare, flag | **deterministic Python** | money must be reproducible |
| triage low-confidence findings → human | **deterministic** | a verdict, not a vibe |
| write the human-readable summary | **LLM** | language again |

The payoff: **wrong arithmetic is structurally impossible.** The audit's numbers
don't depend on which model is plugged in, on temperature, or on a prompt. Swap the
LLM and the dollar figures are identical. That property is the point.

## Architecture

A linear LangGraph pipeline; the value is in *which* node owns money (only `audit`,
and it never calls the LLM).

```
extract ─▶ classify ─▶ audit ─▶ triage ─▶ report
 [LLM*]     [LLM]       [det]    [det]     [LLM]
```

- **extract** — passthrough for already-structured input; runs the LLM
  [text-extraction front-end](#extraction-front-end) when an invoice arrives as
  raw text. Either way the audit numbers are identical.
- **classify** — normalizes each line description to a catalog code with a
  confidence; a description that maps to *nothing* is a confident phantom, not an
  ambiguous one.
- **audit** — `pricing.py`, the deterministic engine. Recomputes the contractually
  correct linehaul (rate → minimum → discount → fuel) and every accessorial
  (honoring waivers, free allowances, and validity rules), then reports variances.
- **triage** — any finding resting on low classification confidence, or any finding
  that declares its own low confidence (e.g. a repeat charge that *might* be a real
  second event), is diverted to a review queue rather than asserted.
- **report** — natural-language summary of the structured findings.

## What it catches

Eight discrepancy types, each requiring a different lookup — which is why this is an
agent and not a regex:

`rate_overcharge` · `weight_break_error` · `fuel_surcharge_error` ·
`unauthorized_accessorial` · `duplicate_charge` · `waived_charge_violation` ·
`discount_error` · `phantom_accessorial`

Plus a ninth labeled category in the eval set — **valid-but-unusual decoys**
(e.g. a legitimately high detention charge) — that the system must *not* flag.
They exist to make the false-positive metric mean something.

## Evaluation

The generator plants known errors and emits a hidden answer key, so scoring is
exact. Current scorecard on 200 invoices (69 planted discrepancies, 31 decoys):

```
Precision (confident)      1.000
Recall (confident)         1.000
F1                         1.000
Dollar-recovery accuracy   0.944   ($5,935 / $6,285)
TP=65  FP=0  FN=0  deferred=4
False positives on decoy (valid-but-unusual) lines: 0
```

The eval does three things a naive scorer doesn't: it counts the **review queue**
as its own outcome (a deferred item is neither a confident catch nor a silent
miss), it breaks recall down **by error type** so a concentrated failure mode is
visible instead of averaged away, and it reports **dollar-recovery accuracy**
because in this domain dollars are the metric, not line counts.

### What this eval proves — and what it doesn't

It proves the **rule engine is complete and correct**: given clean extraction, the
deterministic logic catches every planted error across all eight types and flags
none of the decoys. That's the hybrid design delivering — the numbers can't be
wrong because the LLM never produces them.

It does **not** prove real-world end-to-end recall, because the inputs to the
*audit* are already structured. Real-world recall lives in the **extraction layer**:
how reliably the LLM turns a messy invoice into correct structured fields. That
layer now exists and is measured separately — see [Extraction front-end](#extraction-front-end).
Keeping the two scores apart is deliberate: a perfect rule engine and a perfect
extractor are different claims, and collapsing them would flatter both.

The dollar-recovery gap (0.944, not 1.0) is also intentional: the ~$350 difference
is the four duplicate-charge cases the system **declined to claim** without human
confirmation. Declining to assert overbilling on an ambiguous repeat charge is
correct behavior, not a miss.

### An independent oracle (so 1.000 means "correct," not "self-consistent")

There's a trap in the scorecard above worth calling out, because it's the kind of
thing that makes an eval lie. The answer keys are generated by `generate_invoices.py`,
which computes expected charges with the **same pricing formula the audit engine
runs**. So the dollar metrics are partly *circular*: a perfect score can mean "my two
copies of the formula agree," not "the engine is right." A shared bug — fuel applied
before the discount, a dropped minimum-charge floor — would corrupt the planted
invoices and the auditor identically and still score 1.000.

To break the circle there's a second, **independent** check
([`evaluate_golden.py`](evaluate_golden.py)): a small hand-labeled golden set
([`golden_invoices.jsonl`](golden_invoices.jsonl) +
[`golden_truth.jsonl`](golden_truth.jsonl)) whose expected dollars are **literals
derived by hand** — each with its arithmetic shown, cross-checked with a deliberately
different `Decimal`/`datetime` recompute, and importing nothing from the pricing code.
The checker runs the *real* production graph over those invoices and asserts it
matches the literals. Here **1.000 means the engine matches ground truth**, not
another instance of its own formula.

The 21 cases (36 line-level assertions) are chosen so a shared formula bug can't hide.
They cover all three linehaul error types (rate, weight-break, fuel-week, discount),
all four accessorial verdicts (phantom, unauthorized, waived, overcharge), every
accessorial code and validity branch, free-allowance arithmetic, the minimum-charge
floor, a weight-break boundary (exactly 1000 lb), and an informational undercharge — plus
clean and decoy lines that fail the moment a drifted number draws a phantom finding.
Each error case pins an exact hand-derived `expected` value, and clean cases that *can't*
be pinned that way (a wrong number there shows up as a silent undercharge, not a finding)
are paired with an overcharged twin that can — e.g. the free-allowance case is asserted on
a flagged line so dropping the allowance is caught, not hidden.

Verified with fault injection: dropping the minimum-charge floor, mis-selecting the
weight break, ignoring the customer discount, or ignoring a free allowance each leaves the
circular eval at 1.000 but makes the golden oracle fail. The set is deliberately small —
the value is independence and auditability, not volume.

### Continuous verification

The scorecard above isn't a point-in-time screenshot. A GitHub Actions workflow
([`.github/workflows/eval.yml`](.github/workflows/eval.yml)) runs `make all` and
`make extract-eval` on every push and pull request, then **fails the build** unless:

- confident precision = 1.0,
- confident recall = 1.0,
- false positives on decoy lines = 0, and
- every line of the independent golden set matches hand-derived ground truth
  (`make golden`).

CI runs entirely offline through the deterministic mock client — no API key — so the
guarantee holds for every contributor. Any regression in the audit logic breaks the
build instead of silently eroding the numbers.

### Iteration (v1 → v2)

The first version scored **0.866 precision**. The eval's per-type breakdown
localized both failures immediately:

- **All 9 false positives were duplicate-charge flags on decoy lines.** A second
  detention line was being asserted as a duplicate, but a repeat detention charge
  can be a genuine second event. **Fix:** repeat charges are now flagged
  *low-confidence for review*, not asserted. → decoy false positives: 9 → 0.
- **All 11 phantom charges were being deferred, none confidently caught.** A charge
  mapping to no catalog code was being treated as low-confidence *classification*
  when it's actually a high-confidence *finding*. **Fix:** unmapped codes flag as
  confident phantoms. → phantoms caught: 0/11 → 11/11.

Result: **0.866 → 1.000 precision**, with the genuinely ambiguous cases correctly
sitting in the review queue. The point isn't the 1.0 — it's that the eval found the
defects and the fixes were principled, not number-chasing.

## Extraction front-end

The text/PDF seam, with its own honest scorecard (`make extract-eval`). Each
structured invoice is rendered to messy carrier-style line text, extracted back to
structured fields through the active LLM client, and scored field-by-field against
the original:

```
EXTRACTION FRONT-END — RECALL  (client: MockClient)
  invoices   200   lines   401
  line recall            1.000
  line full-field acc.   1.000
  field: amount 1.000 · type 1.000 · stated_class 1.000 · stated_weight_lb 1.000 · units 1.000
```

**Read that 1.000 correctly.** The default `MockClient` is a deterministic regex
parser, so against this rendered text it is a *baseline*, not a model result — it
exists so the harness, the offline path, and CI all work without an API key. The
real measurement is one environment variable away: set `ANTHROPIC_API_KEY` and the
identical harness scores a genuine model (`claude-haiku-4-5`) on the same task,
which is where extraction recall becomes a real number. Two boundaries are kept
deliberately honest:

- The LLM **only reads** — `extract_line()` copies the stated dollar amount
  verbatim and never recomputes it, so the hybrid guarantee holds end to end.
- Only the **printed charge lines** go through extraction. Shipment context
  (dock/residential flags, lane, customer) is assumed to arrive structured, the way
  it does from an EDI 210 / manifest. Pretending to OCR it out of prose would
  overstate the front-end's reach.

## Roadmap

- ~~**Messy-text / PDF extraction front-end**~~ — *done* (see above); the obvious
  next step is feeding real scanned-PDF text rather than rendered lines.
- **Inference-mode context** — infer `residential`/`no-dock` from address text
  rather than reading explicit flags (harder, more realistic).
- **Multiple simultaneous discrepancies per invoice** (currently capped at one for
  clean line-level scoring).
- **SQLite source of truth** in place of the JSON rate table.

## Repo layout

```
rate_table.json        contract & tariff source of truth
pricing.py             deterministic audit engine (all math + rules live here)
llm_client.py          LLM boundary: real Anthropic client + offline mock
extract.py             messy-text extraction front-end (+ demo)
audit_graph.py         LangGraph pipeline
generate_invoices.py   synthetic invoices + hidden answer keys
run_audit.py           audit every invoice -> audits.jsonl
evaluate.py            scorecard vs answer keys (same-formula keys; see caveat)
evaluate_extraction.py extraction-recall scorecard
evaluate_golden.py     independent oracle: real engine vs hand-labeled ground truth
golden_invoices.jsonl  hand-crafted invoices for the independent check
golden_truth.jsonl     hand-derived expected dollars + outcomes (literals, shown work)
Makefile               make all · make extract-demo · make extract-eval
.github/workflows/     CI: re-runs the pipeline + enforces the quality gate
```

## Notes

- Runs offline by default via a deterministic mock client. Set `ANTHROPIC_API_KEY`
  to route the language stages through a real model; **the audit numbers are
  unchanged either way** — by design.
- All data is synthetic. No real invoices, rates, or customers.
