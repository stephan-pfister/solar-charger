"""Unit tests for the control logic.

Run with:  python3 -m unittest -v
"""

import csv
import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import date, datetime

import controller as C


def make_controller(**overrides):
    cfg = {
        "min_amps": 6, "max_amps": 16, "voltage": 230,
        "grid_tolerance_watts": 200, "update_interval_seconds": 10,
        "log_dir": tempfile.mkdtemp(), "default_mode": C.MODE_SURPLUS,
        "phase_switch_margin_watts": 500, "phase_min_dwell_seconds": 300,
        "max_power_offset_watts": 600,
    }
    cfg.update(overrides)
    return C.SurplusController(cfg, fronius=None, charger=None)


class FakeFronius:
    def __init__(self, pv=0, load=0, grid=0):
        self._pf = {"pv_power": pv, "load_power": load, "grid_power": grid}

    def get_power_flow(self):
        return dict(self._pf)


class FakeChargerDevice:
    """Full charger stand-in for end-to-end update() runs."""

    def __init__(self, car=2, amp=10, power=3400, phases=1, frc=2):
        self.state = {"car": car, "amp": amp, "charging_power": power,
                      "phases": phases, "force_state": frc, "allowed": True,
                      "battery_percent": None, "battery_capacity_wh": None}
        self.calls = []

    def get_status(self):
        return dict(self.state)

    def set_charging(self, amps, force_on=True, phases=None):
        self.calls.append(("set", amps, force_on, phases))

    def stop_charging(self):
        self.calls.append(("stop",))


class ChoosePhaseAndAmps(unittest.TestCase):
    def setUp(self):
        self.c = make_controller()
        self.c._last_phase_switch = 0.0  # dwell long expired

    def test_below_1phase_minimum_does_not_charge(self):
        self.assertEqual(self.c._choose_phase_and_amps(1000), (None, 0))

    def test_1phase_when_surplus_is_moderate(self):
        phases, amps = self.c._choose_phase_and_amps(2300, current=1)
        self.assertEqual(phases, 1)
        self.assertEqual(amps, 10)

    def test_steps_up_to_3phase_only_with_extra_headroom(self):
        # min_3phase is 4140W; margin is 500W
        self.assertEqual(self.c._choose_phase_and_amps(4300, current=1)[0], 1)
        self.assertEqual(self.c._choose_phase_and_amps(4700, current=1)[0], 2)

    def test_step_up_blocked_during_dwell(self):
        self.c._last_phase_switch = time.time()   # just switched
        self.assertEqual(self.c._choose_phase_and_amps(6000, current=1)[0], 1)

    def test_holds_3phase_inside_hysteresis_band(self):
        # Between min_3phase-margin and min_3phase it stays 3-phase at min amps
        phases, amps = self.c._choose_phase_and_amps(3800, current=2)
        self.assertEqual(phases, 2)
        self.assertEqual(amps, self.c.min_amps)

    def test_drops_to_1phase_when_clearly_below(self):
        self.assertEqual(self.c._choose_phase_and_amps(2500, current=2)[0], 1)

    def test_amps_never_exceed_max(self):
        _, amps = self.c._choose_phase_and_amps(50000, current=2)
        self.assertEqual(amps, self.c.max_amps)

    def test_no_flapping_around_the_threshold(self):
        """Surplus oscillating around 4140W must not switch phases repeatedly."""
        self.c._last_phase_switch = 0.0
        current = 1
        switches = 0
        for surplus in [4200, 4000, 4200, 4050, 4180, 3990] * 5:
            phases, _ = self.c._choose_phase_and_amps(surplus, current=current)
            if phases != current:
                switches += 1
                current = phases
        self.assertEqual(switches, 0)


class PowerOffset(unittest.TestCase):
    def test_learns_shortfall_only_in_steady_state(self):
        c = make_controller()
        status = {"car": 2, "charging_power": 16 * 230 - 350}
        # first call only records the setpoint, nothing learned yet
        c._update_power_offset(status, 16, 1)
        self.assertEqual(c._power_offset, 0.0)
        # same setpoint again -> steady, starts learning
        for _ in range(200):
            c._update_power_offset(status, 16, 1)
        self.assertAlmostEqual(c._power_offset, 350, delta=5)

    def test_offset_is_clamped(self):
        c = make_controller(max_power_offset_watts=600)
        status = {"car": 2, "charging_power": 1}  # absurd shortfall
        c._update_power_offset(status, 16, 1)
        for _ in range(500):
            c._update_power_offset(status, 16, 1)
        self.assertLessEqual(c._power_offset, 600)

    def test_not_learned_while_car_is_ramping(self):
        c = make_controller()
        c._update_power_offset({"car": 3, "charging_power": 0}, 16, 1)
        c._update_power_offset({"car": 3, "charging_power": 0}, 16, 1)
        self.assertEqual(c._power_offset, 0.0)


