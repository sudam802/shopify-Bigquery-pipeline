"""
Create test orders in Shopify via Admin REST.

Usage:
    python scripts/generate_test_orders.py
    python scripts/generate_test_orders.py --count 50
    python scripts/generate_test_orders.py --count 1000 --delay 0.6

Auth (first match wins):
    SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (client_credentials)
    or SHOPIFY_ACCESS_TOKEN
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter

import requests
from dotenv import load_dotenv
from faker import Faker

load_dotenv()
fake = Faker()

SHOP = (
    os.getenv("SHOPIFY_STORE", "")
    .strip()
    .removeprefix("https://")
    .removeprefix("http://")
    .rstrip("/")
)
CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
STATIC_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
REQUEST_TIMEOUT = 60

PRODUCTS = [
    ("Wireless Bluetooth Earbuds", 39.99, 12),
    ("Stainless Steel Water Bottle", 24.50, 9),
    ("Organic Cotton T-Shirt", 19.99, 14),
    ("Leather Laptop Sleeve", 45.00, 6),
    ("Ceramic Coffee Mug Set", 28.75, 8),
    ("Yoga Mat Non-Slip", 32.00, 7),
    ("Portable Phone Charger 10000mAh", 21.99, 10),
    ("Scented Soy Candle", 16.50, 11),
    ("Minimalist Desk Lamp", 54.99, 5),
    ("Canvas Tote Bag", 18.00, 10),
    ("Noise Cancelling Headphones", 89.99, 4),
    ("Stainless Steel Kitchen Knife Set", 65.00, 3),
    ("Running Shoes - Men's", 74.99, 5),
    ("Running Shoes - Women's", 74.99, 5),
    ("Bamboo Cutting Board", 22.30, 8),
]

FINANCIAL_STATUSES = [
    ("paid", 55),
    ("pending", 15),
    ("authorized", 10),
    ("partially_refunded", 8),
    ("refunded", 7),
    ("voided", 5),
]


def require_shop() -> None:
    if not SHOP:
        sys.exit("SHOPIFY_STORE is not set (e.g. your-store.myshopify.com)")


def get_access_token() -> str:
    # Prefer client credentials: static shpua_ tokens expire quickly.
    if CLIENT_ID and CLIENT_SECRET:
        url = f"https://{SHOP}/admin/oauth/access_token"
        resp = requests.post(
            url,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            sys.exit(f"Shopify token request failed ({resp.status_code}): {resp.text}")
        token = resp.json().get("access_token")
        if not token:
            sys.exit(f"Shopify token response had no access_token: {resp.text}")
        return token
    if STATIC_TOKEN:
        return STATIC_TOKEN
    sys.exit("Set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET, or SHOPIFY_ACCESS_TOKEN")


def weighted_choice(pairs: list[tuple]):
    items = [p[:-1] if len(p) > 2 else p[0] for p in pairs]
    weights = [p[-1] for p in pairs]
    return random.choices(items, weights=weights, k=1)[0]


def pick_customer(pool: list[dict], reuse_rate: float) -> dict:
    if pool and random.random() < reuse_rate:
        return random.choice(pool)
    first_name = fake.first_name()
    last_name = fake.last_name()
    email = (
        f"{first_name.lower()}.{last_name.lower()}"
        f"{random.randint(1, 9999)}@{fake.free_email_domain()}"
    )
    customer = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "address": {
            "first_name": first_name,
            "last_name": last_name,
            "address1": fake.street_address(),
            "city": fake.city(),
            "province": fake.state(),
            "country": "United States",
            "zip": fake.postcode(),
        },
    }
    pool.append(customer)
    return customer


def build_line_items() -> list[dict]:
    num_items = random.choices([1, 2, 3, 4, 5], weights=[35, 30, 20, 10, 5], k=1)[0]
    chosen = set()
    line_items = []
    for _ in range(num_items):
        title, price = weighted_choice(PRODUCTS)
        # Avoid Shopify merging identical custom lines into one quantity.
        attempt = 0
        unique_title = title
        while unique_title in chosen and attempt < 8:
            title, price = weighted_choice(PRODUCTS)
            unique_title = title
            attempt += 1
        if unique_title in chosen:
            unique_title = f"{title} ({fake.color_name()})"
        chosen.add(unique_title)
        line_items.append(
            {
                "title": unique_title,
                "price": f"{price:.2f}",
                "quantity": random.choices([1, 2, 3], weights=[70, 25, 5], k=1)[0],
            }
        )
    return line_items


def build_payload(customer: dict) -> dict:
    financial_status = weighted_choice(FINANCIAL_STATUSES)
    order = {
        "line_items": build_line_items(),
        "customer": {
            "first_name": customer["first_name"],
            "last_name": customer["last_name"],
            "email": customer["email"],
        },
        "email": customer["email"],
        "billing_address": customer["address"],
        "shipping_address": customer["address"],
        "financial_status": financial_status,
        "tags": "synthetic,generator",
        "note": "Created by scripts/generate_test_orders.py",
        "inventory_behaviour": "bypass",
        "send_receipt": False,
        "send_fulfillment_receipt": False,
    }
    if random.random() < 0.25:
        order["discount_codes"] = [
            {
                "code": random.choice(["WELCOME10", "SAVE15", "VIP20", "FREESHIP"]),
                "amount": random.choice(["5.00", "8.50", "10.00", "15.00"]),
                "type": "fixed_amount",
            }
        ]
    if random.random() < 0.20:
        order["tax_lines"] = [
            {
                "price": f"{random.choice([1.25, 2.40, 3.75, 5.10]):.2f}",
                "rate": 0.08,
                "title": "State tax",
            }
        ]
    return {"order": order}


def post_order(session: requests.Session, url: str, payload: dict) -> tuple[int, dict | str]:
    last_status = 0
    last_body: dict | str = ""
    for attempt in range(8):
        try:
            resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            wait = min(60, 2 ** attempt)
            print(f"  connection error ({exc}); retrying in {wait}s")
            time.sleep(wait)
            last_status = 0
            last_body = str(exc)
            continue
        last_status = resp.status_code
        if resp.status_code == 429:
            body = resp.text
            wait = 60.0 if "order API rate limit" in body else float(
                resp.headers.get("Retry-After", max(15, 2 ** attempt))
            )
            print(f"  rate limited, sleeping {wait:.0f}s")
            time.sleep(wait)
            last_body = body
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  Shopify {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            last_body = resp.text
            continue
        if resp.status_code == 201:
            return 201, resp.json()["order"]
        return resp.status_code, resp.text
    return last_status, last_body


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create synthetic Shopify orders")
    parser.add_argument(
        "--count",
        type=int,
        default=40,
        help="Number of orders to create (default: 40)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=16.0,
        help="Seconds to wait between creates (default: 16; Shopify dev stores cap order creates)",
    )
    parser.add_argument(
        "--reuse-rate",
        type=float,
        default=0.35,
        help="Chance an order reuses an earlier customer from this run (default: 0.35)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    require_shop()
    if args.count < 1:
        sys.exit("--count must be at least 1")

    token = get_access_token()
    url = f"https://{SHOP}/admin/api/{API_VERSION}/orders.json"
    session = requests.Session()
    session.headers.update(
        {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
    )

    pool: list[dict] = []
    created = 0
    failed = 0
    status_counts: Counter[str] = Counter()

    print(f"Creating {args.count} orders on {SHOP} (delay {args.delay}s)...")
    for i in range(1, args.count + 1):
        customer = pick_customer(pool, args.reuse_rate)
        payload = build_payload(customer)
        code, body = post_order(session, url, payload)
        if code == 201 and isinstance(body, dict):
            created += 1
            status_counts[body.get("financial_status") or "unknown"] += 1
            print(
                f"[{i}/{args.count}] {body.get('name')} "
                f"{body.get('financial_status')} "
                f"{customer['email']}"
            )
        else:
            failed += 1
            print(f"[{i}/{args.count}] Failed: {code} {body}")
            if code in (401, 403):
                sys.exit("Stopped: Shopify rejected the credentials or write_orders scope.")
        if i < args.count:
            time.sleep(args.delay)

    print(
        f"Done. Created {created}, failed {failed}, "
        f"unique customers this run {len(pool)}."
    )
    if status_counts:
        print("Financial status mix:", dict(status_counts))
    print("Re-run scripts/sync_shopify_to_postgres.py --orders-only to load them into Postgres.")


if __name__ == "__main__":
    main()
