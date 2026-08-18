"""Unit tests for the control logic.

Run with:  python3 -m unittest -v
"""

import tempfile
import threading
import time
import unittest
from datetime import date

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


if __name__ == "__main__":
    unittest.main()
