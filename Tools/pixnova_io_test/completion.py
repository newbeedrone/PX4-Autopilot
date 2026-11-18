"""Save complete manufacturing results before identity-bound USB firmware restoration."""
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import struct
import sys
import tempfile
import threading
import time
import uuid


def uploader_module():
    # In-tree and exported program layouts both use the repository uploader.
    tools = str(Path(__file__).resolve().parents[1])
    if tools not in sys.path: sys.path.append(tools)
    import px4_uploader
    return px4_uploader


class FlightImage:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        if self.path.suffix.lower() != ".px4": raise ValueError("Choose a normal flight-controller .px4 file")
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.firmware = uploader_module().Firmware(self.path)
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise ValueError("Firmware file changed while it was being validated; select it again")
        firmware = self.firmware
        raw = firmware.image
        if firmware.description.get("magic") != "PX4FWv1" or firmware.board_id != 5900 or firmware.image_size <= 8 or len(raw) != (firmware.image_size + 3) // 4 * 4:
            raise ValueError("Firmware must be a complete PixNova board 5900 application")
        stack, reset = struct.unpack_from("<II", raw)
        ram = 0x20000000 <= stack <= 0x20020000 or 0x24000000 <= stack <= 0x24080000 or 0x30000000 <= stack <= 0x30048000
        # Match the bootloader's vector handover. Existing NuttX images use
        # CONFIG_IDLETHREAD_STACKSIZE=750 and do not encode an 8-byte-aligned
        # initial stack word; imposing AAPCS alignment here rejects those images.
        if not ram or not reset & 1 or not 0x08020000 <= (reset & ~1) < 0x08020000 + firmware.image_size:
            raise ValueError("Firmware is not an application for the PixNova bootloader layout")
        if firmware.image_size > 1966080 or firmware.image_maxsize > 1966080 or firmware.image_size > firmware.image_maxsize:
            raise ValueError("Firmware exceeds the PixNova application flash area")
        if b'"independent_external_checks":1' in raw or b'"protocol_tests":1' in raw:
            raise ValueError("Choose normal flight firmware, not a manufacturing test image")

    def metadata(self):
        return {"path": str(self.path), "sha256": self.sha256, "board_id": self.firmware.board_id,
                "bytes": self.firmware.image_size, "git_identity": self.firmware.description.get("git_identity", "")}


def expected_serial(device_id):
    if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-fA-F]{24,80}", device_id):
        raise ValueError("A verified flight-controller MCU identity is required")
    tail = device_id[-24:].lower()
    return tail[16:24] + tail[8:16] + tail[:8]


