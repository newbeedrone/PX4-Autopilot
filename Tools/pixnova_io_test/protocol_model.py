"""Pure protocol validation and startup sequencing; no serial or GUI access."""
import binascii
from dataclasses import dataclass, asdict
import json
from pathlib import Path
import re
import secrets
import struct
import uuid

from peripherals import DEVICES, Peripherals, interface_result

TESTS = {row["id"]: row for row in json.loads(Path(__file__).with_name("protocols.json").read_text(encoding="utf-8"))["tests"]}
MODE_NUMBER = {name: i for i, name in enumerate(("I2C3", "GPS2_I2C", "SBUS", "DSM", "CAN", "ETH", "GPS1_I2C", "POWER1_I2C", "POWER2_I2C", "UART4_I2C3", "ADC6V6_LOW", "ADC6V6_HIGH", "ADC3V3_LOW", "ADC3V3_HIGH"), 1)}
MODE_NUMBER.update({f"IO_PWM{i}": 14 + i for i in range(1, 9)})
MODE_NUMBER["SBUS_OUT"] = 23


def packet(mode, token, sequence, response=False):
    body = bytearray(b"PN\x01" + bytes([MODE_NUMBER[mode]]) + struct.pack("<II", token, sequence))
    body.append(0xa5 if response else 0x5a)
    body.extend(((token >> (8 * (i % 4))) ^ sequence ^ (i * 37) ^ body[12]) & 255 for i in range(13, 30))
    return bytes(body) + struct.pack("<H", binascii.crc_hqx(body, 0xffff))


def rc_values(token):
    return [1100 + 400 * (((token + channel) & 0xffffffff) % 3) for channel in range(7)]


@dataclass
class ProtocolResult:
    status: str = "NOT RUN"
    reason: str = "No physical protocol measurement"
    source_tx: int = 0
    source_rx: int = 0
    probe_tx: int = 0
    probe_rx: int = 0
    host_rx: int = 0
    errors: int = 0
    session: str = ""
    token: str = ""
    adc_voltage_mv: int | None = None
    probe_output_mv: int | None = None
    frequency_hz: float | None = None
    capture_diagnostics: list[int] | None = None


