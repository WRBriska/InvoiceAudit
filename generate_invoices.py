"""
Synthetic LTL freight invoice generator.

Produces two aligned outputs:
  - synthetic_invoices.jsonl : what the auditor agent sees (invoice only)
  - answer_keys.jsonl        : ground truth for evaluation (never shown to the agent)

Design contract:
  * compute_linehaul() / accessorial_expected() are the SINGLE source of truth for
    "what should this cost". Clean invoices are built from them; planted-error invoices
    record their output as `expected`. Ground truth therefore cannot drift from logic.
  * Each invoice has AT MOST one planted error (keeps line-level eval interpretable).
  * Decoys = valid-but-unusual lines that must NOT be flagged. They drive the
    false-positive metric.
"""

import json
import random
from datetime import date, timedelta

RATE_TABLE_PATH = "rate_table.json"


# --------------------------------------------------------------------------- #
# Source-of-truth pricing (used for BOTH generation and ground truth)
# --------------------------------------------------------------------------- #

def load_rates(path=RATE_TABLE_PATH):
    with open(path) as f:
        return json.load(f)


def weight_break_for(rt, weight_lb):
    for code, b in rt["tariff"]["weight_breaks"].items():
        if code.startswith("_"):
            continue
        if b["min_lb"] <= weight_lb <= b["max_lb"]:
            return code
    raise ValueError(f"no weight break for {weight_lb}")


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def compute_linehaul(rt, customer_id, lane, cls, weight_lb, ship_date):
    """Correct linehaul pipeline: rate -> minimum -> discount -> fuel."""
    cwt_rate = rt["tariff"]["lanes"][lane][cls][weight_break_for(rt, weight_lb)]
    base = (weight_lb / 100.0) * cwt_rate
    base = max(base, rt["tariff"]["minimum_charge"])

    disc = rt["customers"][customer_id]["linehaul_discount_pct"]
    discounted = base * (1 - disc)

    fuel_pct = rt["fuel_surcharge_schedule"][iso_week_key(ship_date)]
    fuel = discounted * fuel_pct

    total = discounted + fuel
    return {
        "cwt_rate": cwt_rate,
        "base": round(base, 2),
        "discount_pct": disc,
        "discounted": round(discounted, 2),
        "fuel_pct": fuel_pct,
        "fuel": round(fuel, 2),
        "total": round(total, 2),
    }


def is_valid_accessorial(rt, code, ctx):
    """Does the shipment context permit this accessorial?"""
    valid_for = rt["accessorials"][code]["valid_for"]
    if "any" in valid_for:
        return True
    checks = {
        "no_dock_origin": not ctx["origin"]["dock"],
        "no_dock_dest": not ctx["dest"]["dock"],
        "residential_dest": ctx["dest"]["residential"],
        "failed_first_attempt": ctx["shipment"]["failed_first_attempt"],
        "inside_requested": ctx["shipment"]["inside_requested"],
        "limited_access_site": ctx["origin"]["limited_access"] or ctx["dest"]["limited_access"],
    }
    return any(checks.get(v, False) for v in valid_for)


def accessorial_expected(rt, customer_id, code, units, ctx):
    """Correct charge for an accessorial, honoring waivers, allowances, validity."""
    cust = rt["customers"][customer_id]
    if code in cust.get("waived_accessorials", []):
        return 0.0  # contract says never bill this
    if not is_valid_accessorial(rt, code, ctx):
        return 0.0  # no basis -> should not be billed
    free = cust.get("free_allowances", {}).get(code, 0)
    billable_units = max(0, units - free)
    return round(rt["accessorials"][code]["rate"] * billable_units, 2)


# --------------------------------------------------------------------------- #
# Random shipment context
# --------------------------------------------------------------------------- #

def random_context(rng, rt):
    customer_id = rng.choice([c for c in rt["customers"] if not c.startswith("_")])
    lane = rng.choice([l for l in rt["tariff"]["lanes"] if not l.startswith("_")])
    cls = rng.choice([c for c in rt["tariff"]["lanes"][lane] if c.startswith("class_")])
    weight_lb = rng.choice([180, 340, 620, 880, 1450, 2900, 6200])

    # ship date within the weeks the fuel schedule covers (W18-W22 of 2026)
    base_day = date(2026, 4, 27)  # Monday of 2026-W18
    ship_date = base_day + timedelta(days=rng.randint(0, 34))

    ctx = {
        "customer_id": customer_id,
        "lane": lane,
        "shipment": {
            "weight_lb": weight_lb,
            "class": cls,
            "ship_date": ship_date.isoformat(),
            "inside_requested": rng.random() < 0.15,
            "failed_first_attempt": rng.random() < 0.12,
        },
        "origin": {"dock": rng.random() < 0.8, "residential": False,
                   "limited_access": rng.random() < 0.1},
        "dest": {"dock": rng.random() < 0.6, "residential": rng.random() < 0.25,
                 "limited_access": rng.random() < 0.1},
        "_ship_date_obj": ship_date,
    }
    return ctx


