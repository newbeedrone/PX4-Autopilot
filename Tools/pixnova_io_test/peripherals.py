"""Flight-controller power-on sensor and read-only storage checks."""

from copy import deepcopy


SENSORS = {
    "icm45686_spi1": {"name": "ICM45686 #1", "bus": "SPI1 · CS PI9", "id": 0xE9},
    "icm45686_spi2": {"name": "ICM45686 #2", "bus": "SPI2 · CS PH5", "id": 0xE9},
    "icm45686_spi3": {"name": "ICM45686 #3", "bus": "SPI3 · CS PI4", "id": 0xE9},
    "bmm150_i2c4": {"name": "BMM150", "bus": "I2C4 · 0x10", "id": 0x32},
    "bmp388_i2c4": {"name": "BMP388", "bus": "I2C4 · 0x76", "id": 0x50},
    "icp20100_i2c2": {"name": "ICP-20100", "bus": "I2C2 · 0x63", "id": 0x63},
}
STORAGE = {
    "eeprom_base_i2c3": {"name": "Base EEPROM", "bus": "I2C3 · 0x51", "id": 32, "kind": "storage", "unit": "B read",
                          "target": "32 B read", "note": "24LC64T, configured 8 KiB. Read 16 bytes at each end; no writes. Contents and full capacity are not validated."},
    "eeprom_imu_i2c4": {"name": "IMU EEPROM", "bus": "I2C4 · 0x50", "id": 32, "kind": "storage", "unit": "B read",
                         "target": "32 B read", "note": "24LC64T, configured 8 KiB. Read 16 bytes at each end; calibration data is not modified."},
    "fram_spi5": {"name": "SPI FRAM", "bus": "SPI5 · CS PG7", "id": 32768, "kind": "storage", "unit": "KiB",
                    "target": "32 KiB", "note": "FM25V02/A FRAM in the board configuration. Live RDID, 32 KiB driver geometry and read samples at both ends. No writes or erase."},
    "sdcard": {"name": "microSD", "bus": "SDMMC2 · /dev/mmcsd0", "id": 1, "kind": "storage", "unit": "MiB", "minimum": True,
                 "demo": 16384, "target": "Readable card", "note": "Identify card capacity and read the first and last sector. Filesystem, write speed and full-media capacity are not tested; no formatting or writes."},
}
DEVICES = {**SENSORS, **STORAGE}
EXPECTED = {"source": {key: value["id"] for key, value in DEVICES.items()},
            "probe": {"probe_input_only_firmware": 1}}


def display_value(spec, value):
    if value is None:
        return "—"
    if spec.get("unit") == "KiB":
        return f"{value / 1024:g} KiB"
    if spec.get("kind") == "storage":
        return f"{value:,} {spec['unit']}"
    return f"0x{value:02X}" if value >= 0 else f"error {value}"


class Peripherals:
    def __init__(self):
        self.reset()

    def reset(self):
        self.power = {role: {} for role in EXPECTED}
        self.summaries = {}
        self.latest = {key: {"status": "WAITING", "detail": None, "stage": "POWER-ON"} for key in DEVICES}
        self.current_seen = set()

    @staticmethod
    def result(role, frame):
        name, passed, detail = frame.get("test"), frame.get("pass"), frame.get("detail")
        if not isinstance(name, str) or name not in EXPECTED[role] or type(passed) is not bool or type(detail) is not int or not -0x80000000 <= detail <= 0x7fffffff:
            raise ValueError("Invalid onboard peripheral result")
        spec = DEVICES.get(name, {})
        matches = detail >= EXPECTED[role][name] if spec.get("minimum") else detail == EXPECTED[role][name]
        good = passed and matches
        return name, {"status": "PASS" if good else "FAIL", "detail": detail, "pass": good}

    def power_result(self, role, frame):
        if role in self.summaries:
            raise ValueError("Unexpected power-on results after completion")
        name, result = self.result(role, frame)
        if name in self.power[role]:
            raise ValueError("Duplicate power-on peripheral result")
        self.power[role][name] = result
        if role == "source":
            self.latest[name] = {**result, "stage": "POWER-ON"}

    def finish_power(self, role, frame):
        if role in self.summaries or type(frame.get("pass")) is not bool or type(frame.get("count")) is not int or frame["count"] != len(EXPECTED[role]) or set(self.power[role]) != set(EXPECTED[role]):
            raise ValueError("Incomplete power-on peripheral results")
        checked, uptime = frame.get("checked_us"), frame.get("uptime_us")
        if type(checked) is not int or type(uptime) is not int or not 0 <= checked <= uptime <= 0x7fffffffffffffff:
            raise ValueError("Invalid power-on check timestamp")
        passed = frame["pass"] and all(item["pass"] for item in self.power[role].values())
        self.summaries[role] = {"pass": passed, "checked_us": checked, "uptime_us": uptime}

    def invalidate(self, role, reason):
        self.summaries[role] = {"pass": False, "error": reason}
        if role == "source":
            for result in self.latest.values():
                result["status"] = "FAIL"
                result["pass"] = False

    def passed(self, roles=("source", "probe")):
        return all(self.summaries.get(role, {}).get("pass") is True for role in roles)

    def begin_current(self):
        self.current_seen.clear()
        self.latest = {key: {"status": "VERIFYING", "detail": None, "stage": "PRE-TEST"} for key in DEVICES}

    def end_current(self):
        for name, result in self.latest.items():
            if name not in self.current_seen:
                result["status"] = "WAITING"

    def current_result(self, frame):
        if frame.get("test") not in DEVICES:
            return None
        name, result = self.result("source", frame)
        if name in self.current_seen:
            raise ValueError("Duplicate pre-test peripheral result")
        self.current_seen.add(name)
        self.latest[name] = {**result, "stage": "PRE-TEST"}
        return result["pass"]

    @property
    def current_passed(self):
        return self.current_seen == set(DEVICES) and all(result["status"] == "PASS" for result in self.latest.values())

    def snapshot(self):
        return deepcopy({"power_on": self.power, "summaries": self.summaries, "latest": self.latest, "definitions": DEVICES,
                         "onboard_passed": self.passed(("source",))})
