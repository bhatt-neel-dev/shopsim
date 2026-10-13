"""Fake but plausible shop data: products, customers, payment methods, clients."""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

from faker import Faker

CATEGORIES: dict[str, dict] = {
    "Electronics": {"code": "ELE", "price": (19, 1499), "items": [
        "Wireless Earbuds", "4K Smart TV", "Bluetooth Speaker", "Gaming Mouse", "Mechanical Keyboard",
        "USB-C Hub", "Smartwatch", "Noise-Cancelling Headphones", "Portable SSD", "HD Webcam"]},
    "Home & Kitchen": {"code": "HOM", "price": (9, 499), "items": [
        "Air Fryer", "Espresso Machine", "Chef's Knife", "Cast Iron Skillet", "Robot Vacuum",
        "Blender", "Electric Kettle", "Bedsheet Set", "Desk Lamp", "Storage Bins"]},
    "Fashion": {"code": "FSH", "price": (12, 249), "items": [
        "Running Shoes", "Denim Jacket", "Leather Belt", "Cotton T-Shirt", "Wool Scarf",
        "Chino Trousers", "Rain Jacket", "Canvas Sneakers", "Backpack", "Sunglasses"]},
    "Sports & Outdoors": {"code": "SPT", "price": (8, 899), "items": [
        "Yoga Mat", "Camping Tent", "Trail Bike Helmet", "Dumbbell Set", "Hiking Boots",
        "Water Bottle", "Sleeping Bag", "Tennis Racket", "Fitness Tracker", "Climbing Rope"]},
    "Books": {"code": "BOK", "price": (6, 79), "items": [
        "Mystery Novel", "Cookbook", "Programming Guide", "Travel Atlas", "Poetry Collection",
        "Biography", "Sci-Fi Anthology", "Children's Picture Book", "History of Trade", "Design Handbook"]},
    "Beauty": {"code": "BTY", "price": (5, 149), "items": [
        "Face Serum", "Hair Dryer", "Sunscreen SPF 50", "Beard Trimmer", "Moisturizer",
        "Perfume", "Makeup Brush Set", "Shampoo", "Lip Balm Pack", "Electric Toothbrush"]},
    "Toys & Games": {"code": "TOY", "price": (7, 199), "items": [
        "Building Blocks", "Board Game", "RC Car", "Puzzle 1000pc", "Plush Bear",
        "Art Kit", "Drone Mini", "Card Game", "Train Set", "Science Kit"]},
    "Grocery": {"code": "GRO", "price": (2, 59), "items": [
        "Organic Coffee Beans", "Green Tea", "Olive Oil", "Basmati Rice", "Dark Chocolate",
        "Almonds", "Honey", "Pasta Pack", "Protein Bars", "Maple Syrup"]},
}
BRANDS = ["Voltix", "Nordway", "Kestrel", "Aurora", "Lumen", "Northpeak", "Bramble", "Cobalt", "Juniper",
          "Halcyon", "Orbit", "Tidewater", "Ember", "Quill", "Sable"]
VARIANTS = ["Pro", "Ultra", "Lite", "Max", "Air", "Plus", "Mini", "X2", "Elite", "Classic", "Sport", "Eco"]
COUNTRIES = {
    "US": (["New York", "Austin", "Seattle", "Chicago", "Denver", "Boston"], 0.07, 30),
    "IN": (["Mumbai", "Bengaluru", "Ahmedabad", "Pune", "Delhi", "Hyderabad"], 0.18, 25),
    "GB": (["London", "Manchester", "Leeds", "Bristol"], 0.20, 10),
    "DE": (["Berlin", "Munich", "Hamburg", "Cologne"], 0.19, 8),
    "CA": (["Toronto", "Vancouver", "Montreal"], 0.13, 7),
    "AU": (["Sydney", "Melbourne", "Brisbane"], 0.10, 7),
    "SG": (["Singapore"], 0.09, 5),
    "AE": (["Dubai", "Abu Dhabi"], 0.05, 8),
}
PAYMENT_METHODS = {"card": 55, "upi": 15, "paypal": 10, "wallet": 8, "netbanking": 5, "gift_card": 4, "cod": 3}
CARD_BRANDS = {"visa": 45, "mastercard": 35, "amex": 10, "rupay": 6, "discover": 4}
TIERS = {"standard": 70, "silver": 18, "gold": 9, "platinum": 3}
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36",
    "ShopSimApp/4.2.1 (Android 16; SM-S931B)",
    "ShopSimApp/4.2.0 (iOS 19.0; iPhone17,2)",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def weighted(rng: random.Random, table: dict) -> str:
    return rng.choices(list(table), weights=list(table.values()))[0]


class FakeData:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.faker = Faker()
        if seed is not None:
            self.faker.seed_instance(seed)

    def product(self) -> dict:
        rng = self.rng
        category = rng.choice(list(CATEGORIES))
        meta = CATEGORIES[category]
        brand = rng.choice(BRANDS)
        item = rng.choice(meta["items"])
        name = f"{brand} {item}" + (f" {rng.choice(VARIANTS)}" if rng.random() < 0.6 else "")
        low, high = meta["price"]
        price = round(rng.uniform(low, high), 2)
        return {
            "sku": f"{meta['code']}-{brand[:3].upper()}-{uuid.uuid4().hex[:8].upper()}",
            "name": name, "category": category, "brand": brand, "price": price, "currency": "USD",
            "stock": rng.randint(0, 400) if rng.random() > 0.05 else 0,
            "rating": round(rng.uniform(2.8, 5.0), 1),
        }

    def customer(self) -> dict:
        rng = self.rng
        country = rng.choices(list(COUNTRIES), weights=[c[2] for c in COUNTRIES.values()])[0]
        name = self.faker.name()
        user = name.lower().replace(" ", ".").replace("'", "")
        return {
            "id": f"CUS-{rng.randint(100000, 999999)}",
            "name": name,
            "email": f"{user}{rng.randint(1, 99)}@{rng.choice(['example.com', 'mail.test', 'shopsim.dev'])}",
            "tier": weighted(rng, TIERS),
            "city": rng.choice(COUNTRIES[country][0]),
            "country": country,
        }

    def payment(self) -> tuple[str, dict | None]:
        method = weighted(self.rng, PAYMENT_METHODS)
        if method != "card":
            return method, None
        return method, {"brand": weighted(self.rng, CARD_BRANDS), "last4": f"{self.rng.randint(0, 9999):04d}"}

    def client_ip(self) -> str:
        return self.faker.ipv4_public()

    def user_agent(self) -> str:
        return self.rng.choice(USER_AGENTS)


def tax_rate(country: str) -> float:
    return COUNTRIES.get(country, ([], 0.1, 0))[1]
