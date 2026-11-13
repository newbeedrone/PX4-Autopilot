#!/usr/bin/env python3
"""Dedicated connector protocol bench; requires the protocol firmware pair."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, ttk

from dashboard import BG, PANEL, BORDER, TEXT, MUTED, GREEN, RED, BLUE, FONT, Action, label
from generate_profile import load_profile
from peripherals import DEVICES, Peripherals
from protocol_model import TESTS, ProtocolRun, packet, rc_values
from transport import SerialEndpoint


class ProtocolStation:
    def __init__(self, root, profile_path, demo=False, on_io_ports=None):
        self.root, self.demo = root, demo
        self.profile_path, self.on_io_ports = profile_path, on_io_ports
        self.profile, self.digest = load_profile(profile_path)
        self.events = queue.Queue(maxsize=4096)
        self.links, self.hello, self.probe_checks = {}, {}, {}
        self.power = Peripherals()
        self.current_devices = {}
        self.probe_ready = False
        self.run = None
        self.results = {}
        self.frames = []
        self.generation = 0
        self.udp = None
        self.pending_udp = {}
        self.udp_sequence = 0
        self.last_udp = self.last_demo = 0
        self.last_pump = time.monotonic()
        self.closed = False
        self.mode = tk.StringVar(value="I2C3")
        self.source_port, self.probe_port = tk.StringVar(), tk.StringVar()
        self.wired = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Connect USB; all protocol outputs remain disabled")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.pump)
        if demo:
            root.after(150, self.connect)

    def _build(self):
        self.root.title("PixNova · Connector Protocol Tests" + (" · DEMO" if self.demo else ""))
        self.root.geometry("1360x930")
        self.root.configure(bg=BG)
        outer = tk.Frame(self.root, bg=BG); outer.pack(fill="both", expand=True, padx=26, pady=20)
        heading = tk.Frame(outer, bg=BG); heading.pack(fill="x")
        label(heading, "PIXNOVA / CONNECTOR PROTOCOLS", size=23, bold=True).pack(side="left")
        Action(heading, "IO ports", self.open_io_ports, 120).pack(side="right")
        label(outer, "DEMO · SIMULATED DATA · NO HARDWARE ACCESS" if self.demo else "CAN, Ethernet, I2C and RC tests · GPS1, TELEM, SPI6 and PWM are in IO ports",
              color=RED if self.demo else BLUE).pack(anchor="w", pady=(4, 15))
        connection = tk.Frame(outer, bg=PANEL, padx=15, pady=12); connection.pack(fill="x")
        label(connection, "PixNova USB").grid(row=0, column=0, sticky="w")
        self.source_combo = ttk.Combobox(connection, textvariable=self.source_port, width=18)
        self.source_combo.grid(row=1, column=0, padx=(0, 16))
        label(connection, "Simulator USB (optional for CAN / ETH)").grid(row=0, column=1, sticky="w")
        self.probe_combo = ttk.Combobox(connection, textvariable=self.probe_port, width=18)
        self.probe_combo.grid(row=1, column=1, sticky="w", padx=(0, 16))
        Action(connection, "Refresh", self.refresh, 90).grid(row=1, column=2, padx=5)
        Action(connection, "Connect USB", self.connect, 120).grid(row=1, column=3, padx=5)
        self.identity = label(connection, "", color=MUTED, size=9)
        self.identity.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        sensor_frame = tk.Frame(outer, bg=BG); sensor_frame.pack(fill="x", pady=14)
        self.sensor_labels = {}
        for index, (key, spec) in enumerate(DEVICES.items()):
            box = tk.Frame(sensor_frame, bg=PANEL, highlightbackground=BORDER, highlightthickness=1, padx=10, pady=7)
            box.grid(row=index // 5, column=index % 5, sticky="nsew", padx=3, pady=3)
            sensor_frame.columnconfigure(index % 5, weight=1)
            label(box, spec["name"], bold=True, size=10).pack(anchor="w")
            value = label(box, "WAITING", color=MUTED, size=9); value.pack(anchor="w")
            self.sensor_labels[key] = (box, value)
        body = tk.Frame(outer, bg=BG); body.pack(fill="both", expand=True)
        self.body_canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.body_canvas.yview)
        scroll.pack(side="right", fill="y"); self.body_canvas.pack(side="left", fill="both", expand=True)
        self.body_canvas.configure(yscrollcommand=scroll.set)
        row = tk.Frame(self.body_canvas, bg=BG)
        body_window = self.body_canvas.create_window(0, 0, window=row, anchor="nw")
        row.bind("<Configure>", lambda event: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")))
        self.body_canvas.bind("<Configure>", lambda event: self.body_canvas.itemconfigure(body_window, width=event.width))
        cards = tk.Frame(row, bg=BG); cards.pack(side="left", fill="both", expand=True)
        self.cards = {}
        for index, (key, spec) in enumerate(TESTS.items()):
            box = tk.Frame(cards, bg=PANEL, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=12)
            box.grid(row=index // 2, column=index % 2, sticky="nsew", padx=5, pady=5)
            cards.columnconfigure(index % 2, weight=1); cards.rowconfigure(index // 2, weight=1)
            label(box, spec["name"], size=14, bold=True).pack(anchor="w")
            label(box, spec["path"], color=MUTED, size=9, wraplength=340, justify="left").pack(anchor="w", pady=6)
            state = label(box, "NOT RUN", color=MUTED, bold=True); state.pack(anchor="w")
            detail = label(box, "No physical protocol measurement", color=MUTED, size=9, wraplength=340, justify="left")
            detail.pack(anchor="w", pady=4)
            Action(box, "Select test", lambda name=key: self.select(name), 100).pack(anchor="w", pady=(5, 0))
            self.cards[key] = (box, state, detail)
        side = tk.Frame(row, bg=PANEL, padx=17, pady=15, width=370); side.pack(side="right", fill="y", padx=(15, 0))
        self.selected = label(side, "", size=16, bold=True); self.selected.pack(anchor="w")
        self.wiring_text = label(side, "", color=TEXT, wraplength=340, justify="left"); self.wiring_text.pack(anchor="w", pady=13)
        self.criteria = label(side, "", color=MUTED, wraplength=340, justify="left"); self.criteria.pack(anchor="w", pady=8)
        tk.Checkbutton(side, text="Selected wiring installed; conflicting\nsignal connections disconnected", variable=self.wired,
                       bg=PANEL, fg=TEXT, selectcolor=BG, activebackground=PANEL, activeforeground=TEXT, justify="left").pack(anchor="w", pady=12)
        Action(side, "Start selected test", self.start, 200, primary=True).pack(anchor="w", pady=5)
        Action(side, "Stop outputs", self.stop, 200, danger=True).pack(anchor="w", pady=5)
        Action(side, "Export JSON", self.export, 200).pack(anchor="w", pady=5)
        label(side, "CAN / ETH are real communication tests.\nNo ADC waveform-only pass is reported.", color=MUTED, size=9, wraplength=340, justify="left").pack(anchor="w", pady=12)
        label(outer, variable=self.status, color=BLUE, wraplength=1270, justify="left").pack(anchor="w", pady=(15, 0))
        self.select("I2C3")
        self.refresh()
        def wheel_bind(widget):
            widget.bind("<MouseWheel>", lambda event: self.body_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
            for child in widget.winfo_children(): wheel_bind(child)
        wheel_bind(row)

    def select(self, mode):
        self.mode.set(mode); self.wired.set(False)
        spec = TESTS[mode]
        self.selected.configure(text=spec["name"])
        self.wiring_text.configure(text=spec["wiring"])
        self.criteria.configure(text="PASS requires:\n" + spec["criterion"])
        self.body_canvas.yview_moveto(0)

    def refresh(self):
        try:
            from serial.tools import list_ports
            values = [port.device for port in list_ports.comports()]
            self.source_combo["values"] = self.probe_combo["values"] = values
        except ImportError:
            self.status.set("Install pyserial for hardware access")

    def close_links(self):
        for link in self.links.values(): link.close()
        for link in self.links.values(): link.join(timeout=.5)
        self.links.clear()

    def open_io_ports(self):
        if self.closed: return
        source, probe = self.source_port.get(), self.probe_port.get()
        self.stop("Switching to IO port tests")
        self.close_links()
        self.generation += 1
        self.closed = True
        if self.on_io_ports is not None:
            self.root.destroy()
            self.on_io_ports()
            return
        # Standalone protocol windows return to the main station in place.
        # The old pump sees closed=True and cannot touch the new USB owners.
        for widget in self.root.winfo_children(): widget.destroy()
        from app import Station
        self.io_station = Station(self.root, self.profile_path, self.demo)
        self.io_station.source_port.set(source)
        self.io_station.probe_port.set(probe)

    def connect(self):
        if self.closed: return
        self.stop()
        self.close_links()
        self.generation += 1
        self.hello.clear(); self.probe_checks.clear(); self.results.clear(); self.frames.clear()
        self.power.reset(); self.current_devices.clear(); self.probe_ready = False; self.run = None
        self.render()
        if self.demo:
            for role, device in (("source", "0000000000000001"), ("probe", "0000000000000002")):
                self.hello[role] = {"role": role, "device_id": device, "firmware": "0" * 16, "protocol_tests": 1}
            self.power.power["source"] = {key: {"pass": True, "detail": spec.get("demo", spec["id"])} for key, spec in DEVICES.items()}
            self.power.summaries["source"] = {"pass": True}
            self.probe_ready = True
            self.status.set("DEMO · select a protocol; no USB or Ethernet access")
            self.render(); return
        ports = {"source": self.source_port.get().strip()}
        if self.probe_port.get().strip(): ports["probe"] = self.probe_port.get().strip()
        if not ports["source"] or len({p.casefold() for p in ports.values()}) != len(ports):
            self.status.set("Choose a source USB port and a distinct simulator USB port"); return
        for role, device in ports.items():
            self.links[role] = SerialEndpoint(role, device, self.events, self.generation)
            self.links[role].start()
        self.status.set("Waiting for firmware identities and complete power-on results")

    def send(self, role, command):
        if self.demo:
            session = self.run.session if self.run else ""
            def emit(frame):
                self.events.put((role, {**frame, "_connection": self.generation, "_received_at": time.monotonic()}))
            if command.startswith("PCHECK "):
                checks = {key: spec.get("demo", spec["id"]) for key, spec in DEVICES.items()} if role == "source" else {"probe_protocol_firmware": 1}
                checks |= {"hrt_clock": 5000, "gpio_inputs": len(self.profile["channels"])}
                for key, value in checks.items(): emit({"type": "check", "session": session, "test": key, "pass": True, "detail": value})
                emit({"type": "checked", "session": session, "mode": self.run.mode, "pass": True})
            elif command.startswith("PROTO "):
                emit({"type": "started", "session": session, "mode": self.run.mode, "role": role})
            elif command.startswith("PING "):
                emit({"type": "heartbeat", "session": session, "running": True})
            return
        if role in self.links:
            self.links[role].send(command)

    def start(self):
        if self.run and self.run.phase not in ("COMPLETE", "FAILED", "STOPPED"):
            self.status.set("Stop the active protocol before selecting another"); return
        mode = self.mode.get(); roles = {"source"} if mode in ("CAN", "ETH") else {"source", "probe"}
        if not self.demo and TESTS[mode].get("kind") == "i2c" and not all(self.hello.get(role, {}).get("i2c_simulators") == 2 for role in roles):
            self.status.set("Update both boards with the final I2C slave firmware"); return
        if not self.demo and TESTS[mode].get("kind") == "adc" and not all(self.hello.get(role, {}).get("adc_levels") == 1 for role in roles):
            self.status.set("Update both boards with the ADC voltage firmware"); return
        if not self.demo and not self.wired.get():
            self.status.set("Install and confirm the selected wiring before starting"); return
        if not roles.issubset(self.hello) or not self.power.complete(("source",)) or ("probe" in roles and not self.probe_ready):
            self.status.set("Complete USB identity/diagnostic transfer and verify the simulator; onboard failures are independent"); return
        try:
            self.udp_close()
            self.run = ProtocolRun(mode, self.digest, time.monotonic(), self.send)
            self.current_devices.clear()
            if mode == "ETH" and not self.demo:
                self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp.bind(("192.168.77.1", 0)); self.udp.setblocking(False)
            self.pending_udp.clear(); self.udp_sequence = 0
            self.run.begin()
        except (OSError, RuntimeError, ValueError) as error:
            if self.run: self.run.fail(str(error)); self.results[mode] = self.run.snapshot()
            self.udp_close(); self.status.set(str(error)); self.render(); return
        self.render()

    def stop(self, reason="Stopped by operator"):
        if self.run and self.run.phase not in ("COMPLETE", "FAILED", "STOPPED"):
            try: self.run.stop(reason)
            except RuntimeError: pass
            self.results[self.run.mode] = self.run.snapshot()
        for role in self.links:
            try: self.send(role, "STOP")
            except RuntimeError: pass
        self.udp_close()
        self.status.set(reason + " · outputs stopped")

    def udp_close(self):
        if self.udp is not None:
            self.udp.close(); self.udp = None

    def handle(self, role, frame, now):
        if frame.get("_connection") != self.generation: return
        self.frames.append({"role": role, **frame})
        if len(self.frames) > 10000: self.frames.pop(0)
        kind = frame["type"]
        if kind == "hello":
            if role in self.hello or frame.get("role") != role or type(frame.get("protocol_tests")) is not int or frame.get("protocol_tests") != 1 or frame.get("profile") != self.digest or frame.get("protocol") != 3 or frame.get("channels") != len(self.profile["channels"]):
                raise ValueError(f"{role}: dedicated protocol firmware/profile required; reconnect USB")
            if role == "source" and frame.get("independent_external_checks") != 1:
                raise ValueError("Update source firmware for independent external checks")
            device = frame.get("device_id")
            if not isinstance(device, str) or not 16 <= len(device) <= 80 or any(c not in "0123456789abcdefABCDEF" for c in device):
                raise ValueError("Missing MCU identity")
            firmware = frame.get("firmware")
            if not isinstance(firmware, str) or len(firmware) != 16 or any(c not in "0123456789abcdefABCDEF" for c in firmware):
                raise ValueError("Missing firmware identity")
            if any(h["device_id"].lower() == device.lower() for h in self.hello.values()): raise ValueError("Both USB ports identify the same MCU")
            self.hello[role] = frame
        elif kind in ("power_check", "power_checked"):
            if role not in self.hello: raise ValueError("Power-on results arrived before identity")
            if role == "source":
                if kind == "power_check": self.power.power_result(role, frame)
                else: self.power.finish_power(role, frame)
            elif kind == "power_check":
                if self.probe_ready or self.probe_checks or frame.get("test") != "probe_protocol_firmware" or frame.get("pass") is not True or type(frame.get("detail")) is not int or frame.get("detail") != 1:
                    raise ValueError("Invalid simulator power-on check")
                self.probe_checks[frame["test"]] = frame
            else:
                checked, uptime = frame.get("checked_us"), frame.get("uptime_us")
                if self.probe_ready or not self.probe_checks or type(frame.get("count")) is not int or frame.get("count") != 1 or frame.get("pass") is not True or type(checked) is not int or type(uptime) is not int or not 0 <= checked <= uptime:
                    raise ValueError("Incomplete simulator power-on check")
                self.probe_ready = True
            self.status.set("Onboard diagnostics received; select a wired external test" if self.power.complete(("source",)) else "Waiting for complete onboard diagnostic transfer")
        if kind in ("link_error", "disconnected"):
            self.hello.pop(role, None)
            if role == "source": self.power.invalidate("source", frame.get("reason", kind))
            else: self.probe_ready = False
            self.status.set(f"{role}: {frame.get('reason', kind)}")
        if kind == "check" and role == "source" and self.run and frame.get("session") == self.run.session and frame.get("test") in DEVICES:
            name, result = Peripherals.result("source", frame)
            self.current_devices[name] = result
        if self.run: self.run.handle(role, frame, now)

    def udp_pump(self, now):
        if not self.udp or not self.run or self.run.phase != "RUNNING": return
        if self.run.samples.get("source", {}).get("detail") != 1:
            if self.run.host_rx:
                self.run.fail("Ethernet physical link lost")
            return
        if self.pending_udp and now - min(self.pending_udp.values()) > 1:
            self.run.fail("Ethernet reply timed out"); return
        for _ in range(32):
            try: data, peer = self.udp.recvfrom(128)
            except BlockingIOError: break
            if peer != ("192.168.77.2", 14570) or len(data) != 32:
                self.run.fail("Unexpected Ethernet peer or packet length"); return
            sequence = int.from_bytes(data[8:12], "little")
            if sequence not in self.pending_udp:
                self.run.fail("Unsolicited/replayed Ethernet response"); return
            del self.pending_udp[sequence]
            self.run.accept_udp(data, sequence, now)
        if now - self.last_udp >= .05:
            self.udp_sequence += 1
            self.udp.sendto(packet("ETH", self.run.token, self.udp_sequence), ("192.168.77.2", 14570))
            self.pending_udp[self.udp_sequence] = now; self.last_udp = now

    def pump(self):
        if self.closed: return
        now = time.monotonic()
        try:
            if now - self.last_pump > .75 and self.run and self.run.phase not in ("COMPLETE", "FAILED", "STOPPED"):
                self.run.fail("GUI processing delayed; restart the test")
            for _ in range(512):
                try: role, frame = self.events.get_nowait()
                except queue.Empty: break
                self.handle(role, frame, time.monotonic())
            if self.demo and self.run and self.run.phase in ("RUNNING", "RC_LOSS") and now - self.last_demo > 1:
                self.last_demo = now
                if self.run.mode == "ETH":
                    self.run.host_rx += 20; self.run.last_host = now
                    self.run.result.host_rx = self.run.host_rx
                for role in (name for name in ("probe", "source") if name in self.run.started):
                    old = self.run.samples.get(role, {})
                    self.run.handle(role, {"type": "protocol_sample", "session": self.run.session, "mode": self.run.mode,
                                          "seq": old.get("seq", 0) + 1, "tx": old.get("tx", 0) + 20, "rx": old.get("rx", 0) + 20,
                                          "errors": 0, "pass": self.run.phase != "RC_LOSS", "detail": (TESTS[self.run.mode]["stimulus_mv"] if role == "probe" else (0 if self.run.mode == "ADC6V6_LOW" else 1650)) if TESTS[self.run.mode].get("kind") == "adc" else 3 if self.run.mode == "CAN" else 0,
                                          "channels": rc_values(self.run.token)}, now)
            self.udp_pump(now)
            if self.run:
                self.run.tick(time.monotonic())
                self.results[self.run.mode] = self.run.snapshot()
                if self.run.phase in ("COMPLETE", "FAILED", "STOPPED"): self.udp_close()
                self.status.set(("DEMO · " if self.demo else "") + self.run.result.reason)
            self.render()
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            if self.run and self.run.phase not in ("COMPLETE", "FAILED", "STOPPED"):
                self.run.fail(str(error)); self.results[self.run.mode] = self.run.snapshot()
            self.stop(str(error))
            self.close_links(); self.hello.clear(); self.probe_ready = False
            self.generation += 1
            self.status.set(str(error) + " · outputs stopped")
        self.last_pump = time.monotonic()
        self.root.after(50, self.pump)

    def render(self):
        for key, (box, value) in self.sensor_labels.items():
            result = self.current_devices.get(key) or self.power.power["source"].get(key)
            color = GREEN if result and result["pass"] else RED if result else MUTED
            value.configure(text=("PASS" if result["pass"] else "FAIL") + f" · {result['detail']}" if result else "WAITING", fg=color)
            box.configure(highlightbackground=color)
        self.identity.configure(text="  |  ".join(f"{role}: {h['device_id']}" for role, h in self.hello.items()))
        for key, (box, state, detail) in self.cards.items():
            result = self.results.get(key, {})
            status = result.get("status", "NOT RUN")
            color = GREEN if status == "PASS" else RED if status == "FAIL" else BLUE if status in ("WAITING", "VERIFYING") else MUTED
            box.configure(highlightbackground=color); state.configure(text=status, fg=color)
            counters = f"FC TX/RX {result.get('source_tx', 0)}/{result.get('source_rx', 0)} · Probe TX/RX {result.get('probe_tx', 0)}/{result.get('probe_rx', 0)}"
            if self.run and self.run.mode == key and key in ("SBUS", "DSM") and self.run.samples.get("source", {}).get("channels"):
                counters += "\nRC μs: " + " / ".join(map(str, self.run.samples["source"]["channels"]))
            detail.configure(text=result.get("reason", "No physical protocol measurement") + "\n" + counters)

    def export(self):
        report = {"utc": datetime.now(timezone.utc).isoformat(), "demo": self.demo, "profile_sha256": self.digest,
                  "devices": deepcopy(self.hello), "power_on": self.power.snapshot(), "probe_power_on": deepcopy(self.probe_checks),
                  "latest_device_checks": deepcopy(self.current_devices),
                  "protocols": deepcopy(self.results), "frames": deepcopy(self.frames),
                  "all_protocols_completed": set(self.results) == set(TESTS) and all(r["status"] == "PASS" for r in self.results.values())}
        report["hardware_protocols_passed"] = not self.demo and report["all_protocols_completed"]
        report["onboard_passed"] = self.power.passed(("source",)) and set(self.current_devices) == set(DEVICES) and all(r["pass"] for r in self.current_devices.values())
        filename = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON report", "*.json")], initialfile="pixnova-protocols.json")
        if not filename: return
        temporary = None
        try:
            destination = Path(filename)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as output:
                temporary = Path(output.name); json.dump(report, output, indent=2)
            os.replace(temporary, destination); temporary = None
        except OSError as error:
            self.status.set("Report export failed: " + str(error))
        finally:
            if temporary: temporary.unlink(missing_ok=True)

    def close(self):
        self.closed = True; self.stop(); self.close_links(); self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("profile.json"))
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    root = tk.Tk(); ProtocolStation(root, args.profile, args.demo); root.mainloop()


if __name__ == "__main__":
    main()
