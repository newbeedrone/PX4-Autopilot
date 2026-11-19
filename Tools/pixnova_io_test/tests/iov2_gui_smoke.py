"""All eight IOV2 GUI outputs, single-channel retest and fault display (demo)."""
from pathlib import Path
import sys
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station, IO_MODES
from dashboard import GREEN, RED

root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
failures, stage = [], "START"
deadline = time.monotonic() + 70


def check():
    global stage
    try:
        assert time.monotonic() < deadline, "IOV2 GUI test timed out"
        if stage == "START":
            station.stop()
            stage = "WAIT_STOP"
        elif stage == "WAIT_STOP" and station.phase == "STOPPED":
            assert station.gpio_stop.confirmed == {"source", "probe"}
            station.view.filter(None)
            assert station.view.map.find_withtag("protocol-pin-fill:IO_PWM1")
            assert station.view.map.find_withtag("protocol-pin-fill:SBUS_OUT:2")
            station.view.nav_items["IOV2 PWM"][1].event_generate("<Button-1>")
            root.update_idletasks()
            assert station.view.filter_group == "IOV2 PWM"
            assert station.view.connector_group == "IOV2 PWM"
            assert station.phase == "STOPPED"
            canvas = station.view.map
            x1, y1, x2, y2 = canvas.bbox("protocol-pin-fill:IO_PWM2")
            canvas.event_generate("<Motion>", x=int((x1+x2)/2), y=int((y1+y2)/2))
            canvas.event_generate("<Button-1>", x=int((x1+x2)/2), y=int((y1+y2)/2))
            root.update_idletasks()
            assert station.view.selected_iov2 == "IO_PWM2"
            assert "P21.7" in station.view.inspector_signal.cget("text")
            assert station.phase == "STOPPED"
            station.start_iov2()
            stage = "ALL"
        elif stage == "ALL" and station.phase == "I2C_COMPLETE":
            assert station.report_snapshot()["iov2_passed"]
            assert "8/8 PASS" in station.state.get()
            assert station.view.nav_items["IOV2 PWM"][1].cget("fg") == GREEN
            assert station.view.nav_items["IOV2 PWM"][2].cget("text") == "8/8"
            for mode in IO_MODES:
                assert station.i2c_results[mode]["outputs_stopped"]
                assert station.view.iov2_cards[mode][1].cget("fg") == GREEN
            station.view.select_iov2("IO_PWM1")
            assert station.view.inspector_heading.cget("text") == "IOV2 PWM INSPECTOR"
            assert station.view.detail_values["measured"].cget("text") == "50.000 Hz"
            assert station.view.detail_values["window"].cget("text") == "3 / 3"
            station.start_iov2("IO_PWM1")
            stage = "RETEST"
        elif stage == "RETEST" and station.phase == "I2C_COMPLETE":
            assert "1/1 PASS" in station.state.get()
            assert station.report_snapshot()["iov2_passed"]
            station.start_iov2("IO_PWM1")
            run = station.i2c_run
            handle = run.handle
            def wrong_frequency(role, frame, now):
                if role == "probe" and frame.get("type") == "protocol_sample":
                    frame = {**frame, "detail": 55000}
                handle(role, frame, now)
            run.handle = wrong_frequency
            stage = "FAULT"
        elif stage == "FAULT" and station.phase == "I2C_COMPLETE":
            assert "0/1 PASS" in station.state.get()
            assert not station.report_snapshot()["iov2_passed"]
            assert station.view.iov2_cards["IO_PWM1"][1].cget("fg") == RED
            assert station.view.nav_items["IOV2 PWM"][1].cget("fg") == RED
            assert station.view.nav_items["IOV2 PWM"][2].cget("text") == "1 FAIL"
            assert station.view.map.itemcget("protocol-pin-fill:IO_PWM1", "outline") == RED
            assert all(station.view.iov2_cards[mode][1].cget("fg") == GREEN for mode in IO_MODES[1:])
            assert station.view.detail_values["measured"].cget("text") == "55.000 Hz"
            assert station.view.detail_values["error"].cget("text") == "+10.00%"
            assert station.view.path.itemcget("inspector:source", "outline") == RED
            assert station.view.path.itemcget("inspector:probe", "outline") == RED
            assert "frequency" in station.view.reason.cget("text")
            print("GUI: left IOV2 navigation, clickable P21 pins, eight outputs, sidebar PASS/FAIL, one-output retest and wrong-frequency red state passed (DEMO)", flush=True)
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
