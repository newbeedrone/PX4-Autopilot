from pathlib import Path
import base64
import os
import struct
import sys
import unittest
import tempfile
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_firmware import build_lock, validate_binary, validate_package, verify_context


class ImageTests(unittest.TestCase):
    digest = "a" * 64

    def image(self, role="source", stack=0x24008000, reset=None):
        flash = 0x08020000 if role == "source" else 0x08000000
        reset = flash + 129 if reset is None else reset
        product = b"PixNova IO Test" if role == "source" else b"PixNova IO Probe"
        return struct.pack("<II", stack, reset) + self.digest.encode() + product + b"\0" * 200

    def test_source_and_probe_have_distinct_valid_boot_addresses(self):
        for role, address in [("source", 0x08020000), ("probe", 0x08000000)]:
            vectors = validate_binary(self.image(role), role, self.digest)
            self.assertEqual(vectors["flash_address"], address)

    def test_misaligned_or_invalid_startup_stack_is_rejected(self):
        for stack in [0x24008002, 0x24008004, 0x08008000, 0]:
            with self.assertRaises(ValueError):
                validate_binary(self.image(stack=stack), "source", self.digest)

    def test_wrong_flash_address_or_non_thumb_reset_is_rejected(self):
        for reset in [0x08000081, 0x08020080, 0x080fffff]:
            with self.assertRaises(ValueError):
                validate_binary(self.image(reset=reset), "source", self.digest)

    def test_wrong_role_or_stale_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_binary(self.image("probe", reset=0x08020081), "source", self.digest)
        with self.assertRaises(ValueError):
            validate_binary(self.image(), "source", "b" * 64)

    def test_px4_package_must_match_the_binary_and_board(self):
        binary = self.image()
        package = dict(magic="PX4FWv1", board_id=5900, image_size=len(binary),
                       image=base64.b64encode(zlib.compress(binary)).decode())
        validate_package(package, binary, 5900)
        for changed in [dict(package, board_id=1), dict(package, image_size=0),
                        dict(package, image=base64.b64encode(zlib.compress(b"stale")).decode()),
                        dict(package, image="invalid-base64")]:
            with self.assertRaises(ValueError):
                validate_package(changed, binary, 5900)

    @unittest.skipUnless(os.name == "posix", "PX4 build helper runs in a POSIX environment")
    def test_concurrent_build_is_rejected_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with build_lock(directory):
                with self.assertRaises(RuntimeError):
                    with build_lock(directory):
                        self.fail("Concurrent build acquired the same lock")
            with build_lock(directory):
                pass

    def test_build_rejects_profile_or_git_revision_changes(self):
        with patch("build_firmware.load_profile", return_value=({}, self.digest)), patch("build_firmware.subprocess.check_output", return_value="revision\n"):
            verify_context(self.digest, "revision")
            for digest, revision in [("changed", "revision"), (self.digest, "changed")]:
                with self.assertRaises(RuntimeError):
                    verify_context(digest, revision)


if __name__ == "__main__":
    unittest.main()
