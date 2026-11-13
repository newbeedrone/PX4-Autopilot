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
MODE_NUMBER = {name: i for i, name in enumerate(("I2C3", "GPS2_I2C", "SBUS", "DSM", "CAN", "ETH", "GPS1_I2C", "POWER1_I2C", "POWER2_I2C", "UART4_I2C3", "ADC6V6_LOW", "ADC6V6_HIGH"), 1)}


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


class ProtocolRun:
    """Interface eligibility is independent of reported onboard faults."""
    def __init__(self, mode, digest, now, send):
        if mode not in TESTS or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid mode or GPIO profile digest")
        self.mode, self.digest, self.send = mode, digest, send
        self.roles = {"source"} if mode in ("CAN", "ETH") else {"source", "probe"}
        self.session, self.token = uuid.uuid4().hex, secrets.randbits(32)
        self.phase, self.deadline = "CHECKING", now + 20
        self.checks = {role: {} for role in self.roles}
        self.checked, self.started, self.samples, self.last_seen = set(), set(), {}, {}
        self.last_ping, self.streak = now, 0
        self.result = ProtocolResult("WAITING", "Fresh startup checks; outputs disabled", session=self.session, token=f"{self.token:08x}")
        self.host_rx, self.host_sequence = 0, 0
        self.last_host = None

    def begin(self):
        for role in sorted(self.roles):
            self.send(role, f"PCHECK {self.session} {self.digest} {self.mode}")

    def command(self, role):
        self.send(role, f"PROTO {self.session} {self.digest} {self.mode} {self.token:08x}")

    def stop(self, reason="Stopped", failed=False):
        for role in self.roles:
            self.send(role, "STOP")
        self.phase = "FAILED" if failed else "STOPPED"
        self.result.status = "FAIL" if failed else "NOT RUN"
        self.result.reason = reason

    def fail(self, reason):
        self.stop(reason, failed=True)

    def handle(self, role, frame, now):
        if self.phase in ("COMPLETE", "FAILED", "STOPPED") or role not in self.roles:
            return
        received = frame.get("_received_at", now)
        if type(received) not in (float, int) or not 0 <= now - received < 2:
            self.fail("Stale USB response"); return
        kind = frame.get("type")
        if kind in ("error", "link_error", "disconnected"):
            self.fail(f"{role}: {frame.get('reason', kind)}"); return
        if frame.get("session") != self.session:
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
                self.deadline = now + 5
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
        if frame.get("mode") != self.mode or any(type(frame.get(k)) is not int or not 0 <= frame[k] <= 0xffffffff for k in keys) or type(frame.get("pass")) is not bool or type(frame.get("detail")) is not int:
            self.fail("Malformed protocol measurement"); return
        old = self.samples.get(role)
        if old and (frame["seq"] <= old["seq"] or any(frame[k] < old[k] for k in ("tx", "rx", "errors"))):
            self.fail("Replayed or regressing protocol counters"); return
        self.last_seen[role] = received
        self.samples[role] = {**frame, "received": received}
        self.result.errors += frame["errors"] - (old["errors"] if old else 0)
        setattr(self.result, role + "_tx", frame["tx"])
        setattr(self.result, role + "_rx", frame["rx"])
        if TESTS[self.mode].get("kind") == "adc":
            if role == "source": self.result.adc_voltage_mv = frame["detail"]
            else: self.result.probe_output_mv = frame["detail"]
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
            else:
                self.fail(f"{role}: protocol errors={frame['errors']}, detail={detail}")
            return
        if role != "source":
            return
        if self.phase == "RC_LOSS":
            # PX4IO must revoke RC validity after the simulator stops sending.
            lost = old is not None and .5 <= received - old["received"] <= 1.8 and not frame["detail"] & ((1 << 1) | (1 << 20) | 1)
            self.streak = self.streak + 1 if lost else 0
            self.result.reason = f"RC stopped: {self.streak}/2 fresh windows with RC validity revoked"
            if self.streak >= 2:
                self.complete("Decoded seven RC channels and verified signal-loss detection; outputs stopped")
            return
        if self.phase != "RUNNING":
            return
        good = frame["pass"] and old is not None and frame["rx"] > old["rx"] and .5 <= received - old["received"] <= 1.8
        if TESTS[self.mode].get("kind") == "adc":
            spec = TESTS[self.mode]
            if not spec["minimum_mv"] <= frame["detail"] <= spec["maximum_mv"]:
                self.fail(f"ADC measured {frame['detail']} mV; expected {spec['minimum_mv']}–{spec['maximum_mv']} mV"); return
            good = good and frame["rx"] >= 10
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
                self.send("probe", "STOP"); self.started.discard("probe")
            else:
                self.complete("Completed protocol measurements; outputs stopped")

    def complete(self, reason):
        self.result.status = "PASS"; self.result.reason = reason; self.phase = "COMPLETE"
        for endpoint in self.roles:
            self.send(endpoint, "STOP")

    def accept_udp(self, data, sequence, now):
        if self.mode != "ETH" or self.phase != "RUNNING" or sequence <= self.host_sequence or data != packet("ETH", self.token, sequence, True):
            self.fail("Invalid/replayed Ethernet response"); return
        self.host_sequence = sequence; self.host_rx += 1; self.last_host = now
        self.result.host_rx = self.host_rx

    def tick(self, now):
        if self.phase in ("COMPLETE", "FAILED", "STOPPED"):
            return
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
        return {**asdict(self.result), "mode": self.mode, "phase": self.phase, "checks": self.checks}
