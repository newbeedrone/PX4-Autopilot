"""One owner thread per USB serial endpoint; bounded JSON lines."""

import json
import queue
import threading
import time


class SerialEndpoint(threading.Thread):
    def __init__(self, role, device, events, generation=0):
        super().__init__(name=f"pixnova-{role}", daemon=True)
        self.role, self.device, self.events = role, device, events
        self.generation = generation
        self.commands = queue.Queue(maxsize=32)
        self.command_lock = threading.Lock()
        self.exit_requested = threading.Event()

    def send(self, command):
        if "\n" in command or "\r" in command or len(command) > 150:
            raise ValueError("Invalid endpoint command")
        with self.command_lock:
            if command == "STOP":
                # A queued START or old heartbeat must not execute after STOP.
                while not self.commands.empty():
                    self.commands.get_nowait()
            try:
                self.commands.put_nowait(command)
            except queue.Full as error:
                raise RuntimeError("USB command queue is full") from error

    def emit(self, frame, terminal=False):
        frame = {**frame, "_connection": self.generation, "_received_at": time.monotonic()}
        try:
            self.events.put_nowait((self.role, frame))
        except queue.Full:
            if not terminal:
                raise RuntimeError("USB event queue overflow")
            # Keep shutdown nonblocking even when the GUI has stopped consuming.
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait((self.role, frame))
            except queue.Full:
                pass

    def close(self):
        self.exit_requested.set()

    def run(self):
        port = None
        try:
            import serial
            port = serial.Serial(self.device, 115200, timeout=0.05, write_timeout=0.25)
            port.reset_input_buffer()
            self.emit({"type": "connected"})
            self.send("HELLO")
            while not self.exit_requested.is_set():
                while not self.exit_requested.is_set():
                    try:
                        with self.command_lock:
                            command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    payload = (command + "\n").encode("ascii")
                    if port.write(payload) != len(payload):
                        raise IOError("Incomplete USB command write")
                if self.exit_requested.is_set():
                    break
                data = port.read_until(b"\n", size=512)
                if not data:
                    continue
                # A timeout can split a line; retain it until the next read.
                deadline = time.monotonic() + 1.0
                while not data.endswith(b"\n"):
                    if self.exit_requested.is_set():
                        return
                    if len(data) >= 512:
                        raise ValueError("Endpoint frame exceeds 512 bytes")
                    if time.monotonic() > deadline:
                        raise ValueError("Incomplete USB frame timed out")
                    data += port.read_until(b"\n", size=512 - len(data))
                frame = json.loads(data.decode("utf-8"))
                if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                    raise ValueError("Invalid endpoint JSON frame")
                self.emit(frame)
        except Exception as error:
            self.emit({"type": "link_error", "reason": str(error)}, terminal=True)
        finally:
            if port is not None:
                try:
                    port.write(b"STOP\n")
                except Exception:
                    pass
                try:
                    port.close()
                except Exception:
                    pass
            self.emit({"type": "disconnected"}, terminal=True)
