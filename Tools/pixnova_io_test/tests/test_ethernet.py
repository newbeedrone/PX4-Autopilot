from collections import deque
import unittest
from unittest.mock import Mock, patch

import test_protocol_model
from ethernet import EthernetPeer
from protocol_model import packet


class Socket:
    def __init__(self):
        self.responses, self.sent = deque(), []
        self.token, self.echo = 0, False
        self.closed = False

    def bind(self, address): self.bound = address
    def setblocking(self, _): pass
    def close(self): self.closed = True
    def sendto(self, data, peer):
        self.sent.append((data, peer))
        if self.echo:
            self.responses.append((packet("ETH", self.token, int.from_bytes(data[8:12], "little"), True), peer))
        return len(data)
    def recvfrom(self, _):
        if not self.responses: raise BlockingIOError()
        return self.responses.popleft()


class EthernetTests(unittest.TestCase):
    def make(self):
        self.case = test_protocol_model.ProtocolTests()
        run = self.case.make("ETH"); self.case.start(run)
        sock = Socket(); sock.token = run.token
        with patch("ethernet.socket.socket", return_value=sock): peer = EthernetPeer()
        return run, peer, sock

    def test_ip_configuration_failure_closes_socket_before_start(self):
        sock = Mock()
        sock.bind.side_effect = OSError("No local IP")
        with patch("ethernet.socket.socket", return_value=sock), self.assertRaisesRegex(ValueError, "192.168.77.1/24"):
            EthernetPeer()
        sock.close.assert_called_once()
        sock.sendto.assert_not_called()

    def test_selected_windows_adapter_is_used_for_outgoing_packets(self):
        sock = Mock()
        with patch("ethernet.socket.socket", return_value=sock), patch("ethernet.os.name", "nt"):
            EthernetPeer(12)
        sock.setsockopt.assert_called_once_with(0, 31, b"\x00\x00\x00\x0c")

    def test_phy_startup_has_a_bounded_negotiation_window(self):
        case = test_protocol_model.ProtocolTests()
        run = case.make("ETH")
        case.checks(run, "source")
        self.assertEqual(run.phase, "STARTING_SOURCE")
        run.tick(10)
        self.assertEqual(run.phase, "STARTING_SOURCE")
        run.tick(21)
        self.assertEqual(run.result.status, "FAIL")

    def test_no_data_is_sent_until_source_reports_physical_link(self):
        run, peer, sock = self.make()
        peer.pump(run, .5)
        self.case.sample(run, "source", 1, detail=0)
        peer.pump(run, 1)
        self.assertEqual(sock.sent, [])
        self.case.sample(run, "source", 2, detail=1)
        peer.pump(run, 2)
        self.assertEqual(len(sock.sent), 1)

    def test_bidirectional_packets_and_stop_confirmation_are_required(self):
        run, peer, sock = self.make(); sock.echo = True
        for step in range(20, 121):
            now = step / 20
            if step % 20 == 0:
                self.case.sample(run, "source", step//20, detail=1)
            peer.pump(run, now)
            if run.phase == "STOPPING": break
        self.assertGreaterEqual(run.host_rx, 20)
        self.assertEqual(run.phase, "STOPPING")
        self.assertNotEqual(run.result.status, "PASS")
        sent = len(sock.sent)
        peer.pump(run, now + .1)
        self.assertEqual(len(sock.sent), sent)
        self.case.confirm_stop(run)
        self.assertEqual(run.result.status, "PASS")
        peer.close()
        self.assertTrue(sock.closed)

    def test_corrupt_or_replayed_reply_stops_without_sending_another_packet(self):
        for corrupt in (True, False):
            run, peer, sock = self.make()
            self.case.sample(run, "source", 1, detail=1)
            peer.pump(run, 1)
            sock.responses.append((packet("ETH", run.token ^ int(corrupt), 1, True), peer.remote))
            if not corrupt:
                sock.responses.append((packet("ETH", run.token, 1, True), peer.remote))
            peer.pump(run, 1.1)
            self.assertEqual(run.phase, "STOPPING")
            self.assertEqual(run.result.status, "FAIL")
            self.assertEqual(len(sock.sent), 1)

    def test_lost_link_or_timeout_cannot_pass(self):
        run, peer, sock = self.make()
        self.case.sample(run, "source", 1, detail=1)
        peer.pump(run, 1)
        peer.pump(run, 2.01)
        self.assertEqual(run.result.status, "FAIL")
        run, peer, sock = self.make(); sock.echo = True
        self.case.sample(run, "source", 1, detail=1)
        peer.pump(run, 1); peer.pump(run, 1.1)
        self.case.sample(run, "source", 2, detail=0)
        peer.pump(run, 2)
        self.assertEqual(run.result.status, "FAIL")

    def test_socket_error_fails_ethernet_and_waits_for_stop_confirmation(self):
        for method, operation in (("sendto", "send"), ("recvfrom", "receive")):
            with self.subTest(operation=operation):
                run, peer, sock = self.make()
                self.case.sample(run, "source", 1, detail=1)
                with patch.object(sock, method, side_effect=OSError("Network down")):
                    peer.pump(run, 1)
                self.assertEqual(self.case.sent[-1], ("source", "STOP"))
                self.case.confirm_stop(run)
                self.assertEqual(run.phase, "FAILED")
                self.assertIn(f"Ethernet {operation} failed: Network down", run.result.reason)