class ProtocolRun:
    """Interface eligibility is independent of reported onboard faults."""
    def __init__(self, mode, digest, now, send, session_stop=False):
        if mode not in TESTS or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid mode or GPIO profile digest")
        self.mode, self.digest, self.send = mode, digest, send
        self.roles = {"source"} if mode in ("CAN", "ETH") else {"source", "probe"}
        self.session, self.token = uuid.uuid4().hex, secrets.randbits(32)
        self.stop_command = f"STOP {self.session}" if session_stop else "STOP"
        self.phase, self.deadline = "CHECKING", now + 20
        self.checks = {role: {} for role in self.roles}
        self.checked, self.started, self.samples, self.last_seen = set(), set(), {}, {}
        self.last_ping, self.streak = now, 0
        self.result = ProtocolResult("WAITING", "Fresh startup checks; outputs disabled", session=self.session, token=f"{self.token:08x}")
        self.host_rx, self.host_sequence = 0, 0
        self.last_host = None
        self.now = now
        self.stop_acks, self.stop_errors = set(), {}
        self.outputs_stopped = False
        self.paired_probe_rx = 0
        self.paired_probe_seq = 0

    def begin(self):
        for role in sorted(self.roles):
            self.send(role, f"PCHECK {self.session} {self.digest} {self.mode}")

    def command(self, role):
        self.send(role, f"PROTO {self.session} {self.digest} {self.mode} {self.token:08x}")

    def stop(self, reason="Stopped", failed=False, now=None):
        self._request_stop("FAILED" if failed else "STOPPED", "FAIL" if failed else "NOT RUN", reason, now)

    def _request_stop(self, phase, status, reason, now=None):
        if self.phase in ("COMPLETE", "FAILED", "STOPPED"):
            return
        if self.phase == "STOPPING":
            # A fault or operator cancellation can revoke a pending PASS.
            # Repeated Stop clicks must not extend the confirmation deadline.
            if status != "PASS" and self.finish_status != "FAIL":
                self.finish_phase, self.finish_status, self.finish_reason = phase, status, reason
                self._stop_message()
            return
        self.now = self.now if now is None else now
        self.finish_phase, self.finish_status, self.finish_reason = phase, status, reason
        self.phase, self.stop_started, self.deadline = "STOPPING", self.now, self.now + 3
        self.stop_acks.clear()
        self._stop_message()
        for role in sorted(self.roles):
            try:
                self.send(role, self.stop_command)
            except RuntimeError as error:
                self.stop_errors[role] = str(error)

    def _stop_message(self):
        self.result.status = "VERIFYING" if self.finish_status == "PASS" else self.finish_status
        pending = ", ".join(sorted(self.roles - self.stop_acks))
        self.result.reason = f"{self.finish_reason} · waiting for STOP confirmation: {pending}"

    def _finish_stop(self):
        self.outputs_stopped = True
        self.phase, self.result.status = self.finish_phase, self.finish_status
        self.result.reason = self.finish_reason + " · outputs stopped (confirmed)"

    def fail(self, reason):
        self.stop(reason, failed=True)

    def handle(self, role, frame, now):
        if self.phase in ("COMPLETE", "FAILED", "STOPPED") or role not in self.roles:
            return
        self.now = now
        received = frame.get("_received_at", now)
        if type(received) not in (float, int) or not 0 <= now - received < 2:
            self.fail("Stale USB response"); return
        kind = frame.get("type")
        if kind in ("error", "link_error", "disconnected"):
            reason = frame.get("reason", kind)
            if reason == "protocol_setup_failed" and self.mode == "ETH":
                reason = f"Ethernet PHY/link setup failed ({frame.get('detail')}); check the adapter, cable and PC interface"
            elif reason == "protocol_setup_failed" and TESTS[self.mode].get("kind") == "io_pwm":
                reason = f"IOV2 setup failed ({frame.get('detail')}); IO must be idle with zero disarmed outputs; power-cycle the board after loading test firmware"
            self.fail(f"{role}: {reason}"); return
        if frame.get("session") != self.session:
            return
        if self.phase == "STOPPING":
            if now > self.deadline:
                self.tick(now)
                return
            if kind == "stopped" and received >= self.stop_started:
                if frame.get("reason") != "requested":
                    self.fail(f"{role}: unexpected STOP reason: {frame.get('reason')}")
                    return
                self.stop_acks.add(role)
                if self.stop_acks == self.roles:
                    self._finish_stop()
                else:
                    self._stop_message()
            return
        if kind == "stopped":
            if self.phase == "RC_LOSS" and role == "probe":
                return
            self.fail(f"{role}: endpoint stopped"); return
        if kind == "check" and self.phase == "CHECKING":
            name = frame.get("test")
            if not isinstance(name, str) or name in self.checks[role] or type(frame.get("pass")) is not bool or type(frame.get("detail")) is not int:
                self.fail("Invalid or duplicate startup check"); return
            good = frame["pass"]
            if role == "source" and name in DEVICES:
                try:
                    good = Peripherals.result(role, frame)[1]["pass"] and good
                except ValueError:
                    good = False
            else:
                good = interface_result(frame)
            self.checks[role][name] = {**frame, "pass": good}
        elif kind == "checked" and self.phase == "CHECKING":
            required = {"hrt_clock", "gpio_inputs"} if role == "source" else {"hrt_clock", "gpio_inputs", "probe_protocol_firmware"}
            known = required | (set(DEVICES) if role == "source" else set())
            if role in self.checked or frame.get("pass") is not True or frame.get("mode") != self.mode or not required.issubset(self.checks[role]) or not set(self.checks[role]).issubset(known) or not all(self.checks[role][key]["pass"] for key in required):
                self.fail(f"{role}: external interface check failed or incomplete"); return
            self.checked.add(role)
            if self.checked == self.roles:
                first = "probe" if "probe" in self.roles else "source"
                self.phase = "STARTING_PROBE" if first == "probe" else "STARTING_SOURCE"
                self.deadline = now + (20 if self.mode == "ETH" else 5)
                self.command(first)
        elif kind == "started":
            expected = "probe" if self.phase == "STARTING_PROBE" else "source" if self.phase == "STARTING_SOURCE" else None
            if role != expected or frame.get("mode") != self.mode or frame.get("role") != role:
                self.fail("Unexpected protocol start acknowledgement"); return
            self.started.add(role); self.last_seen[role] = received
            if role == "probe":
                self.phase = "STARTING_SOURCE"; self.deadline = now + 5; self.command("source")
            else:
                self.phase = "RUNNING"; self.deadline = now + 25
                self.result.reason = "Waiting for verified protocol data"
        elif kind == "heartbeat" and role in self.started:
            if frame.get("running") is not True:
                self.fail("Endpoint is not running"); return
            self.last_seen[role] = received
        elif kind == "protocol_sample" and role in self.started:
            self.accept_sample(role, frame, received, now)

    def accept_sample(self, role, frame, received, now):
        keys = ("seq", "tx", "rx", "errors")
        if frame.get("mode") != self.mode or any(type(frame.get(k)) is not int or not 0 <= frame[k] <= 0xffffffff for k in keys) or type(frame.get("pass")) is not bool or type(frame.get("detail")) is not int or not -0x80000000 <= frame["detail"] <= 0x7fffffff:
            self.fail("Malformed protocol measurement"); return
        old = self.samples.get(role)
        if old and (frame["seq"] <= old["seq"] or any(frame[k] < old[k] for k in ("tx", "rx", "errors"))):
            self.fail("Replayed or regressing protocol counters"); return
        self.last_seen[role] = received
        self.samples[role] = {**frame, "received": received}
        if self.mode == "SBUS_OUT" and role == "probe" and "capture" in frame:
            capture = frame["capture"]
            if not isinstance(capture, list) or len(capture) != 5 or any(type(v) is not int or not -0x80000000 <= v <= 0x7fffffff for v in capture):
                self.fail("Malformed SBUS capture diagnostics"); return
            self.result.capture_diagnostics = list(capture)
        self.result.errors += frame["errors"] - (old["errors"] if old else 0)
        setattr(self.result, role + "_tx", frame["tx"])
        setattr(self.result, role + "_rx", frame["rx"])
        if TESTS[self.mode].get("kind") == "adc":
            if role == "source": self.result.adc_voltage_mv = frame["detail"]
            else: self.result.probe_output_mv = frame["detail"]
        if TESTS[self.mode].get("kind") in ("io_pwm", "sbus_out") and role == "probe":
            self.result.frequency_hz = frame["detail"] / 1000
            expected = TESTS[self.mode]["expected_hz"]
            if self.mode == "SBUS_OUT" and frame["detail"] > 0 and not TESTS[self.mode]["minimum_hz"] <= self.result.frequency_hz <= TESTS[self.mode]["maximum_hz"]:
                self.fail(f"SBUS OUT frame rate {self.result.frequency_hz:.3f} Hz at P8.2 / PG5; expected 40-55 frames/s")
                return
            if self.mode == "SBUS_OUT" and old and frame["rx"] == old["rx"] and not frame["pass"]:
                self.fail("SBUS OUT: no fresh decoded frames at P8.2 / PG5; check signal and ground")
                return
            if self.mode != "SBUS_OUT" and frame["detail"] > 0 and abs(self.result.frequency_hz - expected) > expected * .02:
                self.fail(f"IOV2 PWM frequency {self.result.frequency_hz:.3f} Hz; expected {expected:.3f} Hz at {TESTS[self.mode]['probe_connector']}.{TESTS[self.mode]['probe_connector_pin']}")
                return
        if frame["errors"]:
            detail = frame.get("detail")
            if TESTS[self.mode].get("kind") == "i2c":
                reason = {-99: "No I2C slave ACK; check the selected connector and SCL/SDA wiring",
                          -6: "No I2C slave ACK; check the selected connector and SCL/SDA wiring",
                          -110: "I2C transaction timed out; check line levels and pull-ups",
                          -74: "I2C reply failed data/CRC validation"}.get(detail, "I2C slave transaction failed")
                self.fail(f"{role}: {reason} (errors={frame['errors']}, detail={detail})")
            elif TESTS[self.mode].get("kind") == "adc":
                self.fail(f"{role}: ADC voltage test failed; measured/readback {frame['detail']} mV")
            elif self.mode == "SBUS_OUT" and role == "probe" and self.result.capture_diagnostics:
                code, byte, bit, elapsed, phase = self.result.capture_diagnostics
                reason = {1: "edge timing", 2: "missed or duplicated edge", 3: "UART parity", 4: "UART stop bit", 5: "frame length", 6: "payload"}.get(code, "capture")
                self.fail(f"SBUS OUT {reason} error at byte {byte}, bit {bit}; edge interval {elapsed} us, phase {phase} us")
            else:
                self.fail(f"{role}: protocol errors={frame['errors']}, detail={detail}")
            return
        if role == "probe" and TESTS[self.mode].get("kind") in ("io_pwm", "sbus_out") and self.phase == "RUNNING":
            if not frame["pass"] or (old and (frame["rx"] <= old["rx"] or frame["seq"] != old["seq"] + 1)):
                self.streak = 0
                self.result.status = "WAITING"
                self.result.reason = "0/3 consecutive fresh receiver windows"
        if role != "source":
            return
        if self.phase == "RC_LOSS":
            # PX4IO must revoke RC validity after the simulator stops sending.
            lost = old is not None and .5 <= received - old["received"] <= 1.8 and not frame["detail"] & ((1 << 1) | (1 << 20) | 1)
            self.streak = self.streak + 1 if lost else 0
            self.result.reason = f"RC stopped: {self.streak}/2 fresh windows with RC validity revoked"
            if self.streak >= 2:
                self.complete("Decoded seven RC channels and verified signal-loss detection")
            return
        if self.phase != "RUNNING":
            return
        good = frame["pass"] and old is not None and frame["rx"] > old["rx"] and .5 <= received - old["received"] <= 1.8
        if TESTS[self.mode].get("kind") == "adc":
            spec = TESTS[self.mode]
            if not spec["minimum_mv"] <= frame["detail"] <= spec["maximum_mv"]:
                self.fail(f"ADC measured {frame['detail']} mV; expected {spec['minimum_mv']}–{spec['maximum_mv']} mV"); return
            good = good and frame["rx"] >= 10
        if TESTS[self.mode].get("kind") == "io_pwm":
            good = good and abs(frame["detail"] / 1000 - TESTS[self.mode]["expected_hz"]) <= TESTS[self.mode]["expected_hz"] * .02 and frame["rx"] >= 10
        if self.mode in ("SBUS", "DSM"):
            channels = frame.get("channels")
            if not isinstance(channels, list) or len(channels) != 7 or any(type(v) is not int or not 0 <= v <= 2500 for v in channels):
                self.fail("Invalid RC channel readback"); return
            good = good and all(abs(a-b) <= 5 for a, b in zip(channels, rc_values(self.token)))
        if "probe" in self.roles:
            peer = self.samples.get("probe")
            good = good and peer is not None and 0 <= now - peer["received"] < 1.8 and peer["tx"] >= 10
            if TESTS[self.mode].get("kind") == "i2c":
                good = good and peer["rx"] >= 10 and peer["pass"]
            elif TESTS[self.mode].get("kind") == "adc":
                good = good and peer["rx"] >= 10 and peer["pass"] and peer["detail"] == TESTS[self.mode]["stimulus_mv"]
            elif TESTS[self.mode].get("kind") == "io_pwm":
                good = good and peer["rx"] >= 10 and peer["pass"] and peer["seq"] > self.paired_probe_seq and peer["rx"] > self.paired_probe_rx and abs(peer["detail"] / 1000 - TESTS[self.mode]["expected_hz"]) <= TESTS[self.mode]["expected_hz"] * .02
            elif self.mode == "SBUS_OUT":
                good = good and frame["detail"] == 50000 and frame["rx"] >= 10 and peer["rx"] >= 20 and peer["pass"] and peer["seq"] > self.paired_probe_seq and peer["rx"] > self.paired_probe_rx and 40000 <= peer["detail"] <= 55000
            if peer and TESTS[self.mode].get("kind") in ("io_pwm", "sbus_out"):
                self.paired_probe_seq = peer["seq"]
                self.paired_probe_rx = peer["rx"]
        if self.mode == "ETH":
            good = good and self.host_rx >= 20 and self.last_host is not None and now - self.last_host < 1
        if self.mode == "CAN":
            # Bits 0/1 independently confirm >=20 receives on each controller.
            good = good and frame["detail"] == 3 and frame["tx"] >= 40 and frame["rx"] >= 40
        self.streak = self.streak + 1 if good else 0
        self.result.status = "VERIFYING" if good else "WAITING"
        self.result.reason = f"{self.streak}/3 consecutive fresh protocol windows"
        if self.streak >= 3:
            if self.mode in ("SBUS", "DSM"):
                self.phase = "RC_LOSS"; self.deadline = now + 6; self.streak = 0
                self.result.reason = "Channel pattern verified; stopping simulator to verify RC signal loss"
                self.send("probe", self.stop_command); self.started.discard("probe")
            else:
                self.complete("Completed protocol measurements")

    def complete(self, reason):
        self._request_stop("COMPLETE", "PASS", reason)

    def accept_udp(self, data, sequence, now):
        self.now = now
        if self.mode != "ETH" or self.phase != "RUNNING" or sequence <= self.host_sequence or data != packet("ETH", self.token, sequence, True):
            self.fail("Invalid/replayed Ethernet response"); return
        self.host_sequence = sequence; self.host_rx += 1; self.last_host = now
        self.result.host_rx = self.host_rx

    def tick(self, now):
        if self.phase in ("COMPLETE", "FAILED", "STOPPED"):
            return
        self.now = now
        if self.phase == "STOPPING":
            if now > self.deadline:
                pending = ", ".join(sorted(self.roles - self.stop_acks))
                self.phase, self.result.status = "FAILED", "FAIL"
                self.result.reason = f"{self.finish_reason} · STOP unconfirmed: {pending}; reconnect USB before testing"
            return  # No heartbeat may renew an output lease while stopping.
        if now > self.deadline:
            self.fail(f"{self.phase}: protocol test timed out"); return
        if any(now - self.last_seen[r] > 2 for r in self.started):
            self.fail("USB protocol heartbeat expired"); return
        if self.phase in ("RUNNING", "RC_LOSS") and any(r in self.samples and now - self.samples[r]["received"] > 2 for r in self.started):
            self.fail("Protocol measurement expired"); return
        if now - self.last_ping >= .4:
            for role in self.started:
                self.send(role, f"PING {self.session}")
            self.last_ping = now

    def snapshot(self):
        return {**asdict(self.result), "mode": self.mode, "phase": self.phase, "checks": self.checks,
                "outputs_stopped": self.outputs_stopped, "stop_confirmed_roles": sorted(self.stop_acks),
                "stop_errors": dict(self.stop_errors), "valid_windows": self.streak}
