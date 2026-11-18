"""A slow firmware/folder check must leave the real Tk loop and Stop responsive."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station

root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
directory = tempfile.TemporaryDirectory()
station.view.completion.directory.set(directory.name)
release, entered = threading.Event(), threading.Event()
failures, beats = [], []
stage, started, deadline = "START", 0, time.monotonic() + 15


def slow_prepare(*_):
    entered.set()
    if not release.wait(8): raise RuntimeError("Regression test release timed out")
    return None, Path(directory.name)


def beat():
    if not station.closing:
        beats.append(time.monotonic())
        root.after(20, beat)


def poll():
    global stage, started
    try:
        assert time.monotonic() < deadline, (stage, station.phase)
        if stage == "START" and station.phase == "RUNNING":
            station.stop("Prepare complete workflow")
        elif stage == "START" and station.phase in ("READY", "STOPPED"):
            before = time.monotonic()
            station.start_full_test()
            assert time.monotonic() - before < .2
            assert station.phase == "PREPARING"
            assert not station.network_idle()
            started, stage = time.monotonic(), "CANCEL"
        elif stage == "CANCEL" and time.monotonic() - started > .5:
            assert entered.is_set() and station.phase == "PREPARING"
            assert sum(t >= started for t in beats) >= 10
            station.stop("Cancel preparation")
            assert station.phase == "STOPPED" and not station.suite_active
            release.set()
            started, stage = time.monotonic(), "VERIFY"
        elif stage == "VERIFY" and time.monotonic() - started > .5:
            assert station.phase == "STOPPED" and station.finish_plan is None
            assert station.prepare_worker is None
            print("GUI: slow file validation leaves Tk responsive; Stop cancels; late completion starts no tests", flush=True)
            station.close(); return
    except Exception as error:
        failures.append(error)
        release.set()
        station.close(); return
    root.after(30, poll)


with patch("completion.prepare_plan", side_effect=slow_prepare):
    root.after(20, beat)
    root.after(500, poll)
    root.mainloop()
directory.cleanup()
if failures: raise failures[0]
