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
<html>
<head>
  <meta charset="utf-8">
  <title>Kovanica Testnet PoA Dashboard</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #e2e8f0; margin: 0; padding: 2rem; }
    h1 { margin-top: 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin: 1.5rem 0; }
    .card { background: #151b2b; border: 1px solid #2d3748; border-radius: 0.75rem; padding: 1.25rem; }
    .card h2 { margin: 0 0 0.75rem; font-size: 1.25rem; }
    .ok { color: #48bb78; }
    .err { color: #fc8181; }
    .muted { color: #a0aec0; font-size: 0.875rem; }
    .big { font-size: 2rem; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
    th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #2d3748; }
    th { color: #a0aec0; font-weight: 500; }
    code { font-family: "SFMono-Regular", Consolas, monospace; word-break: break-all; }
    #chart { width: 100%; height: 240px; background: #151b2b; border: 1px solid #2d3748; border-radius: 0.75rem; }
  </style>
</head>
<body>
  <h1>Kovanica Testnet PoA Dashboard</h1>
  <p class="muted">Started: <span id="started"></span> · Refresh every 5s</p>
  <div class="grid" id="cards"></div>
  <div class="card">
    <h2>Block height history</h2>
    <canvas id="chart"></canvas>
  </div>
  <div class="card" style="margin-top:1rem">
    <h2>Raw heads</h2>
    <table id="raw">
      <thead><tr><th>Seed</th><th>Genesis</th><th>Blocks</th><th>Tip</th><th>Slot</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
const seedNames = ["seed1", "seed2", "seed3"];
const colors = { seed1: "#63b3ed", seed2: "#68d391", seed3: "#f6ad55" };

async function refresh() {
  const r = await fetch("/api/state");
  const data = await r.json();
  document.getElementById("started").textContent = data.started;

  const cards = document.getElementById("cards");
  cards.innerHTML = "";
  for (const name of seedNames) {
    const d = data.latest[name] || {};
    const ok = !d.error && d.genesis;
    const blocks = d.blocks !== undefined ? d.blocks : "—";
    const peers = d.peers !== undefined ? d.peers : "—";
    cards.innerHTML += `
      <div class="card">
        <h2>${name} <span class="${ok ? "ok" : "err"}">${ok ? "●" : "●"}</span></h2>
        <div class="big">${blocks}</div>
        <div class="muted">blocks</div>
        ${d.error ? `<div class="err">${d.error}</div>` : ""}
        <div class="muted" style="margin-top:0.75rem">peers: ${peers}</div>
      </div>`;
  }

  const tbody = document.querySelector("#raw tbody");
  tbody.innerHTML = "";
  for (const name of seedNames) {
    const d = data.latest[name] || {};
    tbody.innerHTML += `<tr>
      <td>${name}</td>
      <td><code>${d.genesis ? d.genesis.slice(0, 16) + "…" : "—"}</code></td>
      <td>${d.blocks !== undefined ? d.blocks : "—"}</td>
      <td><code>${d.tip ? d.tip.slice(0, 16) + "…" : "—"}</code></td>
      <td>${d.current_slot !== undefined ? d.current_slot : "—"}</td>
    </tr>`;
  }

  drawChart(data.history);
}

function drawChart(history) {
  const cvs = document.getElementById("chart");
  const ctx = cvs.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = cvs.getBoundingClientRect();
  cvs.width = rect.width * dpr;
  cvs.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);

  if (history.length < 2) return;
  let min = Infinity, max = -Infinity;
  for (const row of history) {
    for (const n of seedNames) {
      const v = row[n];
      if (v != null) { if (v < min) min = v; if (v > max) max = v; }
    }
  }
  if (!isFinite(min)) return;
  const pad = 20;
  const h = rect.height - pad * 2;
  const w = rect.width - pad * 2;
  const range = Math.max(max - min, 1);

  ctx.strokeStyle = "#4a5568";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad, pad); ctx.lineTo(pad, rect.height - pad);
  ctx.lineTo(rect.width - pad, rect.height - pad);
  ctx.stroke();

  for (const name of seedNames) {
    ctx.strokeStyle = colors[name];
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < history.length; i++) {
      const row = history[i];
      const v = row[name];
      if (v == null) continue;
      const x = pad + (i / (history.length - 1)) * w;
      const y = rect.height - pad - ((v - min) / range) * h;
      if (i === 0 || row[seedNames[0]] == null) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}

refresh();
setInterval(refresh, 5000);
window.addEventListener("resize", () => { fetch("/api/state").then(r => r.json()).then(drawChart); });
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
