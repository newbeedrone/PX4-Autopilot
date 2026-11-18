"""Real Tk adapter controls, automatic preparation, cancellation and Ethernet (DEMO)."""
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station
from dashboard import Action, GREEN, RED
from protocol_app import ProtocolStation

root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
panel = station.view.network_panel
stage, failures, callbacks = "INITIAL", [], []
deadline, heartbeat = time.monotonic() + 30, 0


def click(parent, text):
    for child in parent.winfo_children():
        if isinstance(child, Action) and child.text == text:
            child.invoke(); return True
        if click(child, text): return True
    return False


def check():
    global stage, heartbeat
    heartbeat += 1
    try:
        assert time.monotonic() < deadline, (stage, panel.status.get())
        if stage == "INITIAL" and panel.guid() and not panel.worker.busy:
            station.stop()
            station.view.select_connector_group("Ethernet")
            assert click(panel, "Configure IP")
            stage = "CONFIGURED"
        elif stage == "CONFIGURED" and not panel.worker.busy:
            assert panel.last_status["ready"] and panel.last_status["owned"]
            assert len(panel.last_status["adapter"]["ipv4"]) == 2
            assert panel.last_status["adapter"]["ipv4"][0]["address"] == "10.0.0.10"
            assert click(panel, "Restore")
            stage = "RESTORED"
        elif stage == "RESTORED" and not panel.worker.busy:
            assert not panel.last_status["ready"]
            assert panel.last_status["adapter"]["ipv4"] == [{"address": "10.0.0.10", "prefix": 24, "state": "Preferred"}]
            panel.prepare(lambda index: callbacks.append(index))
            station.stop()  # STOP must cancel queued test start, including adapter status work.
            stage = "CANCELLED"
        elif stage == "CANCELLED" and not panel.worker.busy:
            assert not callbacks and panel.pending_test is None
            panel.prepare(lambda index: (callbacks.append(index), station.start_ethernet()))
            stage = "ETH"
        elif stage == "ETH" and station.phase == "I2C_COMPLETE":
            assert callbacks == [1]
            assert station.i2c_results["ETH"]["status"] == "PASS"
            assert panel.last_status["ready"]
            assert station.report_snapshot()["pc_ethernet"]["guid"] == panel.guid()
            assert station.view.ethernet_state.cget("fg") == GREEN
            def denied(*_): raise RuntimeError("Windows permission was cancelled")
            panel.backend.change = denied
            click(panel, "Restore")
            stage = "ERROR"
        elif stage == "ERROR" and not panel.worker.busy:
            assert "permission was cancelled" in panel.status.get()
            assert panel.status_label.cget("fg") == RED
            assert heartbeat > 20
            station.close()
            return
    except Exception as error:
        failures.append(error); station.close(); return
    root.after(50, check)


root.after(1400, check)
root.mainloop()
if failures: raise failures[0]

# The alternate station exposes the same controls and closes its polling cleanly.
root = tk.Tk()
protocol = ProtocolStation(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)


def protocol_check():
    try:
        assert protocol.network_panel.guid()
        assert click(protocol.network_panel, "Configure IP")
        protocol.select("ETH")
        assert protocol.network_panel.winfo_ismapped()
        root.after(500, protocol_finish)
    except Exception as error:
        failures.append(error); protocol.close()


def protocol_finish():
    try: assert protocol.network_panel.last_status["ready"]
    except Exception as error: failures.append(error)
    protocol.close()


root.after(1000, protocol_check)
root.mainloop()
if failures: raise failures[0]
print("GUI: adapter selection, retained IP, configure/restore, automatic Ethernet setup, STOP cancellation and error display passed (DEMO)", flush=True)
