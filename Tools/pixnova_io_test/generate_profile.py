#!/usr/bin/env python3
"""Validate a wiring profile and generate the matching firmware pin table."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re


# Reviewed external FMU signals only. A profile may not enable power controls,
# internal buses or debug/USB pins by removing its own protected-pins metadata.
ALLOWED_SOURCE_PINS = set("PA3 PA6 PA15 PB3 PB6 PB7 PB8 PB9 PC0 PC2 PC3 PC8 PC9 PC12 PD2 PD3 PD4 PD5 PD10 PD11 PD12 PD13 PD14 PE0 PE1 PE6 PE8 PE10 PE11 PF5 PF6 PF8 PF9 PF10 PG14 PH6 PH9 PH10 PH11 PH12 PH13 PH14 PI0 PI5 PI10".split())


def load_profile(path):
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    if profile.get("schema") != 1 or profile.get("protocol") != 3:
        raise ValueError("Unsupported profile version")
    channels = profile["channels"]
    if not isinstance(channels, list) or not 1 <= len(channels) <= 64:
        raise ValueError("A profile must contain 1–64 channels")
    for key, low, high in [("sample_interval_us", 50, 10000), ("wave_interval_us", 50, 10000),
                           ("required_windows", 3, 100)]:
        value = profile[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid {key}")
    if profile["report_interval_ms"] != 1000 or profile["lease_ms"] != 2000:
        raise ValueError("Protocol 3 requires 1000 ms reports and a 2000 ms heartbeat lease")
    tolerance = profile["tolerance_percent"]
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 < tolerance <= 10:
        raise ValueError("Invalid frequency tolerance")
    protected = {"PA11", "PA12", "PA13", "PA14", "PH0", "PH1", "PD8", "PD9"}
    source_periods, period_pins, probe_pins, ids = {}, {}, set(), set()
    positions, group_connectors = set(), {}
    for channel in channels:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", channel["id"]) or channel["id"] in ids:
            raise ValueError("Invalid or duplicate channel ID")
        ids.add(channel["id"])
        for role in ("source_pin", "probe_pin"):
            pin = channel[role]
            match = re.fullmatch(r"P([A-I])(\d|1[0-5])", pin)
            if not match or pin in protected:
                raise ValueError(f"Invalid or protected {role}: {pin}")
        if channel["source_pin"] in {"PC6", "PC7"}:
            raise ValueError("The PX4IO serial connection is excluded")
        if channel["source_pin"] not in ALLOWED_SOURCE_PINS:
            raise ValueError(f"Source pin is outside the reviewed external signals: {channel['source_pin']}")
        if channel["source_pin"] in profile.get("protected_source_pins", {}):
            raise ValueError(f"Onboard sensor pin cannot be toggled: {channel['source_pin']}")
        period = channel["half_period_us"]
        if type(period) is not int or not 500 <= period <= 50000 or period % profile["wave_interval_us"]:
            raise ValueError("Invalid half period")
        if period < 4 * profile["sample_interval_us"]:
            raise ValueError("Insufficient probe samples per waveform half period")
        previous = source_periods.setdefault(channel["source_pin"], period)
        if previous != period:
            raise ValueError("A shared source pin must use the same frequency")
        if period_pins.setdefault(period, channel["source_pin"]) != channel["source_pin"]:
            raise ValueError("Independent source nets must use distinct frequencies")
        if channel["probe_pin"] in probe_pins:
            raise ValueError("Fixture inputs must be independent")
        probe_pins.add(channel["probe_pin"])
        expected = channel["expected_hz"]
        if type(expected) not in (int, float) or not math.isfinite(expected) or abs(expected - 500000 / period) > 1e-6:
            raise ValueError("Expected frequency does not match the half period")
        group, connector, pin = channel["group"], channel["probe_connector"], channel["probe_connector_pin"]
        if not isinstance(group, str) or not 1 <= len(group) <= 32 or not re.fullmatch(r"P\d{1,2}", connector):
            raise ValueError("Invalid port group or connector")
        if type(pin) is not int or not 1 <= pin <= 32 or (connector, pin) in positions:
            raise ValueError("Invalid or duplicate fixture connector position")
        positions.add((connector, pin))
        if group_connectors.setdefault(group, connector) != connector:
            raise ValueError("Each graphical port group must use one fixture connector")
    unique = sorted({500000 / period for period in source_periods.values()})
    tolerance /= 100
    for low, high in zip(unique, unique[1:]):
        if low * (1 + tolerance) >= high * (1 - tolerance):
            raise ValueError("Frequency acceptance bands overlap")
    canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return profile, hashlib.sha256(canonical).hexdigest()


def generate_header(profile, digest):
    lines = ["// Generated from Tools/pixnova_io_test/profile.json. Do not edit.",
             "#pragma once", "#include <stdint.h>",
             "struct GpioTestChannel { const char *name; uint8_t tx_port, tx_pin, rx_port, rx_pin; uint32_t half_period_us; };",
             f'static constexpr const char *GPIO_TEST_PROFILE_SHA = "{digest}";',
             f'static constexpr unsigned GPIO_TEST_PROTOCOL = {profile["protocol"]};',
             f'static constexpr uint32_t GPIO_TEST_WAVE_US = {profile["wave_interval_us"]};',
             f'static constexpr uint32_t GPIO_TEST_SAMPLE_US = {profile["sample_interval_us"]};',
             f'static constexpr uint32_t GPIO_TEST_REPORT_US = {profile["report_interval_ms"] * 1000};',
             f'static constexpr uint32_t GPIO_TEST_LEASE_US = {profile["lease_ms"] * 1000};',
             "static constexpr GpioTestChannel GPIO_TEST_CHANNELS[] = {"]
    for row in profile["channels"]:
        tx, rx = row["source_pin"], row["probe_pin"]
        lines.append(f'    {{"{row["id"]}", {ord(tx[1])-65}, {tx[2:]}, {ord(rx[1])-65}, {rx[2:]}, {row["half_period_us"]}}},')
    lines.extend(["};", "static constexpr unsigned GPIO_TEST_COUNT = sizeof(GPIO_TEST_CHANNELS) / sizeof(GPIO_TEST_CHANNELS[0]);", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("profile.json"))
    parser.add_argument("--header", type=Path)
    parser.add_argument("--wiring", type=Path)
    args = parser.parse_args()
    profile, digest = load_profile(args.profile)
    if args.header:
        args.header.parent.mkdir(parents=True, exist_ok=True)
        args.header.write_text(generate_header(profile, digest), encoding="utf-8")
    if args.wiring:
        fields = ["id", "group", "source_pin", "probe_connector", "probe_connector_pin", "probe_pin", "probe_net", "expected_hz", "note"]
        with args.wiring.open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(profile["channels"])
    print(f"{len(profile['channels'])} inputs; profile SHA256 {digest}")


if __name__ == "__main__":
    main()
