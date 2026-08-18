# Solar Surplus Charger

Connects a **Fronius Symo** inverter to a **go-eCharger** (API v2) to automatically charge your EV using surplus solar power.

## Features

- **Surplus charging** — redirects excess PV export to the charger
- **Automatic phase switching** — 3-phase when surplus is high (>4.1kW), 1-phase when low (>1.4kW)
- **Night charging** — full speed 3-phase between 21:00–05:00 (configurable)
- **Web UI** — control and monitor from your phone at `http://<server-ip>:8080`
- **Override modes** — Force ON, Force OFF, Surplus Only, Auto
- **mDNS discovery** — auto-finds devices on your network
- **Hysteresis** — waits 30s before stopping (handles passing clouds), and a
  dwell time + power margin before switching phases (avoids contactor flapping)
- **Closed-loop current** — measures what the car actually draws and compensates,
  instead of assuming `amps x voltage x phases`

## How It Works

```
┌─────────────┐        ┌──────────────┐        ┌─────────────┐
│   Fronius    │──API──▶│    Server    │──API──▶│ go-eCharger  │
│  Symo 10.0   │        │  (Python)    │        │   (API v2)   │
└─────────────┘        └──────────────┘        └─────────────┘
```

The go-eCharger is **not** behind the Fronius smart meter. The server reads the grid export from Fronius (`P_Grid`) and directs that surplus to the charger.

Every 10 seconds:
1. Read grid power from Fronius Solar API
2. Calculate surplus (negative grid = exporting)
3. Choose phase mode and amperage, or charge full speed at night
4. Send commands to go-eCharger via HTTP API v2

## Requirements

- **Fronius Symo** (or any Fronius inverter with Solar API v1)
- **go-eCharger** with HTTP API v2 (firmware 55+)
- **Python 3.9+**
- Server on the same network (Raspberry Pi, Synology NAS, etc.)

## go-eCharger Settings

In the **go-e app**, configure:
1. **Settings → Internet → Advanced Settings → Local HTTP API v2** → **ON**
2. No schedule or PV mode set in the app (the server handles all logic)
3. Charging mode set to **Neutral**

## Installation

### Option 1: Docker (recommended for Raspberry Pi / Synology)

```bash
git clone https://github.com/stephan-pfister/solar-charger.git
cd solar-charger

# Edit config — set IPs or leave null for auto-discovery
nano config.json

# Start
docker compose up -d

# View logs
docker compose logs -f
```

**Synology NAS:** Install *Container Manager* from Package Center, create a project pointing to this folder, and it picks up `docker-compose.yml` automatically.

### Option 2: systemd service (Linux)

```bash
git clone https://github.com/stephan-pfister/solar-charger.git
cd solar-charger
pip install -r requirements.txt

# Edit config
nano config.json

# Create systemd service
sudo tee /etc/systemd/system/solar-charger.service << 'EOF'
[Unit]
Description=Solar Surplus Charger
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/solar-charger/main.py
WorkingDirectory=/home/pi/solar-charger
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now solar-charger
sudo journalctl -u solar-charger -f
```

## Configuration

Edit `config.json`:

```json
{
    "fronius_ip": null,          // null = auto-discover via mDNS
    "charger_ip": null,          // null = auto-discover via mDNS
    "update_interval_seconds": 10,
    "min_amps": 6,               // minimum charging current
    "max_amps": 16,              // maximum charging current
    "voltage": 230,
    "grid_tolerance_watts": 200, // safety margin kept out of the surplus
    "night_start_hour": 21,      // night charging starts at 21:00
    "night_end_hour": 5,         // night charging ends at 05:00
    "web_port": 8080,            // web UI port
    "web_password": null,        // null = no login required
    "min_charge_minutes_per_day": 0,

    // Phase-switch hysteresis
    "phase_switch_margin_watts": 500,  // extra headroom needed to go 3-phase
    "phase_min_dwell_seconds": 300,    // minimum time between phase switches

    // Closed-loop current correction
    "max_power_offset_watts": 600      // cap on the learned per-phase shortfall
}
```

The minimum surplus is derived from `min_amps x voltage` (1380W for 1-phase,
4140W for 3-phase), not configured separately.

## Web UI & API

Open `http://<server-ip>:8080` in your browser.

### Modes

| Mode | Description |
|------|-------------|
| **Auto** | Surplus during day + full speed at night (default) |
| **Surplus Only** | Surplus-based charging, no night charging |
| **Force ON** | Full speed 3-phase 16A, ignores surplus |
| **Force OFF** | Stop all charging |

### API Endpoints

```
GET  /api/status                    → current status as JSON
POST /api/mode?mode=auto            → set mode (auto, surplus, force_on, force_off)
POST /api/min_charge?enabled=1      → toggle minimum daily charge
GET  /api/logs                      → list available log dates
GET  /api/log/download?date=Y-M-D   → download one day as CSV
GET  /api/log/download/all          → download all logs as ZIP
```

`GET` still works on the two control endpoints so existing bookmarks and phone
shortcuts keep working.

`/api/status` includes `last_update_age` (seconds since the last control cycle);
the UI shows a warning banner when the controller stops cycling. History is
downsampled server-side to 300 points per chart.

## File Structure

```
├── main.py           # Entry point, main loop
├── controller.py     # Surplus logic, phase switching, night mode
├── fronius.py        # Fronius Solar API v1 client
├── charger.py        # go-eCharger HTTP API v2 client
├── discovery.py      # mDNS auto-discovery
├── web.py            # Web UI and REST API
├── test_controller.py # Unit tests (python3 -m unittest)
├── config.json       # Configuration
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Development

```bash
python3 -m unittest -v
```

## License

MIT
