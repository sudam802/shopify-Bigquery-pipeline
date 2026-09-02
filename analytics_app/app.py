from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from charts import build_chart
from query import run_select
from text_to_sql import AmbiguousQuestion, llm_status, question_to_sql, warm_model

app = Flask(__name__)

OVERVIEW_SQL = {
    "kpis": """
        SELECT
          COALESCE(SUM(total_price), 0) AS revenue,
          COUNT(*) AS orders,
          COALESCE(AVG(total_price), 0) AS aov,
          COUNT(*) FILTER (WHERE financial_status = 'paid') AS paid_orders
        FROM shopify_orders
    """,
    "revenue_by_day": """
        SELECT DATE(created_at) AS day,
               ROUND(SUM(total_price), 2) AS revenue,
               COUNT(*) AS orders
        FROM shopify_orders
        GROUP BY 1
        ORDER BY 1
    """,
    "status_mix": """
        SELECT COALESCE(financial_status, 'unknown') AS label,
               COUNT(*) AS value
        FROM shopify_orders
        GROUP BY 1
        ORDER BY value DESC
    """,
    "top_products": """
        SELECT title AS label,
               ROUND(SUM(quantity * price), 2) AS value
        FROM shopify_order_line_items
        GROUP BY title
        ORDER BY value DESC
        LIMIT 8
    """,
}

SUGGESTIONS = [
    "Revenue by day",
    "Top products by sales",
    "Orders by financial status",
    "Top customers by spend",
    "Average order value last 30 days",
    "Monthly revenue",
    "Customers with 2 orders",
]


def pack_result(sql: str, source: str):
    executed, columns, rows = run_select(sql)
    return {
        "sql": executed,
        "source": source,
        "columns": columns,
        "rows": rows,
        "chart": build_chart(columns, rows),
    }


@app.get("/")
def index():
    return render_template("index.html", suggestions=SUGGESTIONS, llm=llm_status())


@app.get("/api/llm")
def llm():
    return jsonify(llm_status())


@app.get("/api/overview")
def overview():
    try:
        payload = {name: pack_result(sql, "overview") for name, sql in OVERVIEW_SQL.items()}
        kpis = payload["kpis"]["rows"][0] if payload["kpis"]["rows"] else {}
        return jsonify({"ok": True, "kpis": kpis, "charts": payload})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/warmup")
def warmup():
    return jsonify(warm_model())


@app.post("/api/ask")
def ask():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "Enter a question"}), 400
    try:
        sql, source = question_to_sql(question)
        result = pack_result(sql, source)
        result["ok"] = True
        result["question"] = question
        if not result["rows"]:
            result["note"] = "The query ran but matched no rows."
        return jsonify(result)
    except AmbiguousQuestion as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
