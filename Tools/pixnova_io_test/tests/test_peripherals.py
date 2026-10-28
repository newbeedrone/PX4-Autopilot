from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peripherals import Peripherals, EXPECTED, SENSORS, STORAGE, DEVICES, display_value


class PeripheralTests(unittest.TestCase):
    def populate(self, model, role="source", bad=None):
        for name, expected in EXPECTED[role].items():
            model.power_result(role, {"test": name, "pass": name != bad, "detail": 0 if name == bad else expected})
        model.finish_power(role, {"pass": bad is None, "count": len(EXPECTED[role]), "checked_us": 100, "uptime_us": 200})

    def test_all_three_imus_and_other_sensors_are_required(self):
        model = Peripherals()
        for name, expected in EXPECTED["source"].items():
            if name != "icm45686_spi3":
                model.power_result("source", {"test": name, "pass": True, "detail": expected})
        with self.assertRaises(ValueError):
            model.finish_power("source", {"pass": True, "count": len(EXPECTED["source"]), "checked_us": 100, "uptime_us": 200})
        self.assertFalse(model.passed(("source",)))

    def test_complete_power_on_and_current_checks_remain_separate(self):
        model = Peripherals()
        self.populate(model, bad="icm45686_spi2")
        model.begin_current()
        for name, spec in DEVICES.items():
            self.assertTrue(model.current_result({"test": name, "pass": True, "detail": spec["id"]}))
        self.assertTrue(model.current_passed)
        self.assertFalse(model.passed(("source",)))
        self.assertEqual(model.power["source"]["icm45686_spi2"]["status"], "FAIL")
        self.assertEqual(model.latest["icm45686_spi2"]["status"], "PASS")

    def test_wrong_id_cannot_be_reported_as_a_pass(self):
        model = Peripherals()
        model.power_result("source", {"test": "icm45686_spi2", "pass": True, "detail": 0xE8})
        self.assertEqual(model.latest["icm45686_spi2"]["status"], "FAIL")

    def test_duplicate_unknown_and_invalid_results_are_rejected(self):
        model = Peripherals()
        frame = {"test": "icm45686_spi1", "pass": True, "detail": 0xE9}
        model.power_result("source", frame)
        for changed in [frame, dict(frame, test="unknown"), dict(frame, test=[]), dict(frame, detail=True)]:
            with self.assertRaises(ValueError):
                model.power_result("source", changed)

    def test_summary_timestamp_and_count_are_checked(self):
        for change in [{"count": True}, {"count": 5}, {"checked_us": 201}, {"uptime_us": -1}]:
            model = Peripherals()
            for name, expected in EXPECTED["source"].items():
                model.power_result("source", {"test": name, "pass": True, "detail": expected})
            with self.assertRaises(ValueError):
                model.finish_power("source", {"pass": True, "count": len(EXPECTED["source"]), "checked_us": 100, "uptime_us": 200, **change})

    def test_snapshot_is_independent_and_reset_removes_previous_board(self):
        model = Peripherals()
        self.populate(model)
        snapshot = model.snapshot()
        model.reset()
        self.assertTrue(snapshot["onboard_passed"])
        self.assertFalse(model.passed(("source",)))
        self.assertEqual(model.latest["icm45686_spi1"]["status"], "WAITING")

    def test_storage_is_required_on_flight_controller_only(self):
        model = Peripherals()
        for name, expected in EXPECTED["source"].items():
            if name != "sdcard":
                model.power_result("source", {"test": name, "pass": True, "detail": expected})
        with self.assertRaises(ValueError):
            model.finish_power("source", {"pass": True, "count": len(EXPECTED["source"]), "checked_us": 1, "uptime_us": 2})
        with self.assertRaises(ValueError):
            Peripherals().power_result("probe", {"test": "sdcard", "pass": True, "detail": 16384})

    def test_storage_read_failures_and_capacity_are_not_chip_ids(self):
        for name, spec in STORAGE.items():
            _, result = Peripherals.result("source", {"test": name, "pass": False, "detail": spec["id"]})
            self.assertFalse(result["pass"])
        for name in ("eeprom_base_i2c3", "eeprom_imu_i2c4"):
            _, result = Peripherals.result("source", {"test": name, "pass": True, "detail": 16})
            self.assertFalse(result["pass"])
        _, result = Peripherals.result("source", {"test": "sdcard", "pass": True, "detail": 0})
        self.assertFalse(result["pass"])
        _, result = Peripherals.result("source", {"test": "sdcard", "pass": True, "detail": 16384})
        self.assertTrue(result["pass"])
        self.assertEqual(display_value(STORAGE["fram_spi5"], 32768), "32 KiB")

    def test_pretest_requires_both_sensors_and_storage(self):
        model = Peripherals()
        model.begin_current()
        for name, spec in SENSORS.items():
            model.current_result({"test": name, "pass": True, "detail": spec["id"]})
        self.assertFalse(model.current_passed)
        for name, spec in STORAGE.items():
            model.current_result({"test": name, "pass": True, "detail": spec.get("demo", spec["id"])})
        self.assertTrue(model.current_passed)
