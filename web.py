"""Minimal web UI and API for controlling the solar charger."""

import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Solar Charger</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee;
         display: flex; justify-content: center; padding: 20px; }
  .container { max-width: 480px; width: 100%; }
  h1 { text-align: center; margin-bottom: 20px; font-size: 1.4em; }
  .card { background: #16213e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 1em; color: #888; margin-bottom: 12px; }
  .stat { display: flex; justify-content: space-between; padding: 6px 0;
          border-bottom: 1px solid #1a1a2e; }
  .stat:last-child { border: none; }
  .stat .label { color: #aaa; }
  .stat .value { font-weight: bold; }
  .modes { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .mode-btn { padding: 14px; border: 2px solid #333; border-radius: 10px;
              background: #0f3460; color: #eee; font-size: 1em; cursor: pointer;
              transition: all 0.2s; }
  .mode-btn:hover { background: #1a508b; }
  .mode-btn.active { border-color: #e94560; background: #e94560; }
  .refresh { text-align: center; color: #555; font-size: 0.8em; margin-top: 10px; }
</style>
</head>
<body>
<div class="container">
  <h1>Solar Charger</h1>
  <div class="card" id="status-card">
    <h2>Status</h2>
    <div id="status">Loading...</div>
  </div>
  <div class="card">
    <h2>Mode</h2>
    <div class="modes">
      <button class="mode-btn" onclick="setMode('auto')">Auto</button>
      <button class="mode-btn" onclick="setMode('surplus')">Surplus Only</button>
      <button class="mode-btn" onclick="setMode('force_on')">Force ON</button>
      <button class="mode-btn" onclick="setMode('force_off')">Force OFF</button>
    </div>
  </div>
  <div class="refresh" id="updated"></div>
</div>
<script>
function setMode(mode) {
  fetch('/api/mode?mode=' + mode).then(() => refresh());
}
function refresh() {
  fetch('/api/status').then(r => r.json()).then(d => {
    let html = '';
    const fields = [
      ['Mode', d.mode || '-'],
      ['Action', d.action || '-'],
      ['PV Power', fmt(d.pv_power, 'W')],
      ['Load', fmt(d.load_power, 'W')],
      ['Grid', fmt(d.grid_power, 'W')],
      ['Surplus', fmt(d.surplus, 'W')],
      ['Charging', fmt(d.charging_power, 'W')],
      ['Amps', d.current_amp ? d.current_amp + ' A' : '-'],
      ['Phases', d.current_phases == 2 ? '3-phase' : d.current_phases == 1 ? '1-phase' : '-'],
    ];
    fields.forEach(f => {
      if (f[1] && f[1] !== '-' && f[1] !== 'undefined W') {
        html += '<div class="stat"><span class="label">' + f[0] +
                '</span><span class="value">' + f[1] + '</span></div>';
      }
    });
    if (!html) html = '<div class="stat"><span class="label">Action</span><span class="value">' + (d.action || 'waiting') + '</span></div>';
    document.getElementById('status').innerHTML = html;
    document.querySelectorAll('.mode-btn').forEach(btn => {
      btn.classList.toggle('active', btn.textContent.toLowerCase().replace(' ', '_') === d.mode);
    });
    // Fix button text matching
    document.querySelectorAll('.mode-btn').forEach(btn => {
      const map = {'auto':'auto','surplus only':'surplus','force on':'force_on','force off':'force_off'};
      btn.classList.toggle('active', map[btn.textContent.toLowerCase()] === d.mode);
    });
    document.getElementById('updated').textContent = 'Updated: ' + new Date().toLocaleTimeString();
  });
}
function fmt(v, unit) { return v != null ? Math.round(v) + ' ' + unit : '-'; }
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    controller = None

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            self._respond(200, "text/html", HTML_PAGE)

        elif parsed.path == "/api/status":
            status = self.controller.last_status.copy() if self.controller.last_status else {}
            status["mode"] = self.controller.mode
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
        else:
            self._respond(404, "text/plain", "Not found")

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def log_message(self, format, *args):
        pass  # suppress request logs


def start_web_server(controller, port=8080):
    """Start the web UI/API in a background thread."""
    RequestHandler.controller = controller
    server = HTTPServer(("0.0.0.0", port), RequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Web UI running at http://0.0.0.0:{port}")
    return server
