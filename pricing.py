"""
pricing.py — deterministic audit engine.

This module contains ALL arithmetic and ALL contract-rule logic. No LLM ever
touches a number. Given a structured invoice + the rate table, it returns the
correct expected charges and a list of rule violations with exact dollar impact.

The hybrid principle in one place: the language model's job ends once a line is
turned into {code, units, stated_class, stated_weight, ...}. From there, money
is pure Python.
"""

import json
from datetime import date

TOLERANCE = 0.50  # dollars; below this, treat charged == expected (penny noise)


def load_rates(path="rate_table.json"):
    with open(path) as f:
        return json.load(f)


def weight_break_for(rt, weight_lb):
    for code, b in rt["tariff"]["weight_breaks"].items():
        if code.startswith("_"):
            continue
        if b["min_lb"] <= weight_lb <= b["max_lb"]:
            return code
    return None


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def expected_linehaul(rt, customer_id, lane, cls, weight_lb, ship_date: date):
    """Correct linehaul: rate -> minimum -> discount -> fuel. Returns total + trace."""
    wb = weight_break_for(rt, weight_lb)
    cwt = rt["tariff"]["lanes"][lane][cls][wb]
    base = max((weight_lb / 100.0) * cwt, rt["tariff"]["minimum_charge"])
    disc = rt["customers"][customer_id]["linehaul_discount_pct"]
    discounted = base * (1 - disc)
    fuel_pct = rt["fuel_surcharge_schedule"][iso_week_key(ship_date)]
    total = discounted * (1 + fuel_pct)
    return round(total, 2), {
        "weight_break": wb, "cwt_rate": cwt, "discount_pct": disc, "fuel_pct": fuel_pct,
    }


def _valid_accessorial(rt, code, inv):
    vf = rt["accessorials"].get(code, {}).get("valid_for", [])
    if "any" in vf:
        return True
    checks = {
        "no_dock_origin": not inv["origin"]["dock"],
        "no_dock_dest": not inv["dest"]["dock"],
        "residential_dest": inv["dest"]["residential"],
        "failed_first_attempt": inv["shipment"]["failed_first_attempt"],
        "inside_requested": inv["shipment"]["inside_requested"],
        "limited_access_site": inv["origin"]["limited_access"] or inv["dest"]["limited_access"],
    }
    return any(checks.get(v, False) for v in vf)


def expected_accessorial(rt, customer_id, code, units, inv):
    """Correct accessorial charge honoring waivers, allowances, validity."""
    cust = rt["customers"][customer_id]
    if code not in rt["accessorials"]:
        return 0.0, "no_basis"                      # phantom
    if code in cust.get("waived_accessorials", []):
        return 0.0, "waived"
    if not _valid_accessorial(rt, code, inv):
        return 0.0, "unauthorized"
    free = cust.get("free_allowances", {}).get(code, 0)
    return round(rt["accessorials"][code]["rate"] * max(0, units - free), 2), "ok"


def audit_invoice(rt, inv):
    """
    The deterministic verdict. Consumes a fully-extracted invoice (the LLM has
    already produced normalized codes/classes) and returns structured findings.
    """
    findings = []
    cust = inv["customer_id"]
    seen_acc = {}  # code -> first line ref, for duplicate detection

    for line in inv["lines"]:
        ref, charged = line["ref"], round(line["amount"], 2)

        if line["type"] == "linehaul":
            exp, _ = expected_linehaul(
                rt, cust, inv["lane"], line["stated_class"],
                line["stated_weight_lb"], date.fromisoformat(inv["ship_date"]))
            if charged - exp > TOLERANCE:
                findings.append(_finding(ref, "linehaul_overcharge", charged, exp,
                    "Linehaul exceeds contracted rate/discount/fuel for stated class & weight."))
            elif exp - charged > TOLERANCE:
                findings.append(_finding(ref, "linehaul_undercharge", charged, exp,
                    "Linehaul below expected (informational)."))
            continue

        # accessorial line
        code = line.get("code", "MISC")
        units = line.get("units", 1)

        if code in seen_acc:
            findings.append(_finding(ref, "duplicate_charge", charged, 0.0,
                f"{code} already billed on {seen_acc[code]}; repeat charge — "
                f"verify it reflects a distinct event.", confidence="low"))
            continue
        seen_acc[code] = ref

        exp, reason = expected_accessorial(rt, cust, code, units, inv)
        if charged - exp > TOLERANCE:
            etype = {
                "waived": "waived_charge_violation",
                "unauthorized": "unauthorized_accessorial",
                "no_basis": "phantom_accessorial",
                "ok": "accessorial_overcharge",
            }[reason]
            findings.append(_finding(ref, etype, charged, exp,
                _explain(code, reason)))

    total = round(sum(f["dollar_impact"] for f in findings if f["dollar_impact"] > 0), 2)
    return {"invoice_id": inv["invoice_id"], "findings": findings,
            "total_overbilled": total}


def _finding(ref, etype, charged, expected, explanation, confidence="high"):
    return {"line_ref": ref, "type": etype, "charged": charged,
            "expected": round(expected, 2),
            "dollar_impact": round(charged - expected, 2),
            "explanation": explanation, "confidence": confidence}


def _explain(code, reason):
    return {
        "waived": f"{code} is waived for this customer per contract.",
        "unauthorized": f"{code} billed without a valid shipment basis.",
        "no_basis": f"{code} does not map to any contracted accessorial.",
        "ok": f"{code} billed above its contracted rate.",
    }[reason]
