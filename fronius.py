"""Fronius Solar API v1 client for Symo inverters."""

import logging
import requests

logger = logging.getLogger(__name__)


class FroniusClient:
    def __init__(self, ip):
        self.base_url = f"http://{ip}"
        self.timeout = 5

    def get_power_flow(self):
        """Read real-time power flow data.

        Returns dict with keys:
            pv_power:   Current PV production in watts (>= 0)
            grid_power: Grid exchange in watts (positive = import, negative = export)
            load_power: Household consumption in watts (>= 0)
        """
        url = f"{self.base_url}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Fronius API error: {e}")
            return None

        site = data.get("Body", {}).get("Data", {}).get("Site", {})

        pv_power = site.get("P_PV") or 0       # None when no production
        grid_power = site.get("P_Grid") or 0    # positive=import, negative=export
        load_power = site.get("P_Load") or 0    # negative in API, we make positive

        result = {
            "pv_power": max(0, pv_power),
            "grid_power": grid_power,
            "load_power": abs(load_power),
        }

        logger.debug(
            f"Fronius: PV={result['pv_power']:.0f}W, "
            f"Grid={result['grid_power']:.0f}W, "
            f"Load={result['load_power']:.0f}W"
        )
        return result
