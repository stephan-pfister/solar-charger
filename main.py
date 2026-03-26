#!/usr/bin/env python3
"""Solar surplus charger — connects Fronius Symo to go-eCharger.

Reads PV surplus from Fronius Solar API and adjusts go-eCharger current
to use excess solar production for EV charging.
"""

import json
import logging
import signal
import sys
import time
from pathlib import Path

from discovery import DeviceDiscovery
from fronius import FroniusClient
from charger import GoECharger
from controller import SurplusController
from web import start_web_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("solar_charger")


def load_config():
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


def resolve_ips(config):
    """Resolve device IPs from config or mDNS discovery."""
    fronius_ip = config.get("fronius_ip")
    charger_ip = config.get("charger_ip")

    if fronius_ip and charger_ip:
        logger.info(f"Using configured IPs: Fronius={fronius_ip}, Charger={charger_ip}")
        return fronius_ip, charger_ip

    logger.info("Running mDNS discovery for missing IPs...")
    disco = DeviceDiscovery()
    found_fronius, found_charger = disco.discover(timeout=15)

    fronius_ip = fronius_ip or found_fronius
    charger_ip = charger_ip or found_charger

    if not fronius_ip:
        logger.error(
            "Fronius IP not found. Set 'fronius_ip' in config.json "
            "or ensure the device is on the network."
        )
        sys.exit(1)

    if not charger_ip:
        logger.error(
            "go-eCharger IP not found. Set 'charger_ip' in config.json "
            "or ensure the device is on the network."
        )
        sys.exit(1)

    # Save discovered IPs to config for next time
    config["fronius_ip"] = fronius_ip
    config["charger_ip"] = charger_ip
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)
    logger.info("Saved discovered IPs to config.json")

    return fronius_ip, charger_ip


def main():
    config = load_config()
    interval = config.get("update_interval_seconds", 10)

    fronius_ip, charger_ip = resolve_ips(config)

    fronius = FroniusClient(fronius_ip)
    charger = GoECharger(charger_ip)
    controller = SurplusController(config, fronius, charger)

    # Test connectivity
    logger.info("Testing Fronius connection...")
    pf = fronius.get_power_flow()
    if pf is None:
        logger.error("Cannot reach Fronius. Check IP and network.")
        sys.exit(1)
    logger.info(
        f"Fronius OK — PV: {pf['pv_power']:.0f}W, "
        f"Grid: {pf['grid_power']:.0f}W, "
        f"Load: {pf['load_power']:.0f}W"
    )

    logger.info("Testing go-eCharger connection...")
    cs = charger.get_status()
    if cs is None:
        logger.error("Cannot reach go-eCharger. Check IP and network.")
        sys.exit(1)
    car_states = {1: "idle", 2: "charging", 3: "waiting", 4: "complete"}
    logger.info(
        f"Charger OK — Car: {car_states.get(cs['car'], '?')}, "
        f"Amp: {cs['amp']}A"
    )

    # Graceful shutdown
    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("Shutting down — stopping charger...")
        charger.stop_charging()
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start web UI
    web_port = config.get("web_port", 8080)
    start_web_server(controller, config=config, port=web_port)

    # Main loop
    logger.info(
        f"Starting surplus charging controller "
        f"(interval={interval}s, min={config.get('min_surplus_watts', 1400)}W, "
        f"phases={config.get('phases', 3)}, "
        f"amps={config.get('min_amps', 6)}-{config.get('max_amps', 16)}A)"
    )

    while running:
        try:
            status = controller.update()
            logger.info(f"Cycle result: {status.get('action', '?')}")
        except Exception:
            logger.exception("Error in control cycle")
        time.sleep(interval)

    logger.info("Solar charger stopped.")


if __name__ == "__main__":
    main()
