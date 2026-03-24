"""mDNS discovery for Fronius inverter and go-eCharger."""

import time
import logging
from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

logger = logging.getLogger(__name__)

FRONIUS_SERVICE = "_http._tcp.local."
CHARGER_SERVICE = "_http._tcp.local."

FRONIUS_NAMES = ["fronius", "symo", "datamanager"]
CHARGER_NAMES = ["go-echarger", "go-e"]


class DeviceDiscovery:
    def __init__(self):
        self.fronius_ip = None
        self.charger_ip = None

    def _on_service_state_change(self, zeroconf, service_type, name, state_change):
        if state_change != ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return

        name_lower = name.lower()
        addresses = info.parsed_addresses()
        if not addresses:
            return
        ip = addresses[0]

        if any(fn in name_lower for fn in FRONIUS_NAMES):
            logger.info(f"Discovered Fronius at {ip} ({name})")
            self.fronius_ip = ip
        elif any(cn in name_lower for cn in CHARGER_NAMES):
            logger.info(f"Discovered go-eCharger at {ip} ({name})")
            self.charger_ip = ip

    def discover(self, timeout=10):
        """Scan the network for Fronius and go-eCharger devices.

        Returns (fronius_ip, charger_ip) — either may be None if not found.
        """
        logger.info(f"Starting mDNS discovery (timeout={timeout}s)...")
        zeroconf = Zeroconf()
        browser = ServiceBrowser(
            zeroconf, "_http._tcp.local.", handlers=[self._on_service_state_change]
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.fronius_ip and self.charger_ip:
                break
            time.sleep(0.5)

        browser.cancel()
        zeroconf.close()

        if self.fronius_ip:
            logger.info(f"Fronius found: {self.fronius_ip}")
        else:
            logger.warning("Fronius not found via mDNS")

        if self.charger_ip:
            logger.info(f"go-eCharger found: {self.charger_ip}")
        else:
            logger.warning("go-eCharger not found via mDNS")

        return self.fronius_ip, self.charger_ip
