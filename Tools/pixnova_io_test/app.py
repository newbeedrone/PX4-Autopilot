#!/usr/bin/env python3
"""Dual-USB PixNova GPIO continuity and frequency test station."""

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
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
from model import Assessment, Result
from peripherals import Peripherals, EXPECTED, DEVICES, display_value, interface_result
from transport import SerialEndpoint
from protocol_model import ProtocolRun, TESTS
from ethernet import EthernetPeer
from workflow import GpioSequence, GpioStop
from completion import CompletionWorker, FirmwareUpdateWorker, PreparationWorker, save_csv

I2C_MODES = tuple(name for name, spec in TESTS.items() if spec.get("kind") == "i2c")
ADC_MODES = tuple(name for name, spec in TESTS.items() if spec.get("kind") == "adc")
IO_MODES = tuple(name for name, spec in TESTS.items() if spec.get("kind") == "io_pwm")
PREPARATION_TIMEOUT = 30  # Slow/unavailable report shares must not trap the station.
SELF_CHECK_DISPLAY_SECONDS = 1.5
ADC_CHANNEL_MODES = {TESTS[mode]["channel"]: tuple(name for name in ADC_MODES if TESTS[name]["channel"] == TESTS[mode]["channel"])
                     for mode in ADC_MODES}


@dataclass
class GpioRetest:
    phase: str
    stage: str | None
    assessment: Assessment
    sequence: GpioSequence | None
    snapshot: dict | None
    report: dict | None
    peripherals: Peripherals
    checked: dict
    check_details: list


