"""Real GUI full workflow in DEMO, manual IOV2 pause, automatic Ethernet and CSV."""
import csv
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station, I2C_MODES, ADC_MODES, IO_MODES
from dashboard import Action, GREEN, RED

directory = tempfile.TemporaryDirectory()
root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
if "--hidden" in sys.argv: root.withdraw()
fault = "--fault" in sys.argv
station.demo_fault.set(fault)
station.view.completion.directory.set(directory.name)
stage, pause_at, failures = "START", None, []
deadline = time.monotonic() + 180
clicks = []


def widgets(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from widgets(widget)


def click(text):
    action = next(widget for widget in widgets(root) if isinstance(widget, Action) and widget.text == text)
    assert action.enabled, text
    if "--hidden" in sys.argv:
        action.invoke()
    else:
        assert action.winfo_ismapped(), text
        action.event_generate("<Button-1>", x=5, y=5)
    clicks.append(text)


def poll():
    global stage, pause_at
    try:
        assert time.monotonic() < deadline, (stage, station.phase, station.state.get())
        if stage == "START":
            if station.phase == "RUNNING":
                click("■  Stop")
                stage = "STOPPING"
            else:
                click("▶  Start test")
                assert station.phase == "PREPARING"
                stage = "PREPARING"
        elif stage == "STOPPING" and station.phase == "STOPPED":
            assert station.gpio_stop.confirmed == {"source", "probe"}
            stage = "START"
        elif stage == "PREPARING" and station.phase != "PREPARING":
            assert station.suite_active and station.extended_suite
            assert station.gpio_sequence.groups[-1] == "FMU PWM"
            stage = "SELF_CHECK"
        elif stage == "SELF_CHECK" and station.suite_stage == "self_check_results":
            assert "SELF-CHECK COMPLETE" in station.state.get()
            assert len(station.peripherals.current_seen) == 10
            assert not station.i2c_results and not station.gpio_sequence.completed
            stage = "FIXED"
        elif stage == "FIXED" and station.phase == "RUNNING":
            report = station.report_snapshot()
            assert report["can_passed"] and report["sbus_out_passed"]
            assert station.suite_stage == "gpio"
            assert "ETH" not in report["connector_tests"]
            stage = "GPIO"
        elif stage == "GPIO" and station.phase == "I2C_RUNNING" and station.i2c_run.mode == "ETH":
            assert set(station.gpio_sequence.completed) == set(station.gpio_sequence.groups) - {"FMU PWM"}
            assert station.gpio_stop.confirmed == {"source", "probe"}
            assert station.view.nav_items["FMU PWM"][2].cget("text") != "PASS"
            assert station.view.nav_items["GPS1"][2].cget("text") == "PASS"
            stage = "FMU"
        elif stage == "FMU" and station.phase == "WAITING_IOV2":
            report = station.report_snapshot()
            assert set(station.gpio_sequence.completed) == set(station.gpio_sequence.groups)
            assert station.gpio_stop.confirmed == {"source", "probe"}
            assert all(report["connector_tests"][mode]["status"] == "PASS" for mode in I2C_MODES + ADC_MODES + ("CAN",))
            assert report["ethernet_passed"] and report["sbus_out_passed"]
            assert not any(mode in station.i2c_results for mode in IO_MODES)
            assert station.view.nav_items["Ethernet"][2].cget("text") == "PASS"
            assert station.view.nav_items["FMU PWM"][2].cget("text") == "PASS"
            assert "FMU" in station.state.get() and "move P21" in station.state.get()
            assert not list(Path(directory.name).glob("*.csv"))
            if "--hidden" in sys.argv:
                station.view.filter("PPM")
            else:
                station.view.nav_items["PPM"][1].event_generate("<Button-1>")
            click("Retest PPM")
            assert station.phase == "CHECKING"
            stage = "PPM_RETEST"
        elif stage == "PPM_RETEST" and station.phase == "WAITING_IOV2":
            assert station.gpio_stop.confirmed == {"source", "probe"}
            assert station.view.nav_items["PPM"][2].cget("text") == "PASS"
            assert station.view.nav_items["FMU PWM"][2].cget("text") == "PASS"
            assert station.i2c_results["ETH"]["status"] == "PASS"
            assert "PPM retest" in station.state.get()
            station.view.filter("IOV2 PWM")
            pause_at, stage = time.monotonic(), "PAUSE"
        elif stage == "PAUSE" and time.monotonic() - pause_at >= 1:
            assert station.phase == "WAITING_IOV2"
            assert not station.suite_completed
            click("Confirm IOV2 wiring")
            stage = "IOV2"
        elif stage == "IOV2" and station.phase == "COMPLETE" and station.finish_worker is None:
            report = station.report_snapshot()
            assert report["test_suite_completed"]
            assert report["external_suite_passed"] == (not fault) and report["all_passed"] == (not fault)
            assert report["can_passed"] and report["iov2_passed"] and report["sbus_out_passed"] and report["ethernet_passed"]
            assert list(report["connector_tests"])[-8:] == list(IO_MODES)
            assert "SBUS" not in report["connector_tests"] and "DSM" not in report["connector_tests"]
            assert report["post_test"]["restoration"]["status"] == "DEMO"
            paths = list(Path(directory.name).glob("*.csv"))
            assert len(paths) == 1
            with paths[0].open(encoding="utf-8-sig", newline="") as stream: rows = list(csv.DictReader(stream))
            assert len([row for row in rows if row["record_type"] == "signal"]) == 43
            assert len([row for row in rows if row["record_type"] == "protocol"]) == 21
            assert any(row["signal"] == "PPM_SIGNAL" and row["status"] == "PASS" for row in rows)
            assert "CSV saved" in station.state.get()
            summary = next(row for row in rows if row["record_type"] == "summary")
            assert summary["status"] == ("FAIL" if fault else "PASS")
            assert summary["finalization_status"] == "PASS"
            assert report["workflow_passed"] == (not fault)
            if fault:
                assert any(row["record_type"] == "signal" and row["status"] == "FAIL" for row in rows)
            for group, (_, title, _) in station.view.nav_items.items():
                if group not in (None, "failed"):
                    count = station.view.nav_items[group][2]
                    failed = fault and group == station.profile["channels"][0]["group"]
                    assert count.cget("text") == ("1 FAIL" if failed else "PASS"), (group, count.cget("text"))
                    assert count.cget("fg") == (RED if failed else GREEN)
                if "--hidden" not in sys.argv:
                    title.event_generate("<Button-1>", x=5, y=5)
                    assert station.view.faults_only == (group == "failed")
                    assert station.view.filter_group == (None if group == "failed" else group)
            print("Clicked " + ", ".join(clicks) + f"; checked {len(station.view.nav_items)} sidebar entries", flush=True)
            print("GUI: self-check, fixed connectors, regular GPIO, Ethernet, FMU, confirmed harness switch and final IOV2. Sidebar badges, CSV and simulated restoration passed. Fault=" + str(fault), flush=True)
            stage = "DONE"
            station.close(); return
    except Exception as error:
        failures.append(error)
        station.phase = "STOPPED"
        station.close(); return
    root.after(80, poll)


root.after(1500, poll)
root.mainloop()
directory.cleanup()
if failures: raise failures[0]
if stage != "DONE": raise RuntimeError("GUI verification window closed before the sequence completed")
