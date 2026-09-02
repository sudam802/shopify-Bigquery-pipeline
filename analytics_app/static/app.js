const charts = {};

function money(n) {
  const value = Number(n || 0);
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function destroyChart(id) {
  if (charts[id]) {
    charts[id].destroy();
    delete charts[id];
  }
}

function renderChart(id, spec) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  destroyChart(id);
  if (!spec) return;
  canvas.hidden = false;
  charts[id] = new Chart(canvas, {
    type: spec.type,
    data: { labels: spec.labels, datasets: spec.datasets },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: "#93a4b8" } } },
      scales: spec.type === "doughnut" ? {} : {
        x: { ticks: { color: "#93a4b8", maxRotation: 45 }, grid: { color: "#2c3846" } },
        y: { ticks: { color: "#93a4b8" }, grid: { color: "#2c3846" } },
      },
    },
  });
}

function fillTable(rows, columns) {
  const table = document.getElementById("result-table");
  if (!columns.length) {
    table.innerHTML = "<tbody><tr><td>No rows</td></tr></tbody>";
    return;
  }
  const head = "<tr>" + columns.map((c) => `<th>${c}</th>`).join("") + "</tr>";
  const body = rows.map((row) => "<tr>" + columns.map((c) => `<td>${row[c] ?? ""}</td>`).join("") + "</tr>").join("");
  table.innerHTML = `<thead>${head}</thead><tbody>${body}</tbody>`;
}

function showOverview() {
  document.getElementById("default-charts").hidden = false;
  document.getElementById("answer").hidden = true;
  document.getElementById("reset-dashboard").hidden = true;
  document.getElementById("error").hidden = true;
}

function showAnswer(data) {
  document.getElementById("default-charts").hidden = true;
  document.getElementById("answer").hidden = false;
  document.getElementById("reset-dashboard").hidden = false;
  document.getElementById("answer-title").textContent = data.question;
  document.getElementById("answer-source").textContent = `via ${data.source}`;
  document.getElementById("sql").textContent = data.sql;

  const note = document.getElementById("answer-note");
  if (data.note) {
    note.hidden = false;
    note.textContent = data.note;
  } else {
    note.hidden = true;
    note.textContent = "";
  }

  const canvas = document.getElementById("chart-answer");
  const empty = document.getElementById("chart-empty");
  const kind = document.getElementById("chart-kind");
  if (data.chart) {
    empty.hidden = true;
    canvas.hidden = false;
    kind.textContent = `${data.chart.type} · ${data.chart.labels.length} points`;
    renderChart("chart-answer", data.chart);
  } else {
    destroyChart("chart-answer");
    canvas.hidden = true;
    empty.hidden = false;
    kind.textContent = "none";
  }

  fillTable(data.rows, data.columns);
  document.getElementById("answer").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadOverview() {
  const res = await fetch("/api/overview");
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "Overview failed");
  document.getElementById("kpi-revenue").textContent = money(data.kpis.revenue);
  document.getElementById("kpi-orders").textContent = Number(data.kpis.orders || 0).toLocaleString();
  document.getElementById("kpi-aov").textContent = money(data.kpis.aov);
  document.getElementById("kpi-paid").textContent = Number(data.kpis.paid_orders || 0).toLocaleString();
  renderChart("chart-revenue", data.charts.revenue_by_day.chart);
  renderChart("chart-status", data.charts.status_mix.chart);
  renderChart("chart-products", data.charts.top_products.chart);
}

let busy = false;
let timer = null;

function setBusy(state) {
  busy = state;
  const button = document.getElementById("ask-button");
  const status = document.getElementById("status");
  button.disabled = state;
  button.textContent = state ? "Running…" : button.dataset.idle;
  document.querySelectorAll(".chip").forEach((chip) => (chip.disabled = state));

  clearInterval(timer);
  if (!state) {
    status.hidden = true;
    return;
  }
  // The local model can take up to a minute, so show progress instead of freezing.
  const started = Date.now();
  status.hidden = false;
  const tick = () => {
    const secs = Math.round((Date.now() - started) / 1000);
    status.textContent = `Generating SQL with the local model… ${secs}s (first run is slowest)`;
  };
  tick();
  timer = setInterval(tick, 1000);
}

async function runQuestion(question) {
  if (busy) return;
  const error = document.getElementById("error");
  error.hidden = true;
  setBusy(true);
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    if (!data.ok) {
      error.hidden = false;
      error.textContent = data.error || "Query failed";
      return;
    }
    showAnswer(data);
  } catch (err) {
    error.hidden = false;
    error.textContent = err.message || "Request failed";
  } finally {
    setBusy(false);
  }
}

document.getElementById("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = document.getElementById("question").value.trim();
  if (question) runQuestion(question);
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.getElementById("question").value = chip.dataset.q;
    runQuestion(chip.dataset.q);
  });
});

fetch("/api/warmup", { method: "POST" }).catch(() => {});

document.getElementById("reset-dashboard").addEventListener("click", showOverview);

loadOverview().catch((err) => {
  const error = document.getElementById("error");
  error.hidden = false;
  error.textContent = err.message;
});
