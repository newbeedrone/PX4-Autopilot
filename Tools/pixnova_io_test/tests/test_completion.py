import base64
import copy
import csv
import json
import queue
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from completion import CompletionPanel, FlightImage, CompletionWorker, FirmwareUpdateWorker, PreparationWorker, expected_serial, program_verified, save_csv
from workflow import GpioSequence, GpioStop
from model import Assessment
from protocol_model import ProtocolRun
from generate_profile import load_profile
import test_station


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        raw = struct.pack("<II", 0x24010000, 0x08020009) + b"normal flight application" * 10
        self.package = {"magic": "PX4FWv1", "board_id": 5900, "image_size": len(raw), "image_maxsize": 1966080,
                        "image": base64.b64encode(zlib.compress(raw)).decode()}
        self.path = self.folder/"normal.px4"
        self.path.write_text(json.dumps(self.package))
        self.report = {"utc": "2026-11-17T00:00:00Z", "demo": False, "profile_sha256": "a"*64,
                       "devices": {"source": {"device_id": "000600000000333738393233510f00490042"}},
                       "channels": [{"id": "PPM_SIGNAL", "group": "PPM", "status": "FAIL", "reason": "No signal"}],
                       "connector_tests": {"CAN": {"status": "PASS", "outputs_stopped": True}},
                       "peripherals": {"latest": {"bmm150": {"status": "FAIL", "detail": 0}}}, "all_passed": False}

    def test_image_validates_board_layout_and_rejects_test_or_bootloader_images(self):
        image = FlightImage(self.path)
        self.assertEqual(image.firmware.board_id, 5900)
        self.assertEqual(len(image.sha256), 64)
        for changes in ({"board_id": 9}, {"image_size": 20}, {"image_maxsize": 0}, {"magic": "other"}):
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps(self.package | changes))
                with self.assertRaises(Exception): FlightImage(self.path)
        for payload in (struct.pack("<II", 0x24010000, 0x08000009) + b"bootloader"*8,
                        struct.pack("<II", 0x24010000, 0x08020009) + b'"independent_external_checks":1'):
            package = self.package | {"image_size": len(payload), "image": base64.b64encode(zlib.compress(payload)).decode()}
            self.path.write_text(json.dumps(package))
            with self.assertRaises(ValueError): FlightImage(self.path)

    def test_gpio_guid_matches_bootloader_uid_word_order(self):
        self.assertEqual(expected_serial(self.report["devices"]["source"]["device_id"]), "004900423233510f33373839")
        with self.assertRaises(ValueError): expected_serial("unknown")

    def test_existing_nuttx_idle_stack_vectors_are_accepted_without_aapcs_alignment(self):
        for stack in (0x2401BD2E, 0x24010004, 0x20010004):
            payload = struct.pack("<II", stack, 0x08020009) + b"normal flight application" * 10
            package = self.package | {"image_size": len(payload), "image": base64.b64encode(zlib.compress(payload)).decode()}
            self.path.write_text(json.dumps(package))
            self.assertEqual(FlightImage(self.path).firmware.board_id, 5900)
        payload = struct.pack("<II", 0x08020000, 0x08020009) + b"invalid stack" * 10
        package = self.package | {"image_size": len(payload), "image": base64.b64encode(zlib.compress(payload)).decode()}
        self.path.write_text(json.dumps(package))
        with self.assertRaises(ValueError): FlightImage(self.path)

    def test_csv_contains_signal_protocol_onboard_failures_and_unique_names(self):
        first = save_csv(self.report, self.folder)
        second = save_csv(self.report, self.folder)
        self.assertNotEqual(first, second)
        with first.open(encoding="utf-8-sig", newline="") as stream: rows = list(csv.DictReader(stream))
        self.assertEqual({r["record_type"] for r in rows}, {"signal", "protocol", "onboard", "summary"})
        self.assertEqual(next(r for r in rows if r.get("signal") == "PPM_SIGNAL")["reason"], "No signal")

    def test_csv_retains_power_on_and_startup_failure_even_if_latest_check_passes(self):
        report = copy.deepcopy(self.report)
        report["peripherals"]["latest"]["bmm150"] = {"status": "PASS", "pass": True, "detail": 50}
        report["peripherals"]["power_on"] = {"source": {"bmm150": {"pass": False, "detail": 0}}}
        report["startup_check_details"] = [{"role": "source", "test": "bmm150", "pass": False, "detail": 0}]
        path = save_csv(report, self.folder)
        with path.open(encoding="utf-8-sig", newline="") as stream: rows = list(csv.DictReader(stream))
        for kind in ("power_on", "startup"):
            self.assertEqual(next(row for row in rows if row["record_type"] == kind)["status"], "FAIL")
        self.assertEqual(next(row for row in rows if row["record_type"] == "onboard")["status"], "PASS")

    def test_csv_separates_firmware_pass_from_unperformed_or_failed_measurements(self):
        report = copy.deepcopy(self.report)
        report["manual_usb_flash_test"] = True
        report["usb_flash_test"] = {"success": True, "application": {"git_prefix": "b5cf3d07e1"}}
        report["post_test"] = {"status": "COMPLETE", "restoration": {"status": "PASS", "crc_verified": True, "path": "normal.px4"}}
        path = save_csv(report, self.folder)
        with path.open(encoding="utf-8-sig", newline="") as stream: rows = list(csv.DictReader(stream))
        flashed = next(row for row in rows if row["record_type"] == "firmware")
        self.assertEqual(flashed["status"], "PASS")
        self.assertEqual(flashed["crc_verified"], "True")
        self.assertEqual(flashed["manual_usb_flash_test"], "True")
        self.assertIn("b5cf3d07e1", flashed["usb_flash_test"])
        self.assertEqual(next(row for row in rows if row["record_type"] == "summary")["status"], "FAIL")

    def test_save_failure_never_requests_usb_restore(self):
        worker = CompletionWorker(self.report, self.folder, FlightImage(self.path), "COM9", "COM143")
        with patch("completion.save_csv", side_effect=OSError("Disk full")), patch("completion.restore_usb") as restore:
            worker.run()
        restore.assert_not_called()
        self.assertEqual(worker.report["post_test"]["status"], "FAILED")

    def test_complete_csv_exists_before_usb_restore_even_for_a_failed_board(self):
        def restore(*_):
            paths = list(self.folder.glob("*.csv"))
            self.assertEqual(len(paths), 1)
            self.assertIn("No signal", paths[0].read_text(encoding="utf-8-sig"))
            return {"status": "PASS", "crc_verified": True}
        worker = CompletionWorker(self.report, self.folder, FlightImage(self.path), "COM9", "COM143")
        with patch("completion.restore_usb", side_effect=restore) as action: worker.run()
        action.assert_called_once()
        self.assertEqual(worker.report["post_test"]["status"], "COMPLETE")
        self.assertFalse(worker.report["all_passed"])

    def test_flash_failure_preserves_measurements_and_is_recorded_in_csv(self):
        worker = CompletionWorker(self.report, self.folder, FlightImage(self.path), "COM9", "COM143")
        with patch("completion.restore_usb", side_effect=RuntimeError("CRC mismatch")): worker.run()
        self.assertEqual(worker.report["post_test"]["status"], "FAILED")
        contents = next(self.folder.glob("*.csv")).read_text(encoding="utf-8-sig")
        self.assertIn("No signal", contents)
        self.assertIn("CRC mismatch", contents)

    def test_identity_size_crc_and_reboot_gate_completion(self):
        image = FlightImage(self.path)
        wanted = expected_serial(self.report["devices"]["source"]["device_id"])
        def board():
            result = Mock(board_type=5900, sn=bytes.fromhex(wanted), fw_maxsize=1966080)
            result.reboot.return_value = True
            return result
        for change in ({"board_type": 1}, {"sn": b"wrong device"}, {"fw_maxsize": 4}):
            probe = board()
            for key, value in change.items(): setattr(probe, key, value)
            with self.assertRaises(ValueError): program_verified(probe, image, wanted, lambda _: None)
            probe.erase.assert_not_called()
        probe = board(); probe.verify.side_effect = RuntimeError("CRC mismatch")
        with self.assertRaises(RuntimeError): program_verified(probe, image, wanted, lambda _: None)
        probe.reboot.assert_not_called()
        probe = board(); probe.reboot.return_value = False
        with self.assertRaises(RuntimeError): program_verified(probe, image, wanted, lambda _: None)
        probe = board()
        result = program_verified(probe, image, wanted, lambda _: None)
        self.assertTrue(result["crc_verified"])
        self.assertEqual([c[0] for c in probe.method_calls], ["erase", "program", "verify", "reboot"])

    def test_demo_saves_csv_without_accessing_usb(self):
        worker = CompletionWorker(self.report | {"demo": True}, self.folder, None, "", "", demo=True)
        with patch("completion.restore_usb") as restore: worker.run()
        restore.assert_not_called()
        self.assertEqual(worker.report["post_test"]["restoration"]["status"], "DEMO")

    def test_no_firmware_selection_prepares_and_saves_measurement_only_csv(self):
        panel = CompletionPanel.__new__(CompletionPanel)
        panel.directory, panel.path = Mock(), Mock()
        panel.status = Mock()
        panel.directory.get.return_value = str(self.folder)
        panel.path.get.return_value = ""
        image, directory = panel.prepare(False)
        self.assertIsNone(image)
        worker = CompletionWorker(self.report | {"all_passed": True}, directory, image, "COM9", "COM143")
        with patch("completion.restore_usb") as restore:
            worker.run()
        restore.assert_not_called()
        self.assertEqual(worker.report["post_test"]["restoration"]["status"], "SKIPPED")
        with Path(worker.report["post_test"]["csv"]).open(encoding="utf-8-sig", newline="") as stream:
            summary = next(row for row in csv.DictReader(stream) if row["record_type"] == "summary")
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["finalization_status"], "PASS")

    def test_independent_update_reuses_usb_restore_without_starting_tests_or_saving_csv(self):
        image = FlightImage(self.path)
        source_id = self.report["devices"]["source"]["device_id"]
        worker = FirmwareUpdateWorker(image, "COM9", "COM143", source_id)
        with patch("completion.restore_usb") as restore, patch("completion.save_csv") as save:
            worker.run()
        self.assertEqual(restore.call_args.args[:4], (image, "COM9", "COM143", source_id))
        self.assertTrue(worker.events.get_nowait()["success"])
        save.assert_not_called()

    def test_final_csv_update_failure_retains_first_csv_and_restoration_evidence(self):
        worker = CompletionWorker(self.report, self.folder, FlightImage(self.path), "COM9", "COM143")
        calls = 0
        def save(*args):
            nonlocal calls
            calls += 1
            if calls > 1: raise OSError("Final CSV update failed")
            return save_csv(*args)
        with patch("completion.save_csv", side_effect=save), patch("completion.restore_usb", return_value={"status": "PASS", "crc_verified": True}):
            worker.run()
        self.assertEqual(worker.report["post_test"]["status"], "FAILED")
        self.assertTrue(worker.report["post_test"]["restoration"]["crc_verified"])
        self.assertIn("No signal", next(self.folder.glob("*.csv")).read_text(encoding="utf-8-sig"))

    def test_all_measurements_passed_csv_remains_pending_until_usb_restore_finishes(self):
        report = copy.deepcopy(self.report)
        report["all_passed"] = True
        def restore(*_):
            with next(self.folder.glob("*.csv")).open(encoding="utf-8-sig", newline="") as stream:
                summary = next(r for r in csv.DictReader(stream) if r["record_type"] == "summary")
            self.assertEqual((summary["status"], summary["measurement_status"], summary["workflow_status"]), ("PENDING", "PASS", "PENDING"))
            return {"status": "PASS", "crc_verified": True}
        worker = CompletionWorker(report, self.folder, FlightImage(self.path), "COM142", "COM9")
        with patch("completion.restore_usb", side_effect=restore): worker.run()
        with next(self.folder.glob("*.csv")).open(encoding="utf-8-sig", newline="") as stream:
            summary = next(r for r in csv.DictReader(stream) if r["record_type"] == "summary")
        self.assertEqual(summary["status"], "PASS")
        self.assertTrue(worker.report["workflow_passed"])

    def test_failed_restore_cannot_leave_an_overall_pass_csv(self):
        report = copy.deepcopy(self.report)
        report["all_passed"] = True
        worker = CompletionWorker(report, self.folder, FlightImage(self.path), "COM142", "COM9")
        with patch("completion.restore_usb", side_effect=RuntimeError("CRC mismatch")): worker.run()
        with next(self.folder.glob("*.csv")).open(encoding="utf-8-sig", newline="") as stream:
            summary = next(r for r in csv.DictReader(stream) if r["record_type"] == "summary")
        self.assertEqual((summary["status"], summary["measurement_status"], summary["workflow_status"]), ("FAIL", "PASS", "FAIL"))
        self.assertFalse(worker.report["workflow_passed"])


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.case = test_station.StationTests(); self.case.setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def test_each_gpio_group_is_measured_and_fmu_is_last_without_expiring_completed_groups(self):
        assessment = Assessment(self.case.profile); assessment.reset("a"*32)
        run = GpioSequence(self.case.profile)
        self.assertEqual(run.groups[-1], "FMU PWM")
        now = 0
        while not run.done:
            indices = list(run.active_indices)
            run.tick(assessment, now)
            for sequence in range(1, 4):
                now += 1
                for index in indices: assessment.accept(self.case.frame(sequence, channel=index), now)
                assessment.expire(now, indices=indices)
            run.tick(assessment, now)
        self.assertTrue(assessment.all_passed)
        self.assertEqual(set(run.completed), set(run.groups))
        self.assertIn("PPM", run.completed)

    def test_missing_group_fails_and_continues_without_claiming_pass(self):
        assessment = Assessment(self.case.profile); assessment.reset("a"*32)
        run = GpioSequence(self.case.profile)
        run.tick(assessment, 0); group = run.group
        run.tick(assessment, 6)
        self.assertNotEqual(group, run.group)
        self.assertTrue(all(row["status"] == "FAIL" for row in run.completed[group]))

    def test_partial_gpio_scope_does_not_include_unconnected_fmu_or_ppm(self):
        selected = [group for group in GpioSequence(self.case.profile).groups if group not in ("FMU PWM", "PPM")]
        run = GpioSequence(self.case.profile, selected)
        self.assertEqual(set(run.groups), set(selected))
        self.assertNotIn("FMU PWM", run.groups)
        self.assertNotIn("PPM", run.groups)
        for bad in ([], ["missing"], ["GPS1", "GPS1"]):
            with self.assertRaises(ValueError): GpioSequence(self.case.profile, bad)

    def test_partial_station_test_cannot_trigger_normal_firmware_restoration(self):
        station = self.case.make_controller()
        station.finish_plan = ("old normal image", "old CSV folder")
        station.start_gpio_groups(["GPS1", "SPI6"])
        self.assertIsNone(station.finish_plan)
        self.assertEqual(station.suite_stage, "gpio_only")
        self.assertFalse(station.extended_suite)
        for index, row in enumerate(station.profile["channels"]):
            if row["group"] not in ("GPS1", "SPI6"):
                self.assertEqual(station.assessment.results[index].status, "NOT RUN")

    def test_partial_scope_cannot_replace_a_complete_run_while_startup_is_active(self):
        station = self.case.make_controller()
        station.phase, station.auto_start = "CHECKING", True
        original = ("selected normal image", "selected CSV folder")
        station.finish_plan = original
        station.start_gpio_groups(["GPS1"])
        self.assertIs(station.finish_plan, original)
        self.assertIsNone(station.gpio_sequence)
        self.assertEqual(station.commands, [])

    def test_firmware_preparation_runs_off_the_calling_thread(self):
        entered, release = threading.Event(), threading.Event()
        calling = threading.get_ident()
        def slow(*_):
            self.assertNotEqual(threading.get_ident(), calling)
            entered.set()
            if not release.wait(2): raise RuntimeError("Test release timed out")
            return None, self.folder
        worker = PreparationWorker("normal.px4", str(self.folder), False)
        with patch("completion.prepare_plan", side_effect=slow):
            try:
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(worker.events.empty())
            finally:
                release.set()
            self.assertEqual(worker.events.get(timeout=2)["plan"], (None, self.folder))

    def preparing_station(self):
        station = self.case.make_controller()
        station.demo = True
        panel = Mock()
        panel.selection.return_value = ("", str(self.folder), True)
        station.view = Mock(completion=panel, network_panel=None)
        worker = Mock(events=queue.Queue())
        with patch("app.PreparationWorker", return_value=worker): station.start_full_test()
        self.assertEqual(station.phase, "PREPARING")
        self.assertFalse(station.commands)
        return station, worker

    def test_preparation_success_starts_only_after_main_thread_receives_result(self):
        station, worker = self.preparing_station()
        station.root = Mock()
        station.start_full_test()  # Double click must not create a second run.
        station.poll_preparation()
        self.assertFalse(station.commands)
        plan = None, self.folder
        worker.events.put({"plan": plan})
        station.poll_preparation()
        self.assertEqual(station.phase, "CHECKING")
        self.assertEqual(station.finish_plan, plan)
        self.assertTrue(station.commands)

    def test_blank_firmware_allows_real_test_even_without_restore_capability(self):
        station = self.case.make_controller()
        station.capabilities = {role: {"i2c_simulators": 2, "adc_levels": 2, "io_pwm": 1, "sbus_out": 1, "session_stop": 1}
                                for role in ("source", "probe")}
        station.view = Mock(network_panel=None)
        station.view.completion.selection.return_value = ("", str(self.folder), False)
        with patch("app.PreparationWorker") as worker:
            station.start_full_test()
        worker.assert_called_once_with("", str(self.folder), False)
        self.assertEqual(station.phase, "PREPARING")
        self.assertFalse(station.commands)

    def test_manual_update_button_uses_selected_path_and_requires_idle_outputs(self):
        station = self.case.make_controller()
        station.closing = False
        station.capabilities["source"] = {"usb_restore": 1}
        station.view = Mock()
        station.view.network_panel.changing = False
        station.view.completion.path.get.return_value = "normal.px4"
        with patch.object(station, "prepare_selection") as prepare:
            station.phase = "RUNNING"
            station.update_firmware()
            prepare.assert_not_called()
            station.phase = "READY"
            station.update_firmware()
            prepare.assert_called_once_with(("normal.px4", None, False), manual=True)

    def test_manual_update_confirmation_uses_the_verified_serial_owner(self):
        station = self.case.make_controller()
        station.closing = False
        station.root = Mock()
        station.view = Mock()
        station.view.network_panel.changing = False
        station.capabilities["source"] = {"usb_restore": 1}
        source_id = "a" * 32
        station.identities["source"] = {"device_id": source_id}
        station.links = {"source": Mock(device="COM9"), "probe": Mock(device="COM143")}
        image = Mock(path=Path("normal.px4"))
        with patch("app.messagebox.askokcancel", side_effect=(False, True)), patch("app.FirmwareUpdateWorker") as worker:
            station.begin_firmware_update(image)
            worker.assert_not_called()
            station.begin_firmware_update(image)
            worker.assert_called_once_with(image, "COM9", "COM143", source_id, False)
            worker.return_value.start.assert_called_once()
        self.assertEqual(station.phase, "FINISHING")

    def test_cancelled_or_disconnected_preparation_cannot_start_later(self):
        for disconnect in (False, True):
            station, worker = self.preparing_station()
            if disconnect: station.hello.pop("source")
            else: station.stop("Cancel preparation")
            station.commands.clear()
            worker.events.put({"plan": (None, self.folder)})
            station.poll_preparation()
            self.assertFalse(station.commands)
            self.assertFalse(station.suite_active)
            self.assertIsNone(station.finish_plan)

    def test_preparation_error_remains_visible_and_sends_no_output_commands(self):
        station, worker = self.preparing_station()
        worker.events.put({"error": "Report folder is not writable"})
        station.poll_preparation()
        self.assertEqual(station.phase, "READY")
        self.assertIn("not writable", station.state.get())
        self.assertFalse(station.commands)

    def test_preparation_timeout_releases_the_station_without_late_start(self):
        station, worker = self.preparing_station()
        with patch("app.time.monotonic", return_value=station.prepare_started + 31): station.poll_preparation()
        self.assertEqual(station.phase, "READY")
        self.assertIn("timed out", station.state.get())
        worker.events.put({"plan": (None, self.folder)})
        station.poll_preparation()
        self.assertFalse(station.commands)

    def test_start_before_usb_verification_explains_how_to_connect(self):
        station = self.case.make_controller()
        station.hello.pop("source")
        station.start_full_test()
        self.assertIn("USB not verified for PixNova", station.state.get())
        self.assertIn("Refresh, select the current ports, then click Connect USB", station.state.get())
        self.assertFalse(station.commands)

    def test_gpio_requires_both_current_stop_acknowledgements(self):
        commands = []
        stop = GpioStop("a"*32, 10, lambda *args: commands.append(args))
        self.assertEqual(len(commands), 2)
        ack = {"type": "stopped", "session": "a"*32, "reason": "requested"}
        stop.handle("source", ack | {"_received_at": 9}, 10.1)
        self.assertFalse(stop.confirmed)
        stop.handle("source", ack, 10.2)
        self.assertFalse(stop.done)
        stop.handle("probe", ack, 10.3)
        self.assertTrue(stop.done)
        self.assertFalse(stop.error)
        stop = GpioStop("a"*32, 10, lambda *_: None)
        stop.tick(14)
        self.assertTrue(stop.error)

    def gpio_station(self):
        station = self.case.make_controller()
        station.assessment.reset("a" * 32)
        station.phase, station.closing, station.auto_start = "RUNNING", False, True
        station.last_pump_at = time.monotonic()
        station.last_seen = {role: station.last_pump_at for role in ("source", "probe")}
        station.capabilities = {role: {"i2c_simulators": 2, "adc_levels": 2, "io_pwm": 1, "session_stop": 1, "sbus_out": 1}
                                for role in ("source", "probe")}
        station.root = Mock()
        station.callbacks = []
        station.root.after.side_effect = lambda _, callback: station.callbacks.append(callback)
        return station

    def acknowledge_gpio(self, station, roles=("source", "probe")):
        now = station.gpio_stop.started_at + .1
        for role in roles:
            station.gpio_stop.handle(role, {"type": "stopped", "session": station.gpio_stop.session, "reason": "requested"}, now)
        station.poll_gpio_stop(now)

    def test_manual_gpio_cancel_waits_for_both_stops_and_never_completes_suite(self):
        station = self.gpio_station()
        station.finish_plan = ("normal.px4", self.folder)
        station.suite_active = True
        station.stop("Operator cancelled")
        self.assertEqual(station.phase, "STOPPING_GPIO")
        self.assertIsNone(station.finish_plan)
        self.acknowledge_gpio(station, ("source",))
        self.assertEqual(station.phase, "STOPPING_GPIO")
        self.acknowledge_gpio(station, ("probe",))
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(station.suite_completed)
        self.assertIn("outputs stopped (confirmed)", station.state.get())
        self.assertTrue(station.report_snapshot()["gpio_outputs_stopped"])

    def test_gpio_to_protocol_handoff_waits_for_both_stop_replies(self):
        station = self.gpio_station()
        station.start_i2c("GPS1_I2C")
        self.assertTrue(all(command.startswith("STOP ") for _, command in station.commands))
        self.acknowledge_gpio(station, ("source",))
        self.assertFalse(station.callbacks)
        self.acknowledge_gpio(station, ("probe",))
        station.callbacks.pop()()
        self.assertEqual(station.phase, "I2C_RUNNING")
        self.assertEqual(station.i2c_run.mode, "GPS1_I2C")
        self.assertTrue(any(command.startswith("PCHECK ") for _, command in station.commands))

    def test_timeout_disconnect_and_second_stop_cancel_pending_handoff(self):
        for failure in ("timeout", "disconnect", "cancel"):
            station = self.gpio_station()
            station.start_i2c("GPS1_I2C")
            if failure == "timeout": station.poll_gpio_stop(station.gpio_stop.deadline + .1)
            elif failure == "disconnect":
                station.handle("source", {"type": "disconnected"})
                station.poll_gpio_stop(time.monotonic())
            else:
                station.stop("Cancel pending switch")
                self.acknowledge_gpio(station)
            self.assertEqual(station.phase, "STOPPED")
            self.assertFalse(station.callbacks)
            self.assertFalse(any(command.startswith("PCHECK ") for _, command in station.commands))
            if failure != "cancel":
                station.start_i2c("GPS1_I2C")
                self.assertFalse(any(command.startswith("PCHECK ") for _, command in station.commands))

    def test_stale_gpio_snapshot_cannot_bypass_shutdown_when_switching_tests(self):
        station = self.gpio_station()
        station.last_pump_at = 0
        station.start_i2c("GPS1_I2C")
        self.assertEqual(station.phase, "STOPPING_GPIO")
        self.assertTrue(all(command.startswith("STOP ") for _, command in station.commands))
        self.assertFalse(station.callbacks)

    def test_selected_gpio_scope_is_retained_across_confirmed_restart(self):
        station = self.gpio_station()
        station.start_gpio_groups(["GPS1", "SPI6"])
        self.acknowledge_gpio(station)
        station.callbacks.pop()()
        self.assertEqual(station.phase, "CHECKING")
        self.assertEqual(station.gpio_sequence.groups, ["GPS1", "SPI6"])
        self.assertEqual(station.suite_stage, "gpio_only")

    def test_gpio_retest_merges_only_selected_results_after_both_stop_replies(self):
        station = self.gpio_station()
        station.phase, station.suite_stage, station.suite_active = "WAITING_IOV2", "fmu", True
        station.gpio_sequence = sequence = GpioSequence(station.profile)
        ppm = sequence.indices["PPM"][0]
        station.assessment.results[0].status = "PASS"
        station.assessment.results[ppm].status = "FAIL"
        station.i2c_results = {"CAN": {"status": "PASS"}}
        station.gpio_snapshot = station.report_snapshot()
        sequence.completed["PPM"] = [{"status": "FAIL"}]
        station.finish_plan = (None, self.folder)
        station.retest_gpio("PPM")
        self.assertEqual(station.phase, "CHECKING")
        self.assertEqual(station.gpio_sequence.groups, ["PPM"])
        self.assertEqual(station.assessment.results[0].status, "PASS")
        self.assertTrue(all(command.startswith("CHECK ") for _, command in station.commands))
        station.phase = "RUNNING"
        station.assessment.results[ppm].status = "PASS"
        station.advance_gpio_sequence(time.monotonic())
        self.acknowledge_gpio(station, ("source",))
        self.assertEqual(station.phase, "STOPPING_GPIO")
        self.assertEqual(sequence.completed["PPM"][0]["status"], "FAIL")
        self.acknowledge_gpio(station, ("probe",))
        self.assertEqual(station.phase, "WAITING_IOV2")
        self.assertIs(station.gpio_sequence, sequence)
        self.assertEqual(station.report_snapshot()["channels"][ppm]["status"], "PASS")
        self.assertEqual(station.report_snapshot()["channels"][0]["status"], "PASS")
        self.assertEqual(station.i2c_results["CAN"]["status"], "PASS")
        self.assertEqual(station.finish_plan, (None, self.folder))
        self.assertIn("Confirm IOV2 wiring", station.state.get())

    def test_gpio_retest_with_unconfirmed_shutdown_keeps_earlier_results(self):
        station = self.gpio_station()
        station.phase, station.suite_stage = "COMPLETE", None
        station.completed_report = {"gpio_outputs_stopped": True}
        ppm = next(i for i, row in enumerate(station.profile["channels"]) if row["group"] == "PPM")
        station.assessment.results[ppm].status = "FAIL"
        station.retest_gpio("PPM")
        station.phase = "RUNNING"
        station.assessment.results[ppm].status = "PASS"
        station.advance_gpio_sequence(time.monotonic())
        station.poll_gpio_stop(station.gpio_stop.deadline + .1)
        self.assertEqual(station.phase, "STOPPED")
        self.assertEqual(station.assessment.results[ppm].status, "FAIL")
        self.assertFalse(station.can_retest_gpio("PPM"))
        self.assertFalse(station.report_snapshot()["gpio_outputs_stopped"])
        self.assertIn("STOP unconfirmed", station.state.get())

    def test_close_keeps_serial_owners_until_gpio_shutdown_is_confirmed(self):
        station = self.gpio_station()
        station.links = {role: Mock() for role in ("source", "probe")}
        station.view = Mock()
        station.view.network_panel.changing = False
        station.pump_after_id = "pump"
        station.close()
        self.assertEqual(station.phase, "STOPPING_GPIO")
        self.assertTrue(all(not link.close.called for link in station.links.values()))
        self.acknowledge_gpio(station)
        station.callbacks.pop()()
        station.root.destroy.assert_called_once()

    def test_flash_uses_owned_serial_devices_even_if_combo_values_change(self):
        station = self.case.make_controller()
        station.finish_plan = ("frozen image", self.folder)
        station.completed_report = {"frozen": True}
        station.source_port, station.probe_port = Mock(), Mock()
        station.source_port.get.return_value, station.probe_port.get.return_value = "WRONG1", "WRONG2"
        station.links = {"source": Mock(device="COM142"), "probe": Mock(device="COM9")}
        with patch("app.CompletionWorker") as worker:
            station.finish_suite()
            self.assertEqual(worker.call_args.args[3:5], ("COM142", "COM9"))
            worker.return_value.start.assert_called_once()

    def test_export_dialog_never_interrupts_active_measurement(self):
        station = self.gpio_station()
        with patch("app.filedialog.asksaveasfilename") as dialog:
            station.export()
        dialog.assert_not_called()
        self.assertEqual(station.phase, "RUNNING")
        self.assertFalse(station.commands)

    def test_failed_gpio_shutdown_can_be_exported_with_its_failure_evidence(self):
        station = self.gpio_station()
        station.stop("Cancel test")
        station.poll_gpio_stop(station.gpio_stop.deadline + .1)
        path = self.folder/"failed-stop.csv"
        with patch("app.filedialog.asksaveasfilename", return_value=str(path)):
            station.export()
        with path.open(encoding="utf-8-sig", newline="") as stream: rows = list(csv.DictReader(stream))
        self.assertTrue(all(row["gpio_outputs_stopped"] == "False" for row in rows))
        self.assertTrue(all("STOP unconfirmed" in row["gpio_stop_error"] for row in rows))
        self.assertEqual(next(row for row in rows if row["record_type"] == "summary")["status"], "FAIL")

    def test_first_gpio_transport_error_is_not_overwritten_by_disconnect_cleanup(self):
        for stopping in (False, True):
            with self.subTest(stopping=stopping):
                station = self.gpio_station()
                if stopping:
                    station.stop()
                station.handle("source", {"type": "link_error", "reason": "Detailed driver failure"})
                station.handle("source", {"type": "disconnected"})
                station.poll_gpio_stop(time.monotonic())
                self.assertIn("Detailed driver failure", station.state.get())
                self.assertIn("Detailed driver failure", station.report_snapshot()["gpio_stop_error"])

    def test_disconnect_before_gpio_measurement_keeps_pins_unmeasured(self):
        for phase in ("STARTING_PROBE", "STARTING_SOURCE"):
            with self.subTest(phase=phase):
                station = self.gpio_station()
                station.phase = phase
                station.handle("probe", {"type": "disconnected"})
                self.assertEqual(station.phase, "STOPPING_GPIO")
                self.assertEqual(len(station.commands), 2)
                self.assertTrue(all(row.status == "NOT RUN" for row in station.assessment.results))

    def test_interrupted_gpio_group_preserves_other_connectors_and_adc(self):
        for failure in ("disconnect", "stale snapshot"):
            with self.subTest(failure=failure):
                station = self.gpio_station()
                station.suite_active = True
                station.gpio_sequence = sequence = GpioSequence(station.profile, ["TELEM1", "SPI6", "FMU PWM"])
                sequence.position = 1
                measured = station.assessment.results[sequence.indices["TELEM1"][0]]
                measured.status = "PASS"
                adc = next(i for i, row in enumerate(station.profile["channels"]) if row.get("method") == "adc_voltage")
                station.assessment.results[adc].status = "PASS"
                if failure == "disconnect":
                    station.handle("probe", {"type": "disconnected"})
                else:
                    station.last_pump_at = 0
                    station.assessment.started_at = time.monotonic() - 5
                    station.report_snapshot()
                self.assertEqual(station.phase, "STOPPING_GPIO")
                self.assertEqual(measured.status, "PASS")
                self.assertEqual(station.assessment.results[adc].status, "PASS")
                self.assertTrue(all(station.assessment.results[i].status == "FAIL" for i in sequence.indices["SPI6"]))
                self.assertTrue(all(station.assessment.results[i].status == "NOT RUN" for i in sequence.indices["FMU PWM"]))

    def test_disconnect_during_gpio_shutdown_keeps_measurements_and_fails_shutdown(self):
        station = self.gpio_station()
        station.suite_active, station.suite_stage = True, "gpio_only"
        station.gpio_sequence = sequence = GpioSequence(station.profile, ["PPM"])
        index, = sequence.indices["PPM"]
        station.assessment.results[index].status = "PASS"
        station.advance_gpio_sequence(time.monotonic())
        self.assertEqual(station.phase, "STOPPING_GPIO")
        station.handle("source", {"type": "link_error", "reason": "Detailed driver failure"})
        station.poll_gpio_stop(time.monotonic())
        report = station.report_snapshot()
        self.assertEqual(report["channels"][index]["status"], "PASS")
        self.assertFalse(report["gpio_outputs_stopped"])
        self.assertFalse(report["selected_gpio_passed"])
        self.assertFalse(report["all_passed"])
        self.assertIn("Detailed driver failure", report["gpio_stop_error"])
        self.assertTrue(all(row["status"] == "NOT RUN" for i, row in enumerate(report["channels"]) if i != index))

    def test_stale_snapshot_finishing_gpio_starts_only_one_shutdown(self):
        station = self.gpio_station()
        station.suite_active, station.suite_stage = True, "gpio_only"
        station.gpio_sequence = sequence = GpioSequence(station.profile, ["PPM"])
        index, = sequence.indices["PPM"]
        station.assessment.results[index].status = "PASS"
        station.last_pump_at = 0
        station.advance_gpio_sequence(time.monotonic())
        self.assertEqual(station.phase, "STOPPING_GPIO")
        self.assertFalse(station.suite_active)
        self.assertIsNone(station.gpio_snapshot)
        command = "STOP " + station.assessment.session
        self.assertEqual(station.commands, [("source", command), ("probe", command)])

    def test_stale_samples_do_not_preempt_gpio_stop_confirmation(self):
        station = self.gpio_station()
        station.suite_active, station.suite_stage = True, "gpio_only"
        station.gpio_sequence = sequence = GpioSequence(station.profile, ["PPM"])
        index, = sequence.indices["PPM"]
        station.assessment.results[index].status = "PASS"
        station.advance_gpio_sequence(time.monotonic())
        station.handle("probe", {"type": "sample", "_received_at": station.gpio_stop.started_at - 3})
        self.assertFalse(station.gpio_stop.done)
        self.assertFalse(station.report_snapshot()["selected_gpio_passed"])
        for role in ("probe", "source"):
            station.handle(role, {"type": "stopped", "session": station.assessment.session, "reason": "requested"})
        station.poll_gpio_stop(time.monotonic())
        self.assertEqual(station.phase, "GPIO_COMPLETE")
        self.assertTrue(station.report_snapshot()["selected_gpio_passed"])

    def test_manual_csv_uses_the_complete_automatic_schema(self):
        station = self.case.completed_controller()
        station.completed_report["post_test"] = {"status": "COMPLETE", "restoration": {"path": "normal.px4", "status": "PASS", "crc_verified": True}}
        report = station.report_snapshot()
        automatic = save_csv(report, self.folder)
        manual = self.folder/"manual.csv"
        with patch("app.filedialog.asksaveasfilename", return_value=str(manual)):
            station.export()
        def rows(path):
            with path.open(encoding="utf-8-sig", newline="") as stream: return list(csv.DictReader(stream))
        self.assertEqual(rows(manual), rows(automatic))
        self.assertTrue({"signal", "protocol", "onboard", "firmware", "summary"}.issubset({r["record_type"] for r in rows(manual)}))

    def protocol_station(self):
        from protocol_app import ProtocolStation
        child = ProtocolStation.__new__(ProtocolStation)
        station = self.case.make_controller()
        child.closed, child.demo, child.udp = False, False, None
        child.status, child.results = station.state, {}
        child.network_panel, child.root = Mock(), Mock()
        child.network_panel.changing = False
        child.links = {role: Mock() for role in ("source", "probe")}
        child.pump_after_id = "pump"
        child.run = ProtocolRun("GPS1_I2C", self.case.digest, 10, station.transmit, session_stop=True)
        child.run.phase, child.run.started = "RUNNING", {"source", "probe"}
        return child

    def test_protocol_window_close_keeps_serial_owners_until_both_stops(self):
        child = self.protocol_station()
        links = dict(child.links)
        child.close()
        self.assertFalse(child.closed)
        self.assertEqual(child.run.phase, "STOPPING")
        self.assertTrue(all(not link.close.called for link in links.values()))
        self.case.confirm_stop(child.run)
        child.close()
        self.assertTrue(child.closed and child.run.outputs_stopped)
        self.assertTrue(all(link.close.call_count == 1 for link in links.values()))

    def test_protocol_export_does_not_open_a_modal_dialog_while_active(self):
        child = self.protocol_station()
        with patch("protocol_app.filedialog.asksaveasfilename") as dialog:
            child.export()
        dialog.assert_not_called()
