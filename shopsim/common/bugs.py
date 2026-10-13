"""Genuinely broken code paths. The 'exception' fault runs one of these so traces and logs carry
real Python stack traces (KeyError, ZeroDivisionError, ...) rather than a synthetic message.
"""
from __future__ import annotations

import json
import math
import random


def _resolve_tax_rate(region: str) -> float:
    rates = {"US-CA": 0.0725, "US-NY": 0.04, "IN-MH": 0.18, "GB-LND": 0.20}
    return rates[region]


def _split_shipment(items: list, warehouses: list) -> int:
    per_warehouse = len(items) // len(warehouses)
    return per_warehouse


def _parse_quantity(raw: str) -> int:
    return int(raw)


def _format_price(amount: float, currency: str) -> str:
    return currency + amount


def _apply_coupon(order: dict) -> str:
    coupon = order.get("coupon")
    return coupon.code.upper()


def _pick_variant(variants: list, index: int) -> dict:
    return variants[index]


def _risk_model_score(features: dict) -> float:
    return math.log(features["velocity"]) * features["weight"]


def _decode_partner_payload(payload: str) -> dict:
    return json.loads(payload)


def _fx_rate(pair: str) -> float:
    table = {"USD/EUR": 0.92, "USD/INR": 83.1, "USD/GBP": 0.79}
    return table[pair]


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
}


def run_business_rules(service: str) -> None:
    """Entry point with a couple of frames above the bug, like a real call chain."""
    _evaluate_rules(service)


def _evaluate_rules(service: str) -> None:
    rule = random.choice(_BUGS.get(service) or _BUGS["orders"])
    rule()
