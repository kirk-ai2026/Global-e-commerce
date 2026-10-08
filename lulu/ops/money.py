from __future__ import annotations

import json
import urllib.request
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from lulu.core import digest, now

FX_URL = "https://www.bankofcanada.ca/valet/observations/FXCNYCAD/json?recent=5"
POLICY = "lulu-ca-tw-coeff-v1"


def decimal(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def fetch_fx():
    req = urllib.request.Request(
        FX_URL, headers={"User-Agent": "LuluOps/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = json.load(response)
    candidates = [
        r
        for r in raw.get("observations", [])
        if decimal((r.get("FXCNYCAD") or {}).get("v"))
    ]
    if not candidates:
        raise ValueError("FX_VALID_OBSERVATION_MISSING")
    latest = max(candidates, key=lambda r: r["d"])
    value = {
        "source": "BANK_OF_CANADA",
        "series": "FXCNYCAD",
        "url": FX_URL,
        "date": latest["d"],
        "cad_per_cny": latest["FXCNYCAD"]["v"],
        "currency": "CAD",
        "base_currency": "CNY",
    }
    return {**value, "fx_id": "FX-" + digest(value)[:24], "retrieved_at": now()}


def quote(variant, fx):
    price = decimal(variant.get("procurement_price_cny"))
    weight = decimal(variant.get("billable_weight_g"))
    basis = variant.get("weight_basis") or "UPSTREAM_BILLABLE_WEIGHT"
    if weight is None:
        weight = decimal(variant.get("predicted_weight_p75_g"))
        basis = "SAME_SKU_P75"
    rate = decimal((fx or {}).get("cad_per_cny"))
    errors = []
    if price is None or price <= 0:
        errors.append("采购价缺失")
    if weight is None or weight <= 0:
        errors.append("计费重量缺失")
    if rate is None or rate <= 0:
        errors.append("汇率快照缺失")
    base = {
        "currency": "CAD",
        "pricing_policy": POLICY,
        "fx_id": (fx or {}).get("fx_id"),
        "fx_date": (fx or {}).get("date"),
        "cad_per_cny": str(rate) if rate else None,
        "procurement_price_cny": str(price) if price is not None else None,
        "billable_weight_g": str(weight) if weight is not None else None,
        "weight_basis": basis,
        "freight_cny_per_kg": "10",
        "multiplier": "1.25",
        "rounding": "CEIL_0.01_CAD",
        "type": "PROVISIONAL_TW_COEFFICIENTS",
        "errors": errors,
    }
    if errors:
        return {
            **base,
            "status": "NEEDS_EVIDENCE",
            "raw_price_cad": None,
            "retail_price_cad": None,
        }
    raw = (price + Decimal("10") * weight / Decimal("1000")) * Decimal("1.25") * rate
    return {
        **base,
        "status": "READY",
        "raw_price_cad": str(raw),
        "retail_price_cad": str(raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)),
    }
