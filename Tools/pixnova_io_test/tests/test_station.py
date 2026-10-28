import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import load_connectors
from generate_profile import load_profile
from model import Assessment
from app import Station
from peripherals import Peripherals, EXPECTED, SENSORS, DEVICES


class StationTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.digest = load_profile(Path(__file__).resolve().parents[1] / "profile.json")
        self.assessment = Assessment(self.profile)
        self.assessment.reset("a" * 32)

    def frame(self, sequence=1, multiplier=1, channel=0, **changes):
        frequency = self.profile["channels"][channel]["expected_hz"] * multiplier
        edges = max(3, int(frequency) + 1)
        span = round((edges - 1) * 1000000 / frequency)
        frame = dict(type="sample", session="a" * 32, seq=sequence, channel=channel,
                     frequency_millihz=(edges - 1) * 1000000000 // span,
                     edges=edges, span_us=span, timing_errors=0)
        frame.update(changes)
        return frame

    def test_three_distinct_windows_required(self):
        for sequence in [1, 1, 1]:
            self.assessment.accept(self.frame(sequence), now=sequence)
        self.assertEqual(self.assessment.results[0].streak, 1)
        self.assertNotEqual(self.assessment.results[0].status, "PASS")
        self.assessment.accept(self.frame(2), now=2)
        self.assessment.accept(self.frame(3), now=3)
        self.assertEqual(self.assessment.results[0].status, "PASS")

    def test_wrong_wire_and_missing_signal_fail(self):
        self.assessment.accept(self.frame(multiplier=1.09))
        self.assertEqual(self.assessment.results[0].status, "FAIL")
        self.assessment.accept(self.frame(2, frequency_millihz=0, edges=0, span_us=0))
        self.assertEqual(self.assessment.results[0].status, "FAIL")

    def test_old_session_and_invalid_channel_ignored(self):
        self.assertFalse(self.assessment.accept(self.frame(session="b" * 32)))
        self.assertFalse(self.assessment.accept(self.frame(channel=True)))
        self.assertEqual(self.assessment.results[0].sequence, 0)

    def test_gaps_timeouts_and_link_loss_remove_pass(self):
        for sequence in range(1, 4):
            self.assessment.accept(self.frame(sequence), now=sequence)
        self.assessment.accept(self.frame(5), now=5)
        self.assertEqual(self.assessment.results[0].streak, 1)
        for sequence in range(6, 8):
            self.assessment.accept(self.frame(sequence), now=sequence)
        self.assessment.expire(now=10)
        self.assertEqual(self.assessment.results[0].status, "FAIL")
        self.assessment.invalidate("USB disconnected")
        self.assertFalse(self.assessment.all_passed)

    def test_overruns_and_inconsistent_frames_cannot_pass(self):
        for change in [dict(timing_errors=1), dict(span_us=1), dict(edges=-1), dict(frequency_millihz=float("nan"))]:
            self.assessment.reset("a" * 32)
            self.assessment.accept(self.frame(**change))
            self.assertEqual(self.assessment.results[0].status, "FAIL")

    def test_silent_pin_times_out_even_when_other_pins_report(self):
        self.assessment.start_measurements(now=0)
        for sequence in range(1, 4):
            self.assessment.accept(self.frame(sequence, channel=1), now=sequence)
        self.assessment.expire(now=3)
        self.assertEqual(self.assessment.results[0].status, "FAIL")
        self.assertEqual(self.assessment.results[0].reason, "Measurement timeout")
        self.assertEqual(self.assessment.results[1].status, "PASS")

    def test_all_channels_and_shared_nets(self):
        for sequence in range(1, 4):
            for channel in range(len(self.profile["channels"])):
                self.assessment.accept(self.frame(sequence, channel=channel), now=sequence)
        self.assertTrue(self.assessment.all_passed)
        outputs = {}
        for row in self.profile["channels"]:
            self.assertEqual(outputs.setdefault(row["source_pin"], row["half_period_us"]), row["half_period_us"])

    def test_sensor_pins_and_aliased_probe_pins_rejected(self):
        for edit in [lambda p: p["channels"][0].update(source_pin="PF14"),
                     lambda p: p["channels"][0].update(source_pin="PF0"),
                     lambda p: p["channels"][0].update(source_pin="PA8"),
                     lambda p: p["channels"][0].update(source_pin="PA11"),
                     lambda p: p["channels"][1].update(probe_pin=p["channels"][0]["probe_pin"])]:
            profile = copy.deepcopy(self.profile)
            edit(profile)
            with tempfile.TemporaryDirectory() as temporary:
                file = Path(temporary) / "profile.json"
                file.write_text(json.dumps(profile))
                with self.assertRaises(ValueError):
                    load_profile(file)

    def make_controller(self):
        class Value:
            def set(self, text):
                self.value = text

            def configure(self, **_):
                pass

        station = Station.__new__(Station)
        station.profile, station.digest = self.profile, self.digest
        station.assessment = Assessment(self.profile)
        station.peripherals = Peripherals()
        station.current_roles = {"source", "probe"}
        for role, tests in EXPECTED.items():
            for name, expected in tests.items():
                station.peripherals.power_result(role, {"test": name, "pass": True, "detail": expected})
            station.peripherals.finish_power(role, {"pass": True, "count": len(tests), "checked_us": 10, "uptime_us": 20})
        for name, spec in DEVICES.items():
            station.peripherals.current_result({"test": name, "pass": True, "detail": spec["id"]})
        station.hello = {"source": True, "probe": True}
        station.links = {"source": object(), "probe": object()}
        station.identities, station.checked, station.last_seen = {}, {}, {}
        station.generation, station.check_details = 0, []
        station.last_pump_at = 0
        station.last_ping = 0
        station.demo, station.auto_start, station.phase = False, False, "READY"
        station.state, station.state_label = Value(), Value()
        station.commands = []
        station.note = lambda text: None
        station.transmit = lambda role, command: station.commands.append((role, command))
        return station

    def source_check_details(self, station):
        for name, spec in DEVICES.items():
            station.handle("source", dict(type="check", session=station.assessment.session, test=name, detail=spec["id"], **{"pass": True}))

    def test_start_is_gated_by_both_checks_and_probe_ack(self):
        station = self.make_controller()
        station.start()
        self.assertEqual([command.split()[0] for _, command in station.commands], ["CHECK", "CHECK"])
        session = station.assessment.session
        self.source_check_details(station)
        station.handle("source", dict(type="checked", session=session, **{"pass": True}))
        self.assertEqual(station.phase, "CHECKING")
        station.handle("probe", dict(type="checked", session="old", **{"pass": True}))
        self.assertEqual(station.phase, "CHECKING")
        station.handle("probe", dict(type="checked", session=session, **{"pass": True}))
        self.assertEqual(station.phase, "STARTING_PROBE")
        self.assertEqual(station.commands[-1][0], "probe")
        station.handle("probe", dict(type="started", session=session))
        source_starts = [command for role, command in station.commands if role == "source" and command.startswith("START ")]
        self.assertEqual(len(source_starts), 1)
        station.handle("source", dict(type="started", session=session))
        self.assertEqual(station.phase, "RUNNING")

    def test_failed_check_stops_both_boards_and_never_starts(self):
        station = self.make_controller()
        station.start()
        station.handle("source", dict(type="checked", session=station.assessment.session, **{"pass": False}))
        self.assertEqual(station.phase, "CHECKING")
        station.handle("probe", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))
        self.assertEqual(station.commands[-2:], [("source", "STOP"), ("probe", "STOP")])

    def test_restart_stops_outputs_then_repeats_both_checks(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.start()
        self.assertEqual(station.commands[:2], [("source", "STOP"), ("probe", "STOP")])
        self.assertEqual([command.split()[0] for _, command in station.commands[2:]], ["CHECK", "CHECK"])
        self.assertTrue(station.auto_start)
        session = station.assessment.session
        self.source_check_details(station)
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=session, **{"pass": True}))
        self.assertEqual(station.phase, "STARTING_PROBE")

    def test_wrong_firmware_or_same_mcu_cannot_be_accepted(self):
        station = self.make_controller()
        station.phase = "CONNECTING"
        station.hello.clear()
        frame = dict(type="hello", role="source", protocol=self.profile["protocol"], profile=self.digest, channels=len(self.profile["channels"]), device_id="1" * 32, firmware="a" * 16)
        station.handle("source", frame)
        self.assertIn("source", station.hello)
        station.handle("probe", dict(frame, role="probe"))
        self.assertEqual(station.phase, "STOPPED")
        self.assertNotIn("probe", station.hello)

    def test_old_connection_or_session_cannot_change_live_state(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.assessment.reset("a" * 32)
        station.handle("source", dict(type="disconnected", _connection=-1))
        station.handle("source", dict(type="stopped", session="b" * 32))
        station.handle("source", dict(type="heartbeat", session="b" * 32, running=True))
        self.assertEqual(station.phase, "RUNNING")
        self.assertNotIn("source", station.last_seen)

    def test_reboot_or_failed_individual_check_stops_the_test(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.handle("source", dict(type="hello"))
        self.assertEqual(station.phase, "STOPPED")
        self.assertNotIn("source", station.hello)
        station = self.make_controller()
        station.start()
        station.handle("source", dict(type="check", session=station.assessment.session, test="bmm150", detail=0, **{"pass": False}))
        self.assertEqual(station.phase, "CHECKING")
        self.source_check_details(station)
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(station.check_details[0]["pass"])

    def test_gui_stall_cannot_turn_queued_old_samples_into_a_pass(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.last_pump_at = 1
        station.assessment.reset("a" * 32)
        import queue
        station.events = queue.Queue()
        for sequence in range(1, 4):
            station.events.put(("probe", self.frame(sequence)))
        station.closing = False
        station.root = type("Root", (), {"after": lambda *args: None})()
        station.draw_results = lambda: None
        with patch("app.time.monotonic", return_value=10):
            station.pump()
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(station.assessment.all_passed)
        self.assertTrue(all(row.status == "FAIL" for row in station.assessment.results))

    def test_expired_results_cannot_be_exported_as_passed(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.assessment.reset("a" * 32)
        station.assessment.start_measurements(now=0)
        for sequence in range(1, 4):
            for channel in range(len(self.profile["channels"])):
                station.assessment.accept(self.frame(sequence, channel=channel), now=sequence)
        station.last_pump_at = 3
        station.last_seen = {"source": 3, "probe": 3}
        snapshot = station.report_snapshot(now=3)
        self.assertTrue(snapshot["all_passed"])
        stale = station.report_snapshot(now=6)
        self.assertFalse(stale["all_passed"])
        self.assertTrue(snapshot["all_passed"])
        self.assertTrue(all(row["status"] == "PASS" for row in snapshot["channels"]))

    def test_nonfinite_tolerance_duplicate_frequencies_and_positions_rejected(self):
        edits = [lambda p: p.update(tolerance_percent=float("nan")),
                 lambda p: p.update(tolerance_percent=-2),
                 lambda p: p.update(required_windows=True),
                 lambda p: p.update(sample_interval_us=0),
                 lambda p: p["channels"][0].update(expected_hz=float("nan")),
                 lambda p: p["channels"][1].update(half_period_us=p["channels"][0]["half_period_us"], expected_hz=p["channels"][0]["expected_hz"]),
                 lambda p: p["channels"][1].update(probe_connector_pin=p["channels"][0]["probe_connector_pin"]),
                 lambda p: (p.pop("protected_source_pins"), p["channels"][0].update(source_pin="PG8"))]
        for edit in edits:
            profile = copy.deepcopy(self.profile)
            edit(profile)
            with tempfile.TemporaryDirectory() as temporary:
                file = Path(temporary) / "profile.json"
                file.write_text(json.dumps(profile))
                with self.assertRaises(ValueError):
                    load_profile(file)

    def test_oversized_measurements_fail_without_arithmetic_errors(self):
        for values in [dict(frequency_millihz=10**400), dict(span_us=10**400), dict(edges=0xffffffff)]:
            self.assessment.reset("a" * 32)
            self.assessment.accept(self.frame(**values))
            self.assertEqual(self.assessment.results[0].status, "FAIL")

    def test_reconnect_replaces_threads_and_ignores_the_old_connection(self):
        import queue
        station = self.make_controller()
        old_links = {"source": Mock(), "probe": Mock()}
        station.links = old_links.copy()
        station.source_port, station.probe_port = Mock(), Mock()
        station.source_port.get.return_value, station.probe_port.get.return_value = "COM4", "COM5"
        station.events, station.connected_at = queue.Queue(), {}
        with patch("app.SerialEndpoint") as endpoint:
            station.connect()
        self.assertEqual(station.phase, "CONNECTING")
        self.assertEqual(station.generation, 1)
        self.assertEqual(endpoint.call_count, 2)
        for link in old_links.values():
            link.close.assert_called_once()
        station.handle("source", dict(type="disconnected", _connection=0))
        self.assertEqual(station.phase, "CONNECTING")
        for role, device_id in [("source", "a" * 32), ("probe", "b" * 32)]:
            station.handle(role, dict(type="hello", role=role, protocol=self.profile["protocol"], profile=self.digest,
                                     channels=len(self.profile["channels"]), device_id=device_id,
                                     firmware="a" * 16, _connection=1))
            for name, expected in EXPECTED[role].items():
                station.handle(role, dict(type="power_check", test=name, detail=expected, **{"pass": True}, _connection=1))
            station.handle(role, dict(type="power_checked", count=len(EXPECTED[role]), checked_us=10, uptime_us=20, **{"pass": True}, _connection=1))
        self.assertEqual(station.phase, "READY")

    def test_report_files_preserve_device_and_self_check_metadata(self):
        import csv
        station = self.make_controller()
        station.identities = {"source": {"device_id": "a" * 32, "firmware": "b" * 16}}
        station.check_details = [dict(role="source", test="bmm150", **{"pass": True}, detail=50)]
        with tempfile.TemporaryDirectory() as temporary:
            for suffix in ("json", "csv"):
                filename = Path(temporary) / ("report." + suffix)
                with patch("app.filedialog.asksaveasfilename", return_value=str(filename)):
                    station.export()
                if suffix == "json":
                    report = json.loads(filename.read_text())
                else:
                    with filename.open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), len(self.profile["channels"]))
                    report = {key: json.loads(rows[0][key]) for key in ("devices", "startup_check_details")}
                self.assertEqual(report["devices"], station.identities)
                self.assertEqual(report["startup_check_details"], station.check_details)

    def test_connector_diagram_rejects_wrong_net_or_missing_pin(self):
        source = Path(__file__).resolve().parents[1] / "connectors.json"
        original = json.loads(source.read_text())
        for edit in [lambda data: data["connectors"][0]["pins"][3].update(nets=["WRONG_NET"]),
                     lambda data: data["connectors"][0]["pins"].pop(3)]:
            data = copy.deepcopy(original)
            edit(data)
            with patch("dashboard.Path.read_text", return_value=json.dumps(data)):
                with self.assertRaises(ValueError):
                    load_connectors(self.profile)

    def test_old_protocol_firmware_is_rejected_before_self_check(self):
        station = self.make_controller()
        station.phase = "CONNECTING"
        station.hello.clear()
        station.handle("source", dict(type="hello", role="source", protocol=1, profile=self.digest,
                                      channels=len(self.profile["channels"]), device_id="a" * 32,
                                      firmware="b" * 16))
        self.assertEqual(station.phase, "STOPPED")
        self.assertNotIn("source", station.hello)

    def test_burst_of_backlogged_windows_cannot_pass(self):
        for sequence in range(1, 4):
            self.assessment.accept(self.frame(sequence), now=10 + sequence * 0.001)
        self.assertEqual(self.assessment.results[0].streak, 1)
        self.assertNotEqual(self.assessment.results[0].status, "PASS")
        self.assessment.accept(self.frame(4), now=11)
        self.assessment.accept(self.frame(5), now=12)
        self.assertEqual(self.assessment.results[0].status, "PASS")

    def test_long_gap_requires_new_consecutive_windows_without_expire_call(self):
        for sequence in range(1, 4):
            self.assessment.accept(self.frame(sequence), now=sequence)
        self.assertEqual(self.assessment.results[0].status, "PASS")
        self.assessment.accept(self.frame(4), now=7)
        self.assertEqual(self.assessment.results[0].streak, 1)
        self.assertNotEqual(self.assessment.results[0].status, "PASS")

    def test_stale_queued_frame_cannot_refresh_heartbeat(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.assessment.reset("a" * 32)
        with patch("app.time.monotonic", return_value=10):
            station.handle("source", dict(type="heartbeat", session="a" * 32, running=True, _received_at=7))
        self.assertEqual(station.phase, "STOPPED")
        self.assertNotIn("source", station.last_seen)

    def test_receiver_timestamp_is_used_instead_of_gui_delivery_time(self):
        station = self.make_controller()
        station.phase = "RUNNING"
        station.assessment.reset("a" * 32)
        with patch("app.time.monotonic", return_value=10):
            station.handle("probe", self.frame(_received_at=9.5))
        self.assertEqual(station.last_seen["probe"], 9.5)
        self.assertEqual(station.assessment.results[0].received_at, 9.5)

    def test_failed_export_preserves_the_existing_report(self):
        station = self.make_controller()
        with tempfile.TemporaryDirectory() as temporary:
            for suffix in ("json", "csv"):
                target = Path(temporary) / ("report." + suffix)
                target.write_text("previous completed report")
                writer = "app.json.dump" if suffix == "json" else "app.csv.DictWriter.writerows"
                with patch("app.filedialog.asksaveasfilename", return_value=str(target)), patch(writer, side_effect=OSError("disk full")), patch("app.messagebox.showerror") as error:
                    station.export()
                error.assert_called_once()
                self.assertEqual(target.read_text(), "previous completed report")
            self.assertEqual(len(list(Path(temporary).iterdir())), 2)

    def test_power_on_results_are_required_before_any_gpio_check(self):
        station = self.make_controller()
        station.peripherals.reset()
        station.start()
        self.assertEqual(station.commands, [])

    def test_missing_individual_pretest_sensor_results_block_start(self):
        station = self.make_controller()
        station.start()
        station.handle("source", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        station.handle("probe", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_source_only_connection_displays_power_on_without_gpio_output(self):
        import queue
        station = self.make_controller()
        station.links = {}
        station.events, station.connected_at = queue.Queue(), {}
        station.source_port, station.probe_port = Mock(), Mock()
        station.source_port.get.return_value, station.probe_port.get.return_value = "COM4", ""
        with patch("app.SerialEndpoint"):
            station.connect()
        station.handle("source", dict(type="hello", role="source", protocol=self.profile["protocol"], profile=self.digest,
                                      channels=len(self.profile["channels"]), device_id="a" * 32, firmware="b" * 16))
        for name, expected in EXPECTED["source"].items():
            station.handle("source", dict(type="power_check", test=name, detail=expected, **{"pass": True}))
        station.handle("source", dict(type="power_checked", count=len(EXPECTED["source"]), checked_us=10, uptime_us=20, **{"pass": True}))
        self.assertEqual(station.phase, "SOURCE_READY")
        self.assertTrue(station.report_snapshot()["peripherals"]["onboard_passed"])
        station.start()
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))
        station.self_check()
        self.source_check_details(station)
        station.handle("source", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "CHECKED")

    def test_failed_power_on_is_visible_and_not_overridden_by_current_pass(self):
        station = self.make_controller()
        station.phase = "CONNECTING"
        station.peripherals.reset()
        for role, tests in EXPECTED.items():
            for name, expected in tests.items():
                bad = name == "icm45686_spi2"
                station.handle(role, dict(type="power_check", test=name, detail=0 if bad else expected, **{"pass": not bad}))
            station.handle(role, dict(type="power_checked", count=len(tests), checked_us=10, uptime_us=20, **{"pass": role != "source"}))
        self.assertEqual(station.phase, "POWER_FAILED")
        station.start()
        self.assertEqual(station.commands, [])
        self.assertEqual(station.peripherals.latest["icm45686_spi2"]["status"], "FAIL")

    def test_storage_pretest_failure_marks_device_red_without_claiming_pin_faults(self):
        station = self.make_controller()
        station.start()
        station.handle("source", dict(type="check", session=station.assessment.session,
                                      test="sdcard", detail=0, **{"pass": False}))
        self.assertEqual(station.phase, "CHECKING")
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": role != "source"}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertEqual(station.peripherals.latest["sdcard"]["status"], "FAIL")
        self.assertTrue(all(result.status == "NOT RUN" for result in station.assessment.results))
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_all_device_results_survive_multiple_failures_in_either_board_order(self):
        for probe_first in (False, True):
            station = self.make_controller()
            station.start()
            session = station.assessment.session
            if probe_first:
                station.handle("probe", dict(type="checked", session=session, **{"pass": False}))
                self.assertEqual(station.phase, "CHECKING")
            bad = {"icm45686_spi2", "eeprom_base_i2c3", "sdcard"}
            for name, spec in DEVICES.items():
                station.handle("source", dict(type="check", session=session, test=name,
                                               detail=0 if name in bad else spec["id"], **{"pass": name not in bad}))
                self.assertEqual(station.phase, "CHECKING")
            station.handle("source", dict(type="checked", session=session, **{"pass": False}))
            if not probe_first:
                station.handle("probe", dict(type="checked", session=session, **{"pass": True}))
            self.assertEqual(station.phase, "STOPPED")
            self.assertEqual({name for name, result in station.peripherals.latest.items() if result["status"] == "FAIL"}, bad)
            self.assertEqual(len(station.check_details), len(DEVICES))
            report = station.report_snapshot()
            self.assertFalse(report["startup_checks"]["source"])
            self.assertFalse(report["all_passed"])
            self.assertTrue(all(row["status"] == "NOT RUN" for row in report["channels"]))
            self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_wrong_individual_id_cannot_be_overridden_by_passing_summary(self):
        station = self.make_controller()
        station.start()
        for name, spec in DEVICES.items():
            station.handle("source", dict(type="check", session=station.assessment.session, test=name,
                                           detail=0 if name == "icm45686_spi2" else spec["id"], **{"pass": True}))
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertEqual(station.peripherals.latest["icm45686_spi2"]["status"], "FAIL")
        self.assertFalse(station.checked["source"])

    def test_duplicate_or_malformed_current_results_stop_without_waiting(self):
        for change in ({"pass": 1}, {"detail": True}, {}):
            station = self.make_controller()
            station.start()
            frame = dict(type="check", session=station.assessment.session, test="icm45686_spi1", detail=0xE9, **{"pass": True})
            if not change:
                station.handle("source", frame)
            station.handle("source", dict(frame, **change))
            self.assertEqual(station.phase, "STOPPED")
            self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_cancelled_check_returns_unreceived_devices_to_waiting(self):
        station = self.make_controller()
        station.start()
        station.handle("source", dict(type="check", session=station.assessment.session, test="icm45686_spi1", detail=0xE9, **{"pass": True}))
        station.stop()
        self.assertEqual(station.peripherals.latest["icm45686_spi1"]["status"], "PASS")
        self.assertEqual(station.peripherals.latest["sdcard"]["status"], "WAITING")
        station.handle("source", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_late_power_results_do_not_restore_cancelled_connection(self):
        station = self.make_controller()
        station.phase = "CONNECTING"
        station.stop()
        station.power_ready()
        self.assertEqual(station.phase, "STOPPED")

    def test_start_during_manual_check_does_not_arm_automatic_output(self):
        station = self.make_controller()
        station.self_check()
        station.start()
        self.source_check_details(station)
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "CHECKED")
        self.assertFalse(station.auto_start)
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))


if __name__ == "__main__":
    unittest.main()
