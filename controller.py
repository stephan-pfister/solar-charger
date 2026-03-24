"""Surplus charging controller with night mode and automatic phase switching.

Logic:
  The go-eCharger is NOT behind the Fronius smart meter.
  Therefore P_Grid from Fronius reflects only PV vs household consumption.

  surplus = -P_Grid  (negative P_Grid = exporting to grid = available surplus)

  Since the charger is not metered, changing charger power does NOT affect
  the next P_Grid reading. We simply redirect the export to the charger.

  Phase switching (daytime):
    surplus >= 4140W (6A * 3 * 230V)  → 3-phase charging
    surplus >= 1380W (6A * 1 * 230V)  → 1-phase charging
    surplus < 1380W                   → stop

  Night mode (21:00 - 05:00):
    Charge at full speed, 3-phase, max amps — regardless of surplus.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


MODE_AUTO = "auto"          # surplus + night schedule
MODE_FORCE_ON = "force_on"  # full speed, ignore surplus
MODE_FORCE_OFF = "force_off"  # stop charging
MODE_SURPLUS = "surplus"    # surplus only, no night charging


class SurplusController:
    def __init__(self, config, fronius, charger):
        self.fronius = fronius
        self.charger = charger
        self.min_amps = config.get("min_amps", 6)
        self.max_amps = config.get("max_amps", 16)
        self.voltage = config.get("voltage", 230)
        self.tolerance = config.get("grid_tolerance_watts", 200)
        self.night_start = config.get("night_start_hour", 21)
        self.night_end = config.get("night_end_hour", 5)

        # Override mode — default is auto (surplus + night)
        self.mode = MODE_AUTO
        self.last_status = {}

        # Hysteresis: wait N consecutive cycles below threshold before stopping
        self._stop_count = 0
        self._stop_threshold = 3  # ~30s at 10s interval

        # Phase thresholds
        self.power_1phase = 1 * self.voltage       # 230W per amp at 1-phase
        self.power_3phase = 3 * self.voltage       # 690W per amp at 3-phase
        self.min_1phase = self.min_amps * self.power_1phase   # 1380W
        self.min_3phase = self.min_amps * self.power_3phase   # 4140W

    def set_mode(self, mode):
        """Set charging mode. Returns True if valid."""
        valid = {MODE_AUTO, MODE_FORCE_ON, MODE_FORCE_OFF, MODE_SURPLUS}
        if mode not in valid:
            return False
        logger.info(f"Mode changed: {self.mode} → {mode}")
        self.mode = mode
        return True

    def _is_night(self):
        """Check if current time is within night charging window."""
        hour = datetime.now().hour
        if self.night_start > self.night_end:
            return hour >= self.night_start or hour < self.night_end
        return self.night_start <= hour < self.night_end

    def _choose_phase_and_amps(self, available_watts):
        """Determine optimal phase mode and amperage for given surplus.

        Returns (phases, amps) where phases is 1 or 2 (psm value),
        or (None, 0) if surplus is too low.
        """
        # Try 3-phase first (more efficient)
        if available_watts >= self.min_3phase:
            amps = int(min(available_watts / self.power_3phase, self.max_amps))
            return 2, amps

        # Fall back to 1-phase
        if available_watts >= self.min_1phase:
            amps = int(min(available_watts / self.power_1phase, self.max_amps))
            return 1, amps

        return None, 0

    def _force_full_speed(self, charger_status, label):
        """Charge at max amps, 3-phase."""
        self._stop_count = 0
        current_phases = charger_status["phases"]
        needs_phase_switch = current_phases != 2

        self.charger.set_charging(
            self.max_amps, force_on=True,
            phases=2 if needs_phase_switch else None,
        )
        logger.info(
            f"{label}: charging at {self.max_amps}A 3-phase "
            f"({self.max_amps * self.power_3phase}W)"
        )
        return {
            "action": label.lower().replace(" ", "_"),
            "mode": self.mode,
            "set_amps": self.max_amps,
            "phases": 3,
            "power": self.max_amps * self.power_3phase,
        }

    def update(self):
        """Run one control cycle. Returns a status dict for logging."""
        charger_status = self.charger.get_status()
        if charger_status is None:
            logger.warning("Could not read charger data, skipping cycle")
            self.last_status = {"action": "skip", "reason": "charger_error"}
            return self.last_status

        # No car connected — nothing to do
        if charger_status["car"] == 1:
            self.last_status = {"action": "idle", "reason": "no_car", "mode": self.mode}
            return self.last_status

        # Car finished charging
        if charger_status["car"] == 4:
            self.last_status = {"action": "idle", "reason": "car_complete", "mode": self.mode}
            return self.last_status

        # ── Force OFF override ──
        if self.mode == MODE_FORCE_OFF:
            if charger_status["car"] == 2:
                self.charger.stop_charging()
            logger.info("Force OFF: charging stopped by override")
            self.last_status = {"action": "force_off", "mode": self.mode}
            return self.last_status

        # ── Force ON override: full speed regardless of surplus ──
        if self.mode == MODE_FORCE_ON:
            self.last_status = self._force_full_speed(charger_status, "Force ON")
            return self.last_status

        # ── Night mode (only in auto mode): full speed, 3-phase ──
        if self.mode == MODE_AUTO and self._is_night():
            self.last_status = self._force_full_speed(charger_status, "Night mode")
            return self.last_status

        # ── Daytime: surplus-based charging with phase switching ──
        power_flow = self.fronius.get_power_flow()
        if power_flow is None:
            logger.warning("Could not read Fronius data, skipping cycle")
            return {"action": "skip", "reason": "fronius_error"}

        grid_power = power_flow["grid_power"]
        pv_power = power_flow["pv_power"]
        load_power = power_flow["load_power"]

        # Surplus = what we're exporting (negative grid = export)
        surplus = -grid_power
        available = surplus - self.tolerance

        target_phases, target_amps = self._choose_phase_and_amps(available)

        status = {
            "mode": self.mode,
            "pv_power": pv_power,
            "load_power": load_power,
            "grid_power": grid_power,
            "surplus": surplus,
            "available": available,
            "current_amp": charger_status["amp"],
            "current_phases": charger_status["phases"],
            "charging_power": charger_status["charging_power"],
        }

        if target_amps >= self.min_amps:
            # Enough surplus — charge
            self._stop_count = 0
            current_phases = charger_status["phases"]
            needs_phase_switch = current_phases != target_phases

            phase_power = self.power_3phase if target_phases == 2 else self.power_1phase
            phase_label = "3-phase" if target_phases == 2 else "1-phase"

            self.charger.set_charging(
                target_amps, force_on=True,
                phases=target_phases if needs_phase_switch else None,
            )
            status["action"] = "charging"
            status["set_amps"] = target_amps
            status["set_phases"] = 3 if target_phases == 2 else 1
            logger.info(
                f"Surplus: {surplus:.0f}W → {phase_label} at {target_amps}A "
                f"({target_amps * phase_power:.0f}W)"
            )
        else:
            # Not enough surplus
            self._stop_count += 1
            if self._stop_count >= self._stop_threshold:
                if charger_status["car"] == 2:
                    self.charger.stop_charging()
                    logger.info(
                        f"Surplus too low: {surplus:.0f}W "
                        f"(need {self.min_1phase:.0f}W for 1-phase). Stopping."
                    )
                status["action"] = "stopped"
            else:
                logger.info(
                    f"Surplus low: {surplus:.0f}W, "
                    f"waiting ({self._stop_count}/{self._stop_threshold})"
                )
                status["action"] = "waiting"

        self.last_status = status
        return status
