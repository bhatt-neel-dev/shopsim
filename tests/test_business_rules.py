from shopsim.common import bugs


def test_order_business_rule_inputs_are_safe():
    assert bugs._apply_coupon({"coupon": None}) == ""
    assert bugs._apply_coupon({"coupon": "spring10"}) == "SPRING10"
    assert bugs._resolve_tax_rate("BR-SP") == 0.0
    assert bugs._split_shipment([1, 2], []) == 0
    assert bugs._parse_quantity("three") == 0
    assert bugs._format_price(42.5, "USD ") == "USD 42.50"


def test_order_exception_fault_does_not_raise_for_malformed_rule_inputs(monkeypatch):
    monkeypatch.setattr(bugs.random, "choice", lambda rules: rules[0])
    bugs.run_business_rules("orders")