def valid_accessorials_for(rt, ctx):
    """Which accessorials could legitimately appear, given context (excl. waived)."""
    cust = rt["customers"][ctx["customer_id"]]
    out = []
    for code in rt["accessorials"]:
        if code.startswith("_"):
            continue
        if code in cust.get("waived_accessorials", []):
            continue
        if is_valid_accessorial(rt, code, ctx):
            out.append(code)
    return out


# --------------------------------------------------------------------------- #
# Build a CLEAN invoice + its (empty) answer key
# --------------------------------------------------------------------------- #

def build_clean(rt, ctx, inv_id, rng):
    s = ctx["shipment"]
    lh = compute_linehaul(rt, ctx["customer_id"], ctx["lane"], s["class"],
                          s["weight_lb"], ctx["_ship_date_obj"])

    lines = [{
        "ref": "L1", "type": "linehaul",
        "description": f"Linehaul {ctx['lane']} {s['class']} {s['weight_lb']}lb",
        "stated_class": s["class"], "stated_weight_lb": s["weight_lb"],
        "applied_discount_pct": lh["discount_pct"], "applied_fuel_pct": lh["fuel_pct"],
        "amount": lh["total"],
    }]

    # add 0-2 legitimately valid accessorials
    pool = valid_accessorials_for(rt, ctx)
    rng.shuffle(pool)
    n_acc = rng.choice([0, 0, 1, 1, 2])
    aidx = 1
    for code in pool[:n_acc]:
        units = rng.randint(1, 3) if code == "DETENTION" else 1
        amt = accessorial_expected(rt, ctx["customer_id"], code, units, ctx)
        if amt <= 0:
            continue
        lines.append({"ref": f"A{aidx}", "type": "accessorial", "code": code,
                      "units": units,
                      "description": rt["accessorials"][code]["description"],
                      "amount": amt})
        aidx += 1

    invoice = {
        "invoice_id": inv_id, "carrier": "Synthetic Freight Co",
        "customer_id": ctx["customer_id"], "ship_date": s["ship_date"],
        "lane": ctx["lane"],
        "origin": ctx["origin"], "dest": ctx["dest"],
        "shipment": {k: v for k, v in s.items()},
        "lines": lines,
        "invoice_total": round(sum(l["amount"] for l in lines), 2),
    }
    key = {"invoice_id": inv_id, "customer_id": ctx["customer_id"],
           "has_discrepancy": False, "discrepancies": [], "decoys": [],
           "total_overbilled": 0.0}
    return invoice, key


# --------------------------------------------------------------------------- #
# Error injectors. Each returns a discrepancy dict and mutates the invoice.
# --------------------------------------------------------------------------- #

def _disc(ref, etype, charged, expected, explanation):
    return {"line_ref": ref, "type": etype, "charged": round(charged, 2),
            "expected": round(expected, 2),
            "dollar_impact": round(charged - expected, 2),
            "explanation": explanation}


def inject_rate_overcharge(rt, inv, key, ctx, rng):
    l = inv["lines"][0]
    expected = l["amount"]
    charged = round(expected * rng.uniform(1.08, 1.25), 2)
    l["amount"] = charged
    return _disc("L1", "rate_overcharge", charged, expected,
                 "Linehaul charged above contracted rate for lane/class/weight.")


def inject_weight_break_error(rt, inv, key, ctx, rng):
    """Bill linehaul at a higher (lower-weight) break than stated weight warrants."""
    s = ctx["shipment"]
    correct_break = weight_break_for(rt, s["weight_lb"])
    breaks = [b for b in rt["tariff"]["weight_breaks"] if not b.startswith("_")]
    idx = breaks.index(correct_break)
    if idx == 0:
        return None  # already cheapest tier; nothing to inflate to
    wrong_break = breaks[idx - 1]
    wrong_rate = rt["tariff"]["lanes"][ctx["lane"]][s["class"]][wrong_break]
    base = max((s["weight_lb"] / 100.0) * wrong_rate, rt["tariff"]["minimum_charge"])
    disc = rt["customers"][ctx["customer_id"]]["linehaul_discount_pct"]
    fuel_pct = rt["fuel_surcharge_schedule"][iso_week_key(ctx["_ship_date_obj"])]
    charged = round(base * (1 - disc) * (1 + fuel_pct), 2)
    expected = inv["lines"][0]["amount"]
    inv["lines"][0]["amount"] = charged
    return _disc("L1", "weight_break_error", charged, expected,
                 f"Rated at break {wrong_break}; stated weight {s['weight_lb']}lb belongs in {correct_break}.")


