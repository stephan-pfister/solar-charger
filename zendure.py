"""Zendure SolarFlow local HTTP client (zenSDK).

The device runs an HTTP server on port 80 and needs no cloud connection
and no authentication. Two endpoints are used:

    GET  /properties/report   -> all device properties as JSON
    POST /properties/write    -> change a property (body must carry the serial)

Naming is from the hub's point of view and is easy to get backwards:

    outputPackPower   power the hub sends OUT to the pack -> battery CHARGES
    packInputPower    power coming IN from the pack       -> battery DISCHARGES

Verified against a SolarFlow 1600 AC+ (firmware message schema v3): while
charging at 599W the device reported outputPackPower=599, packInputPower=0,
gridInputPower=599, and pack power 504W (49.0V * 10.3A), i.e. the difference
is conversion loss.

Percentages in the *writable* SoC properties are stored times ten
(socSet=1000 means 100%, minSoc=100 means 10%). electricLevel is a plain
percent. Temperatures are 0.1 Kelvin.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)


class ZendureClient:
    """Client for Zendure SolarFlow devices with the local zenSDK HTTP API."""

    def __init__(self, ip, serial=None, timeout=4, cache_seconds=60):
        self.base_url = f"http://{ip}"
        self.serial = serial
        # Kept well under the 10s cycle: this call sits next to the Fronius and
        # charger reads and a slow battery must never stall charging control.
        # Measured on a SolarFlow 1600 AC+ with rssi -45dBm, so not a signal
        # problem: the device answers in 0.3-1.3s when it answers at all, and
        # times out outright on roughly two thirds of requests. 2s dropped 15
        # of 22 cycles.
        self.timeout = timeout
        # Because the device is that unreliable, a failed read falls back to
        # the last good one for a while instead of reporting "no battery".
        # SoC and power move slowly; a minute-old reading still beats losing
        # the surplus correction and the charge/discharge control entirely.
        self.cache_seconds = cache_seconds
        self._last_good = None
        self._last_good_ts = 0.0

    def _read(self):
        """Read battery state. Returns a dict, or None if unreachable.

        Keys:
            soc:              state of charge in percent
            charge_power:     watts going INTO the battery (0 when discharging)
            discharge_power:  watts coming OUT of the battery (0 when charging)
            solar_power:      PV power on the Zendure's own MPPT inputs
            home_power:       watts the device feeds into the house
            grid_input_power: watts the device draws from the house/grid
            input_limit:      current charge power limit in watts
            output_limit:     current discharge power limit in watts
            pack_count:       number of battery packs
            serial:           device serial (used for writes)
        """
        try:
            resp = requests.get(f"{self.base_url}/properties/report",
                                timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Zendure read error: {e}")
            return None

        props = data.get("properties")
        if not isinstance(props, dict):
            logger.warning("Zendure response has no properties block")
            return None

        packs = data.get("packData") or []

        # electricLevel is the documented top-level SoC, but fall back to the
        # pack's own socLevel so a firmware that only reports one still works.
        soc = props.get("electricLevel")
        if soc is None and packs:
            soc = packs[0].get("socLevel")

        result = {
            "soc": soc,
            "charge_power": props.get("outputPackPower") or 0,
            "discharge_power": props.get("packInputPower") or 0,
            "solar_power": props.get("solarInputPower") or 0,
            "home_power": props.get("outputHomePower") or 0,
            "grid_input_power": props.get("gridInputPower") or 0,
            "input_limit": props.get("inputLimit"),
            "output_limit": props.get("outputLimit"),
            "pack_count": props.get("packNum") or len(packs),
            "serial": data.get("sn") or self.serial,
        }

        logger.debug(
            f"Zendure: soc={result['soc']}%, "
            f"charge={result['charge_power']}W, "
            f"discharge={result['discharge_power']}W, "
            f"limit={result['input_limit']}W"
        )
        return result

    def get_status(self):
        """Read battery state, falling back to the last good value.

        Returns None only when there is no reading fresh enough to use. A
        served-from-cache result carries stale_seconds so callers can tell.
        """
        fresh = self._read()
        now = time.time()
        if fresh is not None:
            self._last_good = fresh
            self._last_good_ts = now
            return fresh

        age = now - self._last_good_ts
        if self._last_good is not None and age <= self.cache_seconds:
            logger.info(f"Zendure unreachable, using reading from {age:.0f}s ago")
            stale = dict(self._last_good)
            stale["stale_seconds"] = round(age, 1)
            return stale
        return None

    def set_input_limit(self, watts):
        """Set the charge power limit. Returns True on success.

        Used to hand solar surplus to the car first: setting this to 0 pauses
        battery charging without touching any other setting.
        """
        return self._write_property("inputLimit", watts)

    def set_output_limit(self, watts):
        """Set the discharge power limit. Returns True on success.

        With the app's HEMS enabled the device maintains this itself. With HEMS
        off nothing does, and a limit of 0 means the battery can charge but can
        never give the energy back -- so the controller has to drive it.
        """
        return self._write_property("outputLimit", watts)

    def set_ac_mode(self, mode):
        """Switch the AC direction. 1 = charge, 2 = discharge.

        Measured on a SolarFlow 1600 AC+: with acMode=1 a non-zero outputLimit
        is accepted and reported back, but the battery does not discharge --
        packInputPower and outputHomePower stay at 0. Setting acMode=2 with the
        same limit produced 298W within one poll. The mode, not the limit, is
        what actually opens the discharge path.
        """
        if mode not in (1, 2):
            logger.error(f"Invalid Zendure acMode: {mode}")
            return False
        serial = self.serial
        if not serial:
            logger.error("Zendure write needs a serial number")
            return False
        return self._write({"acMode": int(mode)}, serial)

    def _write_property(self, name, watts):
        serial = self.serial
        if not serial:
            logger.error("Zendure write needs a serial number")
            return False
        return self._write({name: max(0, int(watts))}, serial)

    def _write(self, properties, serial, attempts=2):
        """Write properties, retrying once. The device drops requests often
        enough that a single attempt leaves the battery uncontrolled."""
        payload = {"sn": serial, "properties": properties}
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(f"{self.base_url}/properties/write",
                                     json=payload, timeout=self.timeout)
                resp.raise_for_status()
            except requests.RequestException as e:
                if attempt < attempts:
                    logger.warning(f"Zendure write failed ({e}), retrying")
                    continue
                logger.error(f"Zendure write error: {e}")
                return False
            logger.info(f"Zendure set {properties}")
            return True
        return False
