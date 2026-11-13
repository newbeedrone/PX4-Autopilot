"""Compile the real firmware endpoint against host GPIO/HRT substitutes."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))
from generate_profile import generate_header, load_profile
from model import Assessment
from peripherals import Peripherals


@unittest.skipUnless(os.name == "posix" and shutil.which("g++"), "POSIX host g++ required for firmware endpoint tests")
class EndpointTests(unittest.TestCase):
    def compile_and_run(self, receiver, protocols=False):
        profile, digest = load_profile(HERE.parent / "profile.json")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            headers = ["px4_platform_common/px4_config.h", "px4_platform_common/log.h",
                       "px4_platform_common/tasks.h", "px4_platform_common/atomic.h",
                       "px4_platform_common/board_common.h", "px4_platform/gpio.h",
                       "drivers/drv_hrt.h", "lib/version/version.h", "board_config.h", "nuttx/irq.h"]
            for name in headers:
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#include "endpoint_test_platform.hpp"\n')
            (folder / "gpio_test_profile.h").write_text(generate_header(profile, digest))
            executable = folder / "endpoint-test"
            command = ["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-I" + str(folder),
                       "-I" + str(HERE), "-I" + str(ROOT / "src/systemcmds/gpio_test"),
                       str(HERE / "endpoint_test.cpp"), "-o", str(executable)]
            if receiver:
                command.insert(1, "-DCONFIG_GPIO_TEST_RECEIVER=1")
            if protocols:
                command.insert(1, "-DCONFIG_GPIO_TEST_PROTOCOLS=1")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if protocols:
                result = subprocess.run([str(executable), "protocol"], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            hello = subprocess.run([str(executable), "hello"], capture_output=True, text=True, timeout=10)
            self.assertEqual(hello.returncode, 0, hello.stdout + hello.stderr)
            role = "probe" if receiver else "source"
            peripherals = Peripherals()
            frames = [json.loads(line) for line in hello.stdout.splitlines()]
            self.assertEqual(frames[0]["protocol"], profile["protocol"])
            for frame in frames[1:]:
                if frame["type"] == "power_check":
                    peripherals.power_result(role, frame)
                else:
                    peripherals.finish_power(role, frame)
            self.assertTrue(peripherals.passed((role,)))
            if receiver:
                result = subprocess.run([str(executable), "stream"], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                assessment = Assessment(profile)
                assessment.reset("a" * 32)
                frames = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(len(frames), sum(row.get("method") != "adc_voltage" for row in profile["channels"]) * 3)
                for frame in frames:
                    self.assertTrue(assessment.accept(frame, now=frame["seq"]))
                self.assertTrue(assessment.all_passed)

    def test_source_endpoint(self):
        self.compile_and_run(False)

    def test_probe_endpoint(self):
        self.compile_and_run(True)

    def test_protocol_source_command_gates(self):
        self.compile_and_run(False, protocols=True)

    def test_protocol_probe_command_gates(self):
        self.compile_and_run(True, protocols=True)
