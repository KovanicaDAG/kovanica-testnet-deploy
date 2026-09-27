#!/usr/bin/env python3
"""Simple PoA network dashboard for kovanica-testnet seeds.

Serves a live HTML dashboard on http://127.0.0.1:3000 by default.
Fetches /api/head from local seed and remote seeds via SSH.
"""
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

SEEDS = [
    {"name": "seed1", "host": "127.0.0.1", "ssh_key": None, "url": "http://127.0.0.1:8080/api/head"},
    {"name": "seed2", "host": "76.13.250.65", "ssh_key": "/root/.ssh/seed2_deploy_key", "url": "http://127.0.0.1:8080/api/head"},
    {"name": "seed3", "host": "187.7.27.139", "ssh_key": "/root/.ssh/seed3_deploy_key", "url": "http://127.0.0.1:8080/api/head"},
]

INTERVAL_SEC = 5
MAX_HISTORY = 120  # ~10 minutes at 5s

state = {
    "history": [],
    "latest": {},
    "started": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
}


def fetch_local(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def fetch_remote(host, ssh_key, url):
    try:
        cmd = [
            "ssh", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
            "-i", ssh_key, f"root@{host}",
            f"curl -sS --max-time 8 {url}"
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=30)
        return json.loads(out.decode())
    except Exception as e:
        return {"error": str(e)}


def fetch_all():
    result = {}
    for s in SEEDS:
        if s["ssh_key"]:
            result[s["name"]] = fetch_remote(s["host"], s["ssh_key"], s["url"])
        else:
            result[s["name"]] = fetch_local(s["url"])
    return result


def poller():
    while True:
        snapshot = fetch_all()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        row = {"ts": ts}
        for name, data in snapshot.items():
            row[name] = data.get("blocks") if isinstance(data.get("blocks"), int) else None
        state["latest"] = snapshot
        state["history"].append(row)
        if len(state["history"]) > MAX_HISTORY:
            state["history"].pop(0)
        time.sleep(INTERVAL_SEC)


HTML = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kovanica Testnet · PoA Network Dashboard</title>
  <style>
    :root {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-2: #f1f5f9;
      --text: #0f172a;
      --text-muted: #64748b;
      --border: #e2e8f0;
      --accent: #0891b2;
      --accent-2: #d97706;
      --accent-glow: rgba(8, 145, 178, 0.18);
      --success: #10b981;
      --error: #ef4444;
      --warning: #f59e0b;
      --shadow: 0 1px 3px 0 rgb(0 0 0 / 0.05), 0 1px 2px -1px rgb(0 0 0 / 0.05);
      --shadow-lg: 0 10px 25px -5px rgb(0 0 0 / 0.08), 0 8px 10px -6px rgb(0 0 0 / 0.05);
      --radius: 1rem;
      --font-display: "SF Pro Display", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
      --font-body: "SF Pro Text", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
      --font-mono: "SF Mono", "Cascadia Code", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    [data-theme="dark"] {
      --bg: #030712;
      --surface: #0b1221;
      --surface-2: #111827;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --border: #1e293b;
      --accent: #22d3ee;
      --accent-2: #fbbf24;
      --accent-glow: rgba(34, 211, 238, 0.18);
      --success: #34d399;
      --error: #f87171;
      --warning: #fbbf24;
      --shadow: 0 1px 3px 0 rgb(0 0 0 / 0.25), 0 1px 2px -1px rgb(0 0 0 / 0.2);
      --shadow-lg: 0 10px 40px -10px rgb(0 0 0 / 0.6), 0 4px 12px -4px rgb(0 0 0 / 0.4);
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }

    body {
      margin: 0;
      font-family: var(--font-body);
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      min-height: 100vh;
      transition: background 0.35s ease, color 0.35s ease;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 10% 10%, var(--accent-glow), transparent 30%),
        radial-gradient(circle at 90% 90%, rgba(217, 119, 6, 0.08), transparent 35%),
        radial-gradient(circle at 50% 50%, rgba(99, 102, 241, 0.05), transparent 45%);
      z-index: -1;
    }

    .container {
      max-width: 1200px;
      margin: 0 auto;
      padding: 1.5rem;
    }

    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 2rem;
      padding-bottom: 1.25rem;
      border-bottom: 1px solid var(--border);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.875rem;
    }

    .logo {
      width: 2.75rem;
      height: 2.75rem;
      border-radius: 0.75rem;
      background: linear-gradient(135deg, var(--accent), #6366f1);
      display: grid;
      place-items: center;
      color: #fff;
      font-weight: 800;
      font-size: 1.25rem;
      box-shadow: 0 0 20px var(--accent-glow);
    }

    .brand-title {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }

    .brand h1 {
      margin: 0;
      font-family: var(--font-display);
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    /* DEV / testnet marker — this surface is never mainnet. */
    .env-badge {
      font-family: var(--font-mono);
      font-size: 0.6875rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      padding: 0.1875rem 0.5rem;
      border-radius: 0.375rem;
      color: var(--warning);
      background: rgba(245, 158, 11, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.3);
      white-space: nowrap;
    }

    .brand p {
      margin: 0.125rem 0 0;
      font-size: 0.8125rem;
      color: var(--text-muted);
    }

    .dev-footer {
      margin-top: 1.5rem;
      padding: 0.875rem 1rem;
      border-radius: 0.875rem;
      font-size: 0.8125rem;
      color: var(--text-muted);
      background: var(--panel);
      border: 1px solid var(--border);
      text-align: center;
    }

    .dev-footer strong {
      font-family: var(--font-mono);
      font-size: 0.6875rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--warning);
      margin-right: 0.375rem;
    }

    .dev-footer code {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      color: var(--text);
    }

    .controls {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .meta {
      font-size: 0.8125rem;
      color: var(--text-muted);
      text-align: right;
    }

    .theme-toggle {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 0.625rem;
      width: 2.25rem;
      height: 2.25rem;
      display: grid;
      place-items: center;
      cursor: pointer;
      transition: transform 0.15s ease, box-shadow 0.2s ease;
    }

    .theme-toggle:hover { transform: translateY(-1px); box-shadow: var(--shadow); }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.25rem;
      box-shadow: var(--shadow);
      transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.35s ease, border-color 0.35s ease;
      animation: fadeUp 0.5s ease both;
    }

    .card:hover { transform: translateY(-2px); box-shadow: var(--shadow-lg); }

    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1rem;
    }

    .seed-label {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-family: var(--font-display);
      font-size: 0.9375rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .seed-dot {
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--text-muted);
    }

    .seed-dot.ok { background: var(--success); box-shadow: 0 0 10px var(--success); }
    .seed-dot.err { background: var(--error); box-shadow: 0 0 10px var(--error); }
    .seed-dot.ok::after, .seed-dot.err::after {
      content: "";
      display: block;
      width: 100%;
      height: 100%;
      border-radius: 50%;
      animation: pulse 2s infinite;
    }
    .seed-dot.ok::after { background: var(--success); }
    .seed-dot.err::after { background: var(--error); }

    @keyframes pulse {
      0% { transform: scale(1); opacity: 0.7; }
      70% { transform: scale(2.8); opacity: 0; }
      100% { transform: scale(2.8); opacity: 0; }
    }

    .status-badge {
      font-size: 0.6875rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 0.25rem 0.5rem;
      border-radius: 999px;
      background: var(--surface-2);
      color: var(--text-muted);
      border: 1px solid var(--border);
    }

    .status-badge.ok { background: rgba(16, 185, 129, 0.12); color: var(--success); border-color: rgba(16, 185, 129, 0.25); }
    .status-badge.err { background: rgba(239, 68, 68, 0.12); color: var(--error); border-color: rgba(239, 68, 68, 0.25); }

    .metric {
      font-family: var(--font-mono);
      font-size: 2.25rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      line-height: 1;
      margin-bottom: 0.25rem;
    }

    .metric-label {
      font-size: 0.8125rem;
      color: var(--text-muted);
      margin-bottom: 0.875rem;
    }

    .card-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.8125rem;
      color: var(--text-muted);
      padding-top: 0.75rem;
      border-top: 1px solid var(--border);
    }

    .error-text {
      color: var(--error);
      font-size: 0.8125rem;
      margin-top: 0.5rem;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 1.5rem;
      transition: background 0.35s ease, border-color 0.35s ease;
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--border);
      background: var(--surface-2);
    }

    .panel-header h2 {
      margin: 0;
      font-family: var(--font-display);
      font-size: 1rem;
      font-weight: 600;
    }

    .legend {
      display: flex;
      align-items: center;
      gap: 1rem;
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    .legend span { display: inline-flex; align-items: center; gap: 0.375rem; }
    .legend i { width: 0.625rem; height: 0.125rem; border-radius: 2px; }

    .chart-wrap {
      position: relative;
      height: 280px;
      padding: 1rem;
    }

    #chart {
      width: 100%;
      height: 100%;
      display: block;
    }

    .table-wrap {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }

    thead {
      background: var(--surface-2);
      position: sticky;
      top: 0;
    }

    th, td {
      text-align: left;
      padding: 0.875rem 1.25rem;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }

    th {
      font-weight: 600;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
    }

    tbody tr { transition: background 0.15s ease; }
    tbody tr:hover { background: var(--surface-2); }
    tbody tr:last-child td { border-bottom: none; }

    code {
      font-family: var(--font-mono);
      font-size: 0.8125rem;
      background: var(--surface-2);
      padding: 0.125rem 0.375rem;
      border-radius: 0.375rem;
      border: 1px solid var(--border);
      color: var(--accent);
    }

    .empty {
      padding: 2.5rem;
      text-align: center;
      color: var(--text-muted);
      font-size: 0.9375rem;
    }

    .refreshing {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    .spinner {
      width: 0.75rem;
      height: 0.75rem;
      border: 2px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }

    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @media (max-width: 640px) {
      .container { padding: 1rem; }
      header { flex-direction: column; align-items: flex-start; }
      .meta { text-align: left; }
      .brand h1 { font-size: 1.25rem; }
      .metric { font-size: 1.875rem; }
      .chart-wrap { height: 220px; }
      th, td { padding: 0.75rem 1rem; }
      .legend { gap: 0.625rem; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="logo">K</div>
        <div>
          <div class="brand-title">
            <h1>Kovanica</h1>
            <span class="env-badge" title="Non-production network. Not mainnet. No real value.">DEV</span>
          </div>
          <p>kovanica-testnet · PoA Dashboard · <span id="started">—</span></p>
        </div>
      </div>
      <div class="controls">
        <div class="meta">
          <div class="refreshing"><span class="spinner"></span> Updating every 5s</div>
          <div id="last-updated" style="margin-top:0.125rem">Waiting for data…</div>
        </div>
        <button class="theme-toggle" id="themeToggle" aria-label="Toggle theme">☾</button>
      </div>
    </header>

    <section class="grid" id="cards"></section>

    <section class="panel">
      <div class="panel-header">
        <h2>Block height history</h2>
        <div class="legend" id="legend"></div>
      </div>
      <div class="chart-wrap">
        <canvas id="chart"></canvas>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h2>Raw heads</h2>
      </div>
      <div class="table-wrap">
        <table id="raw">
          <thead>
            <tr>
              <th>Seed</th>
              <th>Genesis</th>
              <th>Blocks</th>
              <th>Tip</th>
              <th>Slot</th>
              <th>Peers</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <footer class="dev-footer">
      <strong>DEV</strong> — non-production <code>kovanica-testnet</code>. No real value, no mainnet funds. Loopback-only; access via SSH tunnel.
    </footer>
  </div>

  <script>
    const seedNames = ["seed1", "seed2", "seed3"];
    const seedMeta = {
      seed1: { label: "Seed 1", color: "#22d3ee", lightColor: "#0891b2" },
      seed2: { label: "Seed 2", color: "#34d399", lightColor: "#059669" },
      seed3: { label: "Seed 3", color: "#fbbf24", lightColor: "#d97706" },
    };

    function getColor(name) {
      return document.documentElement.getAttribute("data-theme") === "light"
        ? seedMeta[name].lightColor
        : seedMeta[name].color;
    }

    const themeToggle = document.getElementById("themeToggle");
    const stored = localStorage.getItem("kovanica-theme");
    if (stored) document.documentElement.setAttribute("data-theme", stored);
    themeToggle.textContent = document.documentElement.getAttribute("data-theme") === "light" ? "☀" : "☾";

    themeToggle.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("kovanica-theme", next);
      themeToggle.textContent = next === "light" ? "☀" : "☾";
      refresh();
    });

    function buildLegend() {
      const legend = document.getElementById("legend");
      legend.innerHTML = seedNames.map(n =>
        `<span><i style="background:${getColor(n)}"></i>${seedMeta[n].label}</span>`
      ).join("");
    }

    function formatNumber(n) {
      return n != null ? n.toLocaleString() : "—";
    }

    function truncateHash(h) {
      if (!h) return "—";
      return h.length > 18 ? h.slice(0, 10) + "…" + h.slice(-8) : h;
    }

    async function refresh() {
      try {
        const r = await fetch("/api/state");
        const data = await r.json();
        document.getElementById("started").textContent = data.started || "—";
        document.getElementById("last-updated").textContent = "Last update: " + new Date().toLocaleTimeString();

        const cards = document.getElementById("cards");
        cards.innerHTML = "";
        for (let i = 0; i < seedNames.length; i++) {
          const name = seedNames[i];
          const d = data.latest[name] || {};
          const ok = !d.error && d.genesis;
          const meta = seedMeta[name];
          const delay = i * 80;
          cards.innerHTML += `
            <article class="card" style="animation-delay:${delay}ms; border-top:3px solid ${getColor(name)}">
              <div class="card-header">
                <div class="seed-label">
                  <span class="seed-dot ${ok ? "ok" : "err"}"></span>
                  ${meta.label}
                </div>
                <span class="status-badge ${ok ? "ok" : "err"}">${ok ? "Online" : "Offline"}</span>
              </div>
              <div class="metric">${formatNumber(d.blocks)}</div>
              <div class="metric-label">confirmed blocks</div>
              ${d.error ? `<div class="error-text">${d.error}</div>` : ""}
              <div class="card-footer">
                <span>peers: ${d.peers != null ? d.peers : "—"}</span>
                <span>slot: ${d.current_slot != null ? d.current_slot : "—"}</span>
              </div>
            </article>`;
        }

        const tbody = document.querySelector("#raw tbody");
        tbody.innerHTML = "";
        for (const name of seedNames) {
          const d = data.latest[name] || {};
          tbody.innerHTML += `<tr>
            <td><strong style="color:${getColor(name)}">${seedMeta[name].label}</strong></td>
            <td><code>${truncateHash(d.genesis)}</code></td>
            <td>${formatNumber(d.blocks)}</td>
            <td><code>${truncateHash(d.tip)}</code></td>
            <td>${d.current_slot != null ? d.current_slot : "—"}</td>
            <td>${d.peers != null ? d.peers : "—"}</td>
          </tr>`;
        }

        buildLegend();
        drawChart(data.history);
      } catch (e) {
        console.error("Refresh failed", e);
      }
    }

    function drawChart(history) {
      const cvs = document.getElementById("chart");
      const ctx = cvs.getContext("2d");
      const rect = cvs.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      cvs.width = Math.max(1, Math.floor(rect.width * dpr));
      cvs.height = Math.max(1, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);

      if (history.length < 2) {
        ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--text-muted").trim();
        ctx.font = "14px var(--font-body)";
        ctx.textAlign = "center";
        ctx.fillText("Collecting data…", rect.width / 2, rect.height / 2);
        return;
      }

      let min = Infinity, max = -Infinity;
      for (const row of history) {
        for (const n of seedNames) {
          const v = row[n];
          if (v != null) { min = Math.min(min, v); max = Math.max(max, v); }
        }
      }
      if (!isFinite(min)) return;

      const pad = { top: 24, right: 16, bottom: 32, left: 56 };
      const w = rect.width - pad.left - pad.right;
      const h = rect.height - pad.top - pad.bottom;
      const range = Math.max(max - min, 1);

      // grid
      ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--border").trim();
      ctx.lineWidth = 1;
      ctx.beginPath();
      const ySteps = 4;
      for (let i = 0; i <= ySteps; i++) {
        const y = pad.top + (h / ySteps) * i;
        ctx.moveTo(pad.left, y);
        ctx.lineTo(rect.width - pad.right, y);
      }
      ctx.stroke();

      // y-axis labels
      ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--text-muted").trim();
      ctx.font = "11px var(--font-mono)";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= ySteps; i++) {
        const value = max - (range / ySteps) * i;
        const y = pad.top + (h / ySteps) * i;
        ctx.fillText(Math.round(value).toLocaleString(), pad.left - 10, y);
      }

      // x-axis labels
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const xSteps = 4;
      for (let i = 0; i <= xSteps; i++) {
        const idx = Math.round((history.length - 1) * (i / xSteps));
        const x = pad.left + (idx / (history.length - 1)) * w;
        const label = history[idx].ts ? history[idx].ts.split(" ")[1].slice(0, 5) : "";
        ctx.fillText(label, x, rect.height - pad.bottom + 8);
      }

      for (const name of seedNames) {
        const color = getColor(name);
        const points = [];
        for (let i = 0; i < history.length; i++) {
          const v = history[i][name];
          if (v == null) continue;
          const x = pad.left + (i / (history.length - 1)) * w;
          const y = pad.top + h - ((v - min) / range) * h;
          points.push({ x, y, v });
        }
        if (points.length < 2) continue;

        // area fill
        ctx.save();
        const grad = ctx.createLinearGradient(0, pad.top, 0, rect.height - pad.bottom);
        grad.addColorStop(0, color + "33");
        grad.addColorStop(1, color + "00");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.moveTo(points[0].x, rect.height - pad.bottom);
        for (const p of points) ctx.lineTo(p.x, p.y);
        ctx.lineTo(points[points.length - 1].x, rect.height - pad.bottom);
        ctx.closePath();
        ctx.fill();
        ctx.restore();

        // line
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.5;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.beginPath();
        for (let i = 0; i < points.length; i++) {
          if (i === 0) ctx.moveTo(points[i].x, points[i].y);
          else ctx.lineTo(points[i].x, points[i].y);
        }
        ctx.stroke();

        // last point dot
        const last = points[points.length - 1];
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(last.x, last.y, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue("--surface").trim();
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    refresh();
    setInterval(refresh, 5000);
    window.addEventListener("resize", () => {
      fetch("/api/state").then(r => r.json()).then(data => drawChart(data.history));
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # keep logs quiet


def main():
    port = int(os.environ.get("DASHBOARD_PORT", "3000"))
    bind = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    # wait for first fetch so page isn't empty
    time.sleep(2)
    server = HTTPServer((bind, port), Handler)
    print(f"Dashboard running at http://{bind}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
