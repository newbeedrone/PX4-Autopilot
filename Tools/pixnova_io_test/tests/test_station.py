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

    def test_wrong_uart_frequencies_identify_the_other_signal_without_passing(self):
        for group in ("TELEM1", "TELEM2", "TELEM3", "GPS1", "GPS2", "UART4_I2C3"):
            indices = {row["id"]: i for i, row in enumerate(self.profile["channels"])}
            tx, rx = indices[group + "_TX"], indices[group + "_RX"]
            for index, other in ((tx, rx), (rx, tx)):
                multiplier = self.profile["channels"][other]["expected_hz"] / self.profile["channels"][index]["expected_hz"]
                for sequence in range(1, 4):
                    self.assessment.accept(self.frame(sequence, channel=index, multiplier=multiplier), now=sequence)
                result = self.assessment.results[index]
                self.assertEqual(result.status, "FAIL")
                self.assertEqual(result.streak, 0)
                self.assertIn("matches " + self.profile["channels"][other]["id"] + " frequency", result.reason)

    def test_confirmed_crossed_uart_harness_mapping_matches_bench_frequencies(self):
        # Recorded physical fixture inputs, independently of the profile's channel order.
        expected = {
            "TELEM1_TX": ("PE8", "PF6", "P15", 5, 9.999),
            "TELEM1_RX": ("PF6", "PE8", "P15", 4, 10.893),
            "TELEM2_TX": ("PC12", "PD2", "P18", 5, 14.124),
            "TELEM2_RX": ("PD2", "PC12", "P18", 4, 15.384),
            "GPS2_TX": ("PE1", "PE0", "P3", 5, 51.547),
            "GPS2_RX": ("PE0", "PE1", "P3", 4, 56.180),
            "UART4_I2C3_TX": ("PH13", "PH14", "P17", 6, 60.975),
            "UART4_I2C3_RX": ("PH14", "PH13", "P17", 5, 66.668),
        }
        for index, row in enumerate(self.profile["channels"]):
            if row["id"] not in expected:
                continue
            source, probe, connector, pin, frequency = expected[row["id"]]
            self.assertEqual((row["source_pin"], row["probe_pin"], row["probe_connector"], row["probe_connector_pin"]),
                             (source, probe, connector, pin))
            for sequence in range(1, 4):
                self.assessment.accept(self.frame(sequence, channel=index, multiplier=frequency / row["expected_hz"]), now=sequence)
            self.assertEqual(self.assessment.results[index].status, "PASS")
        self.assertTrue(load_connectors(self.profile))

    def test_unmatched_frequency_keeps_the_generic_wiring_failure(self):
        self.assessment.accept(self.frame(multiplier=0.5))
        self.assertEqual(self.assessment.results[0].status, "FAIL")
        self.assertEqual(self.assessment.results[0].reason, "Frequency outside tolerance; check wiring")

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
                if self.profile["channels"][channel].get("method") == "adc_voltage": continue
                self.assessment.accept(self.frame(sequence, channel=channel), now=sequence)
        self.assertTrue(self.assessment.all_passed)
        outputs = {}
        for row in self.profile["channels"]:
            if row.get("method") == "adc_voltage": continue
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

    def test_buzzer_uses_receiver_pull_up_and_invalid_pull_is_rejected(self):
        buzzer = next(row for row in self.profile["channels"] if row["id"] == "GPS1_BUZZER")
        self.assertEqual(buzzer["probe_pull"], "up")
        profile = copy.deepcopy(self.profile)
        profile["channels"][0]["probe_pull"] = "output-high"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_text(json.dumps(profile))
            with self.assertRaises(ValueError):
                load_profile(path)

    def test_adc_input_cannot_be_changed_into_a_source_waveform(self):
        profile = copy.deepcopy(self.profile)
        row = next(r for r in profile["channels"] if r["id"] == "ADIO_ADC_6V6")
        row.pop("method")
        row.update(half_period_us=1100, expected_hz=500000/1100)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"; path.write_text(json.dumps(profile))
            with self.assertRaises(ValueError): load_profile(path)

    def test_gpio_sample_cannot_claim_an_adc_voltage_pass(self):
        index = next(i for i,r in enumerate(self.profile["channels"]) if r.get("method") == "adc_voltage")
        self.assertFalse(self.assessment.accept(dict(type="sample", session=self.assessment.session, channel=index,
                                                   seq=1, frequency_millihz=454545, edges=455, span_us=999000, timing_errors=0)))
        self.assertNotEqual(self.assessment.results[index].status, "PASS")

    def test_adc_run_updates_the_existing_pin_and_report(self):
        from app import ADC_MODES
        station = self.make_controller()
        station.capabilities = {role: {"i2c_simulators": 2, "adc_levels": 1} for role in ("source", "probe")}
        station.start_adc()
        for mode,voltage in zip(ADC_MODES, (4, 1652)):
            self.assertEqual(station.i2c_run.mode, mode)
            station.i2c_run.result.adc_voltage_mv = voltage
            station.i2c_run.complete("Verified voltage")
            station._update_i2c()
        report = station.report_snapshot()
        row = next(r for r in report["channels"] if r["id"] == "ADIO_ADC_6V6")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual((row["voltage_low_mv"], row["voltage_high_mv"]), (4, 1652))
        self.assertTrue(report["adc_passed"])
        self.assertEqual(len(report["channels"]), 43)
        self.assertFalse(any(command.startswith("START ") for _,command in station.commands))

    def test_adc_capability_is_retained_from_usb_hello(self):
        station = self.make_controller()
        station.phase = "CONNECTING"; station.hello.clear()
        station.handle("probe", dict(type="hello", role="probe", protocol=3, profile=self.digest, channels=43,
                                     device_id="b"*32, firmware="a"*16, independent_external_checks=1,
                                     protocol_tests=1, i2c_simulators=2, adc_levels=1))
        self.assertEqual(station.capabilities["probe"]["adc_levels"], 1)

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
        station.capabilities = {}
        station.i2c_results, station.i2c_queue, station.i2c_run = {}, [], None
        station.suite_active = station.suite_completed = False
        station.suite_gpio_started = station.gpio_snapshot = None
        for role, tests in EXPECTED.items():
            for name, expected in tests.items():
                station.peripherals.power_result(role, {"test": name, "pass": True, "detail": expected})
            station.peripherals.finish_power(role, {"pass": True, "count": len(tests), "checked_us": 10, "uptime_us": 20})
        for name, spec in DEVICES.items():
            station.peripherals.current_result({"test": name, "pass": True, "detail": spec["id"]})
        station.hello = {"source": True, "probe": True}
        station.links = {"source": object(), "probe": object()}
        station.identities, station.checked, station.last_seen = {}, {}, {}
        station.check_details = []
        station.generation, station.check_details = 0, []
        station.last_pump_at = 0
        station.last_ping = 0
        station.demo, station.auto_start, station.phase = False, False, "READY"
        station.state, station.state_label = Value(), Value()
        station.commands = []
        station.note = lambda text: None
        station.transmit = lambda role, command: station.commands.append((role, command))
        return station

    def critical_details(self, station, role):
        tests = (("hrt_clock", 5000), ("gpio_inputs", 43), ("i2c1_no_attached_devices", 0)) if role == "source" else (("hrt_clock", 5000), ("gpio_inputs", 43), (station.peripherals.probe_check, 1))
        for name, detail in tests:
            if not any(item["role"] == role and item["test"] == name for item in station.check_details):
                station.handle(role, dict(type="check", session=station.assessment.session, test=name, detail=detail, **{"pass":True}))

    def source_check_details(self, station):
        self.critical_details(station, "source")
        if "probe" in station.current_roles:
            self.critical_details(station, "probe")
        for name, spec in DEVICES.items():
            station.handle("source", dict(type="check", session=station.assessment.session, test=name, detail=spec["id"], **{"pass": True}))

    def test_all_i2c_connectors_use_protocol_commands_and_preserve_onboard_faults(self):
        from app import I2C_MODES
        from protocol_model import TESTS
        for mode in I2C_MODES:
            station = self.make_controller()
            station.capabilities = {role: {"i2c_simulators": 2} for role in ("source", "probe")}
            with patch("app.time.monotonic", return_value=1):
                station.start_i2c(mode)
                run = station.i2c_run
                self.assertEqual(station.phase, "I2C_RUNNING")
                for role in ("source", "probe"):
                    checks = {"hrt_clock": 5000, "gpio_inputs": 43}
                    if role == "source": checks.update({name: spec["id"] for name, spec in DEVICES.items()})
                    else: checks["probe_protocol_firmware"] = 1
                    for name, detail in checks.items():
                        good = name != "bmm150_i2c4"
                        station.handle(role, dict(type="check", session=run.session, test=name, detail=detail if good else 0, **{"pass": good}))
                    station.handle(role, dict(type="checked", session=run.session, mode=mode, **{"pass": True}))
                for role in ("probe", "source"):
                    station.handle(role, dict(type="started", session=run.session, mode=mode, role=role))
            for sequence in range(1, 5):
                with patch("app.time.monotonic", return_value=1 + sequence):
                    for role in ("probe", "source"):
                        station.handle(role, dict(type="protocol_sample", session=run.session, mode=mode, seq=sequence,
                                                  tx=sequence*20, rx=sequence*20, errors=0, detail=0, **{"pass": True}))
            self.assertEqual(station.i2c_results[mode]["status"], "PASS", TESTS[mode]["name"])
            self.assertEqual(station.phase, "I2C_COMPLETE")
            self.assertEqual(station.peripherals.latest["bmm150_i2c4"]["status"], "FAIL")
            self.assertFalse(any(command.startswith("START ") for _, command in station.commands))
            self.assertTrue(any(command.startswith("PROTO ") for _, command in station.commands))

    def test_i2c_failure_does_not_skip_the_remaining_connectors(self):
        from app import I2C_MODES
        station = self.make_controller()
        station.capabilities = {role: {"i2c_simulators": 2} for role in ("source", "probe")}
        station.start_i2c()
        for index, mode in enumerate(I2C_MODES):
            self.assertEqual(station.i2c_run.mode, mode)
            if index == 0: station.i2c_run.fail("No slave ACK")
            else: station.i2c_run.complete("Verified connector")
            station._update_i2c()
        self.assertEqual(station.phase, "I2C_COMPLETE")
        self.assertEqual(set(station.i2c_results), set(I2C_MODES))
        self.assertEqual(station.i2c_results[I2C_MODES[0]]["status"], "FAIL")
        self.assertTrue(all(station.i2c_results[mode]["status"] == "PASS" for mode in I2C_MODES[1:]))
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_i2c_pins_cannot_be_reintroduced_into_frequency_profile(self):
        for pin in ("PB8", "PB9", "PF0", "PF1", "PA8", "PH8"):
            profile = copy.deepcopy(self.profile)
            profile.pop("protected_source_pins")
            profile["channels"][0]["source_pin"] = pin
            with tempfile.TemporaryDirectory() as temporary:
                filename = Path(temporary) / "profile.json"
                filename.write_text(json.dumps(profile))
                with self.assertRaises(ValueError): load_profile(filename)

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

    def test_protocol_probe_can_run_gpio_after_identity_and_matching_checks(self):
        station = self.make_controller()
        station.phase = "CONNECTING"
        station.hello.clear()
        station.peripherals.reset()
        for role, device in (("source", "a" * 32), ("probe", "b" * 32)):
            station.handle(role, dict(type="hello", role=role, protocol=3, profile=self.digest, channels=43,
                                      device_id=device, firmware="a" * 16, protocol_tests=1, independent_external_checks=1))
            tests = EXPECTED["source"] if role == "source" else {"probe_protocol_firmware": 1}
            for name, detail in tests.items():
                station.handle(role, dict(type="power_check", test=name, detail=detail, **{"pass": True}))
            station.handle(role, dict(type="power_checked", count=len(tests), checked_us=10, uptime_us=20, **{"pass": True}))
        self.assertEqual(station.phase, "READY")
        station.start()
        self.source_check_details(station)
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, mode="GPIO", **{"pass": True}))
        self.assertEqual(station.phase, "STARTING_PROBE")
        for role in ("probe", "source"):
            station.handle(role, dict(type="started", session=station.assessment.session))
        self.assertEqual(station.phase, "RUNNING")
        self.assertEqual(station.peripherals.probe_check, "probe_protocol_firmware")

    def test_probe_marker_must_match_hello_capability(self):
        for protocols, marker in ((None, "probe_protocol_firmware"), (1, "probe_input_only_firmware")):
            station = self.make_controller()
            station.phase = "CONNECTING"
            station.hello.clear()
            station.peripherals.reset()
            hello = dict(type="hello", role="probe", protocol=3, profile=self.digest, channels=43,
                         device_id="b" * 32, firmware="a" * 16, independent_external_checks=1)
            if protocols is not None: hello["protocol_tests"] = protocols
            station.handle("probe", hello)
            station.handle("probe", dict(type="power_check", test=marker, detail=1, **{"pass": True}))
            self.assertEqual(station.phase, "STOPPED")

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
        frame = dict(type="hello", independent_external_checks=1, role="source", protocol=self.profile["protocol"], profile=self.digest, channels=len(self.profile["channels"]), device_id="1" * 32, firmware="a" * 16)
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
        station.handle("source", dict(type="check", session=station.assessment.session, test="hrt_clock", detail=0, **{"pass": False}))
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
        station.checked = {"source": True, "probe": True}
        station.assessment.reset("a" * 32)
        station.assessment.start_measurements(now=0)
        for sequence in range(1, 4):
            for channel in range(len(self.profile["channels"])):
                if self.profile["channels"][channel].get("method") == "adc_voltage": continue
                station.assessment.accept(self.frame(sequence, channel=channel), now=sequence)
        station.last_pump_at = 3
        station.last_seen = {"source": 3, "probe": 3}
        snapshot = station.report_snapshot(now=3)
        self.assertTrue(snapshot["external_passed"])
        self.assertFalse(snapshot["all_passed"])  # ADC voltage suite has not run.
        stale = station.report_snapshot(now=6)
        self.assertFalse(stale["all_passed"])
        self.assertFalse(stale["external_passed"])
        self.assertTrue(snapshot["external_passed"])
        self.assertTrue(all(row["status"] == "PASS" for row in snapshot["channels"] if row.get("method") != "adc_voltage"))
        self.assertNotEqual(next(row for row in snapshot["channels"] if row.get("method") == "adc_voltage")["status"], "PASS")

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
            station.handle(role, dict(type="hello", independent_external_checks=1, role=role, protocol=self.profile["protocol"], profile=self.digest,
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
                with patch("app.filedialog.asksaveasfilename", return_value=str(filename)), \
                     patch("app.messagebox.showerror", side_effect=AssertionError("Unexpected export error")):
                    station.export()
                if suffix == "json":
                    report = json.loads(filename.read_text())
                else:
                    with filename.open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), len(self.profile["channels"]))
                    buzzer = next(row for row in rows if row["id"] == "GPS1_BUZZER")
                    self.assertEqual(buzzer["probe_pull"], "up")
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
        station.handle("source", dict(type="hello", independent_external_checks=1, role="source", protocol=1, profile=self.digest,
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

    def test_missing_interface_evidence_blocks_start(self):
        station = self.make_controller()
        station.start()
        station.handle("source", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        station.handle("probe", dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STOPPED")
        self.assertFalse(any(command.startswith("START ") for _, command in station.commands))

    def test_all_onboard_faults_remain_red_while_external_measurements_pass(self):
        station = self.make_controller()
        station.peripherals.reset()
        for role, tests in EXPECTED.items():
            for name, value in tests.items():
                station.peripherals.power_result(role, dict(test=name, detail=value if role == "probe" else 0,
                                                           **{"pass": role == "probe"}))
            station.peripherals.finish_power(role, dict(count=len(tests), checked_us=10, uptime_us=20,
                                                       **{"pass": role == "probe"}))
        station.start()
        for role in ("source", "probe"):
            self.critical_details(station, role)
        for name in DEVICES:
            station.handle("source", dict(type="check", session=station.assessment.session, test=name,
                                          detail=0, **{"pass": False}))
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STARTING_PROBE")
        for role in ("probe", "source"):
            station.handle(role, dict(type="started", session=station.assessment.session))
        self.assertEqual(station.phase, "RUNNING")
        for sequence in range(1, 4):
            for channel in range(len(self.profile["channels"])):
                if self.profile["channels"][channel].get("method") == "adc_voltage": continue
                station.assessment.accept(self.frame(sequence, channel=channel, session=station.assessment.session), now=sequence)
        station.last_pump_at = 3
        station.last_seen = {"source": 3, "probe": 3}
        report = station.report_snapshot(now=3)
        self.assertTrue(report["external_passed"])
        self.assertFalse(report["onboard_passed"])
        self.assertFalse(report["all_passed"])
        self.assertTrue(all(result["status"] == "FAIL" for result in station.peripherals.latest.values()))

    def test_invalid_interface_details_cannot_be_overridden_by_summary(self):
        for name, value in (("hrt_clock", 0), ("gpio_inputs", 42)):
            station = self.make_controller()
            station.start()
            station.handle("source", dict(type="check", session=station.assessment.session, test=name,
                                          detail=value, **{"pass": True}))
            for role in ("source", "probe"):
                self.critical_details(station, role)
                station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": True}))
            self.assertEqual(station.phase, "STOPPED")

    def test_source_only_connection_displays_power_on_without_gpio_output(self):
        import queue
        station = self.make_controller()
        station.links = {}
        station.events, station.connected_at = queue.Queue(), {}
        station.source_port, station.probe_port = Mock(), Mock()
        station.source_port.get.return_value, station.probe_port.get.return_value = "COM4", ""
        with patch("app.SerialEndpoint"):
            station.connect()
        station.handle("source", dict(type="hello", independent_external_checks=1, role="source", protocol=self.profile["protocol"], profile=self.digest,
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
        self.assertEqual(station.phase, "READY")
        station.start()
        self.assertEqual(station.phase, "CHECKING")
        self.assertEqual([command.split()[0] for _,command in station.commands], ["CHECK", "CHECK"])
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
        self.critical_details(station, "source")
        self.critical_details(station, "probe")
        for name, spec in DEVICES.items():
            station.handle("source", dict(type="check", session=station.assessment.session, test=name,
                                           detail=0 if name == "icm45686_spi2" else spec["id"], **{"pass": True}))
        for role in ("source", "probe"):
            station.handle(role, dict(type="checked", session=station.assessment.session, **{"pass": True}))
        self.assertEqual(station.phase, "STARTING_PROBE")
        self.assertEqual(station.peripherals.latest["icm45686_spi2"]["status"], "FAIL")
        self.assertTrue(station.checked["source"])

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
