from __future__ import annotations

import os
import re
from decimal import Decimal
from datetime import date, datetime

import psycopg2
from psycopg2.extras import RealDictCursor

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|execute|call|vacuum|comment|security|set\s+role|pg_sleep|"
    r"lo_import|lo_export)\b",
    re.IGNORECASE,
)
LIMIT_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)
MAX_ROWS = 500


def connect():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5433"),
        dbname=os.getenv("PG_DB", "shopify_raw"),
        user=os.getenv("PG_USER", "airbyte"),
        password=os.getenv("PG_PASSWORD", "airbyte123"),
    )


def assert_safe_select(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Empty SQL")
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed")
    if FORBIDDEN.search(cleaned):
        raise ValueError("Only read-only SELECT queries are allowed")
    head = cleaned.lstrip("(").lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError("Query must start with SELECT or WITH")
    if not LIMIT_RE.search(cleaned):
        cleaned = f"{cleaned}\nLIMIT {MAX_ROWS}"
    return cleaned


def explain(sql: str) -> None:
    """Ask Postgres to plan the query so bad columns surface before execution."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN {sql}")
    finally:
        conn.rollback()
        conn.close()


def json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_select(sql: str) -> tuple[str, list[str], list[dict]]:
    safe_sql = assert_safe_select(sql)
    conn = connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '8000'")
            cur.execute(safe_sql)
            rows = cur.fetchall()
            columns = list(rows[0].keys()) if rows else [d[0] for d in (cur.description or [])]
        data = [{k: json_safe(v) for k, v in row.items()} for row in rows]
        return safe_sql, columns, data
    finally:
        conn.close()
