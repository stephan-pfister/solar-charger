"""go-eCharger HTTP API v2 client."""

import logging
import requests

logger = logging.getLogger(__name__)


class GoECharger:
    """Client for go-eCharger with HTTP API v2 (firmware 55+)."""

    def __init__(self, ip):
        self.base_url = f"http://{ip}"
        self.timeout = 5

    def _get_status(self, keys):
        """Read status values for given keys."""
        url = f"{self.base_url}/api/status"
        params = {"filter": ",".join(keys)}
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"go-eCharger status error: {e}")
            return None

    def _set_values(self, **kwargs):
        """Set one or more charger parameters."""
        url = f"{self.base_url}/api/set"
        try:
            resp = requests.get(url, params=kwargs, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"go-eCharger set error: {e}")
            return None

    def get_status(self):
        """Get charger status.

        Returns dict with:
            car:            1=idle, 2=charging, 3=waiting, 4=complete
            amp:            Current setting in amps
            charging_power: Current charging power in watts
            allowed:        Whether charging is allowed
            force_state:    0=neutral, 1=off, 2=on
            phases:         Current phase mode (1 or 2 meaning 1-phase or 3-phase)
            battery_percent: Battery SoC percentage (None if unavailable)
            battery_capacity_wh: Battery capacity in Wh (None if unavailable)
        """
        data = self._get_status(["car", "amp", "nrg", "alw", "frc", "psm", "soc", "dwo"])
        if data is None:
            return None

        # Fix: calculate charging power from amps, voltage, and phases
        # nrg array: indices 0-3 are voltages, 4-7 are currents, 8-10 phase powers, 11 total
        # API v2: nrg[11] is total power in watts (not 0.01kW as previously assumed)
        nrg = data.get("nrg", [0] * 16)
        amp = data.get("amp", 0)
        psm = data.get("psm", 2)  # 1=1-phase, 2=3-phase
        phase_count = 3 if psm == 2 else 1

        # Use nrg[11] directly (watts) if available and non-zero, otherwise calculate
        if len(nrg) > 11 and nrg[11] is not None and nrg[11] > 0:
            charging_power = nrg[11]
        elif data.get("car", 0) == 2:
            # Fallback: calculate from amps x voltage x phases
            charging_power = amp * 230 * phase_count
        else:
            charging_power = 0

        result = {
            "car": data.get("car", 0),
            "amp": amp,
            "charging_power": charging_power,
            "allowed": data.get("alw", False),
            "force_state": data.get("frc", 0),
            "phases": psm,
            "battery_percent": data.get("soc", None),
            "battery_capacity_wh": data.get("dwo", None),
        }

        car_states = {1: "idle", 2: "charging", 3: "waiting", 4: "complete"}
        logger.debug(
            f"Charger: car={car_states.get(result['car'], '?')}, "
            f"amp={result['amp']}A, "
            f"power={result['charging_power']:.0f}W, "
            f"frc={result['force_state']}, "
            f"psm={result['phases']}"
        )
        return result

    def set_phases(self, phases):
        """Switch between 1-phase and 3-phase charging.

        Args:
            phases: 1 for single-phase, 2 for three-phase
        Note: phase switching requires the charger to briefly stop charging.
        """
        if phases not in (1, 2):
            logger.error(f"Invalid phase value: {phases}")
            return None
        logger.info(f"Switching to {'1-phase' if phases == 1 else '3-phase'} (psm={phases})")
        return self._set_values(psm=phases)

    def set_charging(self, amps, force_on=True, phases=None):
        """Set charging current, optionally switch phases, and force on/off.

        Args:
            amps: Charging current 6-16A (or 0 to stop)
            force_on: If True, set frc=2 (force charge). If False, set frc=1 (off).
            phases: If set, switch phase mode (1=single, 2=three) before setting amps.
        """
        if phases is not None:
            self.set_phases(phases)

        if amps == 0:
            logger.info("Pausing charging (frc=0, amp=6)")
            return self._set_values(frc=0, amp=6)
        else:
            frc = 2 if force_on else 0
            logger.info(f"Setting charging: {amps}A, frc={frc}")
            return self._set_values(amp=int(amps), frc=frc)

    def stop_charging(self):
        """Stop charging."""
        return self.set_charging(0)

    def is_car_connected(self):
        """Check if a car is connected."""
        status = self.get_status()
        if status is None:
            return False
        return status["car"] in (2, 3, 4)  # charging, waiting, or complete
