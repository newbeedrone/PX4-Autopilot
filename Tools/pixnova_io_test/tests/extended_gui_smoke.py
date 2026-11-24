"""Real Tk controls for the IOV2 harness handoff and Ethernet display (demo)."""
from pathlib import Path
import sys
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station, ADC_CHANNEL_MODES, IO_MODES
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
            click("Retest IOV2")
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
            click("Retest Ethernet")
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
            station.view.nav_items["IOV2 PWM"][1].event_generate("<Button-1>")
            first_pwm = station.view.iov2_cards[IO_MODES[0]][0]
            next(w for w in widgets(first_pwm) if isinstance(w, Action) and w.text == "Retest").event_generate("<Button-1>")
            assert station.i2c_run.mode == IO_MODES[0]
            stage = "PWM_RETEST"
        elif stage == "PWM_RETEST" and station.phase == "I2C_COMPLETE":
            assert station.i2c_results[IO_MODES[0]]["status"] == "PASS"
            station.assessment.results[0].status = "PASS"
            station.demo_peripheral_fault.set(True)
            click("Retest Onboard")
            assert station.phase == "CHECKING"
            stage = "ONBOARD_RETEST"
        elif stage == "ONBOARD_RETEST" and station.phase == "CHECKED":
            assert "ONBOARD RETEST COMPLETE" in station.state.get()
            assert station.view.sensor_cards["icm45686_spi2"]["state"].cget("fg") == RED
            assert station.assessment.results[0].status == "PASS"
            assert station.i2c_results["ETH"]["status"] == "PASS"
            check_sidebar_buttons()
            print("GUI: Ethernet, IOV2 group and single-channel retests, confirmed cancellation and independent onboard retest passed (DEMO)", flush=True)
            station.close()
            return
    except Exception as error:
        failures.append(error)
        station.close()
        return
    root.after(70, check)


def check_sidebar_buttons():
    root.geometry("1180x760")
    view = station.view
    with patch.object(station, "retest_gpio") as gpio:
        for group in view.gpio_groups:
            view.nav_items[group][1].event_generate("<Button-1>")
            root.update_idletasks()
            button = view.gpio_retest_action
            assert button.winfo_ismapped() and button.enabled and button.text.startswith("Retest " + group)
            assert button.winfo_rooty() + button.winfo_height() <= view.page.winfo_rooty() + view.page.winfo_height()
            button.event_generate("<Button-1>")
            gpio.assert_called_with(group)
    with patch.object(station, "_start_connector_modes") as start:
        view.nav_items["GPS1"][1].event_generate("<Button-1>")
        for mode, (box, _) in view.i2c_cards.items():
            next(w for w in widgets(box) if isinstance(w, Action) and w.text == "Retest I2C").invoke()
            start.assert_called_with([mode])
        for channel, (box, _) in view.adc_cards.items():
            next(w for w in widgets(box) if isinstance(w, Action) and w.text == "Retest ADC").invoke()
            start.assert_called_with(list(ADC_CHANNEL_MODES[channel]))
        view.nav_items["CAN"][1].event_generate("<Button-1>")
        click("Retest CAN")
        start.assert_called_with(["CAN"])
        print("GUI: every GPIO connector and each I2C/ADC card has a scoped Retest action; CAN button routes to CAN loopback", flush=True)


root.after(1500, check)
root.mainloop()
if failures: raise failures[0]