class WriteOnChange(unittest.TestCase):
    class FakeCharger:
        def __init__(self):
            self.calls = []

        def set_charging(self, amps, force_on=True, phases=None):
            self.calls.append(("set", amps, force_on, phases))

        def stop_charging(self):
            self.calls.append(("stop",))

    def setUp(self):
        self.c = make_controller()
        self.charger = self.FakeCharger()
        self.c.charger = self.charger

    def test_no_write_when_setpoint_already_matches(self):
        status = {"amp": 10, "phases": 1, "force_state": 2}
        self.assertFalse(self.c._apply_charging(status, 10, 1))
        self.assertEqual(self.charger.calls, [])

    def test_writes_when_amps_differ(self):
        status = {"amp": 10, "phases": 1, "force_state": 2}
        self.assertTrue(self.c._apply_charging(status, 12, 1))
        self.assertEqual(len(self.charger.calls), 1)

    def test_stop_is_not_resent_when_already_stopped(self):
        self.assertFalse(self.c._apply_stop({"force_state": 1}))
        self.assertEqual(self.charger.calls, [])

    def test_stop_is_sent_when_charging(self):
        self.assertTrue(self.c._apply_stop({"force_state": 2}))
        self.assertEqual(self.charger.calls, [("stop",)])

    def test_phase_switch_records_timestamp(self):
        before = self.c._last_phase_switch
        self.c._apply_charging({"amp": 10, "phases": 1, "force_state": 2}, 10, 2)
        self.assertGreater(self.c._last_phase_switch, before)


class NightWindow(unittest.TestCase):
    def test_wraps_over_midnight(self):
        c = make_controller(night_start_hour=21, night_end_hour=5)
        self.assertTrue(c.night_start > c.night_end)
        for hour, expected in [(22, True), (2, True), (4, True),
                               (5, False), (12, False), (20, False), (21, True)]:
            with self.subTest(hour=hour):
                self.assertEqual(
                    hour >= c.night_start or hour < c.night_end, expected)

    def test_same_day_window(self):
        c = make_controller(night_start_hour=1, night_end_hour=5)
        self.assertFalse(c.night_start > c.night_end)


class History(unittest.TestCase):
    def test_downsamples_to_max_points(self):
        c = make_controller()
        now = time.time()
        for i in range(8640):
            c.history.append({"time": now - 8640 + i, "pv_power": i,
                              "surplus": 0, "charging_power": 0})
        self.assertLessEqual(len(c.get_history(minutes=1440, max_points=300)), 300)

    def test_keeps_all_points_when_below_limit(self):
        c = make_controller()
        now = time.time()
        for i in range(50):
            c.history.append({"time": now - i, "pv_power": i,
                              "surplus": 0, "charging_power": 0})
        self.assertEqual(len(c.get_history(minutes=1440, max_points=300)), 50)

    def test_survives_concurrent_appends(self):
        """Iterating the deque while the control thread appends used to raise
        RuntimeError: deque mutated during iteration."""
        c = make_controller()
        now = time.time()
        for i in range(8640):
            c.history.append({"time": now, "pv_power": 0,
                              "surplus": 0, "charging_power": 0})

        stop = threading.Event()
        errors = []

        def writer():
            while not stop.is_set():
                c.history.append({"time": time.time(), "pv_power": 0,
                                  "surplus": 0, "charging_power": 0})

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            for _ in range(2000):
                try:
                    c.get_history(minutes=1440)
                except RuntimeError as e:
                    errors.append(str(e))
                    break
        finally:
            stop.set()
            t.join(timeout=2)
        self.assertEqual(errors, [])