def inject_fuel_error(rt, inv, key, ctx, rng):
    s = ctx["shipment"]
    lh = compute_linehaul(rt, ctx["customer_id"], ctx["lane"], s["class"],
                          s["weight_lb"], ctx["_ship_date_obj"])
    wrong_pct = round(lh["fuel_pct"] + rng.choice([0.03, 0.05, -0.04]), 3)
    charged = round(lh["discounted"] * (1 + wrong_pct), 2)
    expected = lh["total"]
    inv["lines"][0]["amount"] = charged
    inv["lines"][0]["applied_fuel_pct"] = wrong_pct
    return _disc("L1", "fuel_surcharge_error", charged, expected,
                 f"Fuel applied at {wrong_pct:.1%}; schedule for {iso_week_key(ctx['_ship_date_obj'])} is {lh['fuel_pct']:.1%}.")


def inject_unauthorized_accessorial(rt, inv, key, ctx, rng):
    """Add an accessorial whose context basis is absent."""
    # force a context where LIFTGATE has no basis: both ends have docks
    if not ctx["origin"]["dock"] or not ctx["dest"]["dock"]:
        return None
    code = "LIFTGATE"
    if code in rt["customers"][ctx["customer_id"]].get("waived_accessorials", []):
        return None
    rate = rt["accessorials"][code]["rate"]
    ref = f"A{len([l for l in inv['lines'] if l['type']=='accessorial'])+1}"
    inv["lines"].append({"ref": ref, "type": "accessorial", "code": code, "units": 1,
                         "description": rt["accessorials"][code]["description"], "amount": rate})
    return _disc(ref, "unauthorized_accessorial", rate, 0.0,
                 "Liftgate billed but both origin and destination have docks.")


def inject_duplicate_charge(rt, inv, key, ctx, rng):
    acc = [l for l in inv["lines"] if l["type"] == "accessorial"]
    if not acc:
        return None
    orig = acc[0]
    ref = f"A{len(acc)+1}"
    dup = dict(orig); dup["ref"] = ref
    inv["lines"].append(dup)
    return _disc(ref, "duplicate_charge", orig["amount"], 0.0,
                 f"{orig['code']} billed twice on one invoice.")


def inject_waived_violation(rt, inv, key, ctx, rng):
    waived = rt["customers"][ctx["customer_id"]].get("waived_accessorials", [])
    if not waived:
        return None
    code = rng.choice(waived)
    rate = rt["accessorials"][code]["rate"]
    ref = f"A{len([l for l in inv['lines'] if l['type']=='accessorial'])+1}"
    inv["lines"].append({"ref": ref, "type": "accessorial", "code": code, "units": 1,
                         "description": rt["accessorials"][code]["description"], "amount": rate})
    return _disc(ref, "waived_charge_violation", rate, 0.0,
                 f"{code} billed though waived for this customer per contract.")


def inject_discount_error(rt, inv, key, ctx, rng):
    s = ctx["shipment"]
    correct_disc = rt["customers"][ctx["customer_id"]]["linehaul_discount_pct"]
    cwt = rt["tariff"]["lanes"][ctx["lane"]][s["class"]][weight_break_for(rt, s["weight_lb"])]
    base = max((s["weight_lb"] / 100.0) * cwt, rt["tariff"]["minimum_charge"])
    fuel_pct = rt["fuel_surcharge_schedule"][iso_week_key(ctx["_ship_date_obj"])]
    wrong_disc = max(0.0, correct_disc - rng.choice([0.10, 0.15, correct_disc]))  # under- or no discount
    charged = round(base * (1 - wrong_disc) * (1 + fuel_pct), 2)
    expected = inv["lines"][0]["amount"]
    inv["lines"][0]["amount"] = charged
    inv["lines"][0]["applied_discount_pct"] = round(wrong_disc, 2)
    return _disc("L1", "discount_error", charged, expected,
                 f"Discount applied {wrong_disc:.0%}; contract is {correct_disc:.0%}.")


