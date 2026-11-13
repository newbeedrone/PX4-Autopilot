"""Session-bound frequency assessment, independent of serial ports and Tk."""

from dataclasses import dataclass
import math
import time


@dataclass
class Result:
    frequency_hz: float | None = None
    error_percent: float | None = None
    status: str = "NOT RUN"
    reason: str = ""
    streak: int = 0
    sequence: int = 0
    received_at: float = 0
    voltage_low_mv: int | None = None
    voltage_high_mv: int | None = None


class Assessment:
    def __init__(self, profile):
        self.profile = profile
        self.session = ""
        self.started_at = None
        self.results = [Result() for _ in profile["channels"]]

    def reset(self, session):
        self.session = session
        self.started_at = None
        self.results = [Result(status="WAITING") for _ in self.profile["channels"]]

    def start_measurements(self, now=None):
        self.started_at = time.monotonic() if now is None else now

    def accept(self, frame, now=None):
        if frame.get("type") != "sample" or not self.session or frame.get("session") != self.session:
            return False
        index, sequence = frame.get("channel"), frame.get("seq")
        if type(index) is not int or not 0 <= index < len(self.results):
            return False
        if self.profile["channels"][index].get("method") == "adc_voltage":
            return False
        result = self.results[index]
        if type(sequence) is not int or not 1 <= sequence <= 0xffffffff or sequence <= result.sequence:
            return False
        received_at = time.monotonic() if now is None else now
        minimum_spacing = self.profile["report_interval_ms"] / 2000
        gap = result.sequence != 0 and (sequence != result.sequence + 1
                                       or not minimum_spacing <= received_at - result.received_at <= 2.5)
        result.sequence = sequence
        result.received_at = received_at
        measured, edges, span, errors = [frame.get(key) for key in ("frequency_millihz", "edges", "span_us", "timing_errors")]
        result.frequency_hz = result.error_percent = None
        if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in (measured, edges, errors)) or type(span) is not int or not 0 <= span <= self.profile["report_interval_ms"] * 2000:
            self._fail(result, "Invalid measurement frame")
            return True
        if edges > span // self.profile["sample_interval_us"] + 1:
            self._fail(result, "Edge count exceeds the sampling rate")
            return True
        if errors:
            self._fail(result, "Sampling interrupt overrun")
            return True
        if edges < 3 or span <= 0:
            self._fail(result, "No signal or insufficient edges")
            return True
        computed = (edges - 1) * 1_000_000_000 // span
        if abs(computed - measured) > 1:
            self._fail(result, "Inconsistent edge count and time span")
            return True
        frequency = measured / 1000
        expected = self.profile["channels"][index]["expected_hz"]
        error = (frequency - expected) / expected * 100
        result.frequency_hz, result.error_percent = frequency, error
        if not math.isfinite(error) or abs(error) > self.profile["tolerance_percent"]:
            reason = "Frequency outside tolerance; check wiring"
            matches = [row["id"] for i, row in enumerate(self.profile["channels"])
                       if i != index and "expected_hz" in row and abs(frequency - row["expected_hz"]) / row["expected_hz"] * 100
                       <= self.profile["tolerance_percent"]]
            if matches:
                reason += "; matches " + ", ".join(matches) + " frequency"
            self._fail(result, reason)
            return True
        result.streak = 1 if gap else result.streak + 1
        result.status = "PASS" if result.streak >= self.profile["required_windows"] else "VERIFYING"
        result.reason = f"{result.streak} consecutive valid windows"
        return True

    @staticmethod
    def _fail(result, reason):
        result.status, result.reason, result.streak = "FAIL", reason, 0

    def expire(self, now=None, timeout=2.5):
        now = time.monotonic() if now is None else now
        for row, result in zip(self.profile["channels"], self.results):
            if row.get("method") == "adc_voltage":
                continue
            last_seen = result.received_at if result.sequence else self.started_at
            if last_seen is not None and now - last_seen > timeout:
                self._fail(result, "Measurement timeout")

    def invalidate(self, reason):
        for result in self.results:
            self._fail(result, reason)

    @property
    def all_passed(self):
        return bool(self.results) and all(result.status == "PASS" for row, result in zip(self.profile["channels"], self.results)
                                         if row.get("method") != "adc_voltage")
