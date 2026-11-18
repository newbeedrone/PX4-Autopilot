"""Real GUI full workflow in DEMO, manual IOV2 pause, automatic Ethernet and CSV."""
import csv
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station, I2C_MODES, ADC_MODES, IO_MODES

directory = tempfile.TemporaryDirectory()
root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1]/"profile.json", demo=True)
if "--hidden" in sys.argv: root.withdraw()
fault = "--fault" in sys.argv
station.demo_fault.set(fault)
station.view.completion.directory.set(directory.name)
stage, pause_at, failures = "START", None, []
deadline = time.monotonic() + 180


def poll():
    global stage, pause_at
    try:
        assert time.monotonic() < deadline, (stage, station.phase, station.state.get())
        if stage == "START":
            if station.phase == "RUNNING":
                station.stop("Prepare complete workflow")
                stage = "STOPPING"
            else:
                station.start_full_test()
                assert station.phase == "PREPARING"
                stage = "PREPARING"
        elif stage == "STOPPING" and station.phase == "STOPPED":
            assert station.gpio_stop.confirmed == {"source", "probe"}
            stage = "START"
        elif stage == "PREPARING" and station.phase != "PREPARING":
            assert station.suite_active and station.extended_suite
            assert station.gpio_sequence.groups[-1] == "FMU PWM"
            stage = "GPIO"
        elif stage == "GPIO" and station.phase == "WAITING_IOV2":
            report = station.report_snapshot()
            assert set(station.gpio_sequence.completed) == set(station.gpio_sequence.groups)
            assert station.gpio_stop.confirmed == {"source", "probe"}
            assert all(report["connector_tests"][mode]["status"] == "PASS" for mode in I2C_MODES + ADC_MODES + ("CAN",))
            assert not any(mode in station.i2c_results for mode in IO_MODES)
            assert "FMU" in station.state.get() and "move P21" in station.state.get()
            assert not list(Path(directory.name).glob("*.csv"))
            pause_at, stage = time.monotonic(), "PAUSE"
        elif stage == "PAUSE" and time.monotonic() - pause_at >= 1:
            assert station.phase == "WAITING_IOV2"
            assert not station.suite_completed
            station.start_iov2()
            stage = "IOV2"
        elif stage == "IOV2" and station.phase == "COMPLETE" and station.finish_worker is None:
            report = station.report_snapshot()
            assert report["test_suite_completed"]
            assert report["external_suite_passed"] == (not fault) and report["all_passed"] == (not fault)
            assert report["can_passed"] and report["iov2_passed"] and report["sbus_out_passed"] and report["ethernet_passed"]
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
            print("GUI: sequential GPIO/PPM/FMU, I2C/ADC/CAN, confirmed manual IOV2 pause, IOV2/SBUS OUT, automatic Ethernet, complete CSV and DEMO-only restoration passed; fault=" + str(fault), flush=True)
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
