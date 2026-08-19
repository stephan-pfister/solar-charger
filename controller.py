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

import csv
import os
import json
import logging
import time
from collections import deque
from datetime import datetime, date

logger = logging.getLogger(__name__)


MODE_AUTO = "auto"          # surplus + night schedule
MODE_FORCE_ON = "force_on"  # full speed, ignore surplus
MODE_FORCE_OFF = "force_off"  # stop charging
MODE_SURPLUS = "surplus"    # surplus only, no night charging
LOG_FIELDS = [
    "timestamp", "action", "mode", "pv_power", "load_power",
    "grid_power", "surplus", "charging_power", "set_amps",
    "set_phases", "car_state", "force_state",
    "bat_soc", "bat_charge", "bat_discharge",
    "bat_in_limit", "bat_out_limit",
]


class DailyStats:
    """Track daily charging statistics (reset at midnight)."""

    def __init__(self, log_dir="logs"):
        self._log_dir = log_dir
        self._state_path = os.path.join(log_dir, "daily_stats_state.json")
        # Throttle disk writes: persisting on every cycle wears SD cards /
        # keeps NAS volumes from sleeping. Save at most every _save_interval s.
        self._save_interval = 30
        self._last_save = 0.0
        if not self._load():
            self.reset()

    def _load(self):
        """Restore today's stats from disk after a restart, if available."""
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                self.date = date.today()
                self.solar_kwh = data.get("solar_kwh", 0.0)
                self.grid_kwh = data.get("grid_kwh", 0.0)
                self.sessions = data.get("sessions", 0)
                self._was_charging = False
                return True
        except FileNotFoundError:
            logger.info("DailyStats: no saved state yet, starting fresh")
        except (ValueError, OSError) as e:
            logger.warning(f"DailyStats: could not read saved state ({e}); starting fresh")
        return False

    def _save(self, force=False):
        """Persist current stats so a restart doesn't lose today's numbers.

        Throttled: only writes to disk every _save_interval seconds unless
        force=True (used on reset/midnight so day boundaries never get lost).
        """
        now = time.time()
        if not force and (now - self._last_save) < self._save_interval:
            return
        self._last_save = now
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "date": self.date.isoformat(),
                    "solar_kwh": self.solar_kwh,
                    "grid_kwh": self.grid_kwh,
                    "sessions": self.sessions,
                }, f)
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning(f"DailyStats: could not persist state: {e}")

    def reset(self):
        self.date = date.today()
        self.solar_kwh = 0.0
        self.grid_kwh = 0.0
        self.sessions = 0
        self._was_charging = False
        self._save(force=True)

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
        self._save()

    def record_session(self, is_charging):
        """Track charging session count."""
        if is_charging and not self._was_charging:
            self.sessions += 1
        self._was_charging = is_charging
        self._save()

    def to_dict(self):
        return {
            "solar_kwh": round(self.solar_kwh, 2),
            "grid_kwh": round(self.grid_kwh, 2),
            "sessions": self.sessions,
        }


