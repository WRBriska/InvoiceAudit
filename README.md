# Freight Invoice Auditor

[![eval](https://github.com/WRBriska/InvoiceAudit/actions/workflows/eval.yml/badge.svg)](https://github.com/WRBriska/InvoiceAudit/actions/workflows/eval.yml)

A production-shaped AI system that audits LTL freight invoices against contract
terms, flags overbilling with exact dollar impact, and **routes what it isn't sure
about to a human instead of guessing**. Built with LangGraph, with a deliberate
architectural rule: the language model never touches a number.

It ships with a synthetic-data generator, a labeled evaluation set, and an eval
harness — so every claim below is reproducible with one command, and **CI re-runs
the whole pipeline on every push and fails the build if quality regresses.**

```bash
make install   # langgraph + anthropic
make all       # generate data -> audit -> print scorecard
```

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

- **extract** — passthrough for structured input; the seam where a PDF/text
  front-end plugs in (see roadmap).
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

It does **not** prove real-world end-to-end recall, because the inputs here are
already structured. Real-world recall lives entirely in the **extraction layer**:
how reliably the LLM turns a messy PDF into correct structured fields. The harness
is built to measure exactly that the moment a text front-end is added — which is
the honest next milestone, not a number to inflate today.

The dollar-recovery gap (0.944, not 1.0) is also intentional: the ~$350 difference
is the four duplicate-charge cases the system **declined to claim** without human
confirmation. Declining to assert overbilling on an ambiguous repeat charge is
correct behavior, not a miss.

### Continuous verification

The scorecard above isn't a point-in-time screenshot. A GitHub Actions workflow
([`.github/workflows/eval.yml`](.github/workflows/eval.yml)) runs `make all` and
`make extract-eval` on every push and pull request, then **fails the build** unless:

- confident precision = 1.0,
- confident recall = 1.0, and
- false positives on decoy lines = 0.

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

## Roadmap

- **Messy-text / PDF extraction front-end** — the real test of LLM recall; the
  harness already measures it.
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
audit_graph.py         LangGraph pipeline
generate_invoices.py   synthetic invoices + hidden answer keys
run_audit.py           audit every invoice -> audits.jsonl
evaluate.py            scorecard vs answer keys
Makefile               make all
.github/workflows/     CI: re-runs the pipeline + enforces the quality gate
```

## Notes

- Runs offline by default via a deterministic mock client. Set `ANTHROPIC_API_KEY`
  to route the language stages through a real model; **the audit numbers are
  unchanged either way** — by design.
- All data is synthetic. No real invoices, rates, or customers.
