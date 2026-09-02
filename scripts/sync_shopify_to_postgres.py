"""
Pull Shopify Admin data into local Postgres for later querying.

Usage:
    python scripts/sync_shopify_to_postgres.py
    python scripts/sync_shopify_to_postgres.py --full
    python scripts/sync_shopify_to_postgres.py --since 2026-01-01T00:00:00Z
    python scripts/sync_shopify_to_postgres.py --orders-only

Auth (first match wins):
    SHOPIFY_ACCESS_TOKEN
    or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (client_credentials)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import urlencode

import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_batch

load_dotenv()

SHOP = os.getenv("SHOPIFY_STORE", "").strip().removeprefix("https://").removeprefix("http://").rstrip("/")
CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
STATIC_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "shopify_raw")
PG_USER = os.getenv("PG_USER", "airbyte")
PG_PASSWORD = os.getenv("PG_PASSWORD", "airbyte123")

PAGE_SIZE = 250
REQUEST_TIMEOUT = 60
MAX_RETRIES = 6
OVERLAP = timedelta(minutes=2)


def require_shop() -> None:
    if not SHOP:
        sys.exit("SHOPIFY_STORE is not set (e.g. your-store.myshopify.com)")


def get_access_token() -> str:
    if STATIC_TOKEN:
        return STATIC_TOKEN
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit(
            "Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET"
        )
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


def next_page_url(link_header: str) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def shopify_get(session: requests.Session, url: str) -> requests.Response:
    for attempt in range(MAX_RETRIES):
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  rate limited, sleeping {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  Shopify {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code in (401, 403):
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for url: {resp.url}\n{resp.text}",
                response=resp,
            )
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def paginate(
    session: requests.Session,
    path: str,
    params: dict[str, Any],
    key: str,
) -> Iterator[dict[str, Any]]:
    url = f"https://{SHOP}/admin/api/{API_VERSION}/{path}?{urlencode(params)}"
    while url:
        resp = shopify_get(session, url)
        records = resp.json().get(key, [])
        yield from records
        url = next_page_url(resp.headers.get("Link", ""))


def gid_num(gid: str | None) -> int | None:
    if not gid:
        return None
    return int(str(gid).rsplit("/", 1)[-1])


def money_amount(price_set: dict[str, Any] | None) -> str | None:
    amount = ((price_set or {}).get("shopMoney") or {}).get("amount")
    return amount


def shopify_graphql(session: requests.Session, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    for attempt in range(MAX_RETRIES):
        resp = session.post(url, json={"query": query, "variables": variables}, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  rate limited, sleeping {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for url: {resp.url}\n{resp.text}",
                response=resp,
            )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            throttle = any(
                (err.get("extensions") or {}).get("code") == "THROTTLED"
                for err in payload["errors"]
            )
            if throttle:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(payload["errors"])
        cost = ((payload.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
        available = cost.get("currentlyAvailable")
        if available is not None and available < 100:
            time.sleep(1)
        return payload["data"]
    raise RuntimeError("Shopify GraphQL retries exhausted")


ORDERS_GQL = """
query Orders($cursor: String, $query: String) {
  orders(first: 50, after: $cursor, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        number
        createdAt
        updatedAt
        processedAt
        cancelledAt
        displayFinancialStatus
        displayFulfillmentStatus
        tags
        email
        customer { id email }
        currentSubtotalPriceSet { shopMoney { amount currencyCode } }
        currentTotalTaxSet { shopMoney { amount } }
        currentTotalDiscountsSet { shopMoney { amount } }
        currentTotalPriceSet { shopMoney { amount currencyCode } }
        lineItems(first: 100) {
          edges {
            node {
              id
              title
              sku
              quantity
              originalUnitPriceSet { shopMoney { amount } }
              totalDiscountSet { shopMoney { amount } }
              variant { id }
              product { id }
            }
          }
        }
      }
    }
  }
}
"""


def graphql_order_to_row(node: dict[str, Any]) -> dict[str, Any]:
    currency = (
        ((node.get("currentTotalPriceSet") or {}).get("shopMoney") or {}).get("currencyCode")
    )
    tags = node.get("tags") or []
    customer = node.get("customer") or {}
    return {
        "id": gid_num(node.get("id")),
        "name": node.get("name"),
        "order_number": node.get("number"),
        "email": node.get("email") or customer.get("email"),
        "customer": {"id": gid_num(customer.get("id")), "email": customer.get("email")},
        "financial_status": (node.get("displayFinancialStatus") or "").lower() or None,
        "fulfillment_status": (node.get("displayFulfillmentStatus") or "").lower() or None,
        "currency": currency,
        "subtotal_price": money_amount(node.get("currentSubtotalPriceSet")),
        "total_tax": money_amount(node.get("currentTotalTaxSet")),
        "total_discounts": money_amount(node.get("currentTotalDiscountsSet")),
        "total_price": money_amount(node.get("currentTotalPriceSet")),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "processed_at": node.get("processedAt"),
        "cancelled_at": node.get("cancelledAt"),
        "tags": ", ".join(tags) if isinstance(tags, list) else tags,
        "line_items": [
            {
                "id": gid_num(item.get("id")),
                "product_id": gid_num((item.get("product") or {}).get("id")),
                "variant_id": gid_num((item.get("variant") or {}).get("id")),
                "title": item.get("title"),
                "sku": item.get("sku"),
                "quantity": item.get("quantity"),
                "price": money_amount(item.get("originalUnitPriceSet")),
                "total_discount": money_amount(item.get("totalDiscountSet")),
            }
            for edge in (node.get("lineItems") or {}).get("edges") or []
            for item in [edge.get("node") or {}]
            if item.get("id")
        ],
    }


def fetch_orders_graphql(session: requests.Session, since: datetime | None) -> Iterator[dict[str, Any]]:
    query_filter = None
    if since:
        query_filter = f"updated_at:>='{since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    cursor = None
    while True:
        data = shopify_graphql(
            session,
            ORDERS_GQL,
            {"cursor": cursor, "query": query_filter},
        )
        connection = data["orders"]
        for edge in connection.get("edges") or []:
            node = edge.get("node")
            if node:
                yield graphql_order_to_row(node)
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")


def connect_db():
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
    )


def create_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                resource TEXT PRIMARY KEY,
                last_synced_at TIMESTAMPTZ,
                last_run_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS shopify_customers (
                id BIGINT PRIMARY KEY,
                email TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                orders_count INTEGER,
                total_spent NUMERIC,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ,
                raw_data JSONB NOT NULL,
                synced_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS shopify_products (
                id BIGINT PRIMARY KEY,
                title TEXT,
                handle TEXT,
                vendor TEXT,
                product_type TEXT,
                status TEXT,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ,
                raw_data JSONB NOT NULL,
                synced_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS shopify_variants (
                id BIGINT PRIMARY KEY,
                product_id BIGINT,
                title TEXT,
                sku TEXT,
                price NUMERIC,
                inventory_quantity INTEGER,
                raw_data JSONB NOT NULL,
                synced_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS shopify_orders (
                id BIGINT PRIMARY KEY,
                name TEXT,
                order_number BIGINT,
                email TEXT,
                customer_id BIGINT,
                financial_status TEXT,
                fulfillment_status TEXT,
                currency TEXT,
                subtotal_price NUMERIC,
                total_tax NUMERIC,
                total_discounts NUMERIC,
                total_price NUMERIC,
                created_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ,
                processed_at TIMESTAMPTZ,
                cancelled_at TIMESTAMPTZ,
                tags TEXT,
                raw_data JSONB NOT NULL,
                synced_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS shopify_order_line_items (
                id BIGINT PRIMARY KEY,
                order_id BIGINT NOT NULL REFERENCES shopify_orders(id) ON DELETE CASCADE,
                product_id BIGINT,
                variant_id BIGINT,
                title TEXT,
                sku TEXT,
                quantity INTEGER,
                price NUMERIC,
                total_discount NUMERIC,
                raw_data JSONB NOT NULL,
                synced_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON shopify_orders (created_at);
            CREATE INDEX IF NOT EXISTS idx_orders_updated_at ON shopify_orders (updated_at);
            CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON shopify_orders (customer_id);
            CREATE INDEX IF NOT EXISTS idx_orders_financial_status ON shopify_orders (financial_status);
            CREATE INDEX IF NOT EXISTS idx_line_items_order_id ON shopify_order_line_items (order_id);
            CREATE INDEX IF NOT EXISTS idx_customers_email ON shopify_customers (email);
            """
        )
    conn.commit()


