"""Real Tk controls for the IOV2 harness handoff and Ethernet display (demo)."""
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station
from dashboard import Action, GREEN, RED

root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
failures, stage = [], "IO"
deadline = time.monotonic() + 25


def widgets(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from widgets(widget)


def click(text):
    next(w for w in widgets(root) if isinstance(w, Action) and w.text == text).invoke()


def check():
    global stage
    try:
        assert time.monotonic() < deadline, "Extended GUI timed out"
        if stage == "IO":
            station.view.select_connector_group("IOV2 PWM")
            click("Confirm IOV2 wiring")
            stage = "WAIT_IO"
        elif stage == "WAIT_IO" and station.i2c_results.get("IO_PWM1", {}).get("status") == "PASS":
            assert station.view.iov2_cards["IO_PWM1"][1].cget("fg") == GREEN
            assert station.view.iov2_cards["IO_PWM1"][2].cget("text") == "50.00 Hz"
            click("■  Stop")
            assert station.i2c_run.phase == "STOPPING"
            stage = "STOP_IO"
        elif stage == "STOP_IO" and station.phase == "STOPPED":
            assert station.i2c_run.outputs_stopped
            station.view.select_connector_group("Ethernet")
            click("Test Ethernet")
            stage = "ETH"
        elif stage == "ETH" and station.phase == "I2C_COMPLETE":
            assert station.i2c_results["ETH"]["status"] == "PASS"
            assert station.i2c_results["ETH"]["host_rx"] >= 20
            assert station.view.ethernet_state.cget("fg") == GREEN
            assert "1/1 PASS" in station.state.get()
            station.i2c_results["IO_PWM1"]["status"] = "FAIL"
            station.view.select_connector_group("IOV2 PWM")
            assert station.view.iov2_cards["IO_PWM1"][1].cget("fg") == RED
            assert station.view.ethernet_state.cget("fg") == GREEN
            assert station.view.metric_labels["total"].cget("text") == "52"
            assert station.view.metric_labels["fail"].cget("text") == "01"
            station.view.filter("failed")
            assert station.view.connector_group == "IOV2 PWM"
            print("GUI: IOV2 pin/frequency cards, confirmed cancellation, Ethernet counters and independent red fault state passed (DEMO)", flush=True)
            station.close()
            return
    except Exception as error:
        failures.append(error)
        station.close()
        return
    root.after(70, check)


root.after(1500, check)
root.mainloop()
if failures: raise failures[0]
