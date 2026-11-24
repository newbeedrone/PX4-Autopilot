"""Run with a desktop display to verify the real pin diagrams and fault workflow."""

from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Station
from dashboard import GREEN, RED
from peripherals import DEVICES


root = tk.Tk()
station = Station(root, Path(__file__).resolve().parents[1] / "profile.json", demo=True)
failures = []


def check_pass():
    try:
        assert station.assessment.all_passed
        assert station.peripherals.passed()
        completed = station.report_snapshot()
        completed.update(run_state="COMPLETE", test_suite_completed=True, external_suite_passed=True, all_passed=True)
        station.completed_report = completed
        station.suite_completed = True
        station.phase = "COMPLETE"
        station.show_completion()
        station.handle("source", {"type": "disconnected"})
        station.draw_results(); root.update_idletasks()
        assert station.phase == "COMPLETE" and station.state.get().startswith("TEST COMPLETE")
        assert station.view.metric_labels["pass"].cget("text") == "43"
        assert "source USB offline" in station.view.peripheral_summary.cget("text")
        assert station.view.sensor_cards["icm45686_spi1"]["state"].cget("fg") == GREEN
        station.completed_report = None
        station.suite_completed = False
        station.hello["source"] = True
        station.phase = "RUNNING"
        assert set(station.view.adc_cards) == {"ADIO_ADC_3V3", "ADIO_ADC_6V6"}
        station.view.filter("AD&IO")
        adc3 = next(i for i, row in enumerate(station.profile["channels"]) if row["id"] == "ADIO_ADC_3V3")
        station.view.select(adc3)
        root.update_idletasks()
        assert station.view.inspector_heading.cget("text") == "ADC VOLTAGE TEST"
        assert station.view.detail_values["error"].cget("text") == "3.300 V"
        assert station.view.detail_values["window"].cget("text") == "3.300 V"
        pin_map = station.view.map
        def adc_text():
            return [pin_map.itemcget(item, "text") for item in pin_map.find_withtag(f"row:{adc3}") if pin_map.type(item) == "text"]
        assert "PASS · 3.300 V" in adc_text()
        station.i2c_results["ADC3V3_HIGH"]["status"] = "FAIL"
        station._sync_adc_result(); station.draw_results()
        assert "FAIL · 3.300 V" in adc_text()
        assert station.view.adc_cards["ADIO_ADC_3V3"][1].cget("fg") == RED
        assert station.view.adc_cards["ADIO_ADC_6V6"][1].cget("fg") == GREEN
        station.i2c_results["ADC3V3_HIGH"]["status"] = "PASS"
        station._sync_adc_result(); station.view.filter(None); station.view.select(0)
        for name in ("icm45686_spi1", "icm45686_spi2", "icm45686_spi3"):
            assert station.view.sensor_cards[name]["state"].cget("fg") == GREEN
        station.view.select_peripheral_group("Storage")
        root.update_idletasks()
        for name in ("eeprom_base_i2c3", "eeprom_imu_i2c4", "fram_spi5", "sdcard"):
            assert station.view.sensor_cards[name]["state"].cget("fg") == GREEN
        station.view.select_peripheral_group("Sensors")
        assert station.view.map.itemcget("pin-fill:0", "outline") == GREEN
        station.view.filter("SPI6")
        reserved = station.view.map.find_withtag("protected:P9:7")
        assert len(reserved) == 2
        assert any("SPIX_SYNC / PE9" in station.view.map.itemcget(item, "text") for item in reserved)
        assert any("NOT TESTED" in station.view.map.itemcget(item, "text") for item in reserved)
        station.view.filter(None)
        root.geometry("1180x760")
        root.update_idletasks()
        # Click the actual drawn connector pin, rather than calling the controller.
        canvas = station.view.map
        x1, y1, x2, y2 = canvas.bbox("pin-fill:1")
        canvas.event_generate("<Motion>", x=int((x1 + x2) / 2), y=int((y1 + y2) / 2))
        canvas.event_generate("<Button-1>", x=int((x1 + x2) / 2), y=int((y1 + y2) / 2))
        root.update_idletasks()
        assert station.view.selected == 1
        station.demo_fault.set(True)
        station.view.select(0)
        root.after(1300, check_fault)
    except Exception as error:
        failures.append(error)
        station.close()


def check_fault():
    try:
        assert station.assessment.results[0].status == "FAIL"
        assert station.view.map.itemcget("pin-fill:0", "outline") == RED
        assert station.view.path.itemcget("inspector:source", "outline") == RED
        assert station.view.path.itemcget("inspector:probe", "outline") == RED
        assert station.view.metric_labels["fail"].cget("text") == "01"
        station.view.filter("failed")
        assert station.view.map.find_withtag("pin-fill:0")
        assert not station.view.map.find_withtag("pin-fill:4")
        # The details stay accessible on smaller displays through their own scroll area.
        root.update_idletasks()
        station.view.inspector_scroll.yview_moveto(1)
        root.update_idletasks()
        assert station.view.inspector_scroll.yview()[1] == 1
        station.demo_peripheral_fault.set(True)
        station.simulate_peripheral_fault()
        station.view.select_sensor("icm45686_spi2")
        root.update_idletasks()
        assert station.view.sensor_cards["icm45686_spi2"]["state"].cget("fg") == RED
        assert station.view.sensor_cards["icm45686_spi1"]["state"].cget("fg") == GREEN
        assert station.view.path.itemcget("inspector:sensor", "outline") == RED
        assert not station.report_snapshot()["peripherals"]["onboard_passed"]
        root.after(50, check_failed_self_check)
    except Exception as error:
        failures.append(error)
        station.close()


def check_failed_self_check():
    if station.phase == "STOPPING_GPIO":
        root.after(50, check_failed_self_check)
        return
    try:
        assert station.phase == "STOPPED"
        station.demo_peripheral_fault.set(False)
        station.populate_demo_power()
        station.self_check()
        session = station.assessment.session
        station.handle("probe", {"type": "checked", "session": session, "pass": False})
        bad = {"icm45686_spi2", "eeprom_base_i2c3", "sdcard"}
        for name, spec in DEVICES.items():
            station.handle("source", {"type": "check", "session": session, "test": name,
                                      "pass": name not in bad, "detail": 0 if name in bad else spec.get("demo", spec["id"])})
        station.handle("source", {"type": "checked", "session": session, "pass": False})
        station.draw_results()
        root.update_idletasks()
        assert station.phase == "STOPPED"
        assert all(station.view.sensor_cards[name]["state"].cget("fg") == RED for name in bad)
        assert station.view.peripheral_buttons["Sensors"].fg == RED
        assert station.view.peripheral_buttons["Storage"].fg == RED
        assert station.view.sensor_cards["icm45686_spi1"]["state"].cget("fg") == GREEN
        assert all(row.status == "NOT RUN" for row in station.assessment.results)
        print("GUI: completion retained after disconnect, independent ADC cards, pin faults, sensors and storage passed", flush=True)
    except Exception as error:
        failures.append(error)
    finally:
        station.close()


root.after(5200, check_pass)
root.after(12000, lambda: (failures.append(TimeoutError("GUI smoke test timed out")), station.close()))
root.mainloop()
if failures:
    raise failures[0]