def get_watermark(conn, resource: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT last_synced_at FROM sync_state WHERE resource = %s", (resource,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def set_watermark(conn, resource: str, ts: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (resource, last_synced_at, last_run_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (resource) DO UPDATE SET
                last_synced_at = EXCLUDED.last_synced_at,
                last_run_at = NOW()
            """,
            (resource, ts),
        )
    conn.commit()


def parse_shopify_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def max_updated_at(records: list[dict[str, Any]]) -> datetime | None:
    stamps = [parse_shopify_dt(r.get("updated_at")) for r in records]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def list_params(since: datetime | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": PAGE_SIZE}
    if since:
        params["updated_at_min"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return params


def upsert_customers(conn, customers: list[dict[str, Any]]) -> int:
    if not customers:
        return 0
    rows = [
        (
            c["id"],
            c.get("email"),
            c.get("first_name"),
            c.get("last_name"),
            c.get("phone"),
            c.get("orders_count"),
            c.get("total_spent"),
            c.get("created_at"),
            c.get("updated_at"),
            Json(c),
        )
        for c in customers
    ]
    with conn.cursor() as cur:
        execute_batch(
            cur,
            """
            INSERT INTO shopify_customers (
                id, email, first_name, last_name, phone, orders_count, total_spent,
                created_at, updated_at, raw_data, synced_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                phone = EXCLUDED.phone,
                orders_count = EXCLUDED.orders_count,
                total_spent = EXCLUDED.total_spent,
                updated_at = EXCLUDED.updated_at,
                raw_data = EXCLUDED.raw_data,
                synced_at = NOW()
            """,
            rows,
            page_size=200,
        )
    conn.commit()
    return len(rows)


def upsert_products(conn, products: list[dict[str, Any]]) -> tuple[int, int]:
    if not products:
        return 0, 0
    product_rows = [
        (
            p["id"],
            p.get("title"),
            p.get("handle"),
            p.get("vendor"),
            p.get("product_type"),
            p.get("status"),
            p.get("created_at"),
            p.get("updated_at"),
            Json(p),
        )
        for p in products
    ]
    variant_rows = [
        (
            v["id"],
            p["id"],
            v.get("title"),
            v.get("sku"),
            v.get("price"),
            v.get("inventory_quantity"),
            Json(v),
        )
        for p in products
        for v in p.get("variants") or []
    ]
    with conn.cursor() as cur:
        execute_batch(
            cur,
            """
            INSERT INTO shopify_products (
                id, title, handle, vendor, product_type, status,
                created_at, updated_at, raw_data, synced_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                handle = EXCLUDED.handle,
                vendor = EXCLUDED.vendor,
                product_type = EXCLUDED.product_type,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at,
                raw_data = EXCLUDED.raw_data,
                synced_at = NOW()
            """,
            product_rows,
            page_size=200,
        )
        if variant_rows:
            execute_batch(
                cur,
                """
                INSERT INTO shopify_variants (
                    id, product_id, title, sku, price, inventory_quantity, raw_data, synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    product_id = EXCLUDED.product_id,
                    title = EXCLUDED.title,
                    sku = EXCLUDED.sku,
                    price = EXCLUDED.price,
                    inventory_quantity = EXCLUDED.inventory_quantity,
                    raw_data = EXCLUDED.raw_data,
                    synced_at = NOW()
                """,
                variant_rows,
                page_size=200,
            )
    conn.commit()
    return len(product_rows), len(variant_rows)


def upsert_orders(conn, orders: list[dict[str, Any]]) -> tuple[int, int]:
    if not orders:
        return 0, 0
    order_rows = []
    line_rows = []
    for order in orders:
        customer = order.get("customer") or {}
        order_rows.append(
            (
                order["id"],
                order.get("name"),
                order.get("order_number"),
                order.get("email") or customer.get("email"),
                customer.get("id"),
                order.get("financial_status"),
                order.get("fulfillment_status"),
                order.get("currency"),
                order.get("subtotal_price"),
                order.get("total_tax"),
                order.get("total_discounts"),
                order.get("total_price"),
                order.get("created_at"),
                order.get("updated_at"),
                order.get("processed_at"),
                order.get("cancelled_at"),
                order.get("tags"),
                Json(order),
            )
        )
        for item in order.get("line_items") or []:
            line_rows.append(
                (
                    item["id"],
                    order["id"],
                    item.get("product_id"),
                    item.get("variant_id"),
                    item.get("title"),
                    item.get("sku"),
                    item.get("quantity"),
                    item.get("price"),
                    item.get("total_discount"),
                    Json(item),
                )
            )
    with conn.cursor() as cur:
        execute_batch(
            cur,
            """
            INSERT INTO shopify_orders (
                id, name, order_number, email, customer_id, financial_status,
                fulfillment_status, currency, subtotal_price, total_tax,
                total_discounts, total_price, created_at, updated_at,
                processed_at, cancelled_at, tags, raw_data, synced_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
            )
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                customer_id = EXCLUDED.customer_id,
                financial_status = EXCLUDED.financial_status,
                fulfillment_status = EXCLUDED.fulfillment_status,
                currency = EXCLUDED.currency,
                subtotal_price = EXCLUDED.subtotal_price,
                total_tax = EXCLUDED.total_tax,
                total_discounts = EXCLUDED.total_discounts,
                total_price = EXCLUDED.total_price,
                updated_at = EXCLUDED.updated_at,
                processed_at = EXCLUDED.processed_at,
                cancelled_at = EXCLUDED.cancelled_at,
                tags = EXCLUDED.tags,
                raw_data = EXCLUDED.raw_data,
                synced_at = NOW()
            """,
            order_rows,
            page_size=100,
        )
        if line_rows:
            execute_batch(
                cur,
                """
                INSERT INTO shopify_order_line_items (
                    id, order_id, product_id, variant_id, title, sku, quantity,
                    price, total_discount, raw_data, synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    order_id = EXCLUDED.order_id,
                    product_id = EXCLUDED.product_id,
                    variant_id = EXCLUDED.variant_id,
                    title = EXCLUDED.title,
                    sku = EXCLUDED.sku,
                    quantity = EXCLUDED.quantity,
                    price = EXCLUDED.price,
                    total_discount = EXCLUDED.total_discount,
                    raw_data = EXCLUDED.raw_data,
                    synced_at = NOW()
                """,
                line_rows,
                page_size=200,
            )
    conn.commit()
    return len(order_rows), len(line_rows)


def resolve_since(conn, resource: str, args: argparse.Namespace) -> datetime | None:
    if args.full:
        return None
    if args.since:
        return datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    watermark = get_watermark(conn, resource)
    if watermark:
        return watermark - OVERLAP
    return None


def sync_resource(
    conn,
    session: requests.Session,
    resource: str,
    path: str,
    key: str,
    extra_params: dict[str, Any],
    args: argparse.Namespace,
    writer,
    records: Iterator[dict[str, Any]] | None = None,
) -> int:
    since = resolve_since(conn, resource, args)
    params = {**list_params(since), **extra_params}
    label = resource
    if since:
        print(f"Fetching {label} updated since {since.isoformat()}...")
    else:
        print(f"Fetching all {label}...")

    batch: list[dict[str, Any]] = []
    count = 0
    newest: datetime | None = since
    extra_counts: dict[str, int] = {}
    stream = records if records is not None else paginate(session, path, params, key)

    for record in stream:
        batch.append(record)
        if len(batch) >= 250:
            written = writer(conn, batch)
            if isinstance(written, tuple):
                count += written[0]
                extra_counts["nested"] = extra_counts.get("nested", 0) + written[1]
            else:
                count += written
            stamp = max_updated_at(batch)
            if stamp and (newest is None or stamp > newest):
                newest = stamp
            print(f"  wrote {count} {label}...")
            batch = []

    if batch:
        written = writer(conn, batch)
        if isinstance(written, tuple):
            count += written[0]
            extra_counts["nested"] = extra_counts.get("nested", 0) + written[1]
        else:
            count += written
        stamp = max_updated_at(batch)
        if stamp and (newest is None or stamp > newest):
            newest = stamp

    if newest:
        set_watermark(conn, resource, newest)

    if extra_counts:
        print(f"  synced {count} {label} (+ {extra_counts['nested']} nested rows)")
    else:
        print(f"  synced {count} {label}")
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Shopify data into local Postgres")
    parser.add_argument("--full", action="store_true", help="Ignore watermarks and pull everything")
    parser.add_argument("--since", help="ISO timestamp, e.g. 2026-01-01T00:00:00Z")
    parser.add_argument("--orders-only", action="store_true", help="Skip customers and products")
    return parser.parse_args()


def main() -> None:
    require_shop()
    args = parse_args()

    print("Connecting to Postgres...")
    conn = connect_db()
    create_tables(conn)

    print("Authenticating with Shopify...")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"X-Shopify-Access-Token": token, "Accept": "application/json"})

    if not args.orders_only:
        try:
            sync_resource(
                conn, session, "customers", "customers.json", "customers", {}, args, upsert_customers
            )
        except requests.HTTPError as exc:
            print(f"Skipping customers ({exc})")
        try:
            sync_resource(
                conn, session, "products", "products.json", "products", {}, args, upsert_products
            )
        except requests.HTTPError as exc:
            print(f"Skipping products ({exc})")

    since = resolve_since(conn, "orders", args)
    sync_resource(
        conn,
        session,
        "orders",
        "orders.json",
        "orders",
        {"status": "any"},
        args,
        upsert_orders,
        records=fetch_orders_graphql(session, since),
    )

    conn.close()
    print(f"Done at {datetime.now().isoformat(timespec='seconds')}.")


if __name__ == "__main__":
    main()