def save_csv(report, directory, path=None):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if path is None:
        uid = report.get("devices", {}).get("source", {}).get("device_id", "unknown")[-8:]
        path = directory / ("pixnova-" + uid + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8] + ".csv")
    post = report.get("post_test", {})
    measurement_status = "PASS" if report.get("all_passed") else "FAIL"
    finalization_status = ("FAIL" if post.get("status") == "FAILED" else "PENDING" if post.get("status") == "SAVING" else
                       "PASS" if post.get("status") == "COMPLETE" and post.get("restoration", {}).get("status") in ("PASS", "DEMO") else "NOT RUN")
    workflow_status = "FAIL" if measurement_status == "FAIL" or finalization_status == "FAIL" else finalization_status
    summary_status = measurement_status if not post or measurement_status == "FAIL" else workflow_status if workflow_status in ("PASS", "PENDING") else "FAIL"
    shared = {"utc": report["utc"], "demo": report["demo"], "profile_sha256": report["profile_sha256"],
              "measurement_status": measurement_status, "finalization_status": finalization_status, "workflow_status": workflow_status,
              "workflow_passed": workflow_status == "PASS",
              "gpio_session": report.get("session", ""),
              "source_device": report.get("devices", {}).get("source", {}).get("device_id", ""),
              "probe_device": report.get("devices", {}).get("probe", {}).get("device_id", ""),
              "source_firmware": report.get("devices", {}).get("source", {}).get("firmware", ""),
              "probe_firmware": report.get("devices", {}).get("probe", {}).get("firmware", ""),
              "external_suite_passed": report.get("external_suite_passed", False),
              "onboard_passed": report.get("onboard_passed", False),
              "test_suite_completed": report.get("test_suite_completed", False),
              "selected_gpio_groups": json.dumps(report.get("selected_gpio_groups", []), ensure_ascii=False),
              "gpio_outputs_stopped": report.get("gpio_outputs_stopped", False),
              "gpio_stop_confirmed_roles": json.dumps(report.get("gpio_stop_confirmed_roles", [])),
              "gpio_stop_error": report.get("gpio_stop_error", ""),
              "run_state": report.get("run_state", ""),
              "test_scope": report.get("test_scope", ""),
              "selected_gpio_passed": report.get("selected_gpio_passed", False),
              "selected_connector_modes": json.dumps(report.get("selected_connector_modes", [])),
              "excluded_tests": json.dumps(report.get("excluded_tests", [])),
              "manual_usb_flash_test": report.get("manual_usb_flash_test", False),
              "usb_flash_test": json.dumps(report.get("usb_flash_test", {}), ensure_ascii=False),
              "post_test": json.dumps(report.get("post_test", {}), ensure_ascii=False)}
    rows = []
    for channel in report.get("channels", []):
        rows.append({**shared, **channel, "record_type": "signal", "module": channel.get("group", ""), "signal": channel["id"]})
    for name, result in report.get("connector_tests", {}).items():
        rows.append({**shared, **result, "record_type": "protocol", "module": name, "signal": name})
    for name, result in report.get("peripherals", {}).get("latest", {}).items():
        rows.append({**shared, **result, "record_type": "onboard", "module": name, "signal": name})
    for role, checks in report.get("peripherals", {}).get("power_on", {}).items():
        for name, result in checks.items():
            rows.append({**shared, **result, "record_type": "power_on", "module": role,
                         "signal": name, "status": "PASS" if result.get("pass") else "FAIL"})
    for result in report.get("startup_check_details", []):
        rows.append({**shared, **result, "record_type": "startup", "module": result["role"],
                     "signal": result["test"], "status": "PASS" if result["pass"] else "FAIL"})
    restored = post.get("restoration")
    if restored or post.get("status") == "FAILED":
        result = restored or {**post.get("normal_firmware", {}), "status": "FAIL", "reason": post.get("error", "Restoration failed")}
        rows.append({**shared, **result, "record_type": "firmware", "module": "USB firmware restoration", "signal": result.get("path", "")})
    rows.append({**shared, "record_type": "summary", "module": "complete suite", "status": summary_status})
    rows = [{key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()} for row in rows]
    columns = list(dict.fromkeys(key for row in rows for key in row))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=directory, prefix=".pixnova-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader(); writer.writerows(rows)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        return Path(path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def program_verified(protocol, image, wanted, progress):
    if protocol.board_type != 5900 or protocol.sn.hex().lower() != wanted:
        raise ValueError("Bootloader identity mismatch; no firmware erased")
    if len(image.firmware.image) > protocol.fw_maxsize:
        raise ValueError("Firmware exceeds the bootloader's application flash area")
    progress("Erasing flight-controller application")
    protocol.erase()
    progress("Programming normal flight firmware")
    protocol.program(image.firmware)
    progress("Verifying normal flight firmware CRC")
    protocol.verify(image.firmware)
    if image.firmware.image_signed: protocol.verify_signature(True)
    if not protocol.reboot(): raise RuntimeError("Flash CRC verified, but normal application reboot was not confirmed")
    return {**image.metadata(), "status": "PASS", "crc_verified": True, "bootloader_serial": wanted, "application_reboot_confirmed": True}


def restore_usb(image, source_port, probe_port, source_id, progress):
    import serial
    from serial.tools import list_ports
    module = uploader_module()
    wanted = expected_serial(source_id)
    deadline = time.monotonic() + 5
    while True:
        try:
            port = serial.Serial(source_port, 115200, timeout=.1, write_timeout=1)
            break
        except serial.SerialException:
            if time.monotonic() >= deadline: raise
            time.sleep(.1)
    with port:
        port.write(b"HELLO\n")
        deadline = time.monotonic() + 4
        hello = None
        while time.monotonic() < deadline:
            try: frame = json.loads(port.readline())
            except (ValueError, UnicodeError): continue
            if frame.get("type") == "hello": hello = frame; break
        if not hello or hello.get("role") != "source" or hello.get("device_id") != source_id or hello.get("usb_restore") != 1:
            raise ValueError("Flight-controller identity/USB restore capability mismatch; no firmware erased")
        port.write(("BOOTLOADER " + source_id + "\n").encode("ascii"))
        deadline = time.monotonic() + 3
        acknowledged = False
        while time.monotonic() < deadline:
            try: frame = json.loads(port.readline())
            except (ValueError, UnicodeError): continue
            if frame.get("type") == "bootloader" and frame.get("device_id") == source_id:
                acknowledged = True; break
            if frame.get("type") == "error": raise RuntimeError(frame.get("reason", "Bootloader request rejected"))
        if not acknowledged: raise RuntimeError("Bootloader request was not acknowledged; no firmware erased")
    progress("Waiting for the flight-controller USB bootloader")
    deadline = time.monotonic() + 25
    protocol, transport = None, None
    while time.monotonic() < deadline and protocol is None:
        for info in list_ports.comports():
            if info.vid != 0x3643 or info.device == probe_port: continue
            candidate = module.SerialTransport(info.device, timeout=.2)
            try:
                candidate.open()
                found = module.BootloaderProtocol(candidate)
                found.identify()
                if found.board_type != 5900 or found.sn.hex().lower() != wanted:
                    raise ValueError("Bootloader MCU identity does not match the tested flight controller")
                protocol, transport = found, candidate
                break
            except Exception:
                candidate.close()
        if protocol is None: time.sleep(.15)
    if protocol is None: raise RuntimeError("Matching flight-controller USB bootloader not found; no firmware erased")
    try:
        return program_verified(protocol, image, wanted, progress)
    finally:
        transport.close()