def inject_phantom_accessorial(rt, inv, key, ctx, rng):
    amt = round(rng.choice([25, 35, 50]) * 1.0, 2)
    ref = f"A{len([l for l in inv['lines'] if l['type']=='accessorial'])+1}"
    inv["lines"].append({"ref": ref, "type": "accessorial", "code": "MISC",
                         "units": 1, "description": "Administrative fee", "amount": amt})
    return _disc(ref, "phantom_accessorial", amt, 0.0,
                 "Charge does not map to any contracted accessorial.")


INJECTORS = [
    inject_rate_overcharge, inject_weight_break_error, inject_fuel_error,
    inject_unauthorized_accessorial, inject_duplicate_charge,
    inject_waived_violation, inject_discount_error, inject_phantom_accessorial,
]


# --------------------------------------------------------------------------- #
# Decoy: a valid-but-unusual line that must NOT be flagged
# --------------------------------------------------------------------------- #

def add_decoy(rt, inv, key, ctx, rng):
    # high detention (many valid units) — expensive but correct
    units = rng.randint(4, 8)
    amt = accessorial_expected(rt, ctx["customer_id"], "DETENTION", units, ctx)
    if amt <= 0:
        return
    ref = f"A{len([l for l in inv['lines'] if l['type']=='accessorial'])+1}"
    inv["lines"].append({"ref": ref, "type": "accessorial", "code": "DETENTION",
                         "units": units, "description": "Detention (per 30 min)", "amount": amt})
    inv["invoice_total"] = round(sum(l["amount"] for l in inv["lines"]), 2)
    key["decoys"].append({"line_ref": ref,
                          "note": f"High detention ({units} units) but valid and correctly priced."})


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def generate(n=200, error_rate=0.45, decoy_rate=0.18, seed=7):
    rng = random.Random(seed)
    rt = load_rates()
    invoices, keys = [], []

    for i in range(n):
        inv_id = f"INV-2026-{i:05d}"
        ctx = random_context(rng, rt)
        inv, key = build_clean(rt, ctx, inv_id, rng)

        if rng.random() < error_rate:
            injector = rng.choice(INJECTORS)
            disc = injector(rt, inv, key, ctx, rng)
            if disc is not None:
                key["has_discrepancy"] = True
                key["discrepancies"].append(disc)
                key["total_overbilled"] = round(
                    sum(d["dollar_impact"] for d in key["discrepancies"]), 2)
                inv["invoice_total"] = round(sum(l["amount"] for l in inv["lines"]), 2)

        if rng.random() < decoy_rate:
            add_decoy(rt, inv, key, ctx, rng)

        invoices.append(inv)
        keys.append(key)

    return rt, invoices, keys


def self_check(rt, invoices, keys):
    """Verify clean invoices truly reconcile to zero variance (no false ground truth)."""
    problems = 0
    for inv, key in zip(invoices, keys):
        if key["has_discrepancy"]:
            continue
        s = inv["shipment"]
        lh = compute_linehaul(rt, inv["customer_id"], inv["lane"], s["class"],
                              s["weight_lb"], date.fromisoformat(s["ship_date"]))
        charged_lh = next(l["amount"] for l in inv["lines"] if l["type"] == "linehaul")
        if abs(charged_lh - lh["total"]) > 0.02:
            problems += 1
    return problems


if __name__ == "__main__":
    rt, invoices, keys = generate()

    with open("synthetic_invoices.jsonl", "w") as f:
        for inv in invoices:
            pub = {k: v for k, v in inv.items()}  # agent sees the full invoice only
            f.write(json.dumps(pub) + "\n")

    with open("answer_keys.jsonl", "w") as f:
        for key in keys:
            f.write(json.dumps(key) + "\n")

    n_err = sum(1 for k in keys if k["has_discrepancy"])
    n_decoy = sum(1 for k in keys if k["decoys"])
    by_type = {}
    for k in keys:
        for d in k["discrepancies"]:
            by_type[d["type"]] = by_type.get(d["type"], 0) + 1

    print(f"generated {len(invoices)} invoices")
    print(f"  with planted error : {n_err}")
    print(f"  with decoy line    : {n_decoy}")
    print(f"  clean              : {len(invoices) - n_err}")
    print("  error types        :")
    for t, c in sorted(by_type.items()):
        print(f"      {t:28s} {c}")
    print(f"self-check (clean invoices with nonzero variance): {self_check(rt, invoices, keys)}")