class DailyStatsPersistence(unittest.TestCase):
    def test_save_is_throttled(self):
        d = C.DailyStats(tempfile.mkdtemp())
        d._save_interval = 60
        d._last_save = time.time()
        before = d._last_save
        d.record(3000, 10, True)
        self.assertEqual(d._last_save, before)  # throttled, no write

    def test_reset_always_writes(self):
        d = C.DailyStats(tempfile.mkdtemp())
        d._last_save = time.time()
        d.reset()
        self.assertAlmostEqual(d._last_save, time.time(), delta=2)

    def test_energy_is_split_by_source(self):
        d = C.DailyStats(tempfile.mkdtemp())
        d.solar_kwh = d.grid_kwh = 0.0
        d.record(3600, 3600, is_solar=True)    # 3.6kW for 1h = 3.6kWh
        d.record(1800, 3600, is_solar=False)
        self.assertAlmostEqual(d.to_dict()["solar_kwh"], 3.6, places=2)
        self.assertAlmostEqual(d.to_dict()["grid_kwh"], 1.8, places=2)

    def test_midnight_resets(self):
        d = C.DailyStats(tempfile.mkdtemp())
        d.solar_kwh = 5.0
        d.date = date(2020, 1, 1)
        d.check_midnight()
        self.assertEqual(d.solar_kwh, 0.0)
        self.assertEqual(d.date, date.today())




class FakeZendure:
    """Stand-in for ZendureClient. status=None simulates an unreachable device."""

    def __init__(self, status=None):
        self.status = status
        self.written = []

    def get_status(self):
        return self.status

    def set_input_limit(self, watts):
        self.written.append(watts)
        return True


def battery(charge=0, discharge=0, soc=50):
    return {"soc": soc, "charge_power": charge, "discharge_power": discharge,
            "input_limit": 600}


class ZendureCorrection(unittest.TestCase):
    """The house battery must never be mistaken for solar surplus."""

    def _c(self, correction, status):
        c = make_controller(zendure_correction=correction)
        c.zendure = FakeZendure(status)
        c.battery_status = c.zendure.get_status()
        return c

    def test_discharge_is_subtracted(self):
        # 3000W apparent surplus, 800W of it is the battery emptying itself
        c = self._c(True, battery(discharge=800))
        self.assertEqual(c._corrected_surplus(3000), 2200)

    def test_charging_is_not_added_back(self):
        """That power is spoken for -- giving it to the car would import grid."""
        c = self._c(True, battery(charge=600))
        self.assertEqual(c._corrected_surplus(3000), 3000)

    def test_correction_is_opt_in(self):
        c = self._c(False, battery(discharge=800))
        self.assertEqual(c._corrected_surplus(3000), 3000)

    def test_unreachable_battery_changes_nothing(self):
        """A dead battery must never block or distort charging."""
        c = self._c(True, None)
        self.assertIsNone(c.battery_status)
        self.assertEqual(c._corrected_surplus(3000), 3000)
        self.assertEqual(c._battery_fields(), {})

    def test_no_battery_configured(self):
        c = make_controller(zendure_correction=True)
        self.assertEqual(c._corrected_surplus(3000), 3000)
        self.assertEqual(c._battery_fields(), {})

    def test_heavy_discharge_stops_the_car(self):
        """Corrected surplus may go negative; the car must not charge then."""
        c = self._c(True, battery(discharge=1500))
        corrected = c._corrected_surplus(1000)
        self.assertEqual(corrected, -500)
        _, amps = c._choose_phase_and_amps(corrected - c.tolerance)
        self.assertEqual(amps, 0)

    def test_battery_fields_exposed_for_ui(self):
        c = self._c(True, battery(charge=599, soc=13))
        self.assertEqual(c._battery_fields()["bat_soc"], 13)
        self.assertEqual(c._battery_fields()["bat_charge"], 599)


class CsvHeaderMigration(unittest.TestCase):
    """Adding columns must not misalign an existing day's log."""

    def test_old_header_is_migrated_in_place(self):
        c = make_controller()
        path = os.path.join(c._log_dir, f"solar_{date.today().isoformat()}.csv")
        old_fields = [f for f in C.LOG_FIELDS if not f.startswith("bat_")]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=old_fields)
            w.writeheader()
            w.writerow({k: "1" for k in old_fields})

        c._log_to_csv({"action": "idle", "bat_soc": 42}, {"car": 1})

        with open(path, newline="") as f:
            header = next(csv.reader(f))
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(header, C.LOG_FIELDS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["timestamp"], "1")   # old row survived
        self.assertEqual(rows[0]["bat_soc"], "")      # new column empty
        self.assertEqual(rows[1]["bat_soc"], "42")    # new row filled

    def test_migration_runs_only_once(self):
        c = make_controller()
        path = os.path.join(c._log_dir, f"solar_{date.today().isoformat()}.csv")
        c._log_to_csv({"action": "idle"}, {"car": 1})
        c._log_to_csv({"action": "idle"}, {"car": 1})
        self.assertIn(path, c._migrated_logs)
        with open(path, newline="") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 2)


