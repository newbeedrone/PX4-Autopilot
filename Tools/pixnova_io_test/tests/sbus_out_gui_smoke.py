"""Ordinary GUI SBUS OUT receiver, PG5 inspector and missing-signal fault (DEMO)."""
from pathlib import Path
import sys
import time
import tkinter as tk
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station
from dashboard import Action, GREEN, RED

root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
stage, failures = "START", []
deadline = time.monotonic() + 20


def button(parent):
    for widget in parent.winfo_children():
        if isinstance(widget, Action) and widget.text == "Test SBUS OUT": return widget
        match = button(widget)
        if match: return match


def check():
    global stage
    try:
        assert time.monotonic() < deadline, (stage, station.state.get())
        if stage == "START":
            station.stop()
            root.geometry("1180x760")
            root.update_idletasks()
            for frame, title, count in station.view.nav_items.values():
                assert frame.winfo_ismapped(), title.cget("text")
                assert frame.winfo_rooty() + frame.winfo_height() <= root.winfo_rooty() + root.winfo_height(), title.cget("text")
            sidebar = station.view.nav_items["SBUS OUT"]
            assert sidebar[1].cget("text") == "SBUS OUT"
            sidebar[1].event_generate("<Button-1>")
            root.update_idletasks()
            assert station.view.filter_group == "SBUS OUT"
            assert station.view.connector_group == "IOV2 PWM"
            assert station.phase == "STOPPED"  # Navigation does not enable outputs.
            assert station.view.map.find_withtag("protocol-pin-fill:SBUS_OUT:2")
            assert station.view.map_title.cget("text") == "SBUS OUT · SBUS frames"
            assert "P8.2 / PG5" in station.view.inspector_signal.cget("text")
            button(root).invoke()
            stage = "PASS"
        elif stage == "PASS" and station.phase == "I2C_COMPLETE":
            result = station.report_snapshot()
            assert result["sbus_out_passed"]
            assert station.view.iov2_cards["SBUS_OUT"][1].cget("fg") == GREEN
            assert station.view.nav_items["SBUS OUT"][1].cget("fg") == GREEN
            assert station.view.nav_items["SBUS OUT"][2].cget("text") == "PASS"
            assert station.view.map.itemcget("protocol-pin-fill:SBUS_OUT:2", "outline") == GREEN
            assert "frames/s" in station.view.iov2_cards["SBUS_OUT"][2].cget("text")
            assert station.view.inspector_heading.cget("text") == "SBUS OUT INSPECTOR"
            assert station.view.detail_values["expected"].cget("text") == "40–55 frames/s"
            assert int(station.view.detail_values["error"].cget("text")) >= 20
            assert "PG5" in station.view.pin_note.cget("text")
            button(root).invoke()
            run, handle = station.i2c_run, station.i2c_run.handle
            def disconnected(role, frame, now):
                if role == "probe" and frame.get("type") == "protocol_sample":
                    frame = {**frame, "rx": 0, "tx": 0, "detail": 0, "pass": False}
                handle(role, frame, now)
            run.handle = disconnected
            stage = "FAULT"
        elif stage == "FAULT" and station.phase == "I2C_COMPLETE":
            assert not station.report_snapshot()["sbus_out_passed"]
            assert station.view.iov2_cards["SBUS_OUT"][1].cget("fg") == RED
            assert station.view.nav_items["SBUS OUT"][1].cget("fg") == RED
            assert station.view.nav_items["SBUS OUT"][2].cget("text") == "FAIL"
            assert station.view.map.itemcget("protocol-pin-fill:SBUS_OUT:2", "outline") == RED
            station.view.filter("failed")
            assert station.view.map.find_withtag("protocol-pin-fill:SBUS_OUT:2")
            station.view.nav_items["SBUS OUT"][0].event_generate("<Button-1>")
            assert station.view.path.itemcget("inspector:probe", "outline") == RED
            assert station.view.path.itemcget("inspector:source", "outline") == RED
            assert "PG5" in station.view.reason.cget("text")
            assert station.view.metric_labels["fail"].cget("text") == "01"
            print("GUI: left SBUS OUT navigation, P8/PG5 diagram, sidebar PASS/FAIL, decoded frames, confirmed STOP and missing-signal red pin path passed (DEMO)", flush=True)
            station.close(); return
    except Exception as error:
        failures.append(error); station.close(); return
    root.after(60, check)


root.after(1500, check)
root.mainloop()
if failures: raise failures[0]
