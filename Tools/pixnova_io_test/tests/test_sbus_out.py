from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_protocol_model
import test_station
from test_io_adapter import method
from protocol_model import TESTS, MODE_NUMBER

ROOT = Path(__file__).resolve().parents[3]


class SbusOutTests(unittest.TestCase):
    def setUp(self): self.case = test_protocol_model.ProtocolTests()

    def test_build_rejects_cached_unaligned_or_missing_ram_vector_tables(self):
        from build_firmware import validate_probe_vectors
        for address in ("24000c00", "20000004", "20010000"):
            with patch("build_firmware.subprocess.check_output", return_value=address + " 00000298 B g_ram_vectors\n"):
                with self.assertRaises(ValueError): validate_probe_vectors("probe.elf")
        with patch("build_firmware.subprocess.check_output", return_value=""):
            with self.assertRaises(ValueError): validate_probe_vectors("probe.elf")
        with patch("build_firmware.subprocess.check_output", return_value="20000000 00000298 B g_ram_vectors\n"):
            self.assertEqual(validate_probe_vectors("probe.elf"), 0x20000000)

    def test_probe_configuration_must_reserve_vectors_and_avoid_lse_startup_wait(self):
        from build_firmware import validate_protocol_config
        required = ("CONFIG_ARCH_RAMVECTORS=y", "CONFIG_ARCH_HIPRI_INTERRUPT=y", "CONFIG_ARCH_IRQPRIO=y",
                    "CONFIG_BOARD_EARLY_INITIALIZE=y", "CONFIG_STM32H7_DTCMEXCLUDE=y", "CONFIG_STM32H7_RTC_LSICLOCK=y")
        config = "\n".join(required)
        validate_protocol_config(config, "probe")
        for missing in ("CONFIG_STM32H7_DTCMEXCLUDE=y", "CONFIG_STM32H7_RTC_LSICLOCK=y"):
            with self.assertRaises(ValueError): validate_protocol_config(config.replace(missing, ""), "probe")
        with self.assertRaises(ValueError): validate_protocol_config(config + "\nCONFIG_STM32H7_RTC_LSECLOCK=y", "probe")

    @unittest.skipUnless(os.name == "posix" and shutil.which("g++"), "POSIX g++ required")
    def test_fast_receiver_has_no_rtos_calls_and_restores_shared_irq_ownership(self):
        adapter = (ROOT/"boards/newbeedrone/pixnova/src/protocol_test_board.cpp").read_text()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("drivers/drv_hrt.h", "px4_platform_common/atomic.h"):
                path = folder/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("#pragma once\n")
            names = ("sbus_out_start", "sbus_out_stop", "sbus_out_irq", "sbus_out_fast_irq", "sbus_out_step", "watchdog")
            (folder/"sbus_fast_under_test.inc").write_text("\n".join(method(adapter, name) for name in names))
            exe = folder/"sbus-fast"
            subprocess.run(["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-I"+str(folder), "-I"+str(ROOT/"src/systemcmds/gpio_test"),
                            str(Path(__file__).with_name("sbus_fast_capture_native_test.cpp")), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True, timeout=10)

    def test_sbus_output_is_distinct_from_sbus_receiver_and_gpio_profile(self):
        spec = TESTS["SBUS_OUT"]
        self.assertEqual(MODE_NUMBER["SBUS_OUT"], 23)
        self.assertEqual((spec["probe_pin"], spec["probe_connector"], spec["probe_connector_pin"]), ("PG5", "P8", 2))
        case = test_station.StationTests(); case.setUp()
        self.assertFalse(any(row["probe_pin"] == "PG5" for row in case.profile["channels"]))
        self.assertNotEqual(TESTS["SBUS"]["path"], spec["path"])

    def test_decoded_frames_and_confirmed_cleanup_are_both_required(self):
        run = self.case.make("SBUS_OUT"); self.case.start(run)
        for n in range(1, 5):
            self.case.sample(run, "probe", n, rx=47*n, tx=47*n, detail=47619)
            self.case.sample(run, "source", n, detail=50000)
        self.assertEqual(run.phase, "STOPPING")
        self.assertNotEqual(run.result.status, "PASS")
        self.case.confirm_stop(run)
        self.assertEqual(run.result.status, "PASS")
        self.assertAlmostEqual(run.result.frequency_hz, 47.619)

    def test_edge_activity_without_valid_decoding_or_expected_rate_cannot_pass(self):
        for changes in ({"rx": 0, "pass": False}, {"errors": 1}, {"detail": 72000}, {"detail": 0}):
            run = self.case.make("SBUS_OUT"); self.case.start(run)
            for n in range(1, 5):
                self.case.sample(run, "probe", n, **({"detail": 47619} | changes))
                self.case.sample(run, "source", n, detail=50000)
            self.assertNotEqual(run.result.status, "PASS")
            self.assertNotEqual(run.phase, "COMPLETE")

    def test_capture_error_location_is_retained_without_turning_failure_into_pass(self):
        run = self.case.make("SBUS_OUT"); self.case.start(run)
        self.case.sample(run, "probe", 1, errors=1, detail=49000, capture=[4, 15, 10, 106, -4])
        self.case.confirm_stop(run)
        self.assertEqual(run.result.status, "FAIL")
        self.assertEqual(run.snapshot()["capture_diagnostics"], [4, 15, 10, 106, -4])
        self.assertIn("UART stop bit", run.result.reason)
        self.assertIn("byte 15", run.result.reason)

    def test_nonadvancing_probe_frames_do_not_count_as_fresh_windows(self):
        run = self.case.make("SBUS_OUT"); self.case.start(run)
        for n in range(1, 6):
            self.case.sample(run, "probe", n, rx=47, detail=47619)
            self.case.sample(run, "source", n, detail=50000)
        self.assertEqual(run.streak, 0)
        self.assertEqual(run.phase, "RUNNING")

    def test_both_roles_need_updated_capability_before_outputs(self):
        case = test_station.StationTests(); case.setUp()
        station = case.make_controller()
        station.capabilities = {"source": {"sbus_out": 1, "session_stop": 1}, "probe": {"session_stop": 1}}
        station.start_sbus_out()
        self.assertEqual(station.commands, [])
        station.capabilities["probe"]["sbus_out"] = 1
        station.start_sbus_out()
        self.assertEqual(station.i2c_run.mode, "SBUS_OUT")
        self.assertEqual(station.i2c_run.roles, {"source", "probe"})

    @unittest.skipUnless(os.name == "posix" and shutil.which("g++"), "POSIX g++ required")
    def test_complete_receiver_windows_reject_bursts_and_accept_continuous_frames(self):
        adapter = (ROOT/"boards/newbeedrone/pixnova/src/protocol_test_board.cpp").read_text()
        waveform = (Path(__file__).with_name("sbus_capture_native_test.cpp")).read_text()
        waveform = waveform[waveform.index("static void waveform("):waveform.index("int main()")]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("drivers/drv_hrt.h", "px4_platform_common/atomic.h"):
                path = folder/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#pragma once\n")
            (folder/"sbus_receiver_under_test.inc").write_text(method(adapter, "sbus_out_step") + method(adapter, "update"))
            (folder/"sbus_waveform_under_test.inc").write_text(waveform)
            exe = folder/"sbus-receiver"
            subprocess.run(["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-I"+str(folder), "-I"+str(ROOT/"src/systemcmds/gpio_test"),
                            str(Path(__file__).with_name("sbus_receiver_native_test.cpp")), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True, check=True, timeout=10)
        for scenario in range(7):
            run = self.case.make("SBUS_OUT"); self.case.start(run)
            for line in result.stdout.splitlines():
                which, window, frames, errors, detail, passed = map(int, line.split())
                if which != scenario: continue
                now = window * (1.5 if scenario in (4, 6) else 1)
                self.case.sample(run, "probe", window, now=now, rx=frames, tx=frames, errors=errors, detail=detail, **{"pass": bool(passed)})
                self.case.sample(run, "source", window, now=now + .1, detail=50000)
                if run.phase == "STOPPING": break
            if scenario in (3, 4):
                self.assertEqual(run.phase, "RUNNING")
                self.assertEqual(run.streak, 0)
                self.assertNotEqual(run.result.status, "PASS")
            else:
                self.case.confirm_stop(run)
                self.assertEqual(run.result.status, "PASS")

    @unittest.skipUnless(os.name == "posix" and shutil.which("g++"), "POSIX g++ required")
    def test_actual_px4_encoder_to_pg5_decoder_jitter_framing_parity_and_faults(self):
        sbus = (ROOT/"src/lib/rc/sbus.cpp").read_text()
        begin, end = sbus.index("\nvoid\nsbus1_output("), sbus.index("\nvoid\nsbus2_output(")
        defines = [line for line in sbus.splitlines() if line.startswith("#define SBUS_") and any(
            name in line.split()[1] for name in ("RANGE_", "TARGET_", "SCALE_"))]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/"sbus_encoder_under_test.inc").write_text("\n".join(defines)+"\n"+sbus[begin:end])
            exe = folder/"sbus-capture"
            subprocess.run(["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-I"+str(folder), "-I"+str(ROOT/"src/systemcmds/gpio_test"),
                            str(Path(__file__).with_name("sbus_capture_native_test.cpp")), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True, timeout=10)
