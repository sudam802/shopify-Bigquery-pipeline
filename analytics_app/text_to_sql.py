from __future__ import annotations

import os
import re

import requests

from query import assert_safe_select, explain

SCHEMA = """
Database: shopify_raw (PostgreSQL)

shopify_orders(
  id bigint PK, name text, order_number bigint, email text, customer_id bigint,
  financial_status text, fulfillment_status text, currency text,
  subtotal_price numeric, total_tax numeric, total_discounts numeric, total_price numeric,
  created_at timestamptz, updated_at timestamptz, processed_at timestamptz, cancelled_at timestamptz, tags text
)
shopify_order_line_items(
  id bigint PK, order_id bigint FK -> shopify_orders.id, product_id bigint, variant_id bigint,
  title text, sku text, quantity int, price numeric, total_discount numeric
)
shopify_customers(
  id bigint PK, email text, first_name text, last_name text, phone text,
  orders_count int, total_spent numeric, created_at timestamptz
)
shopify_products(
  id bigint PK, title text, handle text, vendor text, product_type text, status text, created_at timestamptz
)
shopify_variants(
  id bigint PK, product_id bigint, title text, sku text, price numeric, inventory_quantity int
)

Business rules:
- Line-item revenue = quantity * price.
- shopify_orders has NO order_id column; its primary key is id.
- Count orders per customer with COUNT(o.id) grouped by o.customer_id,
  joining shopify_customers c ON o.customer_id = c.id for names/emails.
- Prefer aggregations (SUM, COUNT, AVG, DATE_TRUNC) for analytics.
- Do not select raw_data unless asked.
- Use aliases that chart well: label, value, day, month, status, product.
"""

SYSTEM_PROMPT = f"""You are a PostgreSQL text-to-SQL engine for a Shopify analytics warehouse.
Convert the user's natural-language question into ONE read-only SQL query.

{SCHEMA}

Hard rules:
- Output SQL only. No markdown, no explanation, no comments.
- Query must be a single SELECT or WITH ... SELECT.
- Never INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, or CALL.
- Use only the tables and columns listed above.
- Limit to 200 rows unless the question asks for fewer.
- If the question cannot be answered from this schema, return:
  SELECT 'unsupported question' AS error;
"""

FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class AmbiguousQuestion(Exception):
    """Raised when the question cannot be mapped to SQL with confidence."""


def llm_status() -> dict:
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider == "openai":
        return {
            "provider": "openai",
            "ready": bool(os.getenv("OPENAI_API_KEY")),
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        }
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    try:
        resp = requests.get(f"{host}/api/tags", timeout=2)
        resp.raise_for_status()
        names = [m.get("name") for m in resp.json().get("models", [])]
        ready = any(name == model or name.startswith(f"{model}:") or name.split(":")[0] == model.split(":")[0] for name in names)
        return {"provider": "ollama", "ready": ready, "model": model, "host": host, "models": names}
    except requests.RequestException as exc:
        return {"provider": "ollama", "ready": False, "model": model, "host": host, "error": str(exc)}


def extract_sql(text: str) -> str:
    if not text:
        return ""
    fenced = FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip().strip("`").strip()
    for line in text.splitlines():
        if re.match(r"^(select|with)\b", line.strip(), re.I):
            start = text.lower().find(line.strip()[:12].lower())
            text = text[start:] if start >= 0 else text
            break
    return text.rstrip(";").strip()


KEEP_ALIVE = "30m"


def _chat_ollama(messages: list[dict[str, str]]) -> str:
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    resp = requests.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0, "num_predict": 400},
        },
        timeout=180,
    )
    resp.raise_for_status()
    payload = resp.json()
    return ((payload.get("message") or {}).get("content") or "").strip()


def warm_model() -> dict:
    """Load the model into memory so the first real question is not the slowest."""
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider != "ollama":
        return {"warmed": False, "provider": provider}
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": KEEP_ALIVE},
            timeout=180,
        )
        resp.raise_for_status()
        return {"warmed": True, "model": model}
    except requests.RequestException as exc:
        return {"warmed": False, "model": model, "error": str(exc)}


def _chat_openai(messages: list[dict[str, str]]) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise AmbiguousQuestion("OPENAI_API_KEY is not set")
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "temperature": 0,
            "messages": messages,
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _chat(messages: list[dict[str, str]]) -> str:
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider == "openai":
        return _chat_openai(messages)
    return _chat_ollama(messages)


MAX_ATTEMPTS = 3


def llm_sql(question: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    last_error: Exception | None = None

    for _ in range(MAX_ATTEMPTS):
        raw = _chat(messages)
        sql = extract_sql(raw)
        if re.search(r"unsupported question", sql or "", re.I):
            raise AmbiguousQuestion("That question cannot be answered from the Shopify tables.")
        try:
            safe = assert_safe_select(sql)
            explain(safe)
            return safe
        except Exception as error:
            last_error = error
            messages = messages + [
                {"role": "assistant", "content": sql or "(empty)"},
                {
                    "role": "user",
                    "content": (
                        f"PostgreSQL rejected that query: {error}\n"
                        "Use only the columns listed in the schema. "
                        "Return one corrected read-only SELECT, SQL only."
                    ),
                },
            ]

    raise AmbiguousQuestion(f"Could not produce valid SQL after {MAX_ATTEMPTS} attempts: {last_error}")


def question_to_sql(question: str) -> tuple[str, str]:
    stripped = question.strip()
    if re.match(r"^(select|with)\b", stripped, re.I):
        return stripped, "sql"

    status = llm_status()
    if not status.get("ready"):
        provider = status.get("provider")
        extra = status.get("error") or "model is not available"
        if provider == "ollama":
            raise AmbiguousQuestion(
                "Ollama is not ready. Start Ollama, then run "
                f"`ollama pull {status.get('model')}` ({extra})."
            )
        raise AmbiguousQuestion(f"LLM is not ready ({extra}).")

    try:
        return llm_sql(stripped), status.get("provider", "llm")
    except AmbiguousQuestion:
        raise
    except requests.RequestException as exc:
        raise AmbiguousQuestion(f"LLM request failed: {exc}") from exc
    except Exception as exc:
        raise AmbiguousQuestion(f"Could not convert that question to SQL: {exc}") from exc
