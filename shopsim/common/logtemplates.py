"""Background log lines: what each service chatters about between requests.

Each template returns (message, fields). ERROR/CRITICAL lines sometimes carry a real traceback
(see background.LogEmitter).
"""
from __future__ import annotations

import random

from shopsim.common.fakedata import BRANDS, CATEGORIES

_SUPPLIERS = ["Acme Wholesale", "Globex Supply", "Initech Distribution", "Umbrella Logistics", "Stark Imports"]
_PARTNERS = ["Google Shopping feed", "Meta catalog", "PriceRunner", "Affiliate network", "Marketplace sync"]
_CARRIERS = ["DHL", "FedEx", "UPS", "BlueDart", "Royal Mail"]
_BANKS = ["HDFC Bank", "Chase", "Barclays", "Deutsche Bank", "ICICI Bank"]


def _sku(r: random.Random) -> str:
    cat = r.choice(list(CATEGORIES.values()))["code"]
    return f"{cat}-{r.choice(BRANDS)[:3].upper()}-{r.randint(0, 0xFFFFFFFF):08X}"


TEMPLATES: dict[str, dict[str, list]] = {
    "catalog": {
        "DEBUG": [
            lambda r: (f"Cache miss for key product:{r.randint(1, 5000)}", {"event": "cache.miss"}),
            lambda r: ("Query plan: SEARCH products USING INDEX idx_products_category (category=?)",
                       {"event": "db.plan"}),
            lambda r: (f"Price rule engine evaluated {r.randint(3, 40)} rules in {r.uniform(0.2, 4):.2f}ms",
                       {"event": "pricing.rules"}),
            lambda r: (f"Search tokenizer produced {r.randint(1, 9)} tokens", {"event": "search.tokenize"}),
        ],
        "INFO": [
            lambda r: (f"Cache hit ratio {r.uniform(70, 97):.1f}% over last 60s", {"event": "cache.stats"}),
            lambda r: (f"Price sync completed for {r.randint(20, 800)} SKUs from {r.choice(_SUPPLIERS)}",
                       {"event": "pricing.sync"}),
            lambda r: (f"Served product feed to {r.choice(_PARTNERS)} ({r.randint(200, 5000)} items)",
                       {"event": "feed.served"}),
            lambda r: (f"Image CDN warmed {r.randint(10, 300)} thumbnails", {"event": "cdn.warm"}),
        ],
        "WARNING": [
            lambda r: (f"Low stock for SKU {_sku(r)}: {r.randint(0, 9)} units left", {"event": "inventory.low"}),
            lambda r: (f"Image CDN slow response ({r.randint(800, 4000)}ms) for {_sku(r)}", {"event": "cdn.slow"}),
            lambda r: (f"Supplier {r.choice(_SUPPLIERS)} price feed is {r.randint(2, 12)}h stale",
                       {"event": "pricing.stale"}),
        ],
        "ERROR": [
            lambda r: (f"Failed to sync price for SKU {_sku(r)}: supplier API returned 503", {"event": "pricing.error"}),
            lambda r: (f"Thumbnail generation failed for {_sku(r)}: corrupt JPEG data", {"event": "image.error"}),
            lambda r: (f"Search index shard {r.randint(0, 7)} failed health check", {"event": "search.error"}),
        ],
        "CRITICAL": [
            lambda r: ("Search index unavailable: all shards failing, serving degraded results",
                       {"event": "search.down"}),
        ],
    },
    "orders": {
        "DEBUG": [
            lambda r: (f"Cart {r.randint(10000, 99999)} recalculated: {r.randint(1, 8)} lines", {"event": "cart.recalc"}),
            lambda r: (f"Idempotency key cache size {r.randint(100, 5000)}", {"event": "idempotency.stats"}),
            lambda r: (f"Order state machine transition checked in {r.uniform(0.1, 2):.2f}ms", {"event": "fsm.check"}),
        ],
        "INFO": [
            lambda r: (f"Sent {r.randint(1, 60)} abandoned-cart reminders", {"event": "cart.reminders"}),
            lambda r: (f"Shipping label batch of {r.randint(5, 120)} created with {r.choice(_CARRIERS)}",
                       {"event": "shipping.labels"}),
            lambda r: (f"Fulfillment queue depth {r.randint(0, 80)}", {"event": "fulfillment.queue"}),
            lambda r: (f"Promo engine loaded {r.randint(3, 25)} active campaigns", {"event": "promo.load"}),
        ],
        "WARNING": [
            lambda r: (f"Carrier {r.choice(_CARRIERS)} rate API slow ({r.randint(900, 5000)}ms)",
                       {"event": "shipping.slow"}),
            lambda r: (f"Order ORD-{r.randint(100000, 999999)} retrying inventory reservation (attempt {r.randint(2, 4)})",
                       {"event": "order.retry"}),
            lambda r: (f"Duplicate checkout submission ignored for session {r.randint(10**8, 10**9)}",
                       {"event": "order.duplicate"}),
        ],
        "ERROR": [
            lambda r: (f"Failed to publish order event to queue: broker connection timeout", {"event": "queue.error"}),
            lambda r: (f"Shipping label creation failed with {r.choice(_CARRIERS)}: HTTP 500", {"event": "shipping.error"}),
            lambda r: (f"Invoice PDF render failed for ORD-{r.randint(100000, 999999)}", {"event": "invoice.error"}),
        ],
        "CRITICAL": [
            lambda r: ("Order event outbox backlog above 10000: downstream consumers stalled",
                       {"event": "outbox.backlog"}),
        ],
    },
    "payments": {
        "DEBUG": [
            lambda r: (f"Tokenized card in {r.uniform(1, 9):.1f}ms (vault=primary)", {"event": "vault.tokenize"}),
            lambda r: (f"Risk features computed: velocity={r.randint(0, 12)} geo_mismatch={r.random() < 0.1}",
                       {"event": "risk.features"}),
            lambda r: (f"Gateway keep-alive pool: {r.randint(2, 20)} idle connections", {"event": "gateway.pool"}),
        ],
        "INFO": [
            lambda r: (f"Settlement file accepted by {r.choice(_BANKS)} ({r.randint(20, 900)} transactions)",
                       {"event": "settlement.accepted"}),
            lambda r: (f"3-D Secure challenge completed in {r.randint(2, 40)}s", {"event": "3ds.completed"}),
            lambda r: (f"FX rates refreshed: {r.randint(20, 160)} currency pairs", {"event": "fx.refresh"}),
        ],
        "WARNING": [
            lambda r: (f"Gateway latency elevated: p95 {r.randint(700, 3000)}ms", {"event": "gateway.latency"}),
            lambda r: (f"Chargeback received for PAY-{r.randint(10**7, 10**8)}: reason=fraudulent",
                       {"event": "chargeback.received"}),
            lambda r: (f"{r.randint(2, 30)} payments held for manual fraud review", {"event": "risk.review"}),
        ],
        "ERROR": [
            lambda r: (f"Settlement upload to {r.choice(_BANKS)} failed: SFTP connection refused",
                       {"event": "settlement.error"}),
            lambda r: ("Webhook delivery to merchant endpoint failed after 5 retries", {"event": "webhook.error"}),
            lambda r: (f"Refund reconciliation mismatch: {r.randint(1, 9)} records differ", {"event": "recon.error"}),
        ],
        "CRITICAL": [
            lambda r: ("Primary card vault unreachable: failing over to secondary region", {"event": "vault.failover"}),
        ],
    },
}


def make(service: str, level: str, rng: random.Random) -> tuple[str, dict]:
    by_level = TEMPLATES.get(service, TEMPLATES["orders"])
    options = by_level.get(level) or by_level["INFO"]
    message, fields = rng.choice(options)(rng)
    return message, dict(fields)
