#!/usr/bin/env python3
"""Dual-USB PixNova GPIO continuity and frequency test station."""

import argparse
import csv
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import time
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox
import uuid

from dashboard import Dashboard, RED, MUTED, GREEN
from generate_profile import load_profile
from model import Assessment
from peripherals import Peripherals, EXPECTED, DEVICES, display_value, interface_result
from transport import SerialEndpoint
from protocol_model import ProtocolRun, TESTS

I2C_MODES = tuple(name for name, spec in TESTS.items() if spec.get("kind") == "i2c")
ADC_MODES = ("ADC6V6_LOW", "ADC6V6_HIGH")


class Station:
    def __init__(self, root, profile_path, demo=False):
        self.root, self.profile_path = root, profile_path
        self.profile, self.digest = load_profile(profile_path)
        self.assessment = Assessment(self.profile)
        self.peripherals = Peripherals()
        self.current_roles = set()
        self.capabilities, self.i2c_results = {}, {}
        self.i2c_run, self.i2c_queue = None, []
        self.suite_active, self.suite_completed, self.gpio_snapshot = False, False, None
        self.suite_gpio_started = None
        self.events = queue.Queue(maxsize=4096)
        self.links, self.hello, self.checked = {}, {}, {}
        self.identities = {}
        self.generation, self.check_details = 0, []
        self.last_pump_at = time.monotonic()
        self.last_seen, self.connected_at = {}, {}
        self.phase, self.phase_started, self.last_ping = "DISCONNECTED", 0, 0
        self.auto_start, self.closing, self.demo = False, False, demo
        self.demo_sequence, self.last_demo = 0, 0
        self.state = tk.StringVar(value="Connect both USB endpoints")
        self.source_port, self.probe_port = tk.StringVar(), tk.StringVar()
        self.demo_fault = tk.BooleanVar(value=False)
        self.demo_peripheral_fault = tk.BooleanVar(value=False)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.pump)
        if demo:
            root.after(150, self.start_demo)

    def _build(self):
        self.view = Dashboard(self)
        self.source_combo, self.probe_combo = self.view.source_combo, self.view.probe_combo
        self.state_label, self.log = self.view.state_label, self.view.log
        self.refresh_ports()

    def open_protocols(self):
        """Transfer USB ownership after stopping the frequency station."""
        if getattr(self, "protocol_window", None) and not self.protocol_window.closed:
            self.protocol_window.root.lift()
            return
        self.stop("Switching to connector protocol tests")
        self.generation += 1
        for link in self.links.values():
            link.close()
        for link in self.links.values():
            link.join(timeout=.5)
        self.links.clear()
        self.hello.clear()
        self.phase = "DISCONNECTED"
        from protocol_app import ProtocolStation
        window = tk.Toplevel(self.root)
        station = ProtocolStation(window, self.profile_path, self.demo, on_io_ports=self.show_io_ports)
        station.source_port.set(self.source_port.get())
        station.probe_port.set(self.probe_port.get())
        # Closing the frequency window also closes this window's serial owners.
        self.protocol_window = station

    def show_io_ports(self):
        self.root.deiconify()
        self.root.lift()
        self.view.filter(None)
        self.state.set("IO ports · select GPS1, GPS2, TELEM, SPI6 or another connector")

    def note(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        self.log.see("end")
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.0")
        self.log.configure(state="disabled")

    def refresh_ports(self):
        try:
            from serial.tools import list_ports
            ports = list(list_ports.comports())
            names = [item.device for item in ports]
            self.source_combo["values"] = self.probe_combo["values"] = names
            for item in ports:
                self.note(f"USB: {item.device} — {item.description}; serial={item.serial_number or 'unknown'}")
        except ImportError:
            self.note("Install pyserial for hardware connections; the demo works without it.")

    def transmit(self, role, command):
        if not self.demo and role in self.links:
            self.links[role].send(command)

    def connect(self):
        if getattr(self, "protocol_window", None) and not self.protocol_window.closed:
            self.note("Close the protocol window before reconnecting the frequency station.")
            return
        if self.demo:
            self.start_demo()
            return
        if self.phase in ("RUNNING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "CONNECTING", "I2C_RUNNING"):
            self.note("Stop the active operation before reconnecting USB endpoints.")
            return
        ports = {"source": self.source_port.get().strip()}
        if self.probe_port.get().strip():
            ports["probe"] = self.probe_port.get().strip()
        if not ports["source"] or len({port.casefold() for port in ports.values()}) != len(ports):
            messagebox.showerror("USB selection", "Select PixNova USB and, optionally, a distinct probe USB port.")
            return
        self.generation += 1
        for link in self.links.values():
            link.close()
        for link in self.links.values():
            link.join(timeout=0.3)
        self.links.clear()
        self.hello.clear()
        self.identities.clear()
        self.checked.clear()
        self.peripherals.reset()
        self.assessment = Assessment(self.profile)
        self.current_roles.clear()
        self.capabilities.clear(); self.i2c_results.clear()
        self.i2c_run, self.i2c_queue = None, []
        self.suite_active = self.suite_completed = False
        self.gpio_snapshot = None
        self.auto_start = False
        self.check_details.clear()
        self.phase, self.phase_started = "CONNECTING", time.monotonic()
        self.state.set("Connecting — waiting for power-on peripheral checks")
        for role, device in ports.items():
            self.connected_at[role] = time.monotonic()
            self.links[role] = SerialEndpoint(role, device, self.events, self.generation)
            self.links[role].start()

    def start_full_test(self):
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Wait for the active test or stop it before restarting."); return
        if not self.demo and not all(getattr(self, "capabilities", {}).get(role, {}).get("i2c_simulators") == 2
                                     and self.capabilities[role].get("adc_levels") == 1 for role in ("source", "probe")):
            self.note("Connect both boards with the I2C simulator and ADC voltage firmware before starting the complete test.")
            return
        self.i2c_results = {}; self.gpio_snapshot = None; self.suite_completed = False
        self.start()
        self.suite_active = not self.demo and self.phase == "CHECKING" and self.auto_start
        self.suite_gpio_started = None

    def start_i2c(self, mode=None):
        if mode is not None and mode not in I2C_MODES:
            raise ValueError("Invalid I2C connector")
        self._start_connector_modes([mode] if mode else list(I2C_MODES))

    def start_adc(self):
        self._start_connector_modes(list(ADC_MODES))

    def _start_connector_modes(self, modes):
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Wait for the active check or stop it before another test."); return
        if not all(getattr(self, "capabilities", {}).get(role, {}).get("i2c_simulators") == 2 for role in ("source", "probe")) or not self.peripherals.interfaces_ready():
            self.note("Connector tests require both USB endpoints with the final test firmware."); return
        if any(mode in ADC_MODES for mode in modes) and not all(self.capabilities[role].get("adc_levels") == 1 for role in ("source", "probe")):
            self.note("Update both boards to the ADC voltage test firmware."); return
        if self.phase == "RUNNING":
            self.gpio_snapshot = self.report_snapshot()
            self.stop("Switching from GPIO measurements to connector tests")
        self.i2c_queue = modes
        for mode in modes:
            self.i2c_results.pop(mode, None)
        if any(mode in ADC_MODES for mode in modes): self._sync_adc_result()
        self._next_i2c()

    def _next_i2c(self):
        mode = self.i2c_queue.pop(0)
        self.peripherals.begin_current()
        self.i2c_run = ProtocolRun(mode, self.digest, time.monotonic(), self.transmit)
        self.phase = "I2C_RUNNING"
        self.i2c_results[mode] = self.i2c_run.snapshot()
        self.state.set(TESTS[mode]["name"] + " · checking " + ("ADC voltage" if mode in ADC_MODES else "simulated I2C slave"))
        self.i2c_run.begin()

    def _update_i2c(self):
        run = self.i2c_run
        self.i2c_results[run.mode] = run.snapshot()
        if run.mode in ADC_MODES: self._sync_adc_result()
        self.state.set(TESTS[run.mode]["name"] + " · " + run.result.reason)
        if run.phase not in ("COMPLETE", "FAILED", "STOPPED"): return
        self.peripherals.end_current()
        if self.i2c_queue:
            self._next_i2c()
        else:
            self.phase = "COMPLETE" if self.suite_active else "I2C_COMPLETE"
            self.suite_completed = self.suite_active
            self.suite_active = False
            passed = sum(result["status"] == "PASS" for result in self.i2c_results.values())
            self.state.set(f"Connector tests finished · {passed}/{len(self.i2c_results)} PASS · outputs stopped")
            self.state_label.configure(foreground=GREEN if passed == len(self.i2c_results) else RED)

    def adc_summary(self):
        results = [self.i2c_results.get(mode, {}) for mode in ADC_MODES]
        statuses = [item.get("status", "NOT RUN") for item in results]
        status = "FAIL" if "FAIL" in statuses else "PASS" if all(s == "PASS" for s in statuses) else "VERIFYING" if any(s != "NOT RUN" for s in statuses) else "NOT RUN"
        return {"status": status, "low_mv": results[0].get("adc_voltage_mv"), "high_mv": results[1].get("adc_voltage_mv"),
                "reason": "; ".join(f"{TESTS[mode]['name']}: {item.get('reason', 'Not run')}" for mode, item in zip(ADC_MODES, results))}

    def _sync_adc_result(self):
        summary = self.adc_summary()
        for row, result in zip(self.profile["channels"], self.assessment.results):
            if row.get("method") == "adc_voltage":
                result.status, result.reason = summary["status"], summary["reason"]
                result.voltage_low_mv, result.voltage_high_mv = summary["low_mv"], summary["high_mv"]

    def self_check(self):
        if self.phase == "I2C_RUNNING": self.stop("Stopping I2C slave test before self-check")
        if self.phase in ("CHECKING", "CONNECTING", "STARTING_PROBE", "STARTING_SOURCE"):
            self.note("Startup is already in progress.")
            return
        if self.phase == "RUNNING":
            self.stop()
        if "source" not in self.hello or not set(self.hello).issubset(self.peripherals.summaries):
            self.note("Read the flight controller's power-on results before self-check.")
            return
        self.assessment.reset(uuid.uuid4().hex)
        self.checked.clear()
        self.check_details.clear()
        self.current_roles = set(self.hello)
        self.peripherals.begin_current()
        self.phase, self.phase_started = "CHECKING", time.monotonic()
        self.state.set(("DEMO — " if self.demo else "") + "Checking external interfaces; collecting onboard diagnostics")
        try:
            for role in sorted(self.current_roles, reverse=True):
                self.transmit(role, f"CHECK {self.assessment.session} {self.digest}")
        except RuntimeError as error:
            self.stop(str(error), invalid=True)
            return
        if self.demo:
            session = self.assessment.session
            def finish_demo_probe():
                for name, detail in (("hrt_clock", 5000), ("gpio_inputs", len(self.profile["channels"])), ("probe_input_only_firmware", 1)):
                    self.events.put(("probe", {"type": "check", "session": session, "test": name, "pass": True, "detail": detail}))
                self.events.put(("probe", {"type": "checked", "session": session, "pass": True}))
            self.root.after(300, finish_demo_probe)
            def finish_demo_source():
                for name, spec in DEVICES.items():
                    good = name != "icm45686_spi2" or not self.demo_peripheral_fault.get()
                    self.events.put(("source", {"type": "check", "session": session, "test": name, "pass": good, "detail": spec.get("demo", spec["id"]) if good else 0}))
                for name, detail in (("hrt_clock", 5000), ("gpio_inputs", len(self.profile["channels"])), ("i2c1_no_attached_devices", 0)):
                    self.events.put(("source", {"type": "check", "session": session, "test": name, "pass": True, "detail": detail}))
                self.events.put(("source", {"type": "checked", "session": session, "pass": True}))
            self.root.after(550, finish_demo_source)

    def start(self):
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Startup is already in progress; wait for it to finish.")
            return
        if len(self.hello) != 2 or not self.peripherals.interfaces_ready():
            self.note("Complete both USB handshakes and verify the probe before GPIO output.")
            return
        if self.phase == "RUNNING":
            self.stop("Restarting test")
        self.auto_start = True
        self.self_check()

    def power_ready(self):
        roles = set(self.links)
        if self.phase != "CONNECTING" or not roles or not roles.issubset(self.hello) or not roles.issubset(self.peripherals.summaries):
            return
        ready = self.peripherals.interfaces_ready(roles)
        onboard = self.peripherals.passed(("source",))
        self.phase = ("READY" if len(roles) == 2 else "SOURCE_READY") if ready else "POWER_FAILED"
        self.state.set(("DEMO — " if self.demo else "") + ("External tests ready" if ready and onboard else "Onboard faults recorded; external tests available" if ready else "USB/probe validation failed — outputs disabled")
                       + (" · connect probe for GPIO testing" if len(roles) == 1 else ""))
        self.state_label.configure(foreground=GREEN if ready else RED)

    def begin(self):
        self.phase, self.phase_started = "STARTING_PROBE", time.monotonic()
        self.state.set("Starting probe in GPIO receive mode before enabling source outputs")
        self.transmit("probe", f"START {self.assessment.session} {self.digest}")
        if self.demo:
            self.events.put(("probe", {"type": "started", "session": self.assessment.session}))

    def stop(self, reason="Stopped by operator", invalid=False):
        pretest = self.phase == "CHECKING"
        if getattr(self, "i2c_run", None) and self.i2c_run.phase not in ("COMPLETE", "FAILED", "STOPPED"):
            self.i2c_run.stop(reason, failed=invalid)
            self.i2c_results[self.i2c_run.mode] = self.i2c_run.snapshot()
        self.i2c_queue = []
        self.suite_active = False
        for role in tuple(self.links):
            try:
                self.transmit(role, "STOP")
            except RuntimeError:
                pass
        self.phase, self.auto_start = "STOPPED", False
        if pretest:
            self.peripherals.end_current()
        if invalid:
            self.assessment.invalidate(reason)
            if pretest:
                for result in self.assessment.results:
                    result.status = "NOT RUN"
        self.state.set(("DEMO — " if self.demo else "") + reason + " · outputs stopped")
        self.state_label.configure(foreground=RED if invalid else MUTED)
        self.note(reason)

    def handle(self, role, frame):
        if frame.get("_connection", self.generation) != self.generation:
            return
        now = time.monotonic()
        received_at = frame.get("_received_at", now)
        if type(received_at) not in (int, float) or not 0 <= received_at <= now or now - received_at > self.profile["lease_ms"] / 1000:
            self.stop(f"{role}: stale USB response; restart the test", invalid=True)
            return
        kind = frame["type"]
        if self.phase == "I2C_RUNNING" and kind not in ("hello", "power_check", "power_checked", "link_error", "disconnected"):
            if kind == "check" and role == "source" and frame.get("session") == self.i2c_run.session:
                self.peripherals.current_result(frame)
            self.i2c_run.handle(role, frame, now)
            self._update_i2c()
            return

        if kind == "hello":
            if self.phase != "CONNECTING" or role in self.hello:
                self.hello.pop(role, None)
                self.stop(f"{role}: unexpected firmware restart; reconnect USB", invalid=True)
                return
            if frame.get("role") != role or frame.get("profile") != self.digest or type(frame.get("protocol")) is not int or frame.get("protocol") != self.profile["protocol"] or frame.get("channels") != len(self.profile["channels"]):
                self.hello.pop(role, None)
                self.stop(f"{role}: firmware role/protocol/profile mismatch", invalid=True)
                return
            device_id, firmware = frame.get("device_id", ""), frame.get("firmware", "")
            if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-fA-F]{16,80}", device_id) or not isinstance(firmware, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", firmware):
                self.stop(f"{role}: missing hardware or firmware identity", invalid=True)
                return
            device_id = device_id.lower()
            if role == "source" and frame.get("independent_external_checks") != 1:
                self.stop("Update source firmware for independent external checks", invalid=True)
                return
            if role == "probe":
                protocols = frame.get("protocol_tests")
                if protocols is not None and (type(protocols) is not int or protocols != 1 or frame.get("independent_external_checks") != 1):
                    self.stop("Unsupported probe protocol firmware; update the probe", invalid=True)
                    return
                self.peripherals.probe_check = "probe_protocol_firmware" if protocols == 1 else "probe_input_only_firmware"
            if any(identity["device_id"].lower() == device_id for other, identity in self.identities.items() if other != role):
                self.stop("Both serial ports identify as the same MCU", invalid=True)
                return
            if not hasattr(self, "capabilities"): self.capabilities = {}
            self.capabilities[role] = {"i2c_simulators": frame.get("i2c_simulators"), "adc_levels": frame.get("adc_levels")}
            self.identities[role] = {"device_id": device_id, "firmware": firmware}
            self.hello[role] = True
            self.last_seen[role] = received_at
            self.note(f"{role}: ID={device_id}, firmware={firmware}; profile verified")
            self.power_ready()
        elif kind in ("power_check", "power_checked"):
            if role not in self.hello:
                self.stop(f"{role}: power-on result before device identification", invalid=True)
                return
            try:
                if kind == "power_check":
                    self.peripherals.power_result(role, frame)
                    good = self.peripherals.power[role][frame['test']]["pass"]
                    value = display_value(DEVICES[frame['test']], frame['detail']) if role == "source" else str(frame['detail'])
                    self.note(f"{role} POWER-ON: {frame['test']} {'PASS' if good else 'FAIL'}; {value}")
                else:
                    self.peripherals.finish_power(role, frame)
                    self.power_ready()
            except ValueError as error:
                self.peripherals.invalidate(role, str(error))
                self.stop(f"{role}: {error}", invalid=True)
        elif kind in ("link_error", "disconnected"):
            self.hello.pop(role, None)
            if self.phase != "DISCONNECTED":
                self.stop(f"{role}: {frame.get('reason', 'USB disconnected')}", invalid=True)
        elif kind == "error":
            self.stop(f"{role}: {frame.get('reason', 'endpoint rejected command')}", invalid=True)
        elif kind == "check" and self.phase == "CHECKING" and frame.get("session") == self.assessment.session:
            if role not in self.current_roles or role in self.checked or not isinstance(frame.get("test"), str) or not 1 <= len(frame["test"]) <= 64 or type(frame.get("pass")) is not bool or type(frame.get("detail")) is not int or not -0x80000000 <= frame["detail"] <= 0x7fffffff:
                self.stop(f"{role}: invalid self-check response", invalid=True)
                return
            good = frame["pass"]
            try:
                if role == "source":
                    onboard = self.peripherals.current_result(frame)
                    good = onboard if onboard is not None else interface_result(frame, len(self.profile["channels"]))
                else:
                    good = interface_result(frame, len(self.profile["channels"]))
            except ValueError as error:
                self.stop(f"{role}: {error}", invalid=True)
                return
            self.check_details.append({"role": role, "test": frame["test"], "pass": good, "detail": frame["detail"]})
            self.note(f"{role}: {frame['test']} {'PASS' if good else 'FAIL'} (detail={frame['detail']})")
        elif kind == "checked" and self.phase == "CHECKING" and frame.get("session") == self.assessment.session:
            if role not in self.current_roles or role in self.checked or type(frame.get("pass")) is not bool:
                self.stop(f"{role}: invalid self-check summary", invalid=True)
                return
            required = {"hrt_clock", "gpio_inputs"} if role == "source" else {"hrt_clock", "gpio_inputs", self.peripherals.probe_check}
            critical = [item for item in self.check_details if item["role"] == role and item["test"] in required]
            # Onboard diagnostics remain red without changing interface eligibility.
            self.checked[role] = frame["pass"] and {item["test"] for item in critical} == required and len(critical) == len(required) and all(item["pass"] for item in critical)
            # CHECK never enables outputs. Collect both boards' full diagnostics
            # before concluding, even when the first device or board fails.
            if not self.current_roles.issubset(self.checked):
                return
            if not all(self.checked[role] for role in self.current_roles):
                self.stop("External interface check failed or incomplete; see check details", invalid=True)
            else:
                self.note("External interface checks passed; onboard diagnostics are independent")
                if self.auto_start:
                    self.begin()
                else:
                    self.phase = "CHECKED"
                    self.state.set("External checks passed — Start repeats them before output; onboard results are independent")
        elif kind == "started" and frame.get("session") == self.assessment.session:
            if role == "probe" and self.phase == "STARTING_PROBE":
                self.last_seen[role] = received_at
                self.phase, self.phase_started = "STARTING_SOURCE", now
                self.transmit("source", f"START {self.assessment.session} {self.digest}")
                # Keep the probe lease alive while waiting for the source ACK.
                self.transmit("probe", f"PING {self.assessment.session}")
                if self.demo:
                    self.events.put(("source", {"type": "started", "session": self.assessment.session}))
            elif role == "source" and self.phase == "STARTING_SOURCE":
                self.phase, self.phase_started, self.last_demo = "RUNNING", now, now
                self.assessment.start_measurements(now)
                self.last_seen[role] = received_at
                self.demo_sequence = 0
                self.note("Waveform output enabled after external interface checks")
        elif kind == "sample" and role == "probe" and self.phase == "RUNNING":
            if self.assessment.accept(frame, received_at):
                self.last_seen[role] = received_at
        elif kind == "heartbeat" and frame.get("session") == self.assessment.session and frame.get("running") is True:
            self.last_seen[role] = received_at
        elif kind == "stopped" and frame.get("session") == self.assessment.session and self.phase in ("RUNNING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE"):
            self.stop(f"{role}: {frame.get('reason', 'endpoint stopped')}", invalid=True)

    def start_demo(self):
        self.demo = True
        self.hello = {"source": True, "probe": True}
        self.identities = {role: {"device_id": f"DEMO_{role.upper()}", "firmware": "SIMULATED"} for role in ("source", "probe")}
        self.note("DEMO ONLY — generated frequencies, no hardware connected")
        self.populate_demo_power()
        self.start()

    def populate_demo_power(self):
        self.peripherals.reset()
        for role, tests in EXPECTED.items():
            for name, expected in tests.items():
                failed = name == "icm45686_spi2" and self.demo_peripheral_fault.get()
                value = DEVICES.get(name, {}).get("demo", expected)
                self.peripherals.power_result(role, {"test": name, "pass": not failed, "detail": 0 if failed else value})
            self.peripherals.finish_power(role, {"pass": all(item["pass"] for item in self.peripherals.power[role].values()),
                                                "count": len(tests), "checked_us": 650000, "uptime_us": 700000})

    def simulate_peripheral_fault(self):
        self.stop("DEMO: onboard power-on check changed", invalid=True)
        self.populate_demo_power()
        self.assessment = Assessment(self.profile)
        self.state.set("DEMO — ICM45686 #2 FAIL recorded independently; external tests remain available" if self.demo_peripheral_fault.get() else "DEMO — power-on checks pass; Start test repeats the pre-test checks")

    def demo_samples(self, now):
        if now - self.last_demo < 1:
            return
        self.last_demo = now
        self.demo_sequence += 1
        self.last_seen = {"source": now, "probe": now}
        for index, row in enumerate(self.profile["channels"]):
            if row.get("method") == "adc_voltage":
                result = self.assessment.results[index]
                result.status, result.reason = "PASS", "DEMO: 0 V / 1.65 V ADC levels"
                result.voltage_low_mv, result.voltage_high_mv = 0, 1650
                for mode, value in zip(ADC_MODES, (0, 1650)):
                    self.i2c_results[mode] = {"status": "PASS", "reason": "DEMO voltage", "adc_voltage_mv": value}
                continue
            frequency = row["expected_hz"] * (1.10 if self.demo_fault.get() and index == 0 else 1)
            edges = max(3, int(frequency) + 1)
            span = round((edges - 1) * 1_000_000 / frequency)
            self.assessment.accept({"type": "sample", "session": self.assessment.session, "seq": self.demo_sequence, "channel": index, "frequency_millihz": (edges - 1) * 1_000_000_000 // span, "edges": edges, "span_us": span, "timing_errors": 0}, now)

    def pump(self):
        if self.closing:
            return
        try:
            now = time.monotonic()
            if self.phase in ("RUNNING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING") and now - self.last_pump_at > self.profile["lease_ms"] / 1000:
                self.stop("GUI stalled beyond the firmware heartbeat lease; restart the test", invalid=True)
            if self.phase == "RUNNING" and self.last_ping and now - self.last_ping > self.profile["lease_ms"] / 1000:
                self.stop("Firmware heartbeat lease elapsed; restart the test", invalid=True)
            if self.phase == "CHECKING" and now - self.phase_started > 20:
                self.stop("Startup self-check timed out", invalid=True)
            self.last_pump_at = now
            for _ in range(500):
                try:
                    role, frame = self.events.get_nowait()
                except queue.Empty:
                    break
                self.handle(role, frame)
            now = time.monotonic()
            if self.phase == "RUNNING":
                if self.demo:
                    self.demo_samples(now)
                if now - self.last_ping > 0.4:
                    for role in ("source", "probe"):
                        self.transmit(role, f"PING {self.assessment.session}")
                    self.last_ping = now
                if any(now - self.last_seen.get(role, 0) > 2.5 for role in ("source", "probe")):
                    self.stop("Endpoint heartbeat timeout", invalid=True)
                else:
                    self.assessment.expire(now)
                    passed = sum(row.status == "PASS" for row in self.assessment.results)
                    failed = sum(row.status == "FAIL" for row in self.assessment.results)
                    self.state.set(("DEMO — " if self.demo else "") + ("GPIO FREQUENCY CHANNELS PASS" if self.assessment.all_passed else f"RUNNING · {passed} PASS · {failed} FAIL"))
                    self.state_label.configure(foreground=RED if failed else GREEN if self.assessment.all_passed else MUTED)
            elif self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE"):
                if self.phase == "STARTING_SOURCE" and now - self.last_ping > 0.4:
                    self.transmit("probe", f"PING {self.assessment.session}")
                    self.last_ping = now
                if now - self.phase_started > (20 if self.phase in ("CHECKING", "CONNECTING") else 5):
                    self.stop("Startup or USB handshake timed out", invalid=True)
            if self.phase == "RUNNING" and getattr(self, "suite_active", False):
                if self.suite_gpio_started is None: self.suite_gpio_started = now
                if now - self.suite_gpio_started >= 6:
                    snapshot = self.report_snapshot(now)
                    self.stop("GPIO measurements completed; starting I2C slave checks")
                    self.gpio_snapshot = snapshot
                    self._start_connector_modes(list(I2C_MODES + ADC_MODES))
                    self.suite_active = self.phase == "I2C_RUNNING"
            elif self.phase == "I2C_RUNNING":
                self.i2c_run.tick(time.monotonic())
                self._update_i2c()
            self.draw_results()
        except (ValueError, RuntimeError) as error:
            self.stop(str(error), invalid=True)
        self.root.after(70, self.pump)

    def draw_results(self):
        self.view.render()

    def report_snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        if self.phase == "RUNNING":
            if now - self.last_pump_at > self.profile["lease_ms"] / 1000 or (self.last_ping and now - self.last_ping > self.profile["lease_ms"] / 1000) or any(now - self.last_seen.get(role, 0) > 2.5 for role in ("source", "probe")):
                self.stop("Measurement or heartbeat expired before export", invalid=True)
            self.assessment.expire(now)
        rows = [{**channel, **asdict(result)} for channel, result in zip(self.profile["channels"], self.assessment.results)]
        metadata = {"utc": datetime.now(timezone.utc).isoformat(), "demo": self.demo, "session": self.assessment.session, "profile_sha256": self.digest, "run_state": self.phase, "startup_checks": deepcopy(self.checked), "startup_check_details": deepcopy(self.check_details), "devices": deepcopy(self.identities), "peripherals": self.peripherals.snapshot(), "all_passed": self.phase == "RUNNING" and self.assessment.all_passed and self.peripherals.passed() and self.peripherals.current_passed}
        metadata["external_passed"] = self.phase == "RUNNING" and self.assessment.all_passed and all(self.checked.get(role) for role in ("source", "probe"))
        metadata["onboard_passed"] = self.peripherals.passed(("source",)) and self.peripherals.current_passed
        if getattr(self, "gpio_snapshot", None) and self.phase in ("I2C_RUNNING", "I2C_COMPLETE", "COMPLETE"):
            rows = deepcopy(self.gpio_snapshot["channels"])
            for index, row in enumerate(rows):
                if row.get("method") == "adc_voltage": row.update(asdict(self.assessment.results[index]))
        results = getattr(self, "i2c_results", {})
        metadata["connector_tests"] = deepcopy(results)
        metadata["i2c_tests"] = deepcopy({k: v for k, v in results.items() if k in I2C_MODES})
        metadata["adc_tests"] = deepcopy({k: v for k, v in results.items() if k in ADC_MODES})
        metadata["adc_passed"] = set(metadata["adc_tests"]) == set(ADC_MODES) and all(r["status"] == "PASS" for r in metadata["adc_tests"].values())
        metadata["test_suite_completed"] = getattr(self, "suite_completed", False)
        metadata["gpio_measurement_completed"] = getattr(self, "gpio_snapshot", None) is not None
        metadata["i2c_passed"] = set(metadata["i2c_tests"]) == set(I2C_MODES) and all(r["status"] == "PASS" for r in metadata["i2c_tests"].values())
        metadata["external_suite_passed"] = metadata["test_suite_completed"] and all(row["status"] == "PASS" for row in rows) and metadata["i2c_passed"] and metadata["adc_passed"]
        metadata["all_passed"] = metadata["external_suite_passed"] and metadata["onboard_passed"]
        return {**metadata, "channels": rows}

    def export(self):
        # Freeze a validated snapshot before a native file dialog can pause Tk.
        report = self.report_snapshot()
        filename = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON report", "*.json"), ("CSV report", "*.csv")], initialfile="pixnova-io-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        if not filename:
            return
        temporary = None
        try:
            csv_report = filename.lower().endswith(".csv")
            destination = Path(filename)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig" if csv_report else "utf-8", newline="",
                                             dir=destination.parent, prefix="." + destination.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                if csv_report:
                    metadata = {key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in report.items() if key != "channels"}
                    data = [{**metadata, **row} for row in report["channels"]]
                    columns = list(dict.fromkeys(key for row in data for key in row))
                    writer = csv.DictWriter(output, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(data)
                else:
                    json.dump(report, output, indent=2, allow_nan=False)
            os.replace(temporary, destination)
            temporary = None
            self.note(f"Report saved: {filename}")
        except (OSError, ValueError) as error:
            messagebox.showerror("Export failed", str(error))
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self):
        self.closing = True
        if getattr(self, "protocol_window", None) and not self.protocol_window.closed:
            self.protocol_window.close()
        for link in self.links.values():
            link.close()
        for link in self.links.values():
            link.join(timeout=0.5)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("profile.json"))
    parser.add_argument("--demo", action="store_true", help="Simulated frequencies; no serial hardware access")
    parser.add_argument("--protocols", action="store_true", help="Open the connector protocol station")
    args = parser.parse_args()
    root = tk.Tk()
    if args.protocols:
        from protocol_app import ProtocolStation
        ProtocolStation(root, args.profile, args.demo)
    else:
        Station(root, args.profile, args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
