"""Sequential receiver assessment and confirmed GPIO shutdown for the full suite."""
from dataclasses import asdict


class GpioSequence:
    def __init__(self, profile, groups=None):
        available = list(dict.fromkeys(row["group"] for row in profile["channels"] if row.get("method") != "adc_voltage"))
        self.groups = [group for group in available if group != "FMU PWM"] + [group for group in available if group == "FMU PWM"]
        if groups is not None:
            if not groups or len(groups) != len(set(groups)) or not set(groups).issubset(available):
                raise ValueError("Choose distinct GPIO connector groups from the current profile")
            self.groups = [group for group in self.groups if group in groups]
        self.indices = {group: [i for i, row in enumerate(profile["channels"])
                                if row["group"] == group and row.get("method") != "adc_voltage"] for group in self.groups}
        self.position, self.started_at, self.completed = 0, None, {}

    @property
    def done(self): return self.position == len(self.groups)

    @property
    def group(self): return None if self.done else self.groups[self.position]

    @property
    def active_indices(self): return self.indices[self.group] if not self.done else []

    def tick(self, assessment, now):
        if self.done: return True
        if self.started_at is None:
            self.started_at = now
            assessment.started_at = now
        rows = [assessment.results[i] for i in self.active_indices]
        if all(row.status == "PASS" for row in rows) or now - self.started_at >= 6:
            for row in rows:
                if row.status not in ("PASS", "FAIL"):
                    row.status, row.reason = "FAIL", "Insufficient fresh measurement windows"
            self.completed[self.group] = [asdict(row) for row in rows]
            self.position += 1
            self.started_at = now
            assessment.started_at = now
        return self.done


class GpioStop:
    def __init__(self, session, now, send):
        self.session, self.started_at, self.deadline = session, now, now + 3
        self.confirmed, self.error, self.done = set(), "", False
        for role in ("source", "probe"):
            try: send(role, "STOP " + session)
            except RuntimeError as error: self.error = str(error)
        if self.error: self.done = True

    def handle(self, role, frame, now):
        if self.done or frame.get("session") != self.session: return
        if now > self.deadline:
            self.tick(now); return
        if frame.get("type") == "stopped" and self.started_at <= frame.get("_received_at", now) <= now:
            if frame.get("reason") != "requested":
                self.error = role + ": GPIO cleanup failed"
            else:
                self.confirmed.add(role)
            self.done = self.confirmed == {"source", "probe"} or bool(self.error)

    def tick(self, now):
        if not self.done and now > self.deadline:
            self.done, self.error = True, "GPIO STOP unconfirmed; reconnect both USB endpoints"