class Station:
    def __init__(self, root, profile_path, demo=False):
        self.root, self.profile_path = root, profile_path
        self.profile, self.digest = load_profile(profile_path)
        self.assessment = Assessment(self.profile)
        self.peripherals = Peripherals()
        self.current_roles = set()
        self.capabilities, self.i2c_results = {}, {}
        self.i2c_run, self.i2c_queue = None, []
        self.connector_modes, self.connector_cancelled = (), False
        self.suite_active, self.suite_completed, self.gpio_snapshot = False, False, None
        self.completed_report = None
        self.suite_gpio_started = None
        self.suite_stage, self.extended_suite, self.ethernet = None, False, None
        self.gpio_sequence, self.gpio_stop, self.finish_worker = None, None, None
        self.gpio_retest = None
        self.onboard_retest = False
        self.gpio_followup, self.gpio_stop_reason = None, ""
        self.finish_plan = None
        self.prepare_worker = None
        self.events = queue.Queue(maxsize=4096)
        self.links, self.hello, self.checked = {}, {}, {}
        self.identities = {}
        self.generation, self.check_details = 0, []
        self.initial_check_details = []
        self.last_pump_at = time.monotonic()
        self.last_seen, self.connected_at = {}, {}
        self.phase, self.phase_started, self.last_ping = "DISCONNECTED", 0, 0
        self.auto_start, self.closing, self.demo = False, False, demo
        self.demo_sequence, self.last_demo = 0, 0
        self.state = tk.StringVar(value="Select the serial ports and click Connect USB")
        self.source_port, self.probe_port = tk.StringVar(), tk.StringVar()
        self.demo_fault = tk.BooleanVar(value=False)
        self.demo_peripheral_fault = tk.BooleanVar(value=False)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_after_id = root.after(50, self.pump)
        if demo:
            root.after(150, lambda: self.start_demo() if not self.closing and not self.hello else None)

    def _build(self):
        self.view = Dashboard(self)
        self.source_combo, self.probe_combo = self.view.source_combo, self.view.probe_combo
        self.state_label, self.log = self.view.state_label, self.view.log
        self.refresh_ports()

    def open_protocols(self):
        """Transfer USB ownership after stopping the frequency station."""
        if self.operation_locked(): return
        if self.network_busy():
            self.note("Wait for the Ethernet adapter operation before switching windows."); return
        if getattr(self, "protocol_window", None) and not self.protocol_window.closed:
            self.protocol_window.root.lift()
            return
        if self.phase == "I2C_RUNNING":
            self.stop("Switching to connector protocol tests")
            self.root.after(100, self.open_protocols)
            return
        if self.i2c_run and self.i2c_run.phase == "FAILED" and not self.i2c_run.outputs_stopped:
            self.note("STOP was not confirmed; reconnect USB before switching tests.")
            return
        if self.defer_gpio_stop(self.open_protocols, "Switching to connector protocol tests"): return
        self.stop("Switching to connector protocol tests")
        self.generation += 1
        self.view.network_panel.cancel_test()
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
        if self.demo and self.phase == "STOPPING_GPIO" and command.startswith("STOP "):
            self.events.put((role, {"type": "stopped", "session": command.split()[1], "reason": "requested"}))
            return
        if not self.demo and role in self.links:
            self.links[role].send(command)
        elif self.demo and self.phase == "I2C_RUNNING" and self.i2c_run:
            run = self.i2c_run
            def emit(frame): self.events.put((role, {"session": run.session, **frame}))
            if command.startswith("PCHECK "):
                checks = {key: spec.get("demo", spec["id"]) for key, spec in DEVICES.items()} if role == "source" else {"probe_protocol_firmware": 1}
                checks.update(hrt_clock=5000, gpio_inputs=len(self.profile["channels"]))
                for key, value in checks.items(): emit({"type": "check", "test": key, "pass": True, "detail": value})
                emit({"type": "checked", "mode": run.mode, "pass": True})
            elif command.startswith("PROTO "):
                emit({"type": "started", "mode": run.mode, "role": role})
            elif command.startswith("PING "):
                emit({"type": "heartbeat", "running": True})
            elif command == "STOP" or command.startswith("STOP "):
                emit({"type": "stopped", "reason": "requested"})

    def connect(self):
        if self.phase in ("PREPARING", "FINISHING", "STOPPING_GPIO"):
            self.note("Wait for preparation, output shutdown or firmware restoration before reconnecting."); return
        if getattr(self, "protocol_window", None) and not self.protocol_window.closed:
            self.note("Close the protocol window before reconnecting the frequency station.")
            return
        if self.phase in ("RUNNING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "CONNECTING", "I2C_RUNNING"):
            self.note("Stop the active operation before reconnecting USB endpoints.")
            return
        ports = {}
        if not self.demo:
            ports["source"] = self.source_port.get().strip()
            probe = self.probe_port.get().strip()
            if probe:
                ports["probe"] = probe
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
        self.connector_modes, self.connector_cancelled = (), False
        self.suite_active = self.suite_completed = False
        self.completed_report = None
        self.gpio_snapshot = None
        self.suite_stage, self.extended_suite = None, False
        self.gpio_sequence = self.gpio_stop = None
        self.gpio_retest = None
        self.onboard_retest = False
        self.gpio_followup = None
        self.gpio_stop_reason = ""
        self.finish_plan = None
        self.close_ethernet()
        self.auto_start = False
        self.check_details.clear()
        self.initial_check_details = []
        self.last_seen.clear()
        self.connected_at.clear()
        self.last_ping = 0
        self.phase, self.phase_started = "CONNECTING", time.monotonic()
        self.state.set("Connecting — waiting for power-on peripheral checks")
        self.state_label.configure(foreground=MUTED)
        if hasattr(self, "view"):
            self.view.network_panel.cancel_test()
            self.view.completion.reset_status()
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self.view.filter(None)
            self.view.page.yview_moveto(0)
        if self.demo:
            self.start_demo(auto_start=False)
            return
        for role, device in ports.items():
            self.connected_at[role] = time.monotonic()
            self.links[role] = SerialEndpoint(role, device, self.events, self.generation)
            self.links[role].start()

    def start_full_test(self):
        if self.operation_locked(): return
        if self.network_busy():
            self.note("Wait for the Ethernet adapter operation before testing."); return
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING", "RUNNING"):
            self.note("Wait for the active test or stop it before restarting."); return
        if self.i2c_run and self.i2c_run.phase == "FAILED" and not self.i2c_run.outputs_stopped:
            self.note("STOP was not confirmed; reconnect both USB endpoints before testing."); return
        if not self.demo and not {"source", "probe"}.issubset(self.hello):
            missing = ", ".join("PixNova" if role == "source" else "measurement board" for role in ("source", "probe") if role not in self.hello)
            message = f"Cannot start test: USB not verified for {missing} · Refresh, select the current ports, then click Connect USB"
            self.state.set(message); self.state_label.configure(foreground=RED); self.note(message)
            return
        if not self.demo and (not {"source", "probe"}.issubset(self.hello) or not self.peripherals.interfaces_ready()
                              or not all(getattr(self, "capabilities", {}).get(role, {}).get("i2c_simulators") == 2
                                         and self.capabilities[role].get("adc_levels") == 2
                                         and self.capabilities[role].get("io_pwm") == 1
                                         and self.capabilities[role].get("sbus_out") == 1
                                         and self.capabilities[role].get("session_stop") == 1 for role in ("source", "probe"))):
            self.note("Update and connect both boards with the I2C, ADC, IOV2 PWM and SBUS OUT test firmware before starting the complete test.")
            return
        completion = getattr(getattr(self, "view", None), "completion", None)
        if completion is not None:
            try:
                selection = completion.selection(self.demo)
            except Exception as error:
                self.state.set("Cannot start complete test: " + str(error))
                completion.status.set(str(error)); return
            if selection[0] and not self.demo and self.capabilities.get("source", {}).get("usb_restore") != 1:
                self.state.set("Update the flight-controller test firmware for automatic USB restoration"); return
            self.prepare_selection(selection)
            return
        self.begin_full_test(None)

    def can_update_firmware(self):
        if self.closing or self.suite_active or not self.network_idle() or self.network_busy():
            return False
        stop = self.gpio_stop
        if stop and (not stop.done or stop.error):
            return False
        if self.i2c_run and not self.i2c_run.outputs_stopped:
            return False
        return self.demo or ("source" in self.hello and "source" in self.links
                             and self.capabilities.get("source", {}).get("usb_restore") == 1)

    def update_firmware(self):
        if not self.can_update_firmware():
            self.note("Connect PixNova USB and finish or stop the active test before updating.")
            return
        completion = self.view.completion
        path = completion.path.get().strip()
        if not path and not self.demo:
            path = filedialog.askopenfilename(parent=self.root, filetypes=[("PX4 firmware", "*.px4")])
            if not path or not self.can_update_firmware():
                return
            completion.path.set(path)
        self.view.network_panel.cancel_test()
        self.prepare_selection((path, None, self.demo), manual=True)

    def prepare_selection(self, selection, manual=False):
        self.prepare_origin = self.phase, self.generation, dict(self.identities)
        self.prepare_update = manual
        self.prepare_worker = PreparationWorker(*selection)
        self.prepare_started = time.monotonic()
        self.phase = "PREPARING"
        if manual:
            message = "Validating flight firmware · Stop cancels"
        elif selection[0] and not self.demo:
            message = "Preparing test · validating firmware and CSV folder · Stop cancels"
        else:
            message = "Preparing test · checking CSV folder · Stop cancels"
        self.state.set(message)
        self.view.completion.status.set(message)
        self.prepare_worker.start()

    def begin_full_test(self, finish_plan):
        self.i2c_results = {}; self.gpio_snapshot = None; self.suite_completed = False
        self.start()
        self.suite_active = self.phase == "CHECKING" and self.auto_start
        self.suite_stage, self.extended_suite = ("self_check", True) if self.suite_active else (None, False)
        self.suite_gpio_started = None
        self.finish_plan = finish_plan
        self.gpio_sequence = GpioSequence(self.profile) if self.suite_active else None
        self.gpio_stop = None

    def poll_preparation(self):
        worker = getattr(self, "prepare_worker", None)
        if worker is None: return
        try: event = worker.events.get_nowait()
        except queue.Empty:
            if time.monotonic() - self.prepare_started < PREPARATION_TIMEOUT: return
            event = {"error": "Firmware/CSV preparation timed out; check file and folder availability"}
        self.prepare_worker = None
        phase, generation, identities = self.prepare_origin
        if self.phase != "PREPARING" or self.generation != generation or self.identities != identities:
            return
        self.phase = phase
        completion = self.view.completion
        manual = getattr(self, "prepare_update", False)
        if "error" in event:
            message = ("Cannot update flight firmware: " if manual else "Cannot start complete test: ") + event["error"]
            self.state.set(message); completion.status.set(message)
            self.state_label.configure(foreground=RED)
            return
        if manual:
            if self.can_update_firmware():
                self.begin_firmware_update(event["plan"][0])
            return
        if not {"source", "probe"}.issubset(self.hello) or not self.peripherals.interfaces_ready():
            message = "USB disconnected during preparation; reconnect before testing"
            self.state.set(message); completion.status.set(message)
            self.state_label.configure(foreground=RED)
            return
        completion.prepared(event["plan"], self.demo)
        self.begin_full_test(event["plan"])

    def operation_locked(self):
        if self.phase == "PREPARING":
            self.note("Validating firmware and CSV folder in the background; Stop cancels."); return True
        if self.phase == "FINISHING":
            self.note("Saving results or restoring firmware; wait for completion."); return True
        pending = getattr(self, "gpio_stop", None)
        if pending and (not pending.done or pending.error):
            self.note("GPIO STOP is pending or failed; confirm shutdown or reconnect USB."); return True
        return False

    def start_can(self):
        self._start_connector_modes(["CAN"])

    def retest_idle(self):
        if not self.network_idle() or self.network_busy():
            return False
        if self.gpio_stop and (not self.gpio_stop.done or self.gpio_stop.error):
            return False
        return self.i2c_run is None or self.i2c_run.outputs_stopped

    def can_retest_onboard(self):
        roles = set(self.hello)
        return (self.retest_idle() and not self.suite_active and "source" in roles
                and roles.issubset(self.peripherals.summaries))

    def retest_onboard(self):
        if not self.can_retest_onboard():
            return
        self.onboard_retest = True
        self.auto_start = False
        self.self_check(preserve_results=True)

    def can_retest_gpio(self, group):
        if not (self.retest_idle() and {"source", "probe"}.issubset(self.hello)
                and self.peripherals.interfaces_ready()):
            return False
        return not self.suite_active or (self.phase in ("WAITING_ETH", "WAITING_IOV2")
                                         and self.gpio_sequence is not None and group in self.gpio_sequence.completed)

    def retest_gpio(self, group):
        if not self.can_retest_gpio(group):
            self.note("Retest requires both USB endpoints and idle outputs.")
            return
        sequence = GpioSequence(self.profile, [group])
        self.gpio_retest = GpioRetest(self.phase, getattr(self, "suite_stage", None), self.assessment,
                                      self.gpio_sequence, self.gpio_snapshot, self.completed_report,
                                      self.peripherals, self.checked, self.check_details)
        self.assessment = Assessment(self.profile)
        self.peripherals = deepcopy(self.peripherals)
        self.checked, self.check_details = {}, []
        self.gpio_sequence = sequence
        self.gpio_snapshot = self.completed_report = None
        self.suite_active, self.auto_start, self.suite_stage = True, True, "gpio_retest"
        self.self_check(preserve_suite=True)
        if self.phase == "CHECKING":
            selected = set(sequence.active_indices)
            for index, result in enumerate(self.gpio_retest.assessment.results):
                if index not in selected:
                    self.assessment.results[index] = deepcopy(result)

    def finish_gpio_retest(self, measured):
        previous, self.gpio_retest = self.gpio_retest, None
        sequence = self.gpio_sequence
        snapshot = previous.snapshot
        if measured:
            group = sequence.groups[0]
            for index in sequence.indices[group]:
                result = self.assessment.results[index]
                previous.assessment.results[index] = result
                if previous.snapshot is not None:
                    previous.snapshot["channels"][index].update(asdict(result))
            if previous.sequence is not None and group in previous.sequence.indices:
                previous.sequence.completed[group] = sequence.completed[group]
                sequence = previous.sequence
            snapshot = previous.snapshot or self.gpio_snapshot
            rows = sequence.completed[group]

        self.assessment, self.peripherals = previous.assessment, previous.peripherals
        self.checked, self.check_details = previous.checked, previous.check_details
        self.gpio_sequence = sequence if measured else previous.sequence
        self.gpio_snapshot = snapshot
        stop_failed = self.gpio_stop is not None and bool(self.gpio_stop.error)
        self.completed_report = None if measured or stop_failed else previous.report
        self.suite_completed = self.completed_report is not None
        resume = measured and previous.phase in ("WAITING_ETH", "WAITING_IOV2")
        self.auto_start, self.suite_active = False, resume
        self.suite_stage = previous.stage if resume else None
        if not resume:
            self.finish_plan = None
        if measured:
            self.phase = previous.phase if resume else "GPIO_COMPLETE"
            passed = sum(row["status"] == "PASS" for row in rows)
            message = f"{group} retest · {passed}/{len(rows)} PASS · outputs stopped"
            if self.phase == "WAITING_IOV2":
                message += " · move P21 to IOV2 PWM, then click Confirm IOV2 wiring"
            elif self.phase == "WAITING_ETH":
                message += " · click Retest Ethernet to continue"
            self.state.set(message)
            self.state_label.configure(foreground=GREEN if passed == len(rows) else RED)

    def start_gpio_groups(self, groups):
        sequence = GpioSequence(self.profile, groups)
        if self.operation_locked(): return
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Wait for the active operation or stop it before selecting GPIO connectors."); return
        selected_groups = tuple(sequence.groups)
        if self.defer_gpio_stop(lambda: self.start_gpio_groups(selected_groups), "Selecting GPIO connectors"): return
        self.start()
        if self.phase != "CHECKING" or not self.auto_start: return
        self.finish_plan = None
        self.gpio_sequence = sequence
        self.suite_active, self.extended_suite, self.suite_stage = True, False, "gpio_only"
        selected = set(i for indices in sequence.indices.values() for i in indices)
        for index, result in enumerate(self.assessment.results):
            if index not in selected:
                result.status, result.reason = "NOT RUN", "Outside selected GPIO connector scope"

    def gpio_indices(self):
        sequence = getattr(self, "gpio_sequence", None)
        return sequence.active_indices if sequence is not None and self.suite_active else None

    def advance_gpio_sequence(self, now):
        sequence = self.gpio_sequence
        finished = sequence.tick(self.assessment, now)
        if finished or (self.suite_stage == "gpio" and sequence.group == "FMU PWM"):
            snapshot = self.report_snapshot(now)
            if self.phase != "RUNNING":
                return
            self.gpio_snapshot = snapshot
            self.phase = "STOPPING_GPIO"
            self.gpio_stop_reason = ""
            self.gpio_stop = GpioStop(self.assessment.session, now, self.transmit)
            self.state.set("GPIO measurements finished · confirming both endpoints stopped")
            return
        self.state.set(f"Automatic test · GPIO {sequence.position + 1}/{len(sequence.groups)} · {sequence.group}")
        if hasattr(self, "view") and self.view.filter_group != sequence.group:
            self.view.filter(sequence.group)

    def defer_gpio_stop(self, action, reason):
        if self.phase not in ("RUNNING", "STARTING_PROBE", "STARTING_SOURCE"): return False
        self.stop(reason)
        self.gpio_followup = action
        return True

    def poll_gpio_stop(self, now):
        if self.phase != "STOPPING_GPIO": return
        self.gpio_stop.tick(now)
        if not self.gpio_stop.done: return
        self.phase = "STOPPED"
        followup, self.gpio_followup = getattr(self, "gpio_followup", None), None
        if getattr(self, "gpio_retest", None):
            measured = self.suite_active and not self.gpio_stop.error
            self.finish_gpio_retest(measured)
            if measured:
                return
        if self.gpio_stop.error:
            self.suite_active = self.auto_start = False
            origin = getattr(self, "gpio_stop_reason", "")
            if origin and origin != self.gpio_stop.error:
                self.gpio_stop.error = f"{origin} · {self.gpio_stop.error}"
            self.assessment.mark_unmeasured(self.gpio_stop.error)
            self.state.set(self.gpio_stop.error)
            self.state_label.configure(foreground=RED)
        elif followup is not None:
            self.suite_active = self.auto_start = False
            self.state.set("GPIO outputs stopped (confirmed)")
            generation, stopped = self.generation, self.gpio_stop
            def resume():
                if not self.closing and self.generation == generation and self.phase == "STOPPED" and self.gpio_stop is stopped:
                    followup()
            self.root.after(0, resume)
        elif self.suite_active:
            if self.suite_stage == "gpio_only":
                self.suite_active = self.auto_start = False
                self.phase = "GPIO_COMPLETE"
                rows = [row for results in self.gpio_sequence.completed.values() for row in results]
                passed = sum(row["status"] == "PASS" for row in rows)
                self.state.set(f"SELECTED GPIO TESTS COMPLETE · {passed}/{len(rows)} PASS · outputs stopped (confirmed)")
                self.state_label.configure(foreground=GREEN if passed == len(rows) else RED)
            elif self.suite_stage == "gpio":
                self.phase = "WAITING_ETH"
                self.auto_start = False
                if hasattr(self, "view"):
                    self.view.select_connector_group("Ethernet")
                self.state.set("Other interfaces complete · testing Ethernet before FMU PWM")
                self.state_label.configure(foreground=MUTED)
                self.root.after(100, lambda: self.start_ethernet() if self.phase == "WAITING_ETH" and self.suite_active else None)
            elif self.suite_stage == "fmu":
                self.phase = "WAITING_IOV2"
                self.auto_start = False
                if hasattr(self, "view"):
                    self.view.select_connector_group("IOV2 PWM")
                rows = self.gpio_sequence.completed["FMU PWM"]
                passed = sum(row["status"] == "PASS" for row in rows)
                self.state.set(f"FMU PWM finished · {passed}/{len(rows)} PASS · outputs stopped · move P21 to IOV2 PWM, then click Confirm IOV2 wiring")
                self.state_label.configure(foreground=MUTED if passed == len(rows) else RED)
        else:
            self.state.set((getattr(self, "gpio_stop_reason", "") or "Cancelled") + " · GPIO outputs stopped (confirmed)")

    def finish_suite(self):
        if getattr(self, "finish_plan", None) is None: return
        image, directory = self.finish_plan
        self.finish_plan = None
        source = getattr(self.links.get("source"), "device", self.source_port.get())
        probe = getattr(self.links.get("probe"), "device", self.probe_port.get())
        worker = CompletionWorker(self.completed_report, directory, image, source, probe, self.demo)
        message = "Measurements finished · saving CSV"
        if image is not None:
            message += " before firmware restoration"
        self.start_finish_worker(worker, message, release_usb=image is not None or self.demo)

    def begin_firmware_update(self, image):
        source_id = self.identities.get("source", {}).get("device_id", "")
        source = getattr(self.links.get("source"), "device", "")
        probe = getattr(self.links.get("probe"), "device", "")
        if not self.demo:
            if not messagebox.askokcancel("Update Firmware", f"Install {image.path.name} on PixNova {source} ({source_id[-8:]})?",
                                          parent=self.root):
                self.view.completion.status.set("Firmware update cancelled")
                self.state.set("Firmware update cancelled")
                return
            if not self.can_update_firmware() or self.identities.get("source", {}).get("device_id") != source_id:
                return
        worker = FirmwareUpdateWorker(image, source, probe, source_id, self.demo)
        self.start_finish_worker(worker, "Starting flight firmware update", release_usb=not self.demo)

    def start_finish_worker(self, worker, message, release_usb):
        self.finish_worker = worker
        self.phase = "FINISHING"
        self.state.set(message)
        if hasattr(self, "view"):
            self.view.completion.status.set(message)
        self.state_label.configure(foreground=MUTED)
        if release_usb:
            self.generation += 1
            for link in self.links.values(): link.close()
            for link in self.links.values(): link.join(timeout=.5)
            self.links.clear()
            self.hello.clear()
        self.finish_worker.start()

    def poll_completion(self):
        worker = getattr(self, "finish_worker", None)
        if worker is None: return
        while True:
            try: event = worker.events.get_nowait()
            except queue.Empty: break
            if "progress" in event:
                self.state.set(event["progress"])
                self.view.completion.status.set(event["progress"])
            if "update_status" in event:
                self.finish_worker = None
                self.phase = "COMPLETE" if self.completed_report is not None else "STOPPED"
                self.state.set(event["update_status"])
                self.view.completion.status.set(event["update_status"])
                self.state_label.configure(foreground=GREEN if event["success"] else RED)
                self.note(event["update_status"])
                return
            if event.get("done"):
                self.completed_report = event["report"]
                self.phase = "COMPLETE"
                message = self.show_completion()
                self.view.completion.status.set(message + " · " + event["csv"])
                self.finish_worker = None

    def start_i2c(self, mode=None):
        if mode is not None and mode not in I2C_MODES:
            raise ValueError("Invalid I2C connector")
        self._start_connector_modes([mode] if mode else list(I2C_MODES))

    def start_adc(self, channel_id=None):
        if channel_id is not None and channel_id not in ADC_CHANNEL_MODES:
            raise ValueError("Invalid ADC input")
        self._start_connector_modes(list(ADC_CHANNEL_MODES[channel_id] if channel_id else ADC_MODES))

    def start_iov2(self, mode=None):
        if mode is not None and mode not in IO_MODES:
            raise ValueError("Invalid IOV2 PWM output")
        resume = mode is None and self.phase == "WAITING_IOV2" and self.suite_active
        if self.suite_active and not resume:
            self.note("Finish FMU PWM and confirm the P21 change before testing IOV2.")
            return
        self._start_connector_modes([mode] if mode else list(IO_MODES))
        if resume and self.phase == "I2C_RUNNING": self.suite_stage = "iov2"

    def start_sbus_out(self):
        self._start_connector_modes(["SBUS_OUT"])

    def network_idle(self):
        child = getattr(self, "protocol_window", None)
        return self.phase not in ("PREPARING", "RUNNING", "CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING", "STOPPING_GPIO", "FINISHING") and not (child and not child.closed)

    def network_busy(self):
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        return panel is not None and panel.changing

    def start_ethernet(self, interface_index=None):
        if self.operation_locked(): return
        if self.network_busy():
            self.note("Wait for the Ethernet adapter operation before testing."); return
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Wait for the active operation before Ethernet testing."); return
        resume = self.phase == "WAITING_ETH" and self.suite_active
        if self.suite_active and not resume:
            self.note("Finish the other interfaces before testing Ethernet.")
            return
        if self.demo:
            self._start_connector_modes(["ETH"])
            if resume and self.phase == "I2C_RUNNING": self.suite_stage = "ethernet"
            return
        if not self.network_idle():
            self.note("Stop the active test before Ethernet testing."); return
        if "source" not in self.hello or not self.peripherals.interfaces_ready(("source",)):
            self.note("Connect and verify the flight controller USB endpoint before Ethernet testing."); return
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        if panel is not None and interface_index is None:
            generation = self.generation
            panel.prepare(lambda index: self.start_ethernet(index) if not self.closing and generation == self.generation else None)
            return
        try:
            peer = EthernetPeer(interface_index) if interface_index is not None else EthernetPeer()
        except ValueError as error:
            self.state.set(str(error)); self.note(str(error)); return
        self.close_ethernet()
        self.ethernet = peer
        self._start_connector_modes(["ETH"])
        if self.phase != "I2C_RUNNING": self.close_ethernet()
        elif resume: self.suite_stage = "ethernet"

    def close_ethernet(self):
        if getattr(self, "ethernet", None) is not None:
            self.ethernet.close()
            self.ethernet = None

    def _start_connector_modes(self, modes):
        if self.operation_locked(): return
        if self.network_busy():
            self.note("Wait for the Ethernet adapter operation before testing."); return
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        if panel: panel.cancel_test()
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Wait for the active check or stop it before another test."); return
        if self.i2c_run and self.i2c_run.phase == "FAILED" and not self.i2c_run.outputs_stopped:
            self.note("STOP was not confirmed; reconnect both USB endpoints before testing."); return
        roles = set().union(*(set(TESTS[mode]["roles"]) for mode in modes))
        if not roles.issubset(self.hello) or not self.peripherals.interfaces_ready(roles):
            self.note("Connect and verify the required USB endpoints before testing."); return
        if any(mode in I2C_MODES + ADC_MODES for mode in modes) and not all(getattr(self, "capabilities", {}).get(role, {}).get("i2c_simulators") == 2 for role in roles):
            self.note("Update both boards with the final I2C simulator firmware."); return
        if any(mode in IO_MODES for mode in modes) and not all(self.capabilities.get(role, {}).get("io_pwm") == 1 and self.capabilities[role].get("session_stop") == 1 for role in roles):
            self.note("Update both boards with the IOV2 PWM/session STOP firmware."); return
        if "SBUS_OUT" in modes and not all(self.capabilities.get(role, {}).get("sbus_out") == 1 and self.capabilities[role].get("session_stop") == 1 for role in roles):
            self.note("Update both boards with the SBUS OUT / PG5 test firmware."); return
        if self.phase in ("WAITING_IOV2", "WAITING_ETH") and modes != (list(IO_MODES) if self.phase == "WAITING_IOV2" else ["ETH"]):
            self.note("Stop the complete workflow before selecting a different test."); return
        if any(mode in ADC_MODES for mode in modes) and not all(self.capabilities[role].get("adc_levels") == 2 for role in ("source", "probe")):
            self.note("Update both boards to the ADC voltage test firmware."); return
        if self.phase == "RUNNING":
            snapshot = self.report_snapshot()
            if self.phase != "RUNNING": return
            self.gpio_snapshot = snapshot
        if self.defer_gpio_stop(lambda: self._start_connector_modes(modes), "Switching from GPIO measurements to connector tests"): return
        self.completed_report = None
        self.suite_completed = False
        self.connector_modes, self.connector_cancelled = tuple(modes), False
        self.i2c_queue = list(modes)
        for mode in modes:
            self.i2c_results.pop(mode, None)
        if any(mode in ADC_MODES for mode in modes): self._sync_adc_result()
        self._next_i2c()

    def _next_i2c(self):
        mode = self.i2c_queue.pop(0)
        self.peripherals.begin_current()
        roles = TESTS[mode]["roles"]
        self.i2c_run = ProtocolRun(mode, self.digest, time.monotonic(), self.transmit,
                                   session_stop=all(self.capabilities.get(role, {}).get("session_stop") == 1 for role in roles))
        self.phase = "I2C_RUNNING"
        self.i2c_results[mode] = self.i2c_run.snapshot()
        self.state.set(TESTS[mode]["name"] + " · fresh startup checks")
        if hasattr(self, "view"):
            self.view.select_connector_group("CAN" if mode == "CAN" else "Ethernet" if mode == "ETH" else
                                             "IOV2 PWM" if mode in IO_MODES + ("SBUS_OUT",) else "I2C / ADC")
        self.i2c_run.begin()

    def _update_i2c(self):
        run = self.i2c_run
        self.i2c_results[run.mode] = run.snapshot()
        if run.mode in ADC_MODES: self._sync_adc_result()
        self.state.set(TESTS[run.mode]["name"] + " · " + run.result.reason)
        if run.phase not in ("COMPLETE", "FAILED", "STOPPED"): return
        if run.mode == "ETH": self.close_ethernet()
        self.peripherals.end_current()
        if not run.outputs_stopped or self.connector_cancelled:
            self.i2c_queue.clear()
            self.suite_active = self.suite_completed = self.auto_start = False
            self.phase = "STOPPED"
            self.state_label.configure(foreground=RED if run.result.status == "FAIL" else MUTED)
            return
        if self.i2c_queue:
            self._next_i2c()
        else:
            stage = getattr(self, "suite_stage", None)
            if self.suite_active and stage in ("fixed", "ethernet"):
                self.phase = "CHECKED"
                self.suite_stage = "gpio" if stage == "fixed" else "fmu"
                self.auto_start = True
                self.self_check(preserve_suite=True)
                return
            self.phase = "COMPLETE" if self.suite_active else "I2C_COMPLETE"
            self.suite_completed = self.suite_active
            self.suite_active = False
            self.auto_start = False
            if self.suite_completed:
                self.completed_report = self.report_snapshot()
                self.show_completion()
                self.finish_suite()
            else:
                passed = sum(self.i2c_results.get(mode, {}).get("status") == "PASS" for mode in self.connector_modes)
                heading = "IOV2 TESTS COMPLETE" if all(mode in IO_MODES for mode in self.connector_modes) else "ETHERNET TEST COMPLETE" if self.connector_modes == ("ETH",) else "SELECTED TESTS COMPLETE"
                self.state.set(f"{heading} · {passed}/{len(self.connector_modes)} PASS · outputs stopped (confirmed)")
                self.state_label.configure(foreground=GREEN if passed == len(self.connector_modes) else RED)

    def show_completion(self, notice=""):
        report = self.completed_report
        passed = sum(row["status"] == "PASS" for row in report["channels"])
        checks = report["peripherals"]["latest"]
        onboard = sum(row["status"] == "PASS" for row in checks.values())
        external = "PASS" if report["external_suite_passed"] else "FAIL"
        connector_passed = sum(row["status"] == "PASS" for key, row in report["connector_tests"].items() if key in I2C_MODES + ADC_MODES)
        text = (f"TEST COMPLETE · interfaces {external}: {passed}/{len(report['channels'])} signals, "
                f"{connector_passed}/{len(I2C_MODES + ADC_MODES)} I2C/ADC checks · "
                f"onboard {onboard}/{len(checks)} PASS · outputs stopped")
        if report.get("extended_suite"):
            io_passed = sum(report["connector_tests"].get(mode, {}).get("status") == "PASS" for mode in IO_MODES)
            text += f" · IOV2 {io_passed}/8 · SBUS OUT {'PASS' if report['sbus_out_passed'] else 'FAIL'} · Ethernet {'PASS' if report['ethernet_passed'] else 'FAIL'}"
            text += " · CAN " + ("PASS" if report.get("can_passed") else "FAIL")
        post = report.get("post_test", {})
        finish_failed = post.get("status") == "FAILED"
        message = ""
        if finish_failed:
            message = "Finish failed: " + post["error"]
        elif post.get("status") == "COMPLETE":
            restoration = post.get("restoration", {}).get("status")
            if restoration == "SKIPPED":
                message = "CSV saved · measurement-only run completed"
            else:
                message = "CSV saved · " + ("DEMO" if self.demo else "flight firmware restored · CRC and reboot confirmed")
        if message:
            text += " · " + message
        if notice:
            text += f" · {notice}; completed results retained"
        self.state.set(text)
        self.state_label.configure(foreground=GREEN if report["all_passed"] and not finish_failed else RED)
        return message

    def adc_summary(self, channel_id):
        modes = ADC_CHANNEL_MODES[channel_id]
        results = [self.i2c_results.get(mode, {}) for mode in modes]
        statuses = [item.get("status", "NOT RUN") for item in results]
        status = "FAIL" if "FAIL" in statuses else "PASS" if all(s == "PASS" for s in statuses) else "VERIFYING" if any(s != "NOT RUN" for s in statuses) else "NOT RUN"
        return {"status": status, "low_mv": results[0].get("adc_voltage_mv"), "high_mv": results[1].get("adc_voltage_mv"),
                "reason": "; ".join(f"{TESTS[mode]['name']}: {item.get('reason', 'Not run')}" for mode, item in zip(modes, results))}

    def _sync_adc_result(self):
        for row, result in zip(self.profile["channels"], self.assessment.results):
            if row.get("method") == "adc_voltage":
                summary = self.adc_summary(row["id"])
                result.status, result.reason = summary["status"], summary["reason"]
                result.voltage_low_mv, result.voltage_high_mv = summary["low_mv"], summary["high_mv"]

    def self_check(self, preserve_suite=False, preserve_results=False):
        if self.operation_locked(): return
        if self.network_busy():
            self.note("Wait for the Ethernet adapter operation before testing."); return
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        if panel: panel.cancel_test()
        if self.phase == "I2C_RUNNING":
            self.stop("Stopping connector test before self-check")
            return
        if self.i2c_run and self.i2c_run.phase == "FAILED" and not self.i2c_run.outputs_stopped:
            self.note("STOP was not confirmed; reconnect both USB endpoints before testing."); return
        if self.phase in ("CHECKING", "CONNECTING", "STARTING_PROBE", "STARTING_SOURCE"):
            self.note("Startup is already in progress.")
            return
        if self.defer_gpio_stop(self.self_check, "Stopping GPIO before self-check"): return
        if "source" not in self.hello or not set(self.hello).issubset(self.peripherals.summaries):
            self.note("Read the flight controller's power-on results before self-check.")
            return
        if preserve_results:
            self.completed_report = None
            self.suite_completed = False
            self.initial_check_details = []
            self.assessment.session = uuid.uuid4().hex
        elif not preserve_suite:
            self.completed_report = None
            self.suite_completed = False
            self.suite_active = False
            self.suite_stage = None
            self.extended_suite = False
            self.gpio_snapshot = None
            self.gpio_sequence = self.gpio_stop = None
            self.i2c_results.clear()
            self.connector_modes = ()
            self.i2c_run = None
            self.initial_check_details = []
        if not preserve_results:
            self.assessment.reset(uuid.uuid4().hex)
        if preserve_suite:
            sequence = self.gpio_sequence
            for group, rows in sequence.completed.items():
                for index, row in zip(sequence.indices[group], rows):
                    self.assessment.results[index] = Result(**row)
            sequence.started_at = None
            self._sync_adc_result()
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
        if self.operation_locked(): return
        if self.network_busy():
            self.note("Wait for the Ethernet adapter operation before testing."); return
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        if panel: panel.cancel_test()
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "I2C_RUNNING"):
            self.note("Startup is already in progress; wait for it to finish.")
            return
        if len(self.hello) != 2 or not self.peripherals.interfaces_ready():
            self.note("Complete both USB handshakes and verify the probe before GPIO output.")
            return
        if self.defer_gpio_stop(self.start, "Restarting test"): return
        self.auto_start = True
        self.self_check()

    def power_ready(self):
        roles = set(self.links)
        if self.phase != "CONNECTING" or not roles or not roles.issubset(self.hello) or not roles.issubset(self.peripherals.summaries):
            return
        ready = self.peripherals.interfaces_ready(roles)
        onboard = self.peripherals.passed(("source",))
        self.phase = ("READY" if len(roles) == 2 else "SOURCE_READY") if ready else "POWER_FAILED"
        if ready:
            message = "USB ready · " + ("click Start test" if len(roles) == 2 else "connect probe for GPIO testing")
            if not onboard:
                message += " · power-on faults recorded"
        else:
            message = "USB/probe validation failed — outputs disabled"
        self.state.set(("DEMO — " if self.demo else "") + message)
        self.state_label.configure(foreground=GREEN if ready else RED)

    def begin(self):
        self.phase, self.phase_started = "STARTING_PROBE", time.monotonic()
        self.state.set("Starting probe in GPIO receive mode before enabling source outputs")
        self.transmit("probe", f"START {self.assessment.session} {self.digest}")
        if self.demo:
            self.events.put(("probe", {"type": "started", "session": self.assessment.session}))

    def stop(self, reason="Stopped by operator", invalid=False):
        if self.phase == "FINISHING":
            self.note("Firmware restoration is in progress; wait for CRC verification."); return
        if self.phase == "STOPPING_GPIO":
            self.suite_active = self.auto_start = False
            self.gpio_followup = None
            if invalid and not self.gpio_stop.error:
                self.gpio_stop.done, self.gpio_stop.error = True, reason
            self.assessment.mark_unmeasured(self.gpio_stop.error or reason)
            self.note(reason + " · waiting for GPIO STOP confirmation"); return
        self.prepare_worker = None
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        if panel: panel.cancel_test()
        child = getattr(self, "protocol_window", None)
        if child is not None and not child.closed:
            child.stop(reason)
            self.state.set("Connector protocol tests · " + child.status.get())
            self.note(reason)
            return
        pretest = self.phase in ("CONNECTING", "CHECKING")
        if self.phase in ("RUNNING", "STARTING_PROBE", "STARTING_SOURCE"):
            if invalid and self.phase == "RUNNING":
                indices = self.gpio_indices()
                if indices is None:
                    indices = [i for i, row in enumerate(self.profile["channels"]) if row.get("method") != "adc_voltage"]
                self.assessment.invalidate(reason, indices)
            self.assessment.mark_unmeasured(reason)
            self.suite_active = self.suite_completed = self.auto_start = False
            self.finish_plan = None
            self.gpio_followup, self.gpio_stop_reason = None, reason
            self.phase = "STOPPING_GPIO"
            self.gpio_stop = GpioStop(self.assessment.session, time.monotonic(), self.transmit)
            self.state.set(reason + " · waiting for both GPIO STOP confirmations")
            self.state_label.configure(foreground=RED if invalid else MUTED)
            self.note(reason)
            return
        if getattr(self, "i2c_run", None) and self.i2c_run.phase not in ("COMPLETE", "FAILED", "STOPPED"):
            self.connector_cancelled = True
            self.i2c_queue.clear()
            self.suite_active = self.auto_start = False
            self.i2c_run.stop(reason, failed=invalid, now=time.monotonic())
            self._update_i2c()
            self.note(reason)
            return
        self.i2c_queue = []
        self.suite_active = False
        for role in tuple(self.links):
            try:
                self.transmit(role, "STOP")
            except RuntimeError:
                pass
        if self.phase == "COMPLETE" and getattr(self, "completed_report", None):
            self.show_completion(reason)
            self.note(reason)
            return
        self.phase, self.auto_start = "STOPPED", False
        if pretest:
            self.peripherals.end_current()
            if invalid and not getattr(self, "onboard_retest", False):
                self.assessment.mark_unmeasured(reason)
        self.state.set(("DEMO — " if self.demo else "") + reason + " · STOP requested")
        self.state_label.configure(foreground=RED if invalid else MUTED)
        self.note(reason)
        if getattr(self, "gpio_retest", None):
            self.finish_gpio_retest(measured=False)
        self.onboard_retest = False

    def handle(self, role, frame):
        if frame.get("_connection", self.generation) != self.generation:
            return
        kind = frame["type"]
        if self.phase in ("DISCONNECTED", "STOPPED"):
            # Keep the original stop cause while the USB workers drain.
            if kind in ("hello", "link_error", "disconnected"):
                self.hello.pop(role, None)
            return
        if self.phase == "FINISHING":
            if kind in ("link_error", "disconnected"):
                self.hello.pop(role, None)
            return
        if kind in ("power_check", "power_checked") and (self.phase != "CONNECTING" or role not in self.hello):
            # A prior HELLO reply can leave power-on frames in the USB queue.
            return
        now = time.monotonic()
        received_at = frame.get("_received_at", now)
        if self.phase == "STOPPING_GPIO" and kind not in ("hello", "link_error", "disconnected"):
            if kind == "stopped" and type(received_at) in (int, float):
                self.gpio_stop.handle(role, frame, now)
            return
        protocol_frame = (self.phase == "I2C_RUNNING"
                          and kind not in ("hello", "power_check", "power_checked", "link_error", "disconnected"))
        fresh = type(received_at) in (int, float) and 0 <= now - received_at <= self.profile["lease_ms"] / 1000
        if not fresh and not (protocol_frame and self.i2c_run.phase == "STOPPING"):
            self.stop(f"{role}: stale USB response; restart the test", invalid=True)
            return
        if self.phase == "COMPLETE" and getattr(self, "completed_report", None) and kind not in ("hello", "error", "link_error", "disconnected"):
            return
        if protocol_frame:
            if (self.i2c_run.phase == "CHECKING" and kind == "check" and role == "source"
                    and frame.get("session") == self.i2c_run.session):
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
            self.capabilities[role] = {key: frame.get(key) for key in ("i2c_simulators", "adc_levels", "io_pwm", "session_stop", "sbus_out", "usb_restore")}
            self.identities[role] = {"device_id": device_id, "firmware": firmware}
            self.hello[role] = True
            self.last_seen[role] = received_at
            self.note(f"{role}: ID={device_id}, firmware={firmware}; profile verified")
            self.power_ready()
        elif kind in ("power_check", "power_checked"):
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
                if self.suite_active and self.suite_stage == "self_check":
                    self.peripherals.end_current()
                    self.initial_check_details = [dict(row, stage="INITIAL SELF-CHECK") for row in self.check_details]
                    passed = sum(row["status"] == "PASS" for row in self.peripherals.latest.values())
                    self.suite_stage = "self_check_results"
                    self.phase_started = now
                    self.state.set(f"SELF-CHECK COMPLETE · onboard {passed}/{len(DEVICES)} PASS · starting connector tests")
                    self.state_label.configure(foreground=GREEN if self.peripherals.current_passed else RED)
                elif getattr(self, "onboard_retest", False):
                    self.onboard_retest = False
                    self.peripherals.end_current()
                    self.phase = "CHECKED"
                    passed = sum(row["status"] == "PASS" for row in self.peripherals.latest.values())
                    self.state.set(f"ONBOARD RETEST COMPLETE · {passed}/{len(DEVICES)} PASS")
                    self.state_label.configure(foreground=GREEN if self.peripherals.current_passed else RED)
                elif self.auto_start:
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
            indices = self.gpio_indices()
            if (indices is None or frame.get("channel") in indices) and self.assessment.accept(frame, received_at):
                self.last_seen[role] = received_at
        elif kind == "heartbeat" and frame.get("session") == self.assessment.session and frame.get("running") is True:
            self.last_seen[role] = received_at
        elif kind == "stopped" and frame.get("session") == self.assessment.session and self.phase in ("RUNNING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE"):
            self.stop(f"{role}: {frame.get('reason', 'endpoint stopped')}", invalid=True)

    def start_demo(self, auto_start=True):
        self.demo = True
        self.hello = {"source": True, "probe": True}
        self.identities = {role: {"device_id": f"DEMO_{role.upper()}", "firmware": "SIMULATED"} for role in ("source", "probe")}
        self.capabilities = {role: {"i2c_simulators": 2, "adc_levels": 2, "io_pwm": 1, "session_stop": 1, "sbus_out": 1} for role in ("source", "probe")}
        self.note("DEMO ONLY — generated frequencies, no hardware connected")
        self.populate_demo_power()
        if auto_start:
            self.start()
        else:
            self.phase = "READY"
            self.state.set("DEMO ready · click Start test")

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
            indices = self.gpio_indices()
            if indices is not None and index not in indices: continue
            if row.get("method") == "adc_voltage":
                result = self.assessment.results[index]
                modes = ADC_CHANNEL_MODES[row["id"]]
                high = TESTS[modes[1]]["expected_mv"]
                result.status, result.reason = "PASS", "DEMO: simulated ADC voltage levels"
                result.voltage_low_mv, result.voltage_high_mv = 0, high
                for mode, value in zip(modes, (0, high)):
                    self.i2c_results[mode] = {"status": "PASS", "reason": "DEMO voltage", "adc_voltage_mv": value}
                continue
            frequency = row["expected_hz"] * (1.10 if self.demo_fault.get() and index == 0 else 1)
            edges = max(3, int(frequency) + 1)
            span = round((edges - 1) * 1_000_000 / frequency)
            self.assessment.accept({"type": "sample", "session": self.assessment.session, "seq": self.demo_sequence, "channel": index, "frequency_millihz": (edges - 1) * 1_000_000_000 // span, "edges": edges, "span_us": span, "timing_errors": 0}, now)

    def demo_protocol_samples(self, now):
        run = self.i2c_run
        if run.phase != "RUNNING" or now - self.last_demo < 1: return
        self.last_demo = now
        spec = TESTS[run.mode]
        if run.mode == "ETH":
            run.host_rx += 20; run.last_host = now; run.result.host_rx = run.host_rx
        for role in (name for name in ("probe", "source") if name in run.started):
            previous = run.samples.get(role, {})
            detail = spec["stimulus_mv" if role == "probe" else "expected_mv"] if spec.get("kind") == "adc" else round(spec["expected_hz"] * 1000) if spec.get("kind") in ("io_pwm", "sbus_out") else 1 if run.mode == "ETH" else 3 if run.mode == "CAN" else 0
            run.handle(role, {"type": "protocol_sample", "session": run.session, "mode": run.mode,
                              "seq": previous.get("seq", 0) + 1, "tx": previous.get("tx", 0) + 20,
                              "rx": previous.get("rx", 0) + 20, "errors": 0, "pass": True, "detail": detail}, now)

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
            self.poll_preparation()
            now = time.monotonic()
            if (self.phase == "CHECKING" and self.suite_active and self.suite_stage == "self_check_results"
                    and now - self.phase_started >= SELF_CHECK_DISPLAY_SECONDS):
                self.phase = "CHECKED"
                self.suite_stage = "fixed"
                self._start_connector_modes(list(I2C_MODES + ADC_MODES) + ["CAN", "SBUS_OUT"])
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
                    self.assessment.expire(now, indices=self.gpio_indices())
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
                self.advance_gpio_sequence(now)
            elif self.phase == "STOPPING_GPIO":
                self.poll_gpio_stop(now)
            elif self.phase == "I2C_RUNNING":
                if self.demo: self.demo_protocol_samples(time.monotonic())
                if getattr(self, "ethernet", None) is not None:
                    self.ethernet.pump(self.i2c_run, time.monotonic())
                self.i2c_run.tick(time.monotonic())
                self._update_i2c()
            child = getattr(self, "protocol_window", None)
            if child is not None and not child.closed:
                self.state.set("Connector protocol tests · " + child.status.get())
            self.poll_completion()
            self.draw_results()
        except (ValueError, RuntimeError, OSError) as error:
            self.stop(str(error), invalid=True)
        self.pump_after_id = self.root.after(70, self.pump)

    def draw_results(self):
        self.view.render()

    def report_snapshot(self, now=None):
        if getattr(self, "completed_report", None) is not None:
            return {**deepcopy(self.completed_report), "usb_connected": {role: role in self.hello for role in ("source", "probe")}}
        now = time.monotonic() if now is None else now
        if self.phase == "RUNNING":
            if now - self.last_pump_at > self.profile["lease_ms"] / 1000 or (self.last_ping and now - self.last_ping > self.profile["lease_ms"] / 1000) or any(now - self.last_seen.get(role, 0) > 2.5 for role in ("source", "probe")):
                self.stop("Measurement or heartbeat expired before export", invalid=True)
            else:
                self.assessment.expire(now, indices=self.gpio_indices())
        rows = [{**channel, **asdict(result)} for channel, result in zip(self.profile["channels"], self.assessment.results)]
        metadata = {"utc": datetime.now(timezone.utc).isoformat(), "demo": self.demo, "session": self.assessment.session, "profile_sha256": self.digest, "run_state": self.phase, "startup_checks": deepcopy(self.checked), "startup_check_details": deepcopy(self.check_details), "devices": deepcopy(self.identities), "peripherals": self.peripherals.snapshot(), "all_passed": self.phase == "RUNNING" and self.assessment.all_passed and self.peripherals.passed() and self.peripherals.current_passed}
        metadata["external_passed"] = self.phase == "RUNNING" and self.assessment.all_passed and all(self.checked.get(role) for role in ("source", "probe"))
        initial_checks = getattr(self, "initial_check_details", [])
        metadata["startup_check_details"] = deepcopy(initial_checks) + metadata["startup_check_details"]
        failed_initial_device = any(row["test"] in DEVICES and not row["pass"] for row in initial_checks)
        metadata["onboard_passed"] = self.peripherals.passed(("source",)) and self.peripherals.current_passed and not failed_initial_device
        if getattr(self, "gpio_snapshot", None) and self.phase in ("I2C_RUNNING", "I2C_COMPLETE", "COMPLETE", "WAITING_IOV2", "WAITING_ETH"):
            rows = deepcopy(self.gpio_snapshot["channels"])
            for index, row in enumerate(rows):
                if row.get("method") == "adc_voltage": row.update(asdict(self.assessment.results[index]))
        results = getattr(self, "i2c_results", {})
        metadata["connector_tests"] = deepcopy(results)
        metadata["selected_connector_modes"] = list(getattr(self, "connector_modes", ()))
        metadata["selected_connector_results"] = deepcopy({mode: results[mode] for mode in metadata["selected_connector_modes"] if mode in results})
        metadata["i2c_tests"] = deepcopy({k: v for k, v in results.items() if k in I2C_MODES})
        metadata["adc_tests"] = deepcopy({k: v for k, v in results.items() if k in ADC_MODES})
        metadata["adc_passed"] = set(metadata["adc_tests"]) == set(ADC_MODES) and all(r["status"] == "PASS" for r in metadata["adc_tests"].values())
        metadata["test_suite_completed"] = getattr(self, "suite_completed", False)
        metadata["extended_suite"] = getattr(self, "extended_suite", False)
        metadata["iov2_tests"] = deepcopy({mode: results[mode] for mode in IO_MODES if mode in results})
        metadata["iov2_passed"] = len(metadata["iov2_tests"]) == 8 and all(row["status"] == "PASS" and row.get("outputs_stopped") for row in metadata["iov2_tests"].values())
        metadata["ethernet_test"] = deepcopy(results.get("ETH", {}))
        metadata["sbus_out_test"] = deepcopy(results.get("SBUS_OUT", {}))
        metadata["sbus_out_passed"] = results.get("SBUS_OUT", {}).get("status") == "PASS" and results["SBUS_OUT"].get("outputs_stopped") is True
        panel = getattr(getattr(self, "view", None), "network_panel", None)
        metadata["pc_ethernet"] = panel.snapshot() if panel else {}
        metadata["ethernet_passed"] = results.get("ETH", {}).get("status") == "PASS" and results["ETH"].get("outputs_stopped") is True
        metadata["can_test"] = deepcopy(results.get("CAN", {}))
        metadata["can_passed"] = results.get("CAN", {}).get("status") == "PASS" and results["CAN"].get("outputs_stopped") is True
        metadata["gpio_stages"] = deepcopy(getattr(getattr(self, "gpio_sequence", None), "completed", {}))
        metadata["selected_gpio_groups"] = list(getattr(getattr(self, "gpio_sequence", None), "groups", ()))
        metadata["gpio_measurement_completed"] = getattr(self, "gpio_snapshot", None) is not None
        stopped = getattr(self, "gpio_stop", None)
        metadata["gpio_outputs_stopped"] = bool(stopped and stopped.done and not stopped.error and stopped.confirmed == {"source", "probe"})
        metadata["selected_gpio_passed"] = (metadata["gpio_outputs_stopped"] and bool(metadata["gpio_stages"])
                                            and set(metadata["gpio_stages"]) == set(metadata["selected_gpio_groups"])
                                            and all(row["status"] == "PASS" for rows in metadata["gpio_stages"].values() for row in rows))
        metadata["gpio_stop_confirmed_roles"] = sorted(stopped.confirmed) if stopped else []
        metadata["gpio_stop_error"] = stopped.error if stopped else ""
        metadata["i2c_passed"] = set(metadata["i2c_tests"]) == set(I2C_MODES) and all(r["status"] == "PASS" for r in metadata["i2c_tests"].values())
        metadata["external_suite_passed"] = metadata["test_suite_completed"] and all(row["status"] == "PASS" for row in rows) and metadata["i2c_passed"] and metadata["adc_passed"]
        if metadata["extended_suite"]:
            metadata["external_suite_passed"] = metadata["external_suite_passed"] and metadata["iov2_passed"] and metadata["sbus_out_passed"] and metadata["ethernet_passed"] and metadata["can_passed"]
        metadata["all_passed"] = metadata["external_suite_passed"] and metadata["onboard_passed"]
        return {**metadata, "channels": rows}

    def export(self):
        if not self.network_idle() or self.network_busy():
            self.note("Stop the active test and wait for output shutdown before exporting.")
            return
        # File dialogs can pause Tk on Windows. Only open one while outputs
        # are idle, after freezing the complete report.
        report = self.report_snapshot()
        filename = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON report", "*.json"), ("CSV report", "*.csv")], initialfile="pixnova-io-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        if not filename:
            return
        temporary = None
        try:
            csv_report = filename.lower().endswith(".csv")
            destination = Path(filename)
            if csv_report:
                save_csv(report, destination.parent, destination)
            else:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                             dir=destination.parent, prefix="." + destination.name + ".", suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
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
        if self.closing: return
        if self.phase == "FINISHING":
            self.note("Wait for firmware restoration to finish before closing."); return
        if self.phase == "STOPPING_GPIO":
            self.suite_active = self.auto_start = False
            self.gpio_followup = self.close
            return
        if self.defer_gpio_stop(self.close, "Closing station"): return
        if self.phase == "I2C_RUNNING":
            self.stop("Closing station")
            self.root.after(100, self.close)
            return
        child = getattr(self, "protocol_window", None)
        if child is not None and not child.closed:
            child.close()
            if not child.closed:
                self.root.after(100, self.close)
                return
        self.closing = True
        self.prepare_worker = None
        self.root.after_cancel(self.pump_after_id)
        self.view.network_panel.close()
        self.close_ethernet()
        for link in self.links.values():
            link.close()
        for link in self.links.values():
            link.join(timeout=0.5)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("profile.json"))
    parser.add_argument("--demo", action="store_true", help="Simulated frequencies; no serial hardware access")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--protocols", action="store_true", help="Open the connector protocol station")
    view.add_argument("--iov2", action="store_true", help="Open the ordinary station at the independent IOV2 PWM page")
    view.add_argument("--ethernet", action="store_true", help="Open the ordinary station at the Ethernet page")
    args = parser.parse_args()
    root = tk.Tk()
    if args.protocols:
        from protocol_app import ProtocolStation
        ProtocolStation(root, args.profile, args.demo)
    else:
        station = Station(root, args.profile, args.demo)
        if args.iov2:
            station.view.filter("IOV2 PWM")
        elif args.ethernet:
            station.view.select_connector_group("Ethernet")
    root.mainloop()


if __name__ == "__main__":
    main()
