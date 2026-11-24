"""Adapter transactions run against an IP Helper substitute; no real NIC is changed."""
import copy
import ctypes
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from network_config import NetworkSettings, NetworkWorker, UnicastRow, TEST_ADDRESS, elevated_change, main

GUID = "eeb924f4-ed98-407c-b593-106b5de2b65f"
OTHER = "00000000-0000-0000-0000-000000000002"


class Adapters:
    def __init__(self):
        self.rows = [{"guid": GUID, "index": 12, "name": "Ethernet", "status": "Up",
                      "dhcp": "Enabled", "dns": ["10.10.10.32"], "gateways": ["10.10.10.32"],
                      "ipv4": [{"address": "10.10.10.81", "prefix": 24}]}]
        self.address = None
        self.creates = self.deletes = 0
        self.next_dad = 4

    def enumerate(self):
        rows = copy.deepcopy(self.rows)
        if rows and self.address:
            rows[0]["ipv4"].append({"address": TEST_ADDRESS, "prefix": self.address["prefix"]})
        return rows

    def read(self, guid):
        return copy.deepcopy(self.address)

    def create(self, guid):
        assert guid == GUID
        self.creates += 1
        self.address = {"created": 100, "prefix": 24, "dad": self.next_dad, "skip_source": True,
                        "index": self.rows[0]["index"], "luid": 123}

    def delete(self, guid):
        assert guid == GUID
        self.deletes += 1
        self.address = None


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.nic = Adapters()
        self.settings = NetworkSettings(self.directory.name, self.nic, self.nic.enumerate)

    def test_windows_abi_uses_fixed_width_fields(self):
        self.assertEqual(ctypes.sizeof(UnicastRow), 80)
        for field, offset in (("luid", 32), ("index", 40), ("prefix", 60), ("skip_source", 61), ("dad", 64), ("created", 72)):
            self.assertEqual(getattr(UnicastRow, field).offset, offset)

    def test_configure_and_restore_preserve_existing_network(self):
        original = self.nic.enumerate()[0]
        result = self.settings.configure_local(GUID)
        self.assertTrue(result["ready"])
        self.assertTrue(result["owned"])
        self.assertEqual(self.settings.ready_index(GUID), 12)
        self.assertEqual(result["adapter"]["ipv4"][0], original["ipv4"][0])
        self.assertEqual(self.nic.creates, 1)
        # Repeated configure must retain ownership and avoid duplicate additions.
        self.settings.configure_local(GUID)
        self.assertEqual(self.nic.creates, 1)
        result = self.settings.restore_local(GUID)
        self.assertEqual(result["adapter"], original)
        self.assertFalse(result["ready"])
        self.assertFalse(self.settings.journal(GUID)["owned"])
        self.assertEqual(self.nic.deletes, 1)
        self.settings.restore_local(GUID)
        self.assertEqual(self.nic.deletes, 1)

    def test_other_station_cannot_overwrite_an_inflight_ownership_transaction(self):
        second = NetworkSettings(self.directory.name, self.nic, self.nic.enumerate)
        entered, release = threading.Event(), threading.Event()
        create = self.nic.create
        def paused_create(guid):
            entered.set()
            if not release.wait(5): raise RuntimeError("Test did not release address creation")
            create(guid)
        outcome = []
        def configure():
            try: outcome.append(self.settings.configure_local(GUID))
            except Exception as error: outcome.append(error)
        with patch.object(self.nic, "create", side_effect=paused_create):
            thread = threading.Thread(target=configure)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                baseline = self.settings.journal(GUID)
                for operation in (second.configure_local, second.restore_local):
                    with self.assertRaisesRegex(RuntimeError, "Another station"):
                        operation(GUID)
                    self.assertEqual(self.settings.journal(GUID), baseline)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict, outcome)
        self.assertTrue(outcome[0]["owned"])
        self.assertEqual(self.nic.creates, 1)
        second.restore_local(GUID)
        self.assertIsNone(self.nic.address)
        self.assertEqual(self.nic.deletes, 1)

    def test_adapter_lock_is_shared_with_another_python_process_and_released_on_error(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from network_config import NetworkSettings
try:
    with NetworkSettings(sys.argv[2]).mutation_lock(sys.argv[3]):
        print("ACQUIRED")
except RuntimeError:
    print("BUSY")
'''
        command = [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), self.directory.name, GUID]
        with self.assertRaisesRegex(ValueError, "release lock"):
            with self.settings.mutation_lock(GUID):
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "BUSY")
                raise ValueError("release lock")
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ACQUIRED")

    def test_preexisting_address_is_never_claimed_or_deleted(self):
        self.nic.create(GUID)
        self.nic.address["skip_source"] = False
        self.assertFalse(self.settings.configure_local(GUID)["owned"])
        self.settings.restore_local(GUID)
        self.assertIsNotNone(self.nic.address)
        self.assertEqual(self.nic.deletes, 0)

    def test_duplicate_address_on_other_adapter_blocks_configuration(self):
        self.nic.rows.append({**self.nic.rows[0], "guid": OTHER, "ipv4": [{"address": TEST_ADDRESS, "prefix": 24}]})
        with self.assertRaisesRegex(ValueError, "another adapter"):
            self.settings.configure_local(GUID)
        self.assertEqual(self.nic.creates, 0)

    def test_restore_can_remove_owned_address_even_if_another_adapter_conflicts(self):
        self.settings.configure_local(GUID)
        self.nic.rows.append({**self.nic.rows[0], "guid": OTHER, "ipv4": [{"address": TEST_ADDRESS, "prefix": 24}]})
        self.settings.restore_local(GUID)
        self.assertIsNone(self.nic.address)
        self.assertEqual(self.nic.rows[1]["ipv4"][0]["address"], TEST_ADDRESS)

    def test_dad_conflict_rolls_back_only_test_address(self):
        self.nic.next_dad = 2
        original = self.nic.enumerate()[0]
        with self.assertRaisesRegex(ValueError, "IP conflict"):
            self.settings.configure_local(GUID)
        self.assertEqual(self.nic.enumerate()[0], original)
        self.assertEqual(self.nic.deletes, 1)
        self.assertFalse(self.settings.journal(GUID)["owned"])

    def test_disconnected_cable_is_configured_but_never_ready(self):
        self.nic.rows[0]["status"] = "Disconnected"
        self.nic.next_dad = 1
        result = self.settings.configure_local(GUID)
        self.assertFalse(result["ready"])
        self.assertTrue(result["owned"])
        self.assertIsNotNone(self.nic.address)
        with self.assertRaises(ValueError): self.settings.ready_index(GUID)

    def test_administrator_helper_writes_in_place_to_the_callers_response_file(self):
        def invoke(args):
            response = Path(args[args.index("--result") + 1])
            identity = response.stat().st_ino
            self.assertEqual(response.stat().st_size, 0)
            with patch("network_config.NetworkSettings", return_value=self.settings):
                main(args[1:])
            self.assertEqual(response.stat().st_ino, identity)

        with patch("network_config.run_elevated", side_effect=invoke):
            self.assertTrue(elevated_change("configure", GUID, self.directory.name)["ready"])
            self.assertFalse(elevated_change("restore", GUID, self.directory.name)["ready"])

    def test_administrator_helper_failure_preserves_the_returned_error(self):
        with patch("network_config.run_elevated", side_effect=lambda args: main(args[1:])), \
             patch("network_config.NetworkSettings", return_value=self.settings), \
             patch.object(self.settings, "configure_local", side_effect=ValueError("Test address conflict")):
            with self.assertRaisesRegex(RuntimeError, "Test address conflict"):
                elevated_change("configure", GUID, self.directory.name)

    def test_administrator_helper_without_response_reports_a_startup_error(self):
        with patch("network_config.run_elevated"):
            with self.assertRaisesRegex(RuntimeError, "Administrator helper did not return a readable result"):
                elevated_change("configure", GUID, self.directory.name)

    def test_tentative_address_waits_for_dad(self):
        self.nic.next_dad = 1
        def settle(_): self.nic.address["dad"] = 4
        with patch("network_config.time.sleep", side_effect=settle) as wait:
            self.assertTrue(self.settings.configure_local(GUID)["ready"])
        self.assertEqual(wait.call_count, 1)

    def test_rollback_preserves_replaced_or_edited_address_during_dad(self):
        for change in ({"created": 200, "skip_source": False, "dad": 2},
                       {"created": 200, "dad": 4}, {"prefix": 16, "dad": 4},
                       {"skip_source": False, "dad": 4}):
            with self.subTest(change=change):
                self.nic.address = None
                self.nic.next_dad = 1
                deletes = self.nic.deletes
                def replace(_): self.nic.address.update(change)
                with patch("network_config.time.sleep", side_effect=replace):
                    with self.assertRaisesRegex(ValueError, "not removed"):
                        self.settings.configure_local(GUID)
                self.assertIsNotNone(self.nic.address)
                for key, value in change.items(): self.assertEqual(self.nic.address[key], value)
                self.assertEqual(self.nic.deletes, deletes)
                self.assertFalse(self.settings.journal(GUID)["owned"])
                self.settings.restore_local(GUID)
                self.assertEqual(self.nic.deletes, deletes)

    def test_failed_rollback_retains_ownership_for_a_later_restore(self):
        self.nic.next_dad = 2
        with patch.object(self.nic, "delete", side_effect=OSError("NIC temporarily unavailable")):
            with self.assertRaisesRegex(OSError, "temporarily unavailable"):
                self.settings.configure_local(GUID)
        self.assertTrue(self.settings.journal(GUID)["owned"])
        self.assertIsNotNone(self.nic.address)
        self.settings.restore_local(GUID)
        self.assertIsNone(self.nic.address)
        self.assertFalse(self.settings.journal(GUID)["owned"])

    def test_unverified_address_after_create_is_retained(self):
        read = self.nic.read
        calls = 0
        def temporarily_missing(guid):
            nonlocal calls
            calls += 1
            return None if calls == 2 else read(guid)
        with patch.object(self.nic, "read", side_effect=temporarily_missing):
            with self.assertRaisesRegex(ValueError, "ownership could not be verified"):
                self.settings.configure_local(GUID)
        self.assertIsNotNone(self.nic.address)
        self.assertEqual(self.nic.deletes, 0)
        self.assertFalse(self.settings.journal(GUID)["owned"])

    def test_rollback_read_failure_keeps_ownership_until_it_can_be_verified(self):
        self.nic.next_dad = 1
        read = self.nic.read
        calls = 0
        def unavailable(guid):
            nonlocal calls
            calls += 1
            if calls >= 3: raise OSError("Cannot verify address")
            return read(guid)
        with patch.object(self.nic, "read", side_effect=unavailable), patch("network_config.time.sleep"):
            with self.assertRaisesRegex(OSError, "Cannot verify"):
                self.settings.configure_local(GUID)
        self.assertEqual(self.nic.deletes, 0)
        self.assertTrue(self.settings.journal(GUID)["owned"])
        self.settings.restore_local(GUID)
        self.assertIsNone(self.nic.address)

    def test_disappeared_address_during_dad_does_not_delete_other_settings(self):
        self.nic.next_dad = 1
        original = self.nic.enumerate()[0]
        def remove(_): self.nic.address = None
        with patch("network_config.time.sleep", side_effect=remove):
            with self.assertRaisesRegex(RuntimeError, "disappeared"):
                self.settings.configure_local(GUID)
        self.assertEqual(self.nic.deletes, 0)
        self.assertEqual(self.nic.enumerate()[0], original)
        self.assertFalse(self.settings.journal(GUID)["owned"])

    def test_user_replaced_address_is_not_removed(self):
        self.settings.configure_local(GUID)
        self.nic.address["created"] = 200
        with self.assertRaisesRegex(ValueError, "changed outside"):
            self.settings.restore_local(GUID)
        self.assertEqual(self.nic.deletes, 0)

    def test_adapter_guid_survives_index_change_and_unplug_is_reported(self):
        self.settings.configure_local(GUID)
        self.nic.rows[0]["index"] = self.nic.address["index"] = 15
        self.assertEqual(self.settings.ready_index(GUID), 15)
        self.nic.rows.clear()
        with self.assertRaisesRegex(ValueError, "no longer present"):
            self.settings.configure_local(GUID)

    def test_bad_prefix_or_duplicate_preexisting_address_is_retained(self):
        for field, value in (("prefix", 16), ("dad", 2)):
            self.nic.create(GUID)
            self.nic.address[field] = value
            with self.assertRaises(ValueError): self.settings.configure_local(GUID)
            self.assertEqual(self.nic.deletes, 0)

    def test_worker_does_not_block_ui_and_rejects_overlapping_operations(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(2)
            return []
        self.settings.adapters = blocked
        worker = NetworkWorker(self.settings)
        self.assertTrue(worker.request("refresh"))
        self.assertTrue(entered.wait(1))
        self.assertFalse(worker.request("configure", GUID))
        self.assertIsNone(worker.poll())
        release.set()
        deadline = time.monotonic() + 2
        event = None
        while event is None and time.monotonic() < deadline:
            time.sleep(.01)
            event = worker.poll()
        self.assertEqual(event["value"], [])
        self.assertFalse(worker.busy)

    def test_worker_reports_permission_failure(self):
        worker = NetworkWorker(self.settings)
        with patch.object(self.settings, "change", side_effect=RuntimeError("Windows permission was cancelled")):
            worker.request("configure", GUID)
            deadline = time.monotonic() + 2
            event = None
            while event is None and time.monotonic() < deadline:
                time.sleep(.01)
                event = worker.poll()
        self.assertIn("permission was cancelled", event["error"])
        self.assertEqual(self.nic.creates, 0)
