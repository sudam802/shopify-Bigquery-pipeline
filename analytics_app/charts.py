from __future__ import annotations

from typing import Any


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_chart(columns: list[str], rows: list[dict]) -> dict | None:
    if not rows or len(columns) < 2:
        return None

    label_col = columns[0]
    numeric_cols = [
        col for col in columns[1:]
        if any(_numeric(row.get(col)) for row in rows)
    ]
    if not numeric_cols:
        return None

    labels = ["" if row.get(label_col) is None else str(row.get(label_col)) for row in rows]
    looks_like_time = any(token in label_col.lower() for token in ("day", "date", "month", "time", "created"))
    chart_type = "line" if looks_like_time else ("doughnut" if len(rows) <= 8 and len(numeric_cols) == 1 else "bar")

    palette = [
        "#5b8def",
        "#3ddc97",
        "#f5c542",
        "#ff7a59",
        "#c084fc",
        "#22d3ee",
        "#f472b6",
        "#a3e635",
        "#fb923c",
        "#60a5fa",
    ]
    datasets = []
    for i, col in enumerate(numeric_cols[:3]):
        series = [float(row[col]) if _numeric(row.get(col)) else 0 for row in rows]
        per_category = chart_type == "doughnut" or (
            chart_type == "bar" and len(numeric_cols) == 1
        )
        if per_category:
            # One colour per slice/bar, otherwise the whole series renders identically.
            background = [palette[j % len(palette)] for j in range(len(series))]
            border = "#1a222c" if chart_type == "doughnut" else background
        elif chart_type == "line":
            background = palette[i % len(palette)] + "33"
            border = palette[i % len(palette)]
        else:
            background = palette[i % len(palette)]
            border = palette[i % len(palette)]
        datasets.append(
            {
                "label": col.replace("_", " "),
                "data": series,
                "backgroundColor": background,
                "borderColor": border,
                "fill": chart_type == "line",
                "tension": 0.3,
            }
        )
    return {"type": chart_type, "labels": labels, "datasets": datasets}
