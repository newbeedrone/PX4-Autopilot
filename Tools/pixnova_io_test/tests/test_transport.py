import queue
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transport import SerialEndpoint


class FakePort:
    def __init__(self, pieces=()):
        self.incoming = queue.Queue()
        for piece in pieces:
            self.incoming.put(piece)
        self.writes, self.closed = [], False

    def reset_input_buffer(self):
        pass

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def read_until(self, *args, **kwargs):
        try:
            return self.incoming.get(timeout=0.01)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def run_endpoint(self, port, expected, events=None):
        events = queue.Queue() if events is None else events
        endpoint = SerialEndpoint("probe", "FAKE_USB", events, generation=7)
        frames = []
        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=lambda *a, **kw: port)}):
            endpoint.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    _, frame = events.get(timeout=0.02)
                    frames.append(frame)
                    if frame.get("type") == expected:
                        break
                except queue.Empty:
                    pass
            endpoint.close()
            endpoint.join(timeout=1)
        self.assertFalse(endpoint.is_alive(), "Serial owner thread did not stop")
        self.assertTrue(port.closed)
        self.assertEqual(port.writes[-1], b"STOP\n")
        self.assertTrue(any(frame["type"] == expected for frame in frames), frames)
        return frames

    def test_partial_json_is_reassembled_and_generation_cannot_be_spoofed(self):
        frames = self.run_endpoint(FakePort([b'{"type":"heart', b'beat","_connection":99,"_received_at":0}\n']), "heartbeat")
        self.assertEqual(frames[-1]["_connection"], 7)
        self.assertGreater(frames[-1]["_received_at"], 0)

    def test_invalid_or_oversized_frames_stop_and_close_the_port(self):
        for data in (b"not-json\n", b"[]\n", b"x" * 512):
            self.run_endpoint(FakePort([data]), "link_error")

    def test_incomplete_frame_has_a_deadline(self):
        self.run_endpoint(FakePort([b'{"type":']), "link_error")

    def test_full_event_queue_does_not_block_shutdown(self):
        events = queue.Queue(maxsize=1)
        events.put(("old", {"type": "old"}))
        port = FakePort()
        endpoint = SerialEndpoint("probe", "FAKE_USB", events)
        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=lambda *a, **kw: port)}):
            endpoint.start()
            endpoint.join(timeout=1)
        self.assertFalse(endpoint.is_alive())
        self.assertTrue(port.closed)
        self.assertEqual(port.writes, [b"STOP\n"])

    def test_stop_discards_pending_start_and_heartbeat_commands(self):
        endpoint = SerialEndpoint("source", "FAKE_USB", queue.Queue())
        for command in ("CHECK old profile", "START old profile", "PING old", "STOP"):
            endpoint.send(command)
        self.assertEqual(endpoint.commands.get_nowait(), "STOP")
        self.assertTrue(endpoint.commands.empty())

    def test_close_never_executes_a_queued_start(self):
        port = FakePort()
        endpoint = SerialEndpoint("source", "FAKE_USB", queue.Queue())
        endpoint.send("START old profile")
        endpoint.close()
        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=lambda *a, **kw: port)}):
            endpoint.run()
        self.assertEqual(port.writes, [b"STOP\n"])


if __name__ == "__main__":
    unittest.main()