class SurplusController:
    def __init__(self, config, fronius, charger, zendure=None):
        self.fronius = fronius
        self.charger = charger
        self.zendure = zendure
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

        # Override mode -- default after (re)start is surplus-only
        # (configurable via "default_mode" in config.json).
        self.mode = config.get("default_mode", MODE_SURPLUS)
        self.last_status = {}

        # Hysteresis: wait N consecutive cycles below threshold before stopping
        self._stop_count = 0
        self._stop_threshold = 3  # ~30s at 10s interval

        # Phase-switch hysteresis: avoid rapid 1<->3 phase flapping near the
        # threshold (each switch briefly interrupts charging and cycles the
        # charger's contactor). Require extra headroom + a minimum dwell time
        # before switching UP to 3-phase.
        self._last_phase_switch = 0.0
        self._phase_margin = config.get("phase_switch_margin_watts", 500)
        self._phase_dwell = config.get("phase_min_dwell_seconds", 300)

        # Timestamp of the last completed control cycle (for UI stale detection)
        self.last_update_ts = 0.0

        # House battery (Zendure). Reading it is always safe; letting it change
        # the control decision is opt-in until we have measured that the
        # battery really sits behind the Fronius meter.
        self.zendure_correction = config.get("zendure_correction", False)
        self.battery_status = None

        # Active battery control. Only meaningful with HEMS switched off in the
        # Zendure app -- with HEMS on the device overwrites our writes within
        # seconds. With HEMS off nothing regulates the battery, so if we do not
        # drive it, it charges at a fixed limit regardless of sun and never
        # discharges again.
        self.zendure_control = config.get("zendure_control", False)
        self.zendure_max_charge = config.get("zendure_max_charge_watts", 1200)
        self.zendure_max_discharge = config.get("zendure_max_discharge_watts", 800)
        self.zendure_min_soc = config.get("zendure_min_soc_percent", 10)
        self.zendure_reserve = config.get("zendure_reserve_watts", 200)
        self.zendure_deadband = config.get("zendure_deadband_watts", 75)
        self._battery_setpoint = (None, None)   # (input_limit, output_limit)

        # Closed-loop correction: the car draws less than the pilot limit we
        # set, so compensate instead of assuming amps * voltage * phases.
        self._power_offset = 0.0        # watts per phase
        self._max_power_offset = config.get("max_power_offset_watts", 600)
        self._last_setpoint = None

        # Phase thresholds
        self.power_1phase = 1 * self.voltage       # 230W per amp at 1-phase
        self.power_3phase = 3 * self.voltage       # 690W per amp at 3-phase
        self.min_1phase = self.min_amps * self.power_1phase   # 1380W
        self.min_3phase = self.min_amps * self.power_3phase   # 4140W

        # History: 24h at 10s interval = 8640 points
        self.history = deque(maxlen=8640)

        # Daily charge tracking (minutes charged today)
        self._charge_seconds_today = 0
        self._last_charge_date = date.today()
        # CSV log directory
        self._log_dir = config.get("log_dir", "logs")
        os.makedirs(self._log_dir, exist_ok=True)

        self._migrated_logs = set()

        # Daily stats (persisted to survive restarts)
        self.daily_stats = DailyStats(self._log_dir)

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

    def _corrected_surplus(self, surplus):
        """Remove house-battery discharge from the surplus the car may use.

        The Zendure sits behind the Fronius meter, so when it discharges into
        the house less power is drawn from the grid -- which looks exactly like
        extra PV surplus. Charging the car on that would move energy from the
        house battery into the car at roughly 75-80% round-trip efficiency.

        Battery *charging* is deliberately NOT added back. While the battery
        controls itself, that power is genuinely spoken for; handing it to the
        car as well would pull the difference from the grid. Once we command
        inputLimit ourselves that power becomes free anyway, because we set it
        to zero rather than compensating for it on paper.
        """
        if not self.zendure_correction or not self.battery_status:
            return surplus
        return surplus - (self.battery_status.get("discharge_power") or 0)

    def _battery_fields(self):
        """Battery values for the status dict / CSV (empty when unavailable)."""
        b = self.battery_status
        if not b:
            return {}
        return {
            "bat_soc": b.get("soc"),
            "bat_charge": b.get("charge_power"),
            "bat_discharge": b.get("discharge_power"),
            "bat_input_limit": b.get("input_limit"),
            "bat_output_limit": b.get("output_limit"),
        }

    def _control_battery(self, grid_power, car_power):
        """Drive the house battery so the utility meter sits near zero.

        Sign convention: grid_power > 0 means importing. Two corrections are
        needed before a setpoint can be computed:

          * The charger is NOT behind the Fronius meter, so its draw has to be
            added by hand to get the real exchange at the utility meter.
          * The battery IS behind the meter, so its current charge/discharge is
            already contained in grid_power. Backing it out gives the "neutral"
            grid -- what the meter would read if the battery did nothing --
            which is the only stable basis for a new setpoint. Without this the
            loop chases its own tail: charging at 1200W while importing 300W
            would look like a reason to discharge, when in truth there is 900W
            of spare export.

        The car has priority: its measured draw is subtracted first, so the
        battery only ever gets what is left over.
        """
        if not (self.zendure_control and self.zendure and self.battery_status):
            return

        b = self.battery_status
        charge = b.get("charge_power") or 0
        discharge = b.get("discharge_power") or 0
        soc = b.get("soc")

        real_grid = grid_power + car_power
        neutral_grid = real_grid - charge + discharge

        if neutral_grid < -self.zendure_reserve:
            spare = -neutral_grid - self.zendure_reserve
            target_in = int(min(spare, self.zendure_max_charge))
            target_out = 0
        else:
            target_in = 0
            if soc is not None and soc <= self.zendure_min_soc:
                target_out = 0
            else:
                target_out = int(min(max(neutral_grid, 0), self.zendure_max_discharge))

        self._apply_battery_limits(target_in, target_out)

    def _apply_battery_limits(self, input_limit, output_limit):
        """Write limits, but only when they actually moved.

        Same reasoning as for the charger: a setpoint resent every 10s is
        thousands of pointless writes a day.
        """
        last_in, last_out = self._battery_setpoint

        def changed(new, old):
            if old is None:
                return True
            if (new == 0) != (old == 0):     # on/off transitions always matter
                return True
            return abs(new - old) >= self.zendure_deadband

        if changed(input_limit, last_in):
            if self.zendure.set_input_limit(input_limit):
                last_in = input_limit
        if changed(output_limit, last_out):
            if self.zendure.set_output_limit(output_limit):
                last_out = output_limit
        self._battery_setpoint = (last_in, last_out)

    def restore_battery_defaults(self):
        """Hand the battery back a usable state on shutdown.

        If the controller stops while it has parked inputLimit at 0, nothing
        would ever raise it again and the battery would sit idle forever.
        """
        if not (self.zendure_control and self.zendure):
            return
        self.zendure.set_input_limit(self.zendure_max_charge)
        self.zendure.set_output_limit(0)
        logger.info("Zendure limits restored to charge=%sW, discharge=0W",
                    self.zendure_max_charge)

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

    def _choose_phase_and_amps(self, available_watts, current=None):
        """Determine optimal phase mode and amperage for given surplus.

        Args:
            available_watts: surplus we may hand to the charger
            current: the charger's current phase mode (1 or 2), for hysteresis

        Returns (phases, amps) where phases is 1 or 2 (psm value),
        or (None, 0) if surplus is too low.

        Phase switching is hysteretic: stepping UP to 3-phase needs extra
        headroom plus a minimum dwell time since the last switch, stepping
        DOWN only needs the surplus to fall clearly below the threshold
        (that direction is the safe one, so it is never delayed).

        `available_watts` is compensated for the measured shortfall between the
        pilot limit we set and what the car actually draws (see
        _update_power_offset), so a full amp step isn't left unused.
        """
        if available_watts < self.min_1phase:
            return None, 0

        dwell_ok = (time.time() - self._last_phase_switch) >= self._phase_dwell

        if current == 2:
            # Already 3-phase. What minimum 3-phase charging really pulls is
            # below the nominal threshold (the car undershoots the pilot limit),
            # so we can hold it a while longer without importing from the grid.
            sustain = self.min_3phase - self._power_offset * 3
            if available_watts >= (self.min_3phase - self._phase_margin):
                target = 2
            elif available_watts >= sustain and not dwell_ok:
                target = 2      # ride out the dwell, still covered by surplus
            else:
                target = 1
        elif current == 1:
            # Only step up with extra headroom and after the dwell time
            target = 2 if (dwell_ok and
                           available_watts >= self.min_3phase + self._phase_margin) else 1
        else:
            target = 2 if available_watts >= self.min_3phase else 1

        phase_count = 3 if target == 2 else 1
        power_per_amp = self.power_3phase if target == 2 else self.power_1phase
        # Compensate for the shortfall the car actually draws
        usable = available_watts + self._power_offset * phase_count
        amps = int(min(usable / power_per_amp, self.max_amps))

        if amps < self.min_amps:
            if target == 2 and current == 2:
                # Inside the hysteresis band -- hold minimum 3-phase current
                # rather than cycling the contactor for a brief dip.
                amps = self.min_amps
            elif target == 2:
                target = 1
                usable = available_watts + self._power_offset
                amps = int(min(usable / self.power_1phase, self.max_amps))

        if amps < self.min_amps:
            return None, 0
        return target, amps

    def _update_power_offset(self, charger_status, commanded_amps, phases):
        """Learn how far actual charging power falls short of the commanded value.

        The car reliably draws less than the pilot limit we set (measured at
        roughly 350W per phase on this installation), so assuming
        amps * voltage * phases makes the controller under-use the surplus.
        Only learned in steady state (same setpoint as last cycle, car actually
        drawing) so the ramp-up lag doesn't pollute the estimate.
        """
        delivered = charger_status.get("charging_power") or 0
        steady = (charger_status.get("car") == 2 and delivered > 0 and
                  self._last_setpoint == (commanded_amps, phases))
        self._last_setpoint = (commanded_amps, phases)
        if not steady:
            return

        phase_count = 3 if phases == 2 else 1
        commanded = commanded_amps * self.voltage * phase_count
        offset = (commanded - delivered) / phase_count
        offset = max(0.0, min(offset, self._max_power_offset))
        # Exponential moving average -- slow enough to ignore single glitches
        self._power_offset = 0.9 * self._power_offset + 0.1 * offset

    def _apply_charging(self, charger_status, amps, phases, force_on=True):
        """Send setpoints to the charger, but only if they differ from reality.

        Comparing against the status we just read (rather than a cached copy)
        means changes made in the go-e app are picked up automatically, while
        an unchanged setpoint costs no write at all.
        """
        desired_frc = 2 if force_on else 1
        need_phase = phases is not None and charger_status.get("phases") != phases
        need_amps = charger_status.get("amp") != amps
        need_frc = charger_status.get("force_state") != desired_frc

        if not (need_phase or need_amps or need_frc):
            return False

        if need_phase:
            self._last_phase_switch = time.time()
        self.charger.set_charging(
            amps, force_on=force_on, phases=phases if need_phase else None,
        )
        return True

    def _apply_stop(self, charger_status):
        """Stop charging, unless the charger is already stopped."""
        if charger_status.get("force_state") == 1:
            return False
        self.charger.stop_charging()
        return True

    def _force_full_speed(self, charger_status, label):
        """Charge at max amps, 3-phase. Uses frc=2 to restart from any state."""
        self._stop_count = 0

        if charger_status["car"] in (3, 4):
            car_states = {3: "waiting", 4: "complete"}
            logger.info(f"{label}: restarting from {car_states[charger_status['car']]} via frc=2")

        if self._apply_charging(charger_status, self.max_amps, 2, force_on=True):
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

    def _add_history_point(self, status, charger_status=None):
        """Store a data point for the history chart and CSV log."""
        # Surface the charger state on every path so the UI can always show
        # whether a car is plugged in, not just while charging.
        if charger_status:
            status.setdefault("car_state", charger_status.get("car"))
            status.setdefault("charging_power", charger_status.get("charging_power", 0))
            status.setdefault("current_amp", charger_status.get("amp"))
            status.setdefault("current_phases", charger_status.get("phases"))

        point = {
            "time": time.time(),
            "pv_power": status.get("pv_power", 0),
            "surplus": status.get("surplus", 0),
            "charging_power": status.get("charging_power", 0),
        }
        self.history.append(point)
        self._log_to_csv(status, charger_status)

    def _log_to_csv(self, status, charger_status):
        """Append one row to today's CSV log file."""
        today = date.today().isoformat()
        log_path = os.path.join(self._log_dir, f"solar_{today}.csv")
        file_exists = os.path.exists(log_path)
        if file_exists:
            self._migrate_csv_header(log_path)
        try:
            with open(log_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                if not file_exists:
                    writer.writeheader()
                row = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "action": status.get("action", ""),
                    "mode": status.get("mode", self.mode),
                    "pv_power": round(status.get("pv_power", 0)),
                    "load_power": round(status.get("load_power", 0)),
                    "grid_power": round(status.get("grid_power", 0)),
                    "surplus": round(status.get("surplus", 0)),
                    "charging_power": round(status.get("charging_power", 0)),
                    "set_amps": status.get("set_amps", ""),
                    "set_phases": status.get("set_phases", ""),
                    "car_state": charger_status.get("car", "") if charger_status else "",
                    "force_state": charger_status.get("force_state", "") if charger_status else "",
                    "bat_soc": status.get("bat_soc", ""),
                    "bat_charge": status.get("bat_charge", ""),
                    "bat_discharge": status.get("bat_discharge", ""),
                    "bat_in_limit": status.get("bat_input_limit", ""),
                    "bat_out_limit": status.get("bat_output_limit", ""),
                }
                writer.writerow(row)
        except OSError as e:
            logger.warning(f"Could not write log: {e}")

    def _migrate_csv_header(self, log_path):
        """Rewrite today's log once if its header predates the current columns.

        DictWriter writes values in LOG_FIELDS order regardless of what is
        already in the file, so appending new columns to an old file would
        silently misalign every following row. Rewriting once keeps one file
        per day and loses nothing; new columns are simply empty for old rows.
        """
        if log_path in self._migrated_logs:
            return
        self._migrated_logs.add(log_path)
        try:
            with open(log_path, newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
            if header is None or header == LOG_FIELDS:
                return
            with open(log_path, newline="") as f:
                rows = list(csv.DictReader(f))
            tmp = log_path + ".tmp"
            with open(tmp, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_FIELDS,
                                        extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})
            os.replace(tmp, log_path)
            logger.info(f"Migrated log header: {os.path.basename(log_path)} "
                        f"({len(rows)} rows, +{len(LOG_FIELDS) - len(header)} columns)")
        except (OSError, ValueError, csv.Error) as e:
            logger.warning(f"Could not migrate log header, leaving as is: {e}")

    def get_history(self, minutes=10, max_points=300):
        """Return history data points for the last N minutes.

        The deque is snapshotted with list() first: iterating it directly races
        with the control thread's append() (which also pops from the left once
        maxlen is reached) and raises "deque mutated during iteration".

        Points are downsampled to at most max_points -- 24h at a 10s interval is
        8640 points for a chart a few hundred pixels wide, which made /api/status
        a ~650KB response on every poll.
        """
        cutoff = time.time() - minutes * 60
        points = [p for p in list(self.history) if p["time"] >= cutoff]

        if len(points) <= max_points:
            return points

        # Bucket into max_points slots, keeping the peak of each bucket so
        # short surplus spikes stay visible instead of being averaged away.
        step = len(points) / max_points
        out = []
        for i in range(max_points):
            chunk = points[int(i * step):int((i + 1) * step)] or None
            if chunk:
                out.append(max(chunk, key=lambda p: p.get("pv_power", 0)))
        return out


    def update(self):
        """Run one control cycle. Returns a status dict for logging."""
        try:
            return self._update_once()
        finally:
            # Stamped on every path (including errors) so the UI can tell
            # "nothing is happening" from "the controller stopped running".
            self.last_update_ts = time.time()

    def _update_once(self):
        self.daily_stats.check_midnight()

        charger_status = self.charger.get_status()
        if charger_status is None:
            logger.warning("Could not read charger data, skipping cycle")
            self.last_status = {"action": "skip", "reason": "charger_error"}
            return self.last_status

        # Always read Fronius so logs record solar data even without a car
        power_flow = self.fronius.get_power_flow()
        if power_flow is not None:
            pv_power = power_flow["pv_power"]
            load_power = power_flow["load_power"]
            grid_power = power_flow["grid_power"]
            surplus = -grid_power
        else:
            pv_power = load_power = grid_power = surplus = 0

        # House battery. Never fatal: if it cannot be read we simply fall back
        # to the uncorrected surplus, i.e. exactly the behaviour without it.
        self.battery_status = self.zendure.get_status() if self.zendure else None
        usable_surplus = self._corrected_surplus(surplus)

        # Battery control runs on every cycle, whatever the car branch below
        # decides -- the battery still needs regulating when no car is plugged
        # in. Skipped when Fronius is unreadable, since grid_power would be a
        # fabricated zero.
        if power_flow is not None:
            self._control_battery(grid_power,
                                  charger_status.get("charging_power") or 0)

        # No car connected -- log solar data and idle
        if charger_status["car"] == 1:
            self._record_charging(False)
            self.daily_stats.record_session(False)
            self.last_status = {"action": "idle", "reason": "no_car", "mode": self.mode,
                                "pv_power": pv_power, "load_power": load_power,
                                "grid_power": grid_power, "surplus": surplus,
                                **self._battery_fields()}
            self._add_history_point(self.last_status, charger_status)
            return self.last_status

        # Car finished charging -- but don't bail out if we want to restart
        # (frc=2 can wake the car from "complete" state)
        car_complete = charger_status["car"] == 4

        # Charge time estimate
        estimate = self._estimate_charge_time(charger_status)

        # -- Force OFF override --
        if self.mode == MODE_FORCE_OFF:
            self._apply_stop(charger_status)
            self._record_charging(False)
            self.daily_stats.record_session(False)
            logger.info("Force OFF: charging stopped by override")
            self.last_status = {"action": "force_off", "mode": self.mode,
                                "charge_estimate": estimate}
            self._add_history_point(self.last_status, charger_status)
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
            self._add_history_point(self.last_status, charger_status)
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
            self._add_history_point(self.last_status, charger_status)
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
            self._add_history_point(result, charger_status)
            return self.last_status

        # -- Daytime: surplus-based charging with phase switching --
        if power_flow is None:
            logger.warning("Could not read Fronius data, skipping cycle")
            self.last_status = {"action": "skip", "reason": "fronius_error",
                                "mode": self.mode, "car_state": charger_status.get("car")}
            return self.last_status

        available = usable_surplus - self.tolerance

        target_phases, target_amps = self._choose_phase_and_amps(
            available, current=charger_status["phases"]
        )

        status = {
            "mode": self.mode,
            "pv_power": pv_power,
            "load_power": load_power,
            "grid_power": grid_power,
            "surplus": surplus,
            "usable_surplus": usable_surplus,
            "available": available,
            **self._battery_fields(),
            "current_amp": charger_status["amp"],
            "current_phases": charger_status["phases"],
            "charging_power": charger_status["charging_power"],
            "charge_estimate": estimate,
        }

        if target_amps >= self.min_amps:
            # Enough surplus -- charge (frc=2 forces charger on, even from stopped/complete)
            self._stop_count = 0

            phase_power = self.power_3phase if target_phases == 2 else self.power_1phase
            phase_label = "3-phase" if target_phases == 2 else "1-phase"

            if car_complete or charger_status["car"] == 3:
                logger.info(
                    f"Restarting charger from state "
                    f"{'complete' if car_complete else 'waiting'} via frc=2"
                )

            self._update_power_offset(charger_status, target_amps, target_phases)
            self._apply_charging(charger_status, target_amps, target_phases, force_on=True)
            status["action"] = "charging"
            status["power_offset"] = round(self._power_offset)
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
                if self._apply_stop(charger_status):
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

        self._add_history_point(status, charger_status)
        self.last_status = status
        return status