class CompletionWorker:
    def __init__(self, report, directory, image, source_port, probe_port, demo=False):
        self.events, self.busy = queue.Queue(), True
        self.report = deepcopy(report)
        self.directory, self.image = directory, image
        self.source_port, self.probe_port, self.demo = source_port, probe_port, demo

    def start(self): threading.Thread(target=self.run, name="pixnova-completion", daemon=True).start()

    def run(self):
        path = None
        def progress(message): self.events.put({"progress": message})
        try:
            self.report["workflow_passed"] = False
            self.report["post_test"] = {"status": "SAVING", "normal_firmware": self.image.metadata() if self.image else {}}
            path = save_csv(self.report, self.directory)
            progress("CSV saved: " + str(path))
            if self.demo:
                restored = {"status": "DEMO", "crc_verified": False}
            else:
                if self.image is None: raise ValueError("Normal flight firmware was not selected")
                source_id = self.report["devices"]["source"]["device_id"]
                restored = restore_usb(self.image, self.source_port, self.probe_port, source_id, progress)
            self.report["post_test"] = {"status": "COMPLETE", "csv": str(path), "restoration": restored}
            self.report["workflow_passed"] = bool(self.report.get("all_passed"))
            save_csv(self.report, self.directory, path)
            self.events.put({"done": True, "report": self.report, "csv": str(path)})
        except Exception as error:
            self.report["workflow_passed"] = False
            self.report["post_test"] = {**self.report.get("post_test", {}), "status": "FAILED", "csv": str(path) if path else "", "error": str(error)}
            if path:
                try: save_csv(self.report, self.directory, path)
                except Exception: pass  # The first complete measurement CSV remains intact.
            self.events.put({"done": True, "error": str(error), "report": self.report, "csv": str(path) if path else ""})
        finally:
            self.busy = False


def prepare_plan(path, directory, demo):
    """File operations only; callable off the Tk thread."""
    image = None if demo else FlightImage(path)
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=directory): pass
    return image, directory


class PreparationWorker:
    def __init__(self, path, directory, demo):
        self.args = path, directory, demo
        self.events = queue.Queue()

    def start(self):
        threading.Thread(target=self.run, name="pixnova-prepare", daemon=True).start()

    def run(self):
        try: self.events.put({"plan": prepare_plan(*self.args)})
        except Exception as error: self.events.put({"error": str(error)})


class CompletionPanel:
    def __init__(self, parent, station):
        import tkinter as tk
        from tkinter import filedialog, ttk
        from dashboard import Action, label, BG, MUTED
        self.station = station
        self.path = tk.StringVar()
        self.directory = tk.StringVar(value=str(Path.home()/"PixNovaTestReports"))
        self.status = tk.StringVar(value="Choose normal flight firmware before Start test; CSV is saved before USB restoration.")
        panel = tk.Frame(parent, bg=BG); panel.pack(fill="x", pady=(8, 0))
        label(panel, "Normal firmware", size=9).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(panel, textvariable=self.path).grid(row=0, column=1, sticky="ew")
        def choose():
            if station.operation_locked() or station.suite_active or not station.network_idle():
                station.note("Choose normal firmware before starting the test."); return
            selected = filedialog.askopenfilename(filetypes=[("PX4 firmware", "*.px4")])
            if selected: self.path.set(selected)
        Action(panel, "Browse…", choose, width=90).grid(row=0, column=2, padx=(8, 0))
        label(panel, "CSV folder", size=9).grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(panel, textvariable=self.directory).grid(row=1, column=1, sticky="ew", pady=(4, 0))
        def folder():
            if station.operation_locked() or station.suite_active or not station.network_idle():
                station.note("Choose the report folder before starting the test."); return
            selected = filedialog.askdirectory()
            if selected: self.directory.set(selected)
        Action(panel, "Browse…", folder, width=90).grid(row=1, column=2, padx=(8, 0), pady=(4, 0))
        label(panel, variable=self.status, size=9, color=MUTED, anchor="w", justify="left", wraplength=1000).grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)
        label(panel, "Start with P21 on FMU PWM; switch to IOV2 only when prompted. PPM and SBUS OUT go from the flight controller to the fixture.", size=9, color=MUTED, anchor="w", justify="left", wraplength=1000).grid(row=3, column=0, columnspan=3, sticky="ew")
        panel.columnconfigure(1, weight=1)

    def selection(self, demo):
        if not self.directory.get().strip(): raise ValueError("Select the CSV results folder")
        if not demo and not self.path.get().strip():
            raise ValueError("Choose normal flight firmware with Browse before Start test")
        return self.path.get().strip(), self.directory.get().strip(), demo

    def prepared(self, plan, demo):
        image, directory = plan
        self.status.set("After testing: CSV → " + str(directory) + " · " + ("DEMO; no hardware flashed" if demo else "restore " + image.path.name))

    def prepare(self, demo):
        plan = prepare_plan(*self.selection(demo))
        self.prepared(plan, demo)
        return plan
