"""Web UI and API for controlling the solar charger."""

import json
import logging
import os
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import urlparse, parse_qs
from http.cookies import SimpleCookie

logger = logging.getLogger(__name__)

# In-memory session store
_sessions = set()
_web_password = None

SVG_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" rx="96" fill="#0f172a"/>
  <g transform="translate(256,256)">
    <circle r="140" fill="none" stroke="#facc15" stroke-width="24"/>
    <line x1="0" y1="-180" x2="0" y2="-155" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="0" y1="155" x2="0" y2="180" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="-180" y1="0" x2="-155" y2="0" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="155" y1="0" x2="180" y2="0" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="-127" y1="-127" x2="-110" y2="-110" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="110" y1="-110" x2="127" y2="-127" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="-127" y1="127" x2="-110" y2="110" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <line x1="110" y1="110" x2="127" y2="127" stroke="#facc15" stroke-width="16" stroke-linecap="round"/>
    <polygon points="-20,-80 40,-10 5,-10 20,80 -40,10 -5,10" fill="#facc15"/>
  </g>
</svg>"""

MANIFEST_JSON = json.dumps({
    "name": "Solar Charger",
    "short_name": "SolarChg",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0b0f1a",
    "theme_color": "#0b0f1a",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}
    ]
})

SERVICE_WORKER_JS = """
const CACHE = 'solar-charger-v2';
const PRECACHE = ['/', '/manifest.json', '/icon.svg'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solar Charger - Login</title>
<style>
  :root { --bg: #0b0f1a; --card: #151b2e; --accent: #22c55e; --text: #e2e8f0; --muted: #64748b; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'DM Sans', -apple-system, sans-serif; background: var(--bg); color: var(--text);
         display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
  .login-card { background: var(--card); border-radius: 16px; padding: 40px; width: 100%;
                max-width: 380px; text-align: center; }
  .login-card h1 { font-size: 1.4em; margin-bottom: 8px; }
  .login-card p { color: var(--muted); font-size: 0.9em; margin-bottom: 24px; }
  input[type=password] { width: 100%; padding: 12px 16px; border-radius: 10px; border: 2px solid #1e293b;
                         background: #0b0f1a; color: var(--text); font-size: 1em; margin-bottom: 16px;
                         outline: none; transition: border-color 0.2s; }
  input[type=password]:focus { border-color: var(--accent); }
  button { width: 100%; padding: 12px; border-radius: 10px; border: none; background: var(--accent);
           color: #0b0f1a; font-size: 1em; font-weight: 700; cursor: pointer; transition: opacity 0.2s; }
  button:hover { opacity: 0.85; }
  .error { color: #ef4444; font-size: 0.85em; margin-bottom: 12px; display: none; }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
</head>
<body>
<div class="login-card">
  <h1>Solar Charger</h1>
  <p>Enter password to continue</p>
  <div class="error" id="err">Invalid password</div>
  <form onsubmit="return doLogin(event)">
    <input type="password" id="pw" placeholder="Password" autofocus>
    <button type="submit">Login</button>
  </form>
</div>
<script>
function doLogin(e) {
  e.preventDefault();
  fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('pw').value})
  }).then(r => r.json()).then(d => {
    if (d.ok) location.reload();
    else { document.getElementById('err').style.display = 'block'; }
  });
  return false;
}
</script>
</body>
</html>"""

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solar Charger</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0b0f1a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0b0f1a;
    --card: #151b2e;
    --card-border: #1e293b;
    --accent: #22c55e;
    --accent-glow: rgba(34,197,94,0.25);
    --yellow: #facc15;
    --blue: #3b82f6;
    --red: #ef4444;
    --text: #e2e8f0;
    --muted: #64748b;
    --radius: 16px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'DM Sans', -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    display: flex; justify-content: center; padding: 16px;
    min-height: 100vh;
  }
  .container { max-width: 900px; width: 100%; }
  .header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 20px; padding: 0 4px;
  }
  .header h1 { font-size: 1.3em; font-weight: 700; }
  .logout-btn {
    background: none; border: 1px solid var(--card-border); color: var(--muted);
    padding: 6px 14px; border-radius: 8px; font-size: 0.8em; cursor: pointer;
    transition: color 0.2s, border-color 0.2s;
  }
  .logout-btn:hover { color: var(--text); border-color: var(--muted); }
  .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 640px) { .grid { grid-template-columns: 1fr 1fr; } }
  .card {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: var(--radius); padding: 20px;
  }
  .card-full { grid-column: 1 / -1; }
  .card h2 { font-size: 0.8em; text-transform: uppercase; letter-spacing: 0.08em;
             color: var(--muted); margin-bottom: 14px; font-weight: 500; }
  .stat {
    display: flex; justify-content: space-between; padding: 8px 0;
    border-bottom: 1px solid var(--card-border); font-size: 0.95em;
  }
  .stat:last-child { border: none; }
  .stat .label { color: var(--muted); }
  .stat .value { font-weight: 700; transition: all 0.4s ease; }
  .dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 8px; vertical-align: middle; background: var(--muted);
  }
  .dot.charging {
    background: var(--accent);
    animation: pulse 1.5s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { box-shadow: 0 0 0 0 var(--accent-glow); }
    50% { box-shadow: 0 0 0 8px transparent; }
  }
  .daily-stats {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
    text-align: center; grid-column: 1 / -1;
  }
  .daily-stat {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: var(--radius); padding: 16px 12px;
  }
  .daily-stat .val { font-size: 1.4em; font-weight: 700; }
  .daily-stat .lbl { font-size: 0.75em; color: var(--muted); margin-top: 4px; }
  .modes { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .mode-btn {
    padding: 12px; border: 2px solid var(--card-border); border-radius: 12px;
    background: var(--card); color: var(--text); font-size: 0.9em; font-weight: 500;
    cursor: pointer; transition: all 0.2s; font-family: inherit;
  }
  .mode-btn:hover { border-color: var(--muted); }
  .mode-btn.active { border-color: var(--accent); background: rgba(34,197,94,0.1); color: var(--accent); }
  .toggle-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 0; border-top: 1px solid var(--card-border); margin-top: 10px;
  }
  .toggle-label { font-size: 0.85em; color: var(--muted); }
  .toggle { position: relative; width: 44px; height: 24px; cursor: pointer; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle .slider {
    position: absolute; inset: 0; background: #1e293b; border-radius: 12px;
    transition: background 0.2s;
  }
  .toggle .slider::before {
    content: ''; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px;
    background: var(--muted); border-radius: 50%; transition: all 0.2s;
  }
  .toggle input:checked + .slider { background: var(--accent); }
  .toggle input:checked + .slider::before { transform: translateX(20px); background: white; }
  .chart-wrap { width: 100%; height: 220px; }
  .refresh { text-align: center; color: var(--muted); font-size: 0.75em; margin-top: 12px; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Solar Charger</h1>
    <button class="logout-btn" id="logout-btn" onclick="logout()" style="display:none">Logout</button>
  </div>
  <div class="grid">
    <!-- Status Card -->
    <div class="card" id="status-card">
      <h2>Status</h2>
      <div id="status">Loading...</div>
    </div>
    <!-- Chart Card: 10 min -->
    <div class="card">
      <h2>Last 10 Minutes</h2>
      <div class="chart-wrap"><canvas id="chart-10m"></canvas></div>
    </div>
    <!-- Daily Stats Row -->
    <div class="daily-stats" id="daily-stats">
      <div class="daily-stat"><div class="val" id="ds-solar">--</div><div class="lbl">Solar kWh</div></div>
      <div class="daily-stat"><div class="val" id="ds-grid">--</div><div class="lbl">Grid kWh</div></div>
      <div class="daily-stat"><div class="val" id="ds-sessions">--</div><div class="lbl">Sessions</div></div>
    </div>
    <!-- Chart Card: 4 hours -->
    <div class="card card-full">
      <h2>Last 4 Hours</h2>
      <div class="chart-wrap"><canvas id="chart-4h"></canvas></div>
    </div>
    <!-- Chart Card: 24 hours -->
    <div class="card card-full">
      <h2>Last 24 Hours</h2>
      <div class="chart-wrap"><canvas id="chart-24h"></canvas></div>
    </div>
    <!-- Mode Card -->
    <div class="card card-full">
      <h2>Mode</h2>
      <div class="modes">
        <button class="mode-btn" onclick="setMode('auto')">Auto</button>
        <button class="mode-btn" onclick="setMode('surplus')">Surplus Only</button>
        <button class="mode-btn" onclick="setMode('force_on')">Force ON</button>
        <button class="mode-btn" onclick="setMode('force_off')">Force OFF</button>
      </div>
      <div class="toggle-row">
        <span class="toggle-label">Min. daily charge</span>
        <label class="toggle">
          <input type="checkbox" id="min-charge-toggle" onchange="toggleMinCharge(this.checked)">
          <span class="slider"></span>
        </label>
      </div>
    </div>
  </div>
    <!-- Download Log -->
    <div class="card card-full" style="text-align:center">
      <a href="/api/log/download" class="mode-btn" style="display:inline-block;text-decoration:none;padding:12px 24px">
        Download Today's Log (CSV)
      </a>
    </div>
  <div class="refresh" id="updated"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
const charts = {};

function makeChart(canvasId) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { label: 'PV Power', data: [], borderColor: '#facc15', backgroundColor: 'rgba(250,204,21,0.1)',
          fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2 },
        { label: 'Surplus', data: [], borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)',
          fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2 },
        { label: 'Charging', data: [], borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)',
          fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#64748b', font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 10 } },
             grid: { color: 'rgba(100,116,139,0.1)' } },
        y: { ticks: { color: '#64748b', font: { size: 10 },
             callback: v => v >= 1000 ? (v/1000).toFixed(1)+'kW' : v+'W' },
             grid: { color: 'rgba(100,116,139,0.1)' } }
      }
    }
  });
}

function updateChart(chart, history, showSeconds) {
  if (!chart || !history || !history.length) return;
  const labels = history.map(p => {
    const d = new Date(p.time * 1000);
    if (showSeconds) {
      return d.getHours().toString().padStart(2,'0') + ':' +
             d.getMinutes().toString().padStart(2,'0') + ':' +
             d.getSeconds().toString().padStart(2,'0');
    }
    return d.getHours().toString().padStart(2,'0') + ':' +
           d.getMinutes().toString().padStart(2,'0');
  });
  chart.data.labels = labels;
  chart.data.datasets[0].data = history.map(p => Math.round(p.pv_power || 0));
  chart.data.datasets[1].data = history.map(p => Math.round(p.surplus || 0));
  chart.data.datasets[2].data = history.map(p => Math.round(p.charging_power || 0));
  chart.update('none');
}
function setMode(mode) { fetch('/api/mode?mode=' + mode).then(() => refresh()); }
function toggleMinCharge(en) {
  fetch('/api/min_charge?enabled=' + (en ? '1' : '0'));
}
function logout() { fetch('/api/logout', {method:'POST'}).then(() => location.reload()); }
function fmt(v, unit) { return v != null && v !== undefined ? Math.round(v) + ' ' + unit : '-'; }
function refresh() {
  fetch('/api/status').then(r => {
    if (r.status === 401) { location.reload(); return Promise.reject('auth'); }
    return r.json();
  }).then(d => {
    if (!d) return;
    const isCharging = d.action === 'charging' || d.action === 'night_mode' ||
                       d.action === 'force_on' || d.action === 'min_daily_charge';
    const dotClass = isCharging ? 'dot charging' : 'dot';
    let html = '';
    const fields = [
      ['Action', '<span class="' + dotClass + '"></span>' + (d.action || '-')],
      ['Mode', d.mode || '-'],
      ['PV Power', fmt(d.pv_power, 'W')],
      ['Load', fmt(d.load_power, 'W')],
      ['Grid', fmt(d.grid_power, 'W')],
      ['Surplus', fmt(d.surplus, 'W')],
      ['Charging', fmt(d.charging_power, 'W')],
      ['Amps', d.current_amp ? d.current_amp + ' A' : '-'],
      ['Phases', d.current_phases == 2 ? '3-phase' : d.current_phases == 1 ? '1-phase' : '-'],
      ['Est. remaining', d.charge_estimate ? d.charge_estimate.text : 'n/a'],
    ];
    fields.forEach(f => {
      if (f[1] && f[1] !== '-' && f[1] !== 'undefined W') {
        html += '<div class="stat"><span class="label">' + f[0] +
                '</span><span class="value">' + f[1] + '</span></div>';
      }
    });
    if (!html) html = '<div class="stat"><span class="label">Action</span><span class="value">' +
                       (d.action || 'waiting') + '</span></div>';
    document.getElementById('status').innerHTML = html;
    // Mode buttons
    const map = {'auto':'auto','surplus only':'surplus','force on':'force_on','force off':'force_off'};
    document.querySelectorAll('.mode-btn').forEach(btn => {
      btn.classList.toggle('active', map[btn.textContent.toLowerCase()] === d.mode);
    });
    // Min charge toggle
    if (d.min_charge_enabled !== undefined) {
      document.getElementById('min-charge-toggle').checked = d.min_charge_enabled;
    }
    // Daily stats
    if (d.daily_stats) {
      document.getElementById('ds-solar').textContent = d.daily_stats.solar_kwh.toFixed(2);
      document.getElementById('ds-grid').textContent = d.daily_stats.grid_kwh.toFixed(2);
      document.getElementById('ds-sessions').textContent = d.daily_stats.sessions;
    }
    // History chart
    if (d.history_10m) updateChart(charts['10m'], d.history_10m, true);
    if (d.history_4h)  updateChart(charts['4h'],  d.history_4h,  false);
    if (d.history_24h) updateChart(charts['24h'], d.history_24h, false);
    // Logout button
    document.getElementById('logout-btn').style.display = d.auth_enabled ? '' : 'none';
    document.getElementById('updated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  }).catch(e => { if (e !== 'auth') console.error(e); });
}
charts['10m'] = makeChart('chart-10m');
charts['4h']  = makeChart('chart-4h');
charts['24h'] = makeChart('chart-24h');
refresh();
setInterval(refresh, 5000);
if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js'); }
</script>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    controller = None
    config = None

    def _is_authenticated(self):
        global _web_password
        if not _web_password:
            return True
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        token = cookie.get("session")
        if token and token.value in _sessions:
            return True
        return False

    def do_GET(self):
        parsed = urlparse(self.path)

        # PWA assets (no auth needed)
        if parsed.path == "/manifest.json":
            self._respond(200, "application/json", MANIFEST_JSON)
            return
        if parsed.path == "/icon.svg":
            self._respond(200, "image/svg+xml", SVG_ICON)
            return
        if parsed.path == "/sw.js":
            self._respond(200, "application/javascript", SERVICE_WORKER_JS)
            return

        # Auth check
        if not self._is_authenticated():
            if parsed.path.startswith("/api/"):
                self._respond(401, "application/json", '{"error":"unauthorized"}')
            else:
                self._respond(200, "text/html", LOGIN_PAGE)
            return

        if parsed.path == "/" or parsed.path == "":
            self._respond(200, "text/html", HTML_PAGE)

        elif parsed.path == "/api/status":
            status = self.controller.last_status.copy() if self.controller.last_status else {}
            status["mode"] = self.controller.mode
            status["history_10m"] = self.controller.get_history(minutes=10)
            status["history_4h"] = self.controller.get_history(minutes=240)
            status["history_24h"] = self.controller.get_history(minutes=1440)
            status["daily_stats"] = self.controller.daily_stats.to_dict()
            status["min_charge_enabled"] = self.controller.min_charge_enabled
            status["auth_enabled"] = bool(_web_password)
            self._respond(200, "application/json", json.dumps(status))

        elif parsed.path == "/api/mode":
            params = parse_qs(parsed.query)
            mode = params.get("mode", [None])[0]
            if mode and self.controller.set_mode(mode):
                self._respond(200, "application/json", json.dumps({"ok": True, "mode": mode}))
            else:
                self._respond(400, "application/json",
                              json.dumps({"ok": False, "error": "invalid mode",
                                          "valid": ["auto", "force_on", "force_off", "surplus"]}))

        elif parsed.path == "/api/min_charge":
            params = parse_qs(parsed.query)
            enabled = params.get("enabled", ["0"])[0] == "1"
            self.controller.set_min_charge_enabled(enabled)
            self._respond(200, "application/json", json.dumps({"ok": True, "enabled": enabled}))

        elif parsed.path == "/api/log/download":
            from datetime import date as _date
            log_dir = self.controller._log_dir
            today = _date.today().isoformat()
            log_path = os.path.join(log_dir, f"solar_{today}.csv")
            if os.path.exists(log_path):
                with open(log_path, "r") as f:
                    csv_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", f"attachment; filename=solar_{today}.csv")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(csv_data.encode())
            else:
                self._respond(404, "application/json",
                              json.dumps({"error": "No log file for today yet"}))


        else:
            self._respond(404, "text/plain", "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/login":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                self._respond(400, "application/json", '{"ok":false}')
                return
            if data.get("password") == _web_password:
                token = secrets.token_hex(32)
                _sessions.add(token)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self._respond(401, "application/json", '{"ok":false}')
            return

        if parsed.path == "/api/logout":
            cookie_header = self.headers.get("Cookie", "")
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            token = cookie.get("session")
            if token and token.value in _sessions:
                _sessions.discard(token.value)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        self._respond(404, "text/plain", "Not found")

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def log_message(self, format, *args):
        pass  # suppress request logs


def start_web_server(controller, config=None, port=8080):
    """Start the web UI/API in a background thread."""
    global _web_password
    config = config or {}
    _web_password = config.get("web_password")
    RequestHandler.controller = controller
    RequestHandler.config = config
    server = HTTPServer(("0.0.0.0", port), RequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Web UI running at http://0.0.0.0:{port}")
    if _web_password:
        logger.info("Web password protection enabled")
    return server
