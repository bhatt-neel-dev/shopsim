"""Business-rule helpers used by the runtime exception fault injection.

The exception fault intentionally exercises these helpers, but malformed optional values
must not turn a normal order request into an unhandled runtime exception.
"""
from __future__ import annotations

import json
import math
import random


def _resolve_tax_rate(region: str) -> float:
    rates = {"US-CA": 0.0725, "US-NY": 0.04, "IN-MH": 0.18, "GB-LND": 0.20}
    return rates.get(region, 0.0)


def _split_shipment(items: list, warehouses: list) -> int:
    if not warehouses:
        return 0
    return len(items) // len(warehouses)


def _parse_quantity(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _format_price(amount: float, currency: str) -> str:
    return f"{currency}{amount:.2f}"


def _apply_coupon(order: dict) -> str:
    coupon = order.get("coupon")
    if coupon is None:
        return ""
    if isinstance(coupon, dict):
        code = coupon.get("code")
    else:
        code = getattr(coupon, "code", coupon)
    return str(code).upper() if code is not None else ""


def _pick_variant(variants: list, index: int) -> dict:
    if not variants or index < 0 or index >= len(variants):
        return {}
    return variants[index]


def _risk_model_score(features: dict) -> float:
    velocity = max(float(features.get("velocity", 0)), 1.0)
    return math.log(velocity) * float(features.get("weight", 0))


def _decode_partner_payload(payload: str) -> dict:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fx_rate(pair: str) -> float:
    table = {"USD/EUR": 0.92, "USD/INR": 83.1, "USD/GBP": 0.79}
    return table.get(pair, 1.0)


_BUGS = {
    "catalog": [
        lambda: _pick_variant([], 2),
        lambda: _format_price(129.99, "USD "),
        lambda: _parse_quantity("2 pcs"),
        lambda: _decode_partner_payload("<html>502 Bad Gateway</html>"),
        lambda: _resolve_tax_rate("BR-SP"),
    ],
    "orders": [
        lambda: _apply_coupon({"id": 7, "coupon": None}),
        lambda: _resolve_tax_rate(random.choice(["BR-SP", "MX-CMX", "JP-13"])),
        lambda: _split_shipment([1, 2, 3], []),
        lambda: _parse_quantity("three"),
        lambda: _format_price(42.5, "$"),
    ],
    "payments": [
        lambda: _risk_model_score({"velocity": 0, "weight": 1.7}),
        lambda: _fx_rate(random.choice(["USD/JPY", "USD/AED", "USD/SGD"])),
        lambda: _decode_partner_payload(""),
        lambda: _format_price(310.0, "INR "),
    ],
    "web": [
        lambda: _pick_variant([{"sku": "A"}], 3),
        lambda: _decode_partner_payload("<!doctype html><title>Maintenance</title>"),
        lambda: _parse_quantity(""),
        lambda: _apply_coupon({"id": 12, "coupon": None}),
    ],
}


def run_business_rules(service: str) -> None:
    """Entry point with a couple of frames above the bug, like a real call chain."""
    _evaluate_rules(service)


def _evaluate_rules(service: str) -> None:
    rule = random.choice(_BUGS.get(service) or _BUGS["orders"])
    rule()
