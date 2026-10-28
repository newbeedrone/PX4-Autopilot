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
from peripherals import Peripherals, EXPECTED, DEVICES, display_value
from transport import SerialEndpoint


class Station:
    def __init__(self, root, profile_path, demo=False):
        self.root, self.profile_path = root, profile_path
        self.profile, self.digest = load_profile(profile_path)
        self.assessment = Assessment(self.profile)
        self.peripherals = Peripherals()
        self.current_roles = set()
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
        if self.demo:
            self.start_demo()
            return
        if self.phase in ("RUNNING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE", "CONNECTING"):
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
        self.auto_start = False
        self.check_details.clear()
        self.phase, self.phase_started = "CONNECTING", time.monotonic()
        self.state.set("Connecting — waiting for power-on peripheral checks")
        for role, device in ports.items():
            self.connected_at[role] = time.monotonic()
            self.links[role] = SerialEndpoint(role, device, self.events, self.generation)
            self.links[role].start()

    def self_check(self):
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
        self.state.set(("DEMO — " if self.demo else "") + "Startup self-check in progress; outputs disabled")
        try:
            for role in sorted(self.current_roles, reverse=True):
                self.transmit(role, f"CHECK {self.assessment.session} {self.digest}")
        except RuntimeError as error:
            self.stop(str(error), invalid=True)
            return
        if self.demo:
            session = self.assessment.session
            self.root.after(300, lambda: self.events.put(("probe", {"type": "checked", "session": session, "pass": True})))
            def finish_demo_source():
                for name, spec in DEVICES.items():
                    self.events.put(("source", {"type": "check", "session": session, "test": name, "pass": True, "detail": spec.get("demo", spec["id"])}))
                self.events.put(("source", {"type": "checked", "session": session, "pass": True}))
            self.root.after(550, finish_demo_source)

    def start(self):
        if self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE"):
            self.note("Startup is already in progress; wait for it to finish.")
            return
        if len(self.hello) != 2 or not self.peripherals.passed():
            self.note("Both boards must pass power-on checks before GPIO output. Correct faults and power-cycle the affected board.")
            return
        if self.phase == "RUNNING":
            self.stop("Restarting test")
        self.auto_start = True
        self.self_check()

    def power_ready(self):
        roles = set(self.links)
        if self.phase != "CONNECTING" or not roles or not roles.issubset(self.hello) or not roles.issubset(self.peripherals.summaries):
            return
        passed = self.peripherals.passed(roles)
        self.phase = ("READY" if len(roles) == 2 else "SOURCE_READY") if passed else "POWER_FAILED"
        self.state.set(("DEMO — " if self.demo else "") + ("Power-on checks passed — ready" if passed else "Power-on peripheral check FAILED — outputs disabled")
                       + (" · connect probe for GPIO testing" if len(roles) == 1 else ""))
        self.state_label.configure(foreground=GREEN if passed else RED)

    def begin(self):
        self.phase, self.phase_started = "STARTING_PROBE", time.monotonic()
        self.state.set("Starting input-only probe before enabling source outputs")
        self.transmit("probe", f"START {self.assessment.session} {self.digest}")
        if self.demo:
            self.events.put(("probe", {"type": "started", "session": self.assessment.session}))

    def stop(self, reason="Stopped by operator", invalid=False):
        pretest = self.phase == "CHECKING"
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
        if kind == "hello":
            if self.phase != "CONNECTING":
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
            if any(identity["device_id"].lower() == device_id for other, identity in self.identities.items() if other != role):
                self.stop("Both serial ports identify as the same MCU", invalid=True)
                return
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
                    good = self.peripherals.current_result(frame) is not False and good
            except ValueError as error:
                self.stop(f"{role}: {error}", invalid=True)
                return
            self.check_details.append({"role": role, "test": frame["test"], "pass": good, "detail": frame["detail"]})
            self.note(f"{role}: {frame['test']} {'PASS' if good else 'FAIL'} (detail={frame['detail']})")
        elif kind == "checked" and self.phase == "CHECKING" and frame.get("session") == self.assessment.session:
            if role not in self.current_roles or role in self.checked or type(frame.get("pass")) is not bool:
                self.stop(f"{role}: invalid self-check summary", invalid=True)
                return
            self.checked[role] = frame["pass"] and all(item["pass"] for item in self.check_details if item["role"] == role)
            if role == "source":
                self.checked[role] = self.checked[role] and self.peripherals.current_passed
            # CHECK never enables outputs. Collect both boards' full diagnostics
            # before concluding, even when the first device or board fails.
            if not self.current_roles.issubset(self.checked):
                return
            if not all(self.checked[role] for role in self.current_roles):
                self.stop("Startup self-check failed or incomplete; see device results", invalid=True)
            else:
                self.note("Startup self-checks passed")
                if self.auto_start:
                    self.begin()
                else:
                    self.phase = "CHECKED"
                    self.state.set("Self-check passed — Start test repeats checks before output" if self.peripherals.passed(self.current_roles)
                                   else "Current checks passed; correct the power-on fault and power-cycle before GPIO testing")
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
                self.note("Waveform output enabled after successful self-checks")
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
        self.state.set("DEMO — ICM45686 #2 power-on FAIL; outputs disabled" if self.demo_peripheral_fault.get() else "DEMO — power-on checks pass; Start test repeats the pre-test checks")

    def demo_samples(self, now):
        if now - self.last_demo < 1:
            return
        self.last_demo = now
        self.demo_sequence += 1
        self.last_seen = {"source": now, "probe": now}
        for index, row in enumerate(self.profile["channels"]):
            frequency = row["expected_hz"] * (1.10 if self.demo_fault.get() and index == 0 else 1)
            edges = max(3, int(frequency) + 1)
            span = round((edges - 1) * 1_000_000 / frequency)
            self.assessment.accept({"type": "sample", "session": self.assessment.session, "seq": self.demo_sequence, "channel": index, "frequency_millihz": (edges - 1) * 1_000_000_000 // span, "edges": edges, "span_us": span, "timing_errors": 0}, now)

    def pump(self):
        if self.closing:
            return
        try:
            now = time.monotonic()
            if self.phase in ("RUNNING", "STARTING_PROBE", "STARTING_SOURCE") and now - self.last_pump_at > self.profile["lease_ms"] / 1000:
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
                    self.state.set(("DEMO — " if self.demo else "") + ("ALL CHANNELS PASS" if self.assessment.all_passed else f"RUNNING · {passed} PASS · {failed} FAIL"))
                    self.state_label.configure(foreground=RED if failed else GREEN if self.assessment.all_passed else MUTED)
            elif self.phase in ("CONNECTING", "CHECKING", "STARTING_PROBE", "STARTING_SOURCE"):
                if self.phase == "STARTING_SOURCE" and now - self.last_ping > 0.4:
                    self.transmit("probe", f"PING {self.assessment.session}")
                    self.last_ping = now
                if now - self.phase_started > (20 if self.phase in ("CHECKING", "CONNECTING") else 5):
                    self.stop("Startup or USB handshake timed out", invalid=True)
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
                    writer = csv.DictWriter(output, fieldnames=data[0].keys())
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
        for link in self.links.values():
            link.close()
        for link in self.links.values():
            link.join(timeout=0.5)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("profile.json"))
    parser.add_argument("--demo", action="store_true", help="Simulated frequencies; no serial hardware access")
    args = parser.parse_args()
    root = tk.Tk()
    Station(root, args.profile, args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
