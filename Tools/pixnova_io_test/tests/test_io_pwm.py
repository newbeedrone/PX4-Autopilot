from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_protocol_model
import test_station
from app import IO_MODES
from protocol_model import TESTS, MODE_NUMBER, ProtocolRun
from workflow import GpioSequence, GpioStop


class IoPwmTests(unittest.TestCase):
    def station(self):
        self.case = test_station.StationTests()
        self.case.setUp()
        station = self.case.completed_controller(pending_last=True)
        station.suite_stage, station.extended_suite = "fixed", True
        station.root = Mock()
        station.gpio_sequence = GpioSequence(station.profile)
        for cap in station.capabilities.values(): cap.update(io_pwm=1, session_stop=1, sbus_out=1)
        self.case.confirm_stop(station.i2c_run); station._update_i2c()
        station.i2c_results["CAN"] = {"status": "PASS", "outputs_stopped": True}
        station.i2c_results["SBUS_OUT"] = {"status": "PASS", "outputs_stopped": True}
        return station

    def confirm_gpio_stop(self, station, fmu=False):
        now = time.monotonic()
        sequence = station.gpio_sequence
        sequence.position = len(sequence.groups) if fmu else len(sequence.groups) - 1
        for group in sequence.groups[:sequence.position]:
            indices = sequence.indices[group]
            sequence.completed[group] = [{"status": "PASS", "frequency_hz": 25.0} for _ in indices]
            for index in indices:
                station.assessment.results[index].status = "PASS"
        station.phase, station.suite_stage = "STOPPING_GPIO", "fmu" if fmu else "gpio"
        station.gpio_stop = GpioStop(station.assessment.session, now, station.transmit)
        frame = {"type": "stopped", "session": station.assessment.session, "reason": "requested"}
        station.gpio_stop.handle("source", frame, now)
        station.poll_gpio_stop(now)
        self.assertEqual(station.phase, "STOPPING_GPIO")
        station.gpio_stop.handle("probe", frame, now)
        station.poll_gpio_stop(now)

    def test_full_workflow_waits_for_ethernet_and_ends_with_confirmed_harness_switch(self):
        station = self.station()
        self.assertEqual(station.suite_stage, "gpio")
        self.confirm_gpio_stop(station)
        self.assertEqual(station.phase, "WAITING_ETH")
        self.assertFalse(station.suite_completed)
        with patch("app.EthernetPeer", return_value=Mock()):
            station.start_ethernet()
        self.assertEqual(station.i2c_run.roles, {"source"})
        station.i2c_run.complete("Verified real Ethernet exchange")
        self.case.confirm_stop(station.i2c_run); station._update_i2c()
        self.assertEqual(station.phase, "CHECKING")
        self.assertTrue(station.report_snapshot()["ethernet_passed"])
        self.assertTrue(station.report_snapshot()["adc_passed"])
        self.assertEqual(station.assessment.results[0].frequency_hz, 25.0)
        self.assertEqual(station.gpio_sequence.group, "FMU PWM")

        self.confirm_gpio_stop(station, fmu=True)
        self.assertEqual(station.phase, "WAITING_IOV2")
        self.assertFalse(station.suite_completed)
        self.assertFalse(station.auto_start)
        self.assertIsNone(station.completed_report)
        self.assertFalse(any("IO_PWM" in command for _, command in station.commands))
        station.start_iov2()
        for mode in IO_MODES:
            self.assertEqual(station.i2c_run.mode, mode)
            station.i2c_run.result.frequency_hz = TESTS[mode]["expected_hz"]
            station.i2c_run.complete("Measured output")
            self.case.confirm_stop(station.i2c_run); station._update_i2c()
        self.assertEqual(station.phase, "COMPLETE")
        self.assertTrue(station.report_snapshot()["external_suite_passed"])
        self.assertIn("IOV2 8/8", station.state.get())
        self.assertIn("Ethernet PASS", station.state.get())

    def test_missing_network_configuration_does_not_start_outputs_or_complete(self):
        station = self.station()
        self.confirm_gpio_stop(station)
        commands = list(station.commands)
        with patch("app.EthernetPeer", side_effect=ValueError("Configure isolated Ethernet")):
            station.start_ethernet()
        self.assertEqual(station.phase, "WAITING_ETH")
        self.assertEqual(station.commands, commands)
        self.assertIsNone(station.completed_report)

    def test_iov2_capability_is_required_and_manual_self_check_cancels_workflow(self):
        station = self.station()
        self.confirm_gpio_stop(station, fmu=True)
        station.capabilities["probe"]["io_pwm"] = 0
        commands = list(station.commands)
        station.start_iov2()
        self.assertEqual(station.phase, "WAITING_IOV2")
        self.assertEqual(station.commands, commands)
        station.self_check()
        self.assertFalse(station.suite_active)
        self.assertEqual(station.phase, "CHECKING")

    def test_iov2_reuses_the_reviewed_fmu_receive_harness(self):
        case = test_station.StationTests(); case.setUp()
        fmu = [row for row in case.profile["channels"] if row["group"] == "FMU PWM"]
        for mode, original in zip(IO_MODES, fmu):
            for key in ("probe_pin", "probe_connector", "probe_connector_pin"):
                self.assertEqual(TESTS[mode][key], original[key])

    def test_independent_single_output_retest_uses_only_its_current_result(self):
        case = test_station.StationTests(); case.setUp()
        station = case.make_controller()
        station.capabilities = {role: {"io_pwm": 1, "session_stop": 1} for role in ("source", "probe")}
        station.i2c_results = {mode: {"status": "PASS", "session": "previous"} for mode in IO_MODES}
        station.start_iov2("IO_PWM3")
        self.assertEqual(station.i2c_run.mode, "IO_PWM3")
        self.assertEqual(station.i2c_queue, [])
        station.i2c_run.complete("Measured 75 Hz")
        case.confirm_stop(station.i2c_run); station._update_i2c()
        self.assertIn("1/1 PASS", station.state.get())
        self.assertEqual(station.i2c_results["IO_PWM2"]["session"], "previous")
        with self.assertRaises(ValueError): station.start_iov2("FMU_PWM3")

    def test_wrong_or_missing_probe_frequency_never_passes(self):
        case = test_protocol_model.ProtocolTests()
        for mode in IO_MODES:
            for multiplier in (None, 0, 1.05):
                run = case.make(mode); case.start(run)
                for n in range(1, 6):
                    if multiplier is not None: case.sample(run, "probe", n, detail=round(TESTS[mode]["expected_hz"] * 1000 * multiplier))
                    case.sample(run, "source", n, detail=round(TESTS[mode]["expected_hz"] * 1000))
                self.assertNotEqual(run.result.status, "PASS")
                if multiplier == 1.05:
                    self.assertIn(run.phase, ("STOPPING", "FAILED"))
                else:
                    self.assertEqual(run.phase, "RUNNING")

    def test_invalid_frequency_details_fail_without_gui_arithmetic_errors(self):
        case = test_protocol_model.ProtocolTests()
        for detail in (10**350, 0x80000000, -0x80000001):
            run = case.make("IO_PWM1"); case.start(run)
            case.sample(run, "probe", 1, detail=detail)
            self.assertEqual(run.result.status, "FAIL")
            self.assertIn("Malformed", run.result.reason)

    def test_reused_receiver_window_and_lost_signal_cannot_complete_a_run(self):
        case = test_protocol_model.ProtocolTests()
        run = case.make("IO_PWM1"); case.start(run)
        for n in (1, 2):
            case.sample(run, "probe", n, now=n, detail=50000)
            case.sample(run, "source", n, now=n + .1, detail=50000)
        self.assertEqual(run.streak, 1)
        case.sample(run, "source", 3, now=3.1, detail=50000)
        self.assertEqual(run.streak, 0)
        case.sample(run, "probe", 3, now=3.2, rx=40, detail=0, **{"pass": False})
        case.sample(run, "probe", 4, now=4, detail=50000)
        case.sample(run, "source", 4, now=4.1, detail=50000)
        self.assertEqual(run.phase, "RUNNING")
        self.assertEqual(run.streak, 1)
        for n in (5, 6):
            case.sample(run, "probe", n, now=n, detail=50000)
            case.sample(run, "source", n, now=n + .1, detail=50000)
        case.confirm_stop(run)
        self.assertEqual(run.result.status, "PASS")

    def test_receiver_loss_or_missing_report_resets_existing_streak(self):
        case = test_protocol_model.ProtocolTests()
        for loss in (True, False):
            run = case.make("IO_PWM1"); case.start(run)
            for n in (1, 2, 3):
                case.sample(run, "probe", n, detail=50000)
                case.sample(run, "source", n, now=n + .1, detail=50000)
            self.assertEqual(run.streak, 2)
            if loss:
                case.sample(run, "probe", 4, rx=60, detail=0, **{"pass": False})
            else:
                case.sample(run, "probe", 5, detail=50000)
            self.assertEqual(run.streak, 0)
            self.assertEqual(run.result.status, "WAITING")
            self.assertEqual(run.phase, "RUNNING")

    def test_source_first_arrival_still_requires_three_distinct_receiver_windows(self):
        case = test_protocol_model.ProtocolTests()
        run = case.make("IO_PWM1"); case.start(run)
        for n in range(1, 5):
            case.sample(run, "source", n, now=n, detail=50000)
            if n < 4: case.sample(run, "probe", n, now=n + .1, detail=50000)
        case.confirm_stop(run)
        self.assertEqual(run.result.status, "PASS")

    def test_all_iov2_frequencies_require_fresh_windows_and_confirmed_shutdown(self):
        case = test_protocol_model.ProtocolTests()
        for mode in IO_MODES:
            run = case.make(mode); case.start(run)
            for n in range(1, 5):
                case.sample(run, "probe", n, detail=round(TESTS[mode]["expected_hz"] * 1000))
                case.sample(run, "source", n, detail=round(TESTS[mode]["expected_hz"] * 1000))
            self.assertEqual(run.phase, "STOPPING")
            self.assertNotEqual(run.result.status, "PASS")
            case.confirm_stop(run)
            self.assertEqual(run.result.status, "PASS")
            self.assertEqual(run.result.frequency_hz, TESTS[mode]["expected_hz"])

    def test_session_stop_can_cancel_before_any_startup_command_was_executed(self):
        sent = []
        run = ProtocolRun("IO_PWM1", "a"*64, 0, lambda *args: sent.append(args), session_stop=True)
        run.begin(); run.stop(now=.01)
        self.assertTrue(all(command == "STOP " + run.session for _, command in sent[-2:]))
        for role in run.roles:
            run.handle(role, {"type": "stopped", "session": run.session, "reason": "requested"}, .02)
        self.assertEqual(run.phase, "STOPPED")
        self.assertTrue(run.outputs_stopped)

    def test_native_io_write_policy_rejects_arming_and_unselected_outputs(self):
        root = Path(__file__).resolve().parents[3]
        source = r'''
#include "io_pwm_policy.hpp"
#include <assert.h>
#include <stdio.h>
int main() {
    using namespace protocol_test;
    uint16_t saved[3]{0,50,200}, zeros[8]{}, pwm[8]{};
    const uint32_t token=1234;
    for(unsigned ch=0;ch<8;ch++) {
        Mode mode=static_cast<Mode>(unsigned(Mode::IoPwm1)+ch);
        printf("%u:%u\n", unsigned(mode), io_pwm_hz(mode));
        for(unsigned i=0;i<8;i++) pwm[i]=i==ch?io_pwm_width(token):0;
        assert(io_pwm_write_allowed(mode,token,false,true,saved,PX4IO_PAGE_DIRECT_PWM,0,pwm,8));
        assert(!io_pwm_write_allowed(mode,token,true,true,saved,PX4IO_PAGE_DIRECT_PWM,0,pwm,8));
        pwm[(ch+1)%8]=1500;
        assert(!io_pwm_write_allowed(mode,token,false,true,saved,PX4IO_PAGE_DIRECT_PWM,0,pwm,8));
        assert(io_pwm_write_allowed(mode,token,true,true,saved,PX4IO_PAGE_DIRECT_PWM,0,zeros,8));
        uint16_t arm=PX4IO_P_SETUP_ARMING_FMU_ARMED;
        assert(!io_pwm_write_allowed(mode,token,false,true,saved,PX4IO_PAGE_SETUP,PX4IO_P_SETUP_ARMING,&arm,1));
        assert(!io_pwm_write_allowed(mode,token,true,true,saved,PX4IO_PAGE_SETUP,PX4IO_P_SETUP_ARMING,&arm,1));
    }
    for(unsigned page=0;page<256;page++) {
        if(page==PX4IO_PAGE_STATUS) continue;
        assert(!io_pwm_write_allowed(Mode::IoPwm1,token,true,true,saved,page,0,zeros,1));
        assert(!io_pwm_write_allowed(Mode::Sbus,token,false,true,saved,page,0,zeros,8));
    }
    uint16_t flags=PX4IO_P_STATUS_FLAGS_RAW_PWM;
    assert(!io_pwm_write_allowed(Mode::IoPwm1,token,true,true,saved,PX4IO_PAGE_STATUS,PX4IO_P_STATUS_FLAGS,&flags,1));
    flags=0;
    assert(io_pwm_write_allowed(Mode::IoPwm1,token,true,true,saved,PX4IO_PAGE_STATUS,PX4IO_P_STATUS_FLAGS,&flags,1));
    assert(!io_pwm_write_allowed(Mode::IoPwm1,token,true,false,saved,PX4IO_PAGE_STATUS,PX4IO_P_STATUS_FLAGS,&flags,1));
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            cpp = Path(tmp)/"policy.cpp"; cpp.write_text(source)
            exe = Path(tmp)/"policy"
            subprocess.run(["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-I"+str(root/"src"), "-I"+str(root/"src/systemcmds/gpio_test"), str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [f"{MODE_NUMBER[mode]}:{TESTS[mode]['expected_hz']}" for mode in IO_MODES])
