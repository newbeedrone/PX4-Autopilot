"""Token/CRC-checked Ethernet exchange shared by both station windows."""
import os
import socket
import struct

from protocol_model import packet

REPLY_TIMEOUT_SECONDS = 1.0


class EthernetPeer:
    local_address = "192.168.77.1"
    remote = ("192.168.77.2", 14570)

    def __init__(self, interface_index=None):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind((self.local_address, 0))
            if os.name == "nt" and interface_index is not None:
                self.socket.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_UNICAST_IF", 31), struct.pack("!I", interface_index))
            self.socket.setblocking(False)
        except OSError:
            self.socket.close()
            raise ValueError("Ethernet not started: use Configure IP in the Ethernet panel for 192.168.77.1/24 and connect the PixNova Ethernet adapter") from None
        self.pending, self.sequence, self.last_send = {}, 0, 0
        self.skipped_startup = set()

    def close(self):
        self.socket.close()

    def pump(self, run, now):
        if run.mode != "ETH" or run.phase != "RUNNING":
            return
        if run.samples.get("source", {}).get("detail") != 1:
            if run.host_rx:
                run.fail("Ethernet physical link lost")
            return
        # Check the receive queue before declaring a timeout after a delayed GUI tick.
        for _ in range(32):
            try:
                data, peer = self.socket.recvfrom(128)
            except BlockingIOError:
                break
            except OSError as error:
                run.fail(f"Ethernet receive failed: {error}")
                return
            sequence = int.from_bytes(data[8:12], "little")
            if peer != self.remote or len(data) != 32:
                run.fail("Unexpected Ethernet response")
                return
            if sequence in self.skipped_startup and data == packet("ETH", run.token, sequence, True):
                self.skipped_startup.remove(sequence)
                continue
            if sequence not in self.pending:
                run.fail("Unexpected or replayed Ethernet response")
                return
            del self.pending[sequence]
            first_reply = run.host_rx == 0
            run.accept_udp(data, sequence, now)
            if run.phase != "RUNNING":
                return
            if first_reply:
                # Initial probes can be lost during neighbor discovery. Once a
                # valid echo arrives, every subsequent probe must be answered.
                self.skipped_startup = {earlier for earlier in self.pending if earlier < sequence}
                for earlier in self.skipped_startup:
                    del self.pending[earlier]
        if self.pending:
            oldest = next(iter(self.pending))
            if now - self.pending[oldest] > REPLY_TIMEOUT_SECONDS:
                reason = "Ethernet reply timed out" if run.host_rx else "Ethernet startup received no valid reply"
                run.fail(f"{reason} (sequence {oldest}, PC sent {self.sequence}, verified {run.host_rx})")
                return
        if now - self.last_send >= .05:
            self.sequence += 1
            data = packet("ETH", run.token, self.sequence)
            try:
                sent = self.socket.sendto(data, self.remote)
            except OSError as error:
                run.fail(f"Ethernet send failed: {error}")
                return
            if sent != len(data):
                run.fail("Incomplete Ethernet datagram write")
                return
            self.pending[self.sequence] = now
            self.last_send = now
