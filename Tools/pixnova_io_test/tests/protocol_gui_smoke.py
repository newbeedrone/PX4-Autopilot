"""Exercise USB ownership and STOP confirmation through actual Tk controls."""
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station
from dashboard import Action, GREEN


root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1] / "profile.json", demo=True)
failures = []
stage = "OPEN"
deadline = time.monotonic() + 20


def widgets(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from widgets(widget)


def check():
    global stage
    try:
        assert time.monotonic() < deadline, "Protocol GUI test timed out"
        if stage == "OPEN" and station.phase == "RUNNING":
            button = next(w for w in widgets(root) if isinstance(w, Action) and w.text == "Protocols")
            button.event_generate("<Button-1>")
            assert station.phase == "STOPPING_GPIO"
            stage = "WAIT_HANDOFF"
        child = getattr(station, "protocol_window", None)
        if stage == "WAIT_HANDOFF" and child is not None:
            assert station.gpio_stop.confirmed == {"source", "probe"}
            child.connect()
            child.mode.set("ADC3V3_HIGH")
            child.start()
            stage = "CANCEL"
        if stage == "CANCEL" and child.run.phase == "RUNNING":
            stop = next(w for w in widgets(root) if isinstance(w, Action) and w.text == "■  Stop")
            stop.event_generate("<Button-1>")
            assert child.run.phase == "STOPPING"
            assert "outputs stopped" not in station.state.get()
            assert "waiting for STOP confirmation" in station.state.get()
            stage = "CONFIRM_CANCEL"
        elif stage == "CONFIRM_CANCEL" and child.run.phase == "STOPPED":
            assert child.run.outputs_stopped
            assert child.run.stop_acks == {"source", "probe"}
            if "outputs stopped (confirmed)" not in station.state.get():
                root.after(50, check)
                return
            child.start()
            stage = "COMPLETE"
        elif stage == "COMPLETE" and child.run.phase == "COMPLETE":
            assert child.run.result.status == "PASS"
            assert child.run.outputs_stopped
            assert child.run.stop_acks == {"source", "probe"}
            assert child.cards["ADC3V3_HIGH"][1].cget("fg") == GREEN
            print("GUI: parent Stop reaches protocol owner; cancellation and PASS require both STOP replies", flush=True)
            station.close()
            return
    except Exception as error:
        failures.append(error)
        station.close()
        return
    root.after(50, check)


root.after(1500, check)
root.mainloop()
if failures:
    raise failures[0]
