"""Verify bottom-of-page access and the independent firmware update button."""
from pathlib import Path
import sys
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station
from dashboard import Action


root = tk.Tk()
station = Station(root, Path(sys.path[0]) / "profile.json", demo=True)
root.title("PixNova scroll and firmware button regression")
root.geometry("1180x760")
failures = []
stage = "RUNNING"
deadline = time.monotonic() + 16


def widgets(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from widgets(widget)


def poll():
    global stage
    try:
        assert time.monotonic() < deadline, (stage, station.state.get())
        button = station.view.completion.update_action
        assert button.text == "Update Firmware"
        if stage == "RUNNING" and station.phase == "RUNNING":
            assert not button.enabled
            stop = next(w for w in widgets(root) if isinstance(w, Action) and w.text == "■  Stop")
            stop.event_generate("<Button-1>", x=5, y=5)
            stage = "STOPPING"
        elif stage == "STOPPING" and station.phase == "STOPPED":
            page, log = station.view.page, station.log
            root.update_idletasks()
            fixed = (station.source_combo, button, station.state_label, station.view.export_action)
            fixed_y = [widget.winfo_rooty() for widget in fixed]
            assert page.yview()[1] < 1
            port_map = station.view.map
            assert port_map.winfo_height() >= port_map.bbox("all")[3]
            assert not port_map.cget("yscrollcommand")
            port_map.event_generate("<MouseWheel>", delta=-120)
            assert page.yview()[0] > 0
            page.yview_moveto(1)
            root.update_idletasks()
            assert page.yview()[1] == 1
            assert log.winfo_rooty() >= page.winfo_rooty()
            assert log.winfo_rooty() + log.winfo_height() <= page.winfo_rooty() + page.winfo_height()
            assert [widget.winfo_rooty() for widget in fixed] == fixed_y
            assert all(widget.winfo_rooty() + widget.winfo_height() <= page.winfo_rooty() for widget in fixed)
            port_map.select_pin(0)
            root.update_idletasks()
            inspector_y = station.view.inspector_scroll.winfo_rooty()
            assert page.winfo_rooty() - 24 <= inspector_y < page.winfo_rooty() + page.winfo_height()
            assert button.enabled and not station.suite_active
            button.event_generate("<Button-1>", x=5, y=5)
            assert station.phase == "PREPARING"
            stage = "UPDATE"
        elif stage == "UPDATE" and station.phase == "STOPPED":
            assert "firmware update simulated" in station.state.get()
            assert not station.suite_active and not station.completed_report
            station.assessment.results[0].status = "PASS"
            station.i2c_results["CAN"] = {"status": "PASS"}
            station.note("OLD SESSION RESULT")
            station.view.completion.path.set("previous-selection.px4")
            directory = station.view.completion.directory.get()
            connect = next(w for w in widgets(root) if isinstance(w, Action) and w.text == "Connect USB")
            connect.event_generate("<Button-1>", x=5, y=5)
            assert station.phase == "READY" and not station.auto_start
            assert all(r.status == "NOT RUN" for r in station.assessment.results)
            assert not station.i2c_results
            assert "OLD SESSION RESULT" not in station.log.get("1.0", "end")
            assert "simulated" not in station.view.completion.status.get()
            assert station.view.completion.path.get() == "previous-selection.px4"
            assert station.view.completion.directory.get() == directory
            stage = "DONE"
            station.close()
            return
    except Exception as error:
        failures.append(error)
        station.close()
        return
    root.after(70, poll)


with patch("completion.restore_usb") as restore:
    root.after(700, poll)
    root.mainloop()
    restore.assert_not_called()
if failures:
    raise failures[0]
assert stage == "DONE"
print("GUI: expanded All ports map uses the lower pane scrollbar. Fixed controls, update and reconnect passed.")
