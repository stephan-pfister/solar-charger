"""Surplus charging controller with night mode and automatic phase switching.

Logic:
  The go-eCharger is NOT behind the Fronius smart meter.
  Therefore P_Grid from Fronius reflects only PV vs household consumption.

  surplus = -P_Grid  (negative P_Grid = exporting to grid = available surplus)

  Since the charger is not metered, changing charger power does NOT affect
  the next P_Grid reading. We simply redirect the export to the charger.

  Phase switching (daytime):
    surplus >= 4140W (6A * 3 * 230V)  -> 3-phase charging
    surplus >= 1380W (6A * 1 * 230V)  -> 1-phase charging
    surplus < 1380W                   -> stop

  Night mode (21:00 - 05:00):
    Charge at full speed, 3-phase, max amps -- regardless of surplus.
"""

import logging
import time
from collections import deque
from datetime import datetime, date

logger = logging.getLogger(__name__)


MODE_AUTO = "auto"          # surplus + night schedule
MODE_FORCE_ON = "force_on"  # full speed, ignore surplus
MODE_FORCE_OFF = "force_off"  # stop charging
MODE_SURPLUS = "surplus"    # surplus only, no night charging


class DailyStats:
    """Track daily charging statistics (reset at midnight)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.date = date.today()
        self.solar_kwh = 0.0
        self.grid_kwh = 0.0
        self.sessions = 0
        self._was_charging = False

    def check_midnight(self):
        today = date.today()
        if today != self.date:
            self.reset()

    def record(self, power_watts, interval_seconds, is_solar):
        """Record energy charged in this interval."""
        kwh = (power_watts * interval_seconds) / 3_600_000
        if is_solar:
            self.solar_kwh += kwh
        else:
            self.grid_kwh += kwh

    def record_session(self, is_charging):
        """Track charging session count."""
        if is_charging and not self._was_charging:
            self.sessions += 1
        self._was_charging = is_charging

    def to_dict(self):
        return {
            "solar_kwh": round(self.solar_kwh, 2),
            "grid_kwh": round(self.grid_kwh, 2),
            "sessions": self.sessions,
        }


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
        self.interval = config.get("update_interval_seconds", 10)

        # Minimum daily charge
        self.min_charge_minutes = config.get("min_charge_minutes_per_day", 0)
        self.min_charge_enabled = self.min_charge_minutes > 0

        # Override mode -- default is auto (surplus + night)
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

        # History: last 60 data points (~10 min at 10s interval)
        self.history = deque(maxlen=60)

        # Daily stats
        self.daily_stats = DailyStats()

        # Daily charge tracking (minutes charged today)
        self._charge_seconds_today = 0
        self._last_charge_date = date.today()

    def set_mode(self, mode):
        """Set charging mode. Returns True if valid."""
        valid = {MODE_AUTO, MODE_FORCE_ON, MODE_FORCE_OFF, MODE_SURPLUS}
        if mode not in valid:
            return False
        logger.info(f"Mode changed: {self.mode} -> {mode}")
        self.mode = mode
        return True

    def set_min_charge_enabled(self, enabled):
        """Toggle minimum daily charge feature."""
        self.min_charge_enabled = bool(enabled)

    def _is_night(self):
        """Check if current time is within night charging window."""
        hour = datetime.now().hour
        if self.night_start > self.night_end:
            return hour >= self.night_start or hour < self.night_end
        return self.night_start <= hour < self.night_end

    def _check_daily_charge_reset(self):
        """Reset daily charge counter at midnight."""
        today = date.today()
        if today != self._last_charge_date:
            self._charge_seconds_today = 0
            self._last_charge_date = today

    def _needs_min_charge(self):
        """Check if minimum daily charge hasn't been met."""
        if not self.min_charge_enabled or self.min_charge_minutes <= 0:
            return False
        self._check_daily_charge_reset()
        return (self._charge_seconds_today / 60) < self.min_charge_minutes

    def _record_charging(self, is_charging):
        """Track charging time for minimum daily charge."""
        self._check_daily_charge_reset()
        if is_charging:
            self._charge_seconds_today += self.interval

    def _estimate_charge_time(self, charger_status):
        """Estimate remaining charge time based on battery info."""
        soc = charger_status.get("battery_percent")
        capacity_wh = charger_status.get("battery_capacity_wh")
        power = charger_status.get("charging_power", 0)

        if soc is None or capacity_wh is None or not power or power <= 0:
            return None

        remaining_wh = capacity_wh * (100 - soc) / 100
        hours = remaining_wh / power
        h = int(hours)
        m = int((hours - h) * 60)
        return {"hours": h, "minutes": m, "text": f"{h}h {m}m"}

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
        """Charge at max amps, 3-phase. Uses frc=2 to restart from any state."""
        self._stop_count = 0
        current_phases = charger_status["phases"]
        needs_phase_switch = current_phases != 2

        if charger_status["car"] in (3, 4):
            car_states = {3: "waiting", 4: "complete"}
            logger.info(f"{label}: restarting from {car_states[charger_status['car']]} via frc=2")

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

    def _add_history_point(self, status):
        """Store a data point for the history chart."""
        point = {
            "time": time.time(),
            "pv_power": status.get("pv_power", 0),
            "surplus": status.get("surplus", 0),
            "charging_power": status.get("charging_power", 0),
        }
        self.history.append(point)

    def get_history(self):
        """Return history data points as a list."""
        return list(self.history)

    def update(self):
        """Run one control cycle. Returns a status dict for logging."""
        self.daily_stats.check_midnight()

        charger_status = self.charger.get_status()
        if charger_status is None:
            logger.warning("Could not read charger data, skipping cycle")
            self.last_status = {"action": "skip", "reason": "charger_error"}
            return self.last_status

        # No car connected -- nothing to do
        if charger_status["car"] == 1:
            self._record_charging(False)
            self.daily_stats.record_session(False)
            self.last_status = {"action": "idle", "reason": "no_car", "mode": self.mode}
            return self.last_status

        # Car finished charging -- but don't bail out if we want to restart
        # (frc=2 can wake the car from "complete" state)
        car_complete = charger_status["car"] == 4

        # Charge time estimate
        estimate = self._estimate_charge_time(charger_status)

        # -- Force OFF override --
        if self.mode == MODE_FORCE_OFF:
            self.charger.stop_charging()  # always send frc=1
            self._record_charging(False)
            self.daily_stats.record_session(False)
            logger.info("Force OFF: charging stopped by override")
            self.last_status = {"action": "force_off", "mode": self.mode,
                                "charge_estimate": estimate}
            return self.last_status

        # -- Force ON override: full speed regardless of surplus --
        if self.mode == MODE_FORCE_ON:
            self._record_charging(True)
            self.daily_stats.record_session(True)
            self.daily_stats.record(charger_status["charging_power"], self.interval, False)
            result = self._force_full_speed(charger_status, "Force ON")
            result["charge_estimate"] = estimate
            result["charging_power"] = charger_status["charging_power"]
            self.last_status = result
            return self.last_status

        # -- Night mode (only in auto mode): full speed, 3-phase --
        if self.mode == MODE_AUTO and self._is_night():
            self._record_charging(True)
            self.daily_stats.record_session(True)
            self.daily_stats.record(charger_status["charging_power"], self.interval, False)
            result = self._force_full_speed(charger_status, "Night mode")
            result["charge_estimate"] = estimate
            result["charging_power"] = charger_status["charging_power"]
            self.last_status = result
            return self.last_status

        # -- Minimum daily charge check --
        if self._needs_min_charge() and self.mode in (MODE_AUTO, MODE_SURPLUS):
            self._record_charging(True)
            self.daily_stats.record_session(True)
            self.daily_stats.record(charger_status["charging_power"], self.interval, False)
            result = self._force_full_speed(charger_status, "Min daily charge")
            result["charge_estimate"] = estimate
            result["charging_power"] = charger_status["charging_power"]
            mins_done = self._charge_seconds_today / 60
            result["min_charge_progress"] = f"{mins_done:.0f}/{self.min_charge_minutes}min"
            self.last_status = result
            self._add_history_point(result)
            return self.last_status

        # -- Daytime: surplus-based charging with phase switching --
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
            "charge_estimate": estimate,
        }

        if target_amps >= self.min_amps:
            # Enough surplus -- charge (frc=2 forces charger on, even from stopped/complete)
            self._stop_count = 0
            current_phases = charger_status["phases"]
            needs_phase_switch = current_phases != target_phases

            phase_power = self.power_3phase if target_phases == 2 else self.power_1phase
            phase_label = "3-phase" if target_phases == 2 else "1-phase"

            if car_complete or charger_status["car"] == 3:
                logger.info(
                    f"Restarting charger from state "
                    f"{'complete' if car_complete else 'waiting'} via frc=2"
                )

            self.charger.set_charging(
                target_amps, force_on=True,
                phases=target_phases if needs_phase_switch else None,
            )
            status["action"] = "charging"
            status["set_amps"] = target_amps
            status["set_phases"] = 3 if target_phases == 2 else 1

            self._record_charging(True)
            self.daily_stats.record_session(True)
            self.daily_stats.record(charger_status["charging_power"], self.interval, True)

            logger.info(
                f"Surplus: {surplus:.0f}W -> {phase_label} at {target_amps}A "
                f"({target_amps * phase_power:.0f}W)"
            )
        else:
            # Not enough surplus
            self._stop_count += 1
            self._record_charging(False)
            self.daily_stats.record_session(False)
            if self._stop_count >= self._stop_threshold:
                self.charger.stop_charging()  # always send frc=1 to ensure clean stop
                logger.info(
                    f"Surplus too low: {surplus:.0f}W "
                    f"(need {self.min_1phase:.0f}W for 1-phase). Stopped (frc=1)."
                )
                status["action"] = "stopped"
            else:
                logger.info(
                    f"Surplus low: {surplus:.0f}W, "
                    f"waiting ({self._stop_count}/{self._stop_threshold})"
                )
                status["action"] = "waiting"

        self._add_history_point(status)
        self.last_status = status
        return status
