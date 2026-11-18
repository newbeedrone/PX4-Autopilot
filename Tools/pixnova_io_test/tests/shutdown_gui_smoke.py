"""Real Tk GPIO handoff, export interlock, and parent/child shutdown in DEMO."""
from pathlib import Path
import sys
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station

root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
root.withdraw()
stage, failures = "GPIO", []
deadline = time.monotonic() + 25
child = None


def poll():
    global stage, child
    try:
        assert time.monotonic() < deadline, (stage, station.phase)
        if stage == "GPIO" and station.phase == "RUNNING":
            station.view.render()
            assert not station.view.export_action.enabled
            with patch("app.filedialog.asksaveasfilename") as dialog:
                station.view.export_action.invoke()
                station.export()
                dialog.assert_not_called()
            station.open_protocols()
            assert station.phase == "STOPPING_GPIO" and not getattr(station, "protocol_window", None)
            stage = "TRANSFER"
        elif stage == "TRANSFER" and getattr(station, "protocol_window", None):
            child = station.protocol_window
            child.root.withdraw()
            assert station.gpio_stop.confirmed == {"source", "probe"}
            stage = "CONNECT"
        elif stage == "CONNECT" and child.probe_ready:
            child.wired.set(True)
            child.mode.set("GPS1_I2C")
            child.start()
            assert child.run and child.run.phase == "CHECKING"
            assert not child.export_action.enabled
            with patch("protocol_app.filedialog.asksaveasfilename") as dialog:
                child.export()
                dialog.assert_not_called()
            station.close()
            assert not child.closed and not station.closing
            assert child.run.phase == "STOPPING"
            stage = "CLOSING"
    except Exception as error:
        failures.append(error)
        station.phase = "STOPPED"
        if child: child.run = None
        station.close()
        return
    if not station.closing: root.after(30, poll)


root.after(500, poll)
root.mainloop()
if failures: raise failures[0]
assert stage == "CLOSING" and child.closed and station.closing
assert child.run.outputs_stopped and child.run.stop_acks == {"source", "probe"}
print("GUI: GPIO STOP precedes protocol ownership; active export disabled; parent close waits for both child STOP acknowledgements", flush=True)
