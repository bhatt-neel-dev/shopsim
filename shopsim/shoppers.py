"""Virtual shoppers: headless Chromium sessions that browse, search and buy in the storefront.

The pages carry the Motadata RUM SDK, so every page view, click, API call, long task and JS
error reaches Motadata the way a real visitor's would. How many sessions start, which journeys
they take and on which devices comes from the control panel (``Effective.frontend`` of ``web``),
refreshed every few seconds. The shoppers post their counters back to the panel.

    python -m shopsim shoppers        (needs: pip install -r requirements-shoppers.txt
                                       && playwright install --only-shell chromium)
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import time

import httpx
from playwright.async_api import Browser, Page, async_playwright

from shopsim import settings as settings_mod
from shopsim.config import FrontendEffective

log = logging.getLogger("shopsim.shoppers")

# Playwright device descriptors per device class (user agent, viewport, touch, scale).
DEVICES = {
    "desktop": ["Desktop Chrome", "Desktop Edge", "Desktop Firefox", "Desktop Safari"],
    "mobile": ["iPhone 14", "iPhone 13 Mini", "Pixel 7", "Galaxy S9+"],
    "tablet": ["iPad Pro 11", "iPad (gen 7)", "Galaxy Tab S4"],
}
# Where sessions "come from": a public range per country (for geo lookups on X-Forwarded-For),
# plus the browser locale and time zone to match.
GEO = [
    ("US", 30, "en-US", "America/Chicago", ["23.20.0.0/14", "98.137.0.0/16", "72.229.0.0/16"]),
    ("IN", 20, "en-IN", "Asia/Kolkata", ["49.36.0.0/14", "117.192.0.0/10", "106.192.0.0/11"]),
    ("GB", 9, "en-GB", "Europe/London", ["86.128.0.0/10", "81.2.0.0/16"]),
    ("DE", 8, "de-DE", "Europe/Berlin", ["91.64.0.0/14", "84.128.0.0/10"]),
    ("JP", 7, "ja-JP", "Asia/Tokyo", ["126.0.0.0/8", "153.128.0.0/9"]),
    ("BR", 7, "pt-BR", "America/Sao_Paulo", ["177.32.0.0/11", "189.0.0.0/11"]),
    ("FR", 6, "fr-FR", "Europe/Paris", ["90.0.0.0/9"]),
    ("AU", 5, "en-AU", "Australia/Sydney", ["1.120.0.0/13", "101.160.0.0/11"]),
    ("SG", 4, "en-SG", "Asia/Singapore", ["116.86.0.0/15"]),
    ("CA", 4, "en-CA", "America/Toronto", ["24.48.0.0/14"]),
]
SEARCH_WORDS = ["wireless", "pro", "smart", "leather", "kit", "air", "mini", "organic", "max", "set", "lamp", "shoes"]


def weighted(rng: random.Random, weights: dict[str, float], default: str) -> str:
    items = [(k, w) for k, w in (weights or {}).items() if w > 0]
    if not items:
        return default
    return rng.choices([k for k, _ in items], weights=[w for _, w in items])[0]


def public_ip(rng: random.Random, cidrs: list[str]) -> str:
    net = ipaddress.ip_network(rng.choice(cidrs))
    return str(net.network_address + rng.randint(1, net.num_addresses - 2))


class JourneyFailed(Exception):
    pass


class Shopper:
    """One browser session following one journey."""

    def __init__(self, page: Page, fe: FrontendEffective, rng: random.Random, base_url: str):
        self.page = page
        self.fe = fe
        self.rng = rng
        self.base = base_url.rstrip("/")
        self.views = 0

    async def think(self) -> None:
        mean = max(self.fe.think_time_ms, 50)
        await asyncio.sleep(min(max(self.rng.expovariate(1 / mean), 150), mean * 4) / 1000)

    async def settle(self) -> None:
        """Wait until the SPA finished rendering the current route."""
        try:
            await self.page.wait_for_function("() => !document.querySelector('#app .skeleton')", timeout=20000)
        except Exception as exc:  # noqa: BLE001
            raise JourneyFailed(f"page never rendered: {type(exc).__name__}") from exc
        self.views += 1

    async def click(self, selector: str, *, pick: bool = True) -> bool:
        items = self.page.locator(selector)
        count = await items.count()
        if not count:
            return False
        await items.nth(self.rng.randrange(count) if pick else 0).click()
        await self.settle()
        return True

    async def home(self) -> None:
        resp = await self.page.goto(self.base + "/", wait_until="domcontentloaded", timeout=30000)
        if resp is None or resp.status >= 500:
            raise JourneyFailed(f"storefront answered {resp.status if resp else 'nothing'}")
        await self.settle()
        try:
            await self.page.wait_for_selector("[data-testid=cat-link]", timeout=5000)
        except Exception:  # noqa: BLE001 - the category bar is optional
            pass

    async def category(self) -> bool:
        return await self.click("[data-testid=cat-link]")

    async def product(self) -> bool:
        if not await self.click("[data-testid=product-card]"):
            return False
        if self.rng.random() < self.fe.rage_click_rate:
            await self.rage_click()
        return True

    async def rage_click(self) -> None:
        button = self.page.locator("[data-testid=coupon]")
        if await button.count():
            for _ in range(self.rng.randint(4, 7)):
                await button.click(delay=20)
                await asyncio.sleep(self.rng.uniform(0.05, 0.15))

    async def add_to_cart(self) -> bool:
        button = self.page.locator("[data-testid=add-to-cart]")
        if not await button.count() or await button.is_disabled():
            return False
        if self.rng.random() < 0.3:
            await self.page.fill("#qty", str(self.rng.randint(2, 3)))
        await button.click()
        try:
            await self.page.wait_for_selector("#notice .alert", timeout=15000)
        except Exception:  # noqa: BLE001 - a slow or failed add is still part of the session
            pass
        return True

    async def search(self) -> None:
        await self.page.fill("[data-testid=search-input]", self.rng.choice(SEARCH_WORDS))
        await self.page.press("[data-testid=search-input]", "Enter")
        await self.settle()

    async def cart(self) -> None:
        await self.page.locator("header [data-testid=cart-link]").click()
        await self.settle()

    async def checkout(self, submit: bool) -> None:
        if not await self.click("[data-testid=checkout]", pick=False):
            return
        if not submit:
            await self.page.fill("input[name=coupon]", self.rng.choice(["", "WELCOME10", "SAVE20"]))
            return
        await self.think()
        await self.page.click("[data-testid=place-order]")
        try:
            await self.page.wait_for_selector("[data-testid=order-confirmation], #notice .alert.err", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        if await self.page.locator("[data-testid=order-confirmation]").count():
            await self.settle()

    async def account(self) -> None:
        await self.page.locator("header [data-testid=account-link]").click()
        await self.settle()
        if await self.page.locator("[data-testid=login-submit]").count():
            await self.think()
            await self.page.click("[data-testid=login-submit]")
            await self.settle()
        await self.think()
        if await self.click("[data-testid=orders-link]", pick=False):
            await self.think()
            await self.click("[data-testid=order-row]")

    # ---- journeys --------------------------------------------------------------------------------
    async def browse(self) -> None:
        await self.home()
        for _ in range(self.rng.randint(1, 3)):
            await self.think()
            await self.category()
            await self.think()
            await self.product()
            if self.rng.random() < 0.3:
                await self.think()
                await self.page.go_back()
                await self.settle()

    async def search_journey(self) -> None:
        await self.home()
        await self.think()
        await self.search()
        await self.think()
        if await self.product() and self.rng.random() < 0.5:
            await self.think()
            await self.add_to_cart()

    async def buy(self) -> None:
        await self.home()
        for _ in range(self.rng.choices([1, 2, 3], [60, 30, 10])[0]):
            await self.think()
            if self.rng.random() < 0.6:
                await self.category()
            else:
                await self.search()
            await self.think()
            if await self.product():
                await self.think()
                await self.add_to_cart()
        await self.think()
        await self.cart()
        await self.think()
        await self.checkout(submit=True)
        if self.rng.random() < 0.4:
            await self.think()
            await self.click("[data-testid=orders-link]", pick=False)

    async def abandon(self) -> None:
        await self.home()
        await self.think()
        await self.category()
        await self.think()
        if await self.product():
            await self.add_to_cart()
        await self.think()
        await self.cart()
        if self.rng.random() < 0.6:
            await self.think()
            await self.checkout(submit=False)

    async def account_journey(self) -> None:
        await self.home()
        await self.think()
        await self.account()


class Shoppers:
    def __init__(self):
        s = settings_mod.settings
        self.s = s
        self.rng = random.Random()
        self.fe = FrontendEffective(enabled=False)
        self.connected = False
        self.active = 0
        self.counters = {"started": 0, "completed": 0, "failed": 0, "skipped": 0, "views": 0}
        self.by_journey: dict[str, int] = {}
        self.by_device: dict[str, int] = {}
        self.by_country: dict[str, int] = {}
        self.last_error = ""
        self.browser_version = ""
        self.sessions_since_launch = 0

    # ---- control panel ---------------------------------------------------------------------------
    async def poll_forever(self, client: httpx.AsyncClient) -> None:
        while True:
            try:
                resp = await client.get(f"{self.s.control_url}/api/effective/web", timeout=3)
                resp.raise_for_status()
                fe = resp.json().get("frontend")
                if fe:
                    self.fe = FrontendEffective.model_validate(fe)
                if not self.connected:
                    log.info("connected to control panel at %s", self.s.control_url)
                self.connected = True
            except Exception as exc:  # noqa: BLE001 - keep the last known settings
                if self.connected:
                    log.warning("lost control panel: %s", exc)
                self.connected = False
            await asyncio.sleep(3)

    async def report_forever(self, client: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await client.post(f"{self.s.control_url}/api/rum/report", json=self.report(), timeout=3)
            except Exception:  # noqa: BLE001
                pass

    def report(self) -> dict:
        return {"active": self.active, **self.counters, "by_journey": self.by_journey, "by_device": self.by_device,
                "by_country": self.by_country, "last_error": self.last_error, "browser": self.browser_version,
                "cap": self.cap(), "storefront": self.s.storefront_url, "connected": self.connected}

    def cap(self) -> int:
        return max(1, min(self.fe.max_concurrent, self.s.max_shoppers))

    # ---- sessions --------------------------------------------------------------------------------
    async def session(self, browser: Browser, pw) -> None:
        fe, rng = self.fe, self.rng
        journey = weighted(rng, fe.journeys, "browse")
        device = weighted(rng, fe.devices, "desktop")
        country, _, locale, tz, cidrs = rng.choices(GEO, weights=[g[1] for g in GEO])[0]
        descriptor = dict(pw.devices[rng.choice(DEVICES[device])])
        descriptor.pop("default_browser_type", None)
        options = {**descriptor, "locale": locale, "timezone_id": tz, "ignore_https_errors": True}
        if fe.geo_spoof:
            options["extra_http_headers"] = {"X-Forwarded-For": public_ip(rng, cidrs)}
        for table, key in ((self.by_journey, journey), (self.by_device, device), (self.by_country, country)):
            table[key] = table.get(key, 0) + 1
        self.counters["started"] += 1
        self.active += 1
        context = await browser.new_context(**options)
        page = await context.new_page()
        page.set_default_timeout(20000)
        shopper = Shopper(page, fe, rng, self.s.storefront_url)
        steps = {"browse": shopper.browse, "search": shopper.search_journey, "buy": shopper.buy,
                 "abandon": shopper.abandon, "account": shopper.account_journey}
        try:
            await steps[journey]()
            await shopper.think()
            self.counters["completed"] += 1
        except Exception as exc:  # noqa: BLE001 - an outage or a timeout ends the session early
            self.counters["failed"] += 1
            self.last_error = f"{journey}/{device}: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            log.info("session failed: %s", self.last_error)
        finally:
            self.counters["views"] += shopper.views
            try:
                # Leaving the page makes the SDK flush what it still holds.
                await page.goto("about:blank", timeout=5000)
                await asyncio.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass
            await context.close()
            self.active -= 1
            self.sessions_since_launch += 1

    async def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s shopsim-shoppers %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        log.info("shoppers starting: storefront %s, at most %d browsers", self.s.storefront_url, self.s.max_shoppers)
        async with httpx.AsyncClient() as client, async_playwright() as pw:
            loop = asyncio.get_running_loop()
            tasks = [loop.create_task(self.poll_forever(client)), loop.create_task(self.report_forever(client))]
            browser: Browser | None = None
            sessions: set[asyncio.Task] = set()
            tick = 0.5
            try:
                while True:
                    await asyncio.sleep(tick)
                    fe = self.fe
                    if browser is not None and (not browser.is_connected() or (
                            self.sessions_since_launch >= 150 and not sessions)):
                        await self._close(browser)
                        browser = None
                    rate = fe.sessions_per_min if (fe.enabled and fe.web_state != "down") else 0
                    expected = rate / 60 * tick
                    arrivals = int(expected) + (1 if self.rng.random() < expected - int(expected) else 0)
                    for _ in range(arrivals):
                        if len(sessions) >= self.cap():
                            self.counters["skipped"] += 1
                            continue
                        if browser is None:
                            browser = await self._launch(pw)
                        task = loop.create_task(self.session(browser, pw))
                        sessions.add(task)
                        task.add_done_callback(sessions.discard)
            finally:
                for task in [*tasks, *sessions]:
                    task.cancel()
                if browser is not None:
                    await self._close(browser)

    async def _launch(self, pw) -> Browser:
        browser = await pw.chromium.launch(headless=True, args=[
            "--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--disable-extensions",
            "--js-flags=--max-old-space-size=256"])
        self.browser_version = f"chromium {browser.version}"
        self.sessions_since_launch = 0
        log.info("launched %s", self.browser_version)
        return browser

    @staticmethod
    async def _close(browser: Browser) -> None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    try:
        asyncio.run(Shoppers().run())
    except KeyboardInterrupt:
        pass
