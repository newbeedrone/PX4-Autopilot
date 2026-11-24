import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from peripherals import DEVICES
from protocol_model import ProtocolRun, packet, rc_values


class ProtocolTests(unittest.TestCase):
    def make(self, mode="I2C3"):
        self.sent = []
        run = ProtocolRun(mode, "a" * 64, 0, lambda *args: self.sent.append(args))
        run.begin()
        return run

    def checks(self, run, role, fault=None, omit=None):
        checks = {name: spec.get("demo", spec["id"]) for name, spec in DEVICES.items()} if role == "source" else {"probe_protocol_firmware": 1}
        checks |= {"hrt_clock": 5000, "gpio_inputs": 43}
        for name, value in checks.items():
            if name == omit: continue
            run.handle(role, {"type": "check", "session": run.session, "test": name, "pass": name != fault, "detail": value}, .1)
        blocking = fault in ("hrt_clock", "gpio_inputs", "probe_protocol_firmware")
        run.handle(role, {"type": "checked", "session": run.session, "mode": run.mode, "pass": not blocking}, .2)

    def start(self, run):
        for role in sorted(run.roles): self.checks(run, role)
        for role in (name for name in ("probe", "source") if name in run.roles):
            run.handle(role, {"type": "started", "session": run.session, "mode": run.mode, "role": role}, .3)
        self.assertEqual(run.phase, "RUNNING")

    def sample(self, run, role, sequence, now=None, **changes):
        frame = {"type": "protocol_sample", "mode": run.mode, "session": run.session, "seq": sequence,
                 "tx": sequence * 20, "rx": sequence * 20, "errors": 0, "detail": 3 if run.mode == "CAN" else 0, "pass": True}
        frame.update(changes)
        run.handle(role, frame, sequence if now is None else now)

    def confirm_stop(self, run):
        self.assertEqual(run.phase, "STOPPING")
        self.assertFalse(run.outputs_stopped)
        for role in sorted(run.roles):
            run.handle(role, {"type": "stopped", "session": run.session, "reason": "requested"}, run.now + .01)
        self.assertTrue(run.outputs_stopped)

    def test_bmm150_failure_is_reported_without_blocking_protocol(self):
        run = self.make()
        self.checks(run, "probe")
        self.checks(run, "source", fault="bmm150_i2c4")
        self.assertEqual(run.phase, "STARTING_PROBE")
        self.assertFalse(run.checks["source"]["bmm150_i2c4"]["pass"])
        self.assertTrue(any(command.startswith("PROTO") for role, command in self.sent))

    def test_adc_levels_require_measured_voltages_and_both_endpoints(self):
        for mode,output,voltage in (("ADC6V6_LOW", 0, 3), ("ADC6V6_HIGH", 3300, 1649),
                                    ("ADC3V3_LOW", 0, 4), ("ADC3V3_HIGH", 3300, 3294)):
            run = self.make(mode); self.start(run)
            for n in range(1, 5):
                self.sample(run, "probe", n, detail=output)
                self.sample(run, "source", n, detail=voltage)
            self.confirm_stop(run)
            self.assertEqual(run.result.status, "PASS")
            self.assertEqual(run.result.adc_voltage_mv, voltage)
            self.assertEqual(run.result.probe_output_mv, output)

    def test_adc_incorrect_voltage_fails_even_if_firmware_claims_pass(self):
        for mode,value in (("ADC6V6_LOW", 1650), ("ADC6V6_HIGH", 0), ("ADC6V6_HIGH", 540), ("ADC6V6_HIGH", 3300),
                           ("ADC3V3_LOW", 3300), ("ADC3V3_HIGH", 1650), ("ADC3V3_HIGH", 2999), ("ADC3V3_HIGH", 3401)):
            run = self.make(mode); self.start(run)
            self.sample(run, "source", 1, detail=value)
            self.assertEqual(run.phase, "STOPPING")
            self.assertEqual(run.result.status, "FAIL")
            self.assertIn("ADC measured", run.result.reason)

    def test_adc_source_voltage_alone_or_incorrect_probe_level_cannot_pass(self):
        for mode,voltage in (("ADC6V6_HIGH", 1650), ("ADC3V3_HIGH", 3300)):
            for missing in (True, False):
                run = self.make(mode); self.start(run)
                for n in range(1, 6):
                    if not missing: self.sample(run, "probe", n, detail=0)
                    self.sample(run, "source", n, detail=voltage)
                self.assertNotEqual(run.result.status, "PASS")

    def test_missing_storage_diagnostic_does_not_block_interfaces(self):
        for missing in ("sdcard", "fram_spi5", "eeprom_base_i2c3", "eeprom_imu_i2c4"):
            run = self.make()
            self.checks(run, "source", omit=missing)
            self.assertEqual(run.phase, "CHECKING")
            self.checks(run, "probe")
            self.assertEqual(run.phase, "STARTING_PROBE")

    def test_external_clock_and_gpio_failures_remain_blocking(self):
        for fault in ("hrt_clock", "gpio_inputs"):
            run = self.make("CAN")
            self.checks(run, "source", fault=fault)
            self.assertEqual(run.phase, "STOPPING")
            self.assertEqual(run.result.status, "FAIL")

    def test_all_onboard_failures_allow_external_protocol_but_remain_failed(self):
        run = self.make("CAN")
        for name in DEVICES:
            run.handle("source", {"type": "check", "session": run.session, "test": name, "pass": False, "detail": 0}, .1)
        for name, value in (("hrt_clock", 5000), ("gpio_inputs", 43)):
            run.handle("source", {"type": "check", "session": run.session, "test": name, "pass": True, "detail": value}, .1)
        run.handle("source", {"type": "checked", "session": run.session, "mode": "CAN", "pass": True, "onboard_pass": False}, .2)
        self.assertEqual(run.phase, "STARTING_SOURCE")
        self.assertTrue(all(not run.checks["source"][name]["pass"] for name in DEVICES))

    def test_invalid_interface_detail_blocks_even_when_summary_passes(self):
        for name, value in (("hrt_clock", 1), ("gpio_inputs", 42)):
            run = self.make("CAN")
            for key, detail in (("hrt_clock", 5000), ("gpio_inputs", 43)):
                run.handle("source", {"type": "check", "session": run.session, "test": key,
                                      "pass": True, "detail": value if key == name else detail}, .1)
            run.handle("source", {"type": "checked", "session": run.session, "mode": "CAN", "pass": True}, .2)
            self.assertEqual(run.phase, "STOPPING")
            self.assertEqual(run.result.status, "FAIL")

    def test_check_cannot_be_reused_for_another_protocol(self):
        run = self.make("CAN")
        run.handle("source", {"type":"checked", "session":run.session, "mode":"GPIO", "pass":True}, .1)
        self.assertEqual(run.phase, "STOPPING")
        self.assertEqual(run.result.status, "FAIL")

    def test_probe_is_started_before_source(self):
        run = self.make()
        self.checks(run, "source"); self.checks(run, "probe")
        starts = [(role, command) for role, command in self.sent if command.startswith("PROTO")]
        self.assertEqual([role for role, _ in starts], ["probe"])

    def test_pass_requires_both_endpoints_and_three_fresh_windows(self):
        run = self.make(); self.start(run)
        for n in range(1, 4):
            self.sample(run, "probe", n); self.sample(run, "source", n)
            self.assertNotEqual(run.phase, "COMPLETE")
        self.sample(run, "probe", 4); self.sample(run, "source", 4)
        self.assertEqual(run.phase, "STOPPING")
        self.assertNotEqual(run.result.status, "PASS")
        self.sample(run, "probe", 3, now=4.1, _received_at=1)
        self.confirm_stop(run)
        self.assertEqual(run.phase, "COMPLETE")
        self.assertEqual(run.result.status, "PASS")
        self.assertEqual(set(self.sent[-2:]), {("probe", "STOP"), ("source", "STOP")})

    def test_source_claim_alone_cannot_pass_i2c(self):
        run = self.make(); self.start(run)
        for n in range(1, 8): self.sample(run, "source", n)
        self.assertNotEqual(run.result.status, "PASS")

    def test_replay_and_counter_regression_fail(self):
        for changes in ({"seq": 1}, {"rx": 0}, {"errors": 1}):
            run = self.make("CAN"); self.start(run)
            self.sample(run, "source", 1)
            self.sample(run, "source", 2, **changes)
            self.assertEqual(run.phase, "STOPPING")
            self.assertEqual(run.result.status, "FAIL")

    def test_frozen_counters_do_not_pass(self):
        run = self.make("CAN"); self.start(run)
        for n in range(1, 6): self.sample(run, "source", n, rx=20)
        self.assertNotEqual(run.result.status, "PASS")

    def test_can_requires_both_directions_and_minimum_frame_count(self):
        for changes in ({"detail": 1}, {"detail": 2}, {"rx": 39}, {"tx": 39}):
            run = self.make("CAN"); self.start(run)
            for n in range(1, 6):
                self.sample(run, "source", n, **changes)
            self.assertNotEqual(run.result.status, "PASS")
        run = self.make("CAN"); self.start(run)
        for n in range(1, 5): self.sample(run, "source", n)
        self.confirm_stop(run)
        self.assertEqual(run.result.status, "PASS")

    def test_failed_can_measurement_keeps_observed_counters(self):
        run = self.make("CAN"); self.start(run)
        self.sample(run, "source", 1, tx=4, rx=0, errors=5, detail=0, **{"pass": False})
        self.assertEqual(run.phase, "STOPPING")
        self.assertEqual(run.result.status, "FAIL")
        self.assertEqual(run.result.source_tx, 4)
        self.assertEqual(run.result.errors, 5)

    def test_bursty_or_stale_frames_do_not_pass(self):
        run = self.make("CAN"); self.start(run)
        for n in range(1, 6): self.sample(run, "source", n, now=.4 + n * .01)
        self.assertNotEqual(run.result.status, "PASS")
        self.sample(run, "source", 7, now=5, _received_at=1)
        self.assertEqual(run.phase, "STOPPING")
        self.assertEqual(run.result.status, "FAIL")

    def test_disconnect_and_lease_expiry_stop_both(self):
        run = self.make(); self.start(run)
        run.tick(3)
        self.assertEqual(run.phase, "STOPPING")
        self.assertEqual(run.result.status, "FAIL")
        self.assertIn(("source", "STOP"), self.sent)
        self.assertIn(("probe", "STOP"), self.sent)

    def test_ethernet_requires_pc_validation(self):
        run = self.make("ETH"); self.start(run)
        for n in range(1, 6): self.sample(run, "source", n)
        self.assertNotEqual(run.result.status, "PASS")
        run.accept_udp(packet("ETH", run.token, 1, True), 1, 5)
        run.accept_udp(packet("ETH", run.token, 1, True), 1, 5.1)
        self.assertEqual(run.phase, "STOPPING")
        self.assertEqual(run.result.status, "FAIL")

    def test_ethernet_corruption_and_nonce_mismatch_fail(self):
        run = self.make("ETH"); self.start(run)
        run.accept_udp(packet("ETH", run.token ^ 1, 1, True), 1, 1)
        self.assertEqual(run.phase, "STOPPING")
        self.assertEqual(run.result.status, "FAIL")

    def test_rc_checks_channel_values_then_actual_signal_loss(self):
        run = self.make("SBUS"); self.start(run)
        for n in range(1, 5):
            self.sample(run, "probe", n)
            self.sample(run, "source", n, channels=rc_values(run.token), detail=(1 << 1) | (1 << 20))
        self.assertEqual(run.phase, "RC_LOSS")
        self.assertIn(("probe", "STOP"), self.sent)
        self.sample(run, "source", 5, channels=rc_values(run.token), detail=(1 << 1) | (1 << 20), rx=80, **{"pass": False})
        self.assertEqual(run.phase, "RC_LOSS")
        self.sample(run, "source", 6, channels=rc_values(run.token), detail=0, rx=80, **{"pass": False})
        self.sample(run, "source", 7, channels=rc_values(run.token), detail=0, rx=80, **{"pass": False})
        self.assertEqual(run.phase, "STOPPING")
        self.assertNotEqual(run.result.status, "PASS")
        self.confirm_stop(run)
        self.assertEqual(run.phase, "COMPLETE")

    def test_rc_wrong_channel_pattern_never_passes(self):
        run = self.make("DSM"); self.start(run)
        for n in range(1, 6):
            self.sample(run, "probe", n)
            self.sample(run, "source", n, channels=[1500] * 7)
        self.assertEqual(run.phase, "RUNNING")

    def test_completion_requires_fresh_matching_acknowledgements_from_every_role(self):
        run = self.make(); self.start(run)
        for n in range(1, 5):
            self.sample(run, "probe", n); self.sample(run, "source", n)
        ack = {"type": "stopped", "session": run.session, "reason": "requested"}
        run.handle("source", {**ack, "session": "old"}, 4.01)
        run.handle("probe", {**ack, "_received_at": 3.9}, 4.02)
        run.handle("other", ack, 4.03)
        self.assertEqual(run.stop_acks, set())
        run.handle("source", ack, 4.04)
        run.handle("source", ack, 4.05)
        self.assertEqual(run.phase, "STOPPING")
        self.assertNotEqual(run.result.status, "PASS")
        self.assertFalse(run.snapshot()["outputs_stopped"])
        run.handle("probe", ack, 4.06)
        self.assertEqual(run.phase, "COMPLETE")
        self.assertEqual(run.result.status, "PASS")
        self.assertEqual(run.snapshot()["stop_confirmed_roles"], ["probe", "source"])

    def test_missing_stop_ack_times_out_without_heartbeats_or_a_pass(self):
        run = self.make(); self.start(run)
        run.complete("Measured")
        sent = list(self.sent)
        run.handle("source", {"type": "stopped", "session": run.session, "reason": "requested"}, .4)
        for now in (1, 2, 3): run.tick(now)
        self.assertEqual(self.sent, sent)
        self.assertEqual(run.phase, "STOPPING")
        run.tick(3.31)
        self.assertEqual(run.phase, "FAILED")
        self.assertEqual(run.result.status, "FAIL")
        self.assertFalse(run.outputs_stopped)
        self.assertIn("STOP unconfirmed: probe", run.result.reason)

    def test_stop_send_error_and_unexpected_stop_reason_cannot_pass(self):
        for error in (True, False):
            run = self.make(); self.start(run)
            if error:
                def broken_send(*_): raise RuntimeError("write failed")
                run.send = broken_send
            run.complete("Measured")
            if not error:
                run.handle("source", {"type": "stopped", "session": run.session, "reason": "heartbeat_timeout"}, .4)
            run.tick(4)
            self.assertEqual(run.phase, "FAILED")
            self.assertFalse(run.outputs_stopped)
            self.assertNotEqual(run.result.status, "PASS")
            if error: self.assertEqual(set(run.snapshot()["stop_errors"]), run.roles)

    def test_ack_after_stop_deadline_cannot_turn_timeout_into_pass(self):
        run = self.make("CAN"); self.start(run); run.complete("Measured")
        run.handle("source", {"type": "stopped", "session": run.session, "reason": "requested"}, run.deadline + .01)
        self.assertEqual(run.phase, "FAILED")
        self.assertFalse(run.outputs_stopped)
        self.assertEqual(run.result.status, "FAIL")

    def test_disconnect_during_stopping_revokes_pending_pass(self):
        run = self.make(); self.start(run); run.complete("Measured")
        run.handle("probe", {"type": "disconnected"}, .4)
        self.confirm_stop(run)
        self.assertEqual(run.phase, "FAILED")
        self.assertIn("disconnected", run.result.reason)

    def test_cancel_pending_pass_preserves_deadline_and_waits_for_stop(self):
        run = self.make(); self.start(run); run.complete("Measured")
        deadline = run.deadline
        run.stop("Operator cancelled", now=1)
        run.stop("Operator cancelled again", now=2)
        self.assertEqual(run.deadline, deadline)
        self.confirm_stop(run)
        self.assertEqual(run.phase, "STOPPED")
        self.assertEqual(run.result.status, "NOT RUN")

    def test_rc_loss_stop_ack_does_not_replace_final_stop_confirmation(self):
        run = self.make("SBUS"); self.start(run)
        for n in range(1, 5):
            self.sample(run, "probe", n)
            self.sample(run, "source", n, channels=rc_values(run.token), detail=2)
        run.handle("probe", {"type": "stopped", "session": run.session, "reason": "requested"}, 4.1)
        self.sample(run, "source", 5, channels=rc_values(run.token), detail=0, rx=80, **{"pass": False})
        self.sample(run, "source", 6, channels=rc_values(run.token), detail=0, rx=80, **{"pass": False})
        self.assertEqual(run.phase, "STOPPING")
        run.handle("source", {"type": "stopped", "session": run.session, "reason": "requested"}, 6.1)
        self.assertEqual(run.phase, "STOPPING")
        run.handle("probe", {"type": "stopped", "session": run.session, "reason": "requested"}, 6.2)
        self.assertEqual(run.phase, "COMPLETE")

    def test_cpp_codecs_match_python_and_px4_channel_scaling(self):
        root = HERE.parents[2]
        source = r'''
#include "protocol_codec.hpp"
#include <stdio.h>
#include <assert.h>
#include <math.h>
int main() {
    using namespace protocol_test;
    assert(unsigned(Mode::AdcLow)==11 && unsigned(Mode::AdcHigh)==12);
    assert(unsigned(Mode::Adc3v3Low)==13 && unsigned(Mode::Adc3v3High)==14);
    assert(mode("ADC3V3_LOW")==Mode::Adc3v3Low && mode("ADC3V3_HIGH")==Mode::Adc3v3High);
    assert(strcmp(name(Mode::Adc3v3High),"ADC3V3_HIGH")==0);
    assert(adc_voltage_valid(Mode::Adc3v3Low,0) && adc_voltage_valid(Mode::Adc3v3Low,100));
    assert(!adc_voltage_valid(Mode::Adc3v3Low,101));
    assert(adc_voltage_valid(Mode::Adc3v3High,3000) && adc_voltage_valid(Mode::Adc3v3High,3400));
    assert(!adc_voltage_valid(Mode::Adc3v3High,2999) && !adc_voltage_valid(Mode::Adc3v3High,3401));
    assert(!adc_voltage_valid(Mode::Adc3v3High,1650) && adc_voltage_valid(Mode::AdcHigh,1650));
    assert(!adc_voltage_valid(Mode::None,0));
    const uint8_t known[]="123456789";
    assert(crc16(known,9)==0x29b1);
    for(uint32_t token : {0u, 1u, 0xffffffffu}) {
        uint8_t frame[32];
        sbus(frame,token);
        assert(frame[0]==15 && frame[23]==0 && frame[24]==0);
        for(unsigned c=0;c<16;c++) {
            unsigned raw=0;
            for(unsigned b=0;b<11;b++) raw|=((frame[1+(c*11+b)/8]>>((c*11+b)%8))&1)<<b;
            assert(abs(int(lround(raw*.625)+874)-int(rc_value(token,c)))<=1);
        }
        dsm(frame,token);
        for(unsigned c=0;c<7;c++) {
            unsigned word=(frame[2+c*2]<<8)|frame[3+c*2];
            assert((word>>11)==c);
            unsigned mapped=c==0?2:c==1?0:c==2?1:c;
            assert(abs(int(lround((word&2047)*.583)+903)-int(rc_value(token,mapped)))<=1);
        }
    }
    uint8_t p[32];
    packet(p,Mode::Ethernet,0x12345678,42,true);
    for(auto b:p) printf("%02x",b);
    assert(valid_packet(p,32,Mode::Ethernet,0x12345678,42,true));
    for(unsigned i=0;i<32;i++) {p[i]^=1;assert(!valid_packet(p,32,Mode::Ethernet,0x12345678,42,true));p[i]^=1;}
    assert(!valid_packet(p,31,Mode::Ethernet,0x12345678,42,true));
    assert(!valid_packet(p,32,Mode::Ethernet,0x12345678,43,true));
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            cpp = Path(tmp) / "codecs.cpp"; cpp.write_text("#include <initializer_list>\n#include <stdlib.h>\n" + source)
            exe = Path(tmp) / "codecs"
            subprocess.run(["g++", "-std=c++14", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            "-I" + str(root / "src/systemcmds/gpio_test"), str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(bytes.fromhex(result.stdout), packet("ETH", 0x12345678, 42, True))
