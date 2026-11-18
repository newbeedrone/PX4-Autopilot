"""Exercise production IO adapter functions against the actual IO mixer."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def method(source, name):
    """Keep the production function body and preprocessor branches unchanged."""
    position = source.index("ProtocolTest::" + name + "(")
    start = source.rfind("\n", 0, position) + 1
    body = source.index("{", position)
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


@unittest.skipUnless(os.name == "posix" and shutil.which("g++"), "POSIX g++ required")
class IoAdapterTests(unittest.TestCase):
    def test_uart_crc_register_defaults_outputs_cleanup_and_lease_loss(self):
        adapter = (ROOT/"boards/newbeedrone/pixnova/src/protocol_test_board.cpp").read_text()
        mixer = (ROOT/"src/modules/px4iofirmware/mixer.cpp").read_text()
        names = ("start", "stop", "watchdog", "feed", "uart_start", "uart_write", "io_read", "io_transfer", "io_write", "io_pwm_start", "io_pwm_step", "io_pwm_stop", "sbus_out_start", "sbus_out_step", "update")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("drivers/drv_hrt.h", "px4_platform_common/atomic.h"):
                path = folder/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#pragma once\n")
            (folder/"io_adapter_under_test.inc").write_text("\n".join(method(adapter, name) for name in names))
            (folder/"io_mixer_under_test.inc").write_text(mixer[mixer.index("#define FMU_INPUT_DROP_LIMIT_US"):])
            source = (HERE/"io_pwm_native_test.cpp").read_text()
            (folder/"io_pwm_native_test.cpp").write_text(source)
            executable = folder/"io-adapter"
            command = ["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                       "-I"+str(folder), "-I"+str(ROOT/"src"), "-I"+str(ROOT/"src/systemcmds/gpio_test"),
                       str(folder/"io_pwm_native_test.cpp"), "-o", str(executable)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
