"""Compile the actual PixNova sensor/storage checks with fault-injected buses."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


@unittest.skipUnless(os.name == "posix" and shutil.which("g++"), "POSIX host g++ required for board check tests")
class BoardCheckTests(unittest.TestCase):
    def test_live_devices_read_only_storage_and_complete_failure_reporting(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ("px4_platform_common/px4_config.h", "px4_platform_common/i2c.h",
                         "px4_platform/gpio.h", "px4_arch/micro_hal.h", "drivers/drv_hrt.h",
                         "nuttx/i2c/i2c_master.h", "nuttx/spi/spi.h", "board_config.h",
                         "px4_platform_common/px4_mtd.h", "px4_platform_common/px4_manifest.h",
                         "nuttx/mtd/mtd.h", "nuttx/fs/fs.h", "nuttx/fs/ioctl.h"):
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#pragma once\n")
            executable = folder / "board-check-test"
            command = ["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-I" + str(folder),
                       "-I" + str(ROOT / "src/systemcmds/gpio_test"),
                       str(HERE / "board_check_test.cpp"), "-o", str(executable)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