class RecordingZendure:
    """Records writes so tests can assert on the setpoints, not the traffic."""

    def __init__(self, soc=50, charge=0, discharge=0, in_limit=1200, out_limit=0):
        self.state = {"soc": soc, "charge_power": charge,
                      "discharge_power": discharge, "solar_power": 0,
                      "home_power": 0, "grid_input_power": charge,
                      "input_limit": in_limit, "output_limit": out_limit,
                      "pack_count": 1, "serial": "TESTSN"}
        self.input_writes = []
        self.output_writes = []
        self.mode_writes = []

    def get_status(self):
        return dict(self.state)

    def set_input_limit(self, w):
        self.input_writes.append(w)
        return True

    def set_output_limit(self, w):
        self.output_writes.append(w)
        return True

    def set_ac_mode(self, m):
        self.mode_writes.append(m)
        return True


class BatteryControlTest(unittest.TestCase):
    """The battery loop, driven the way the real cycle drives it."""

    def _controller(self, zendure, **overrides):
        cfg = {"zendure_control": True, "zendure_max_charge_watts": 1200,
               "zendure_max_discharge_watts": 800, "zendure_min_soc_percent": 10,
               "zendure_reserve_watts": 200, "zendure_deadband_watts": 75,
               "log_dir": self.tmp}
        cfg.update(overrides)
        c = C.SurplusController(cfg, object(), object(), zendure=zendure)
        c.battery_status = zendure.get_status()
        return c

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_charges_from_spare_export(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=-5000, car_power=0)
        # 5000W export minus the 200W reserve, clamped to the 1200W max
        self.assertEqual(z.input_writes, [1200])
        self.assertEqual(z.output_writes, [0])

    def test_own_charging_is_backed_out_before_deciding(self):
        """Importing 300W *while* charging 1200W still means spare export.

        Without backing the battery's own draw out of the meter reading the
        loop would read this as a reason to discharge.
        """
        z = RecordingZendure(charge=1200)
        c = self._controller(z)
        c._control_battery(grid_power=300, car_power=0)
        # neutral grid = 300 - 1200 = -900 export -> 900 - 200 reserve = 700
        self.assertEqual(z.input_writes, [700])
        self.assertEqual(z.output_writes, [0])

    def test_car_has_priority_over_battery(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        # 5000W export on the Fronius, but the (unmetered) car already takes 4500
        c._control_battery(grid_power=-5000, car_power=4500)
        self.assertEqual(z.input_writes, [300])

    def test_car_using_everything_pauses_the_battery(self):
        z = RecordingZendure(charge=600)
        c = self._controller(z)
        c._control_battery(grid_power=-600, car_power=1200)
        self.assertEqual(z.input_writes, [0])

    def test_discharges_to_cover_house_at_night(self):
        z = RecordingZendure(charge=0, discharge=0, in_limit=1200)
        c = self._controller(z)
        c._control_battery(grid_power=700, car_power=0)
        self.assertEqual(z.input_writes, [0])
        self.assertEqual(z.output_writes, [700])

    def test_no_grid_charging_at_night(self):
        """The failure mode that HEMS-off created: charging with no sun."""
        z = RecordingZendure(charge=1200, in_limit=1200)
        c = self._controller(z)
        # Importing 1400W of which 1200W is the battery itself -> 200W real load
        c._control_battery(grid_power=1400, car_power=0)
        self.assertEqual(z.input_writes, [0])
        self.assertEqual(z.output_writes, [200])

    def test_discharge_stops_at_min_soc(self):
        z = RecordingZendure(soc=10, charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=700, car_power=0)
        self.assertEqual(z.output_writes, [0])

    def test_discharge_is_capped(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=3000, car_power=0)
        self.assertEqual(z.output_writes, [800])

    def test_small_changes_do_not_rewrite(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=-1000, car_power=0)   # -> 800
        z.state["charge_power"] = 800
        c.battery_status = z.get_status()
        c._control_battery(grid_power=-230, car_power=0)    # -> 830, +30
        self.assertEqual(z.input_writes, [800], "30W drift must not rewrite")

    def test_crossing_zero_always_writes(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=-1000, car_power=0)   # -> 800
        z.state["charge_power"] = 800
        c.battery_status = z.get_status()
        c._control_battery(grid_power=800, car_power=0)     # neutral 0 -> pause
        self.assertEqual(z.input_writes, [800, 0])

    def test_disabled_flag_writes_nothing(self):
        z = RecordingZendure(charge=1200)
        c = self._controller(z, zendure_control=False)
        c._control_battery(grid_power=-5000, car_power=0)
        self.assertEqual(z.input_writes, [])
        self.assertEqual(z.output_writes, [])

    def test_unreachable_battery_writes_nothing(self):
        z = RecordingZendure()
        c = self._controller(z)
        c.battery_status = None
        c._control_battery(grid_power=-5000, car_power=0)
        self.assertEqual(z.input_writes, [])

    def test_shutdown_restores_a_usable_state(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=800, car_power=0)     # parks input at 0
        c.restore_battery_defaults()
        self.assertEqual(z.input_writes[-1], 1200,
                         "a stuck inputLimit=0 would never charge again")


class BatteryAcModeTest(unittest.TestCase):
    """acMode gates the direction; a discharge limit alone does nothing.

    Measured on the real device: with acMode=1 an outputLimit of 300W was
    accepted and echoed back, but packInputPower stayed at 0. Only acMode=2
    actually moved power.
    """

    _controller = BatteryControlTest._controller

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_charging_selects_charge_mode(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=-5000, car_power=0)
        self.assertEqual(z.mode_writes, [1])

    def test_discharging_selects_discharge_mode(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=700, car_power=0)
        self.assertEqual(z.mode_writes, [2])

    def test_mode_is_not_rewritten_while_unchanged(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=-5000, car_power=0)
        z.state["charge_power"] = 1200
        c.battery_status = z.get_status()
        c._control_battery(grid_power=-5000, car_power=0)
        self.assertEqual(z.mode_writes, [1], "mode must be written once, not per cycle")

    def test_idle_keeps_the_current_mode(self):
        """Neither side wants power: do not flap the direction."""
        z = RecordingZendure(soc=5, charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=700, car_power=0)   # below min SoC -> no discharge
        self.assertEqual(z.mode_writes, [], "idle battery must not switch mode")

    def test_switching_from_charge_to_discharge(self):
        z = RecordingZendure(charge=1200)
        c = self._controller(z)
        c._control_battery(grid_power=-5000, car_power=0)     # charge
        z.state["charge_power"] = 1200
        c.battery_status = z.get_status()
        c._control_battery(grid_power=1400, car_power=0)      # sun gone
        self.assertEqual(z.mode_writes, [1, 2])
        self.assertEqual(z.input_writes[-1], 0)

    def test_shutdown_returns_the_device_to_charge_mode(self):
        z = RecordingZendure(charge=0)
        c = self._controller(z)
        c._control_battery(grid_power=700, car_power=0)   # leaves it in discharge
        c.restore_battery_defaults()
        self.assertEqual(z.mode_writes[-1], 1)
        self.assertEqual(z.input_writes[-1], 1200)


class BatteryHistorySeries(unittest.TestCase):
    """bat_power on history points: signed, and absent when unreadable."""

    def _c(self, status):
        c = make_controller()
        c.zendure = FakeZendure(status)
        c.battery_status = c.zendure.get_status()
        return c

    def test_charging_is_positive(self):
        c = self._c(battery(charge=1200))
        c._add_history_point({"pv_power": 5000, "surplus": 3000}, None)
        self.assertEqual(c.history[-1]["bat_power"], 1200)

    def test_discharging_is_negative(self):
        c = self._c(battery(discharge=800))
        c._add_history_point({"pv_power": 0, "surplus": -500}, None)
        self.assertEqual(c.history[-1]["bat_power"], -800)

    def test_unreadable_battery_leaves_key_out(self):
        """A missing key becomes a gap in the chart, not a fake zero."""
        c = self._c(None)
        c._add_history_point({"pv_power": 0, "surplus": 0}, None)
        self.assertNotIn("bat_power", c.history[-1])

    def test_survives_branches_without_battery_fields(self):
        """force_off builds a status dict with no bat_* keys; the point still
        carries the battery, because it is read from battery_status."""
        c = self._c(battery(discharge=298))
        c._add_history_point({"action": "force_off", "mode": "force_off"}, None)
        self.assertEqual(c.history[-1]["bat_power"], -298)

    def test_downsampling_keeps_the_field(self):
        c = self._c(battery(charge=600))
        for _ in range(900):
            c._add_history_point({"pv_power": 1000, "surplus": 500}, None)
        pts = c.get_history(minutes=60, max_points=100)
        self.assertLessEqual(len(pts), 100)
        self.assertTrue(all("bat_power" in p for p in pts))
        self.assertEqual(pts[0]["bat_power"], 600)


class SessionCounting(unittest.TestCase):
    """One plug-in is one session, however cloudy the day is."""

    def _stats(self):
        return C.DailyStats(tempfile.mkdtemp())

    def test_single_run_counts_once(self):
        st = self._stats()
        for i in range(50):
            st.record_session(True, now=1000 + i * 10)
        self.assertEqual(st.sessions, 1)

    def test_short_dip_does_not_split(self):
        st = self._stats()
        st.record_session(True, now=1000)
        st.record_session(False, now=1010)
        st.record_session(True, now=1020)
        self.assertEqual(st.sessions, 1)

    def test_long_pause_while_plugged_in_is_still_one_session(self):
        """Hours of cloud with the cable in is one visit, not two."""
        st = self._stats()
        st.record_session(True, now=1000)
        for t in range(1010, 1010 + 4 * 3600, 10):
            st.record_session(False, now=t)
        st.record_session(True, now=1010 + 4 * 3600)
        self.assertEqual(st.sessions, 1)

    def test_unplug_and_replug_is_two(self):
        st = self._stats()
        st.record_session(True, now=1000)
        for t in range(1010, 1200, 10):
            st.record_session(False, car_connected=False, now=t)
        st.record_session(True, now=1300)
        self.assertEqual(st.sessions, 2)

    def test_brief_car_glitch_does_not_double_count(self):
        """One cycle of car=1 is a charger blip, not a new visit."""
        st = self._stats()
        st.record_session(True, now=1000)
        st.record_session(False, car_connected=False, now=1010)
        st.record_session(True, now=1020)
        self.assertEqual(st.sessions, 1)

    def test_restart_does_not_resume_a_stale_session(self):
        d = tempfile.mkdtemp()
        st = C.DailyStats(d)
        st.record_session(True, now=1000)
        st2 = C.DailyStats(d)
        self.assertEqual(st2.sessions, 1)
        st2.record_session(True, now=2000)
        self.assertEqual(st2.sessions, 2)


class StatusAlwaysCarriesReadings(unittest.TestCase):
    """No mode may drop the readings the UI renders.

    Force ON/OFF and night mode used to build their status dict from scratch,
    so switching mode made PV, load, grid, surplus and the battery disappear
    from the status card.
    """

    READINGS = ["pv_power", "load_power", "grid_power", "surplus",
                "usable_surplus", "bat_soc", "bat_charge", "bat_discharge"]

    def _run(self, mode, car=2, hour=12):
        fronius = FakeFronius(pv=6000, load=1500, grid=-4500)
        charger = FakeChargerDevice(car=car, amp=10, power=3400)
        c = C.SurplusController(
            {"log_dir": tempfile.mkdtemp(), "default_mode": mode,
             "zendure_correction": True, "zendure_control": False},
            fronius, charger, zendure=FakeZendure(battery(discharge=600, soc=44)))
        with unittest.mock.patch.object(C, "datetime") as dt:
            dt.now.return_value = datetime(2026, 8, 19, hour, 0)
            return c.update()

    def _assert_complete(self, status, mode):
        for key in self.READINGS:
            self.assertIn(key, status, f"{mode}: {key} fehlt im Status")

    def test_surplus_mode(self):
        self._assert_complete(self._run(C.MODE_SURPLUS), "surplus")

    def test_force_on(self):
        self._assert_complete(self._run(C.MODE_FORCE_ON), "force_on")

    def test_force_off(self):
        self._assert_complete(self._run(C.MODE_FORCE_OFF), "force_off")

    def test_night_mode(self):
        self._assert_complete(self._run(C.MODE_AUTO, hour=23), "night")

    def test_no_car(self):
        self._assert_complete(self._run(C.MODE_SURPLUS, car=1), "idle")

    def test_usable_surplus_subtracts_discharge(self):
        """The UI shows this instead of the raw surplus, so it must be right."""
        st = self._run(C.MODE_SURPLUS)
        self.assertEqual(st["surplus"], 4500)
        self.assertEqual(st["usable_surplus"], 3900)   # 4500 - 600 entladen


if __name__ == "__main__":
    unittest.main()
