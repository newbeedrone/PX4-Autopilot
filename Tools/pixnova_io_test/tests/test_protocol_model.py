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

    def test_bmm150_failure_is_reported_without_blocking_protocol(self):
        run = self.make()
        self.checks(run, "probe")
        self.checks(run, "source", fault="bmm150_i2c4")
        self.assertEqual(run.phase, "STARTING_PROBE")
        self.assertFalse(run.checks["source"]["bmm150_i2c4"]["pass"])
        self.assertTrue(any(command.startswith("PROTO") for role, command in self.sent))

    def test_adc_levels_require_measured_voltages_and_both_endpoints(self):
        for mode,output,voltage in (("ADC6V6_LOW", 0, 3), ("ADC6V6_HIGH", 3300, 1649)):
            run = self.make(mode); self.start(run)
            for n in range(1, 5):
                self.sample(run, "probe", n, detail=output)
                self.sample(run, "source", n, detail=voltage)
            self.assertEqual(run.result.status, "PASS")
            self.assertEqual(run.result.adc_voltage_mv, voltage)
            self.assertEqual(run.result.probe_output_mv, output)

    def test_adc_incorrect_voltage_fails_even_if_firmware_claims_pass(self):
        for mode,value in (("ADC6V6_LOW", 1650), ("ADC6V6_HIGH", 0), ("ADC6V6_HIGH", 540), ("ADC6V6_HIGH", 3300)):
            run = self.make(mode); self.start(run)
            self.sample(run, "source", 1, detail=value)
            self.assertEqual(run.phase, "FAILED")
            self.assertIn("ADC measured", run.result.reason)

    def test_adc_source_voltage_alone_or_incorrect_probe_level_cannot_pass(self):
        for missing in (True, False):
            run = self.make("ADC6V6_HIGH"); self.start(run)
            for n in range(1, 6):
                if not missing: self.sample(run, "probe", n, detail=0)
                self.sample(run, "source", n, detail=1650)
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
            self.assertEqual(run.phase, "FAILED")

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
            self.assertEqual(run.phase, "FAILED")

    def test_check_cannot_be_reused_for_another_protocol(self):
        run = self.make("CAN")
        run.handle("source", {"type":"checked", "session":run.session, "mode":"GPIO", "pass":True}, .1)
        self.assertEqual(run.phase, "FAILED")

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
            self.assertEqual(run.phase, "FAILED")

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
        self.assertEqual(run.result.status, "PASS")

    def test_failed_can_measurement_keeps_observed_counters(self):
        run = self.make("CAN"); self.start(run)
        self.sample(run, "source", 1, tx=4, rx=0, errors=5, detail=0, **{"pass": False})
        self.assertEqual(run.phase, "FAILED")
        self.assertEqual(run.result.source_tx, 4)
        self.assertEqual(run.result.errors, 5)

    def test_bursty_or_stale_frames_do_not_pass(self):
        run = self.make("CAN"); self.start(run)
        for n in range(1, 6): self.sample(run, "source", n, now=.4 + n * .01)
        self.assertNotEqual(run.result.status, "PASS")
        self.sample(run, "source", 7, now=5, _received_at=1)
        self.assertEqual(run.phase, "FAILED")

    def test_disconnect_and_lease_expiry_stop_both(self):
        run = self.make(); self.start(run)
        run.tick(3)
        self.assertEqual(run.phase, "FAILED")
        self.assertIn(("source", "STOP"), self.sent)
        self.assertIn(("probe", "STOP"), self.sent)

    def test_ethernet_requires_pc_validation(self):
        run = self.make("ETH"); self.start(run)
        for n in range(1, 6): self.sample(run, "source", n)
        self.assertNotEqual(run.result.status, "PASS")
        run.accept_udp(packet("ETH", run.token, 1, True), 1, 5)
        run.accept_udp(packet("ETH", run.token, 1, True), 1, 5.1)
        self.assertEqual(run.phase, "FAILED")

    def test_ethernet_corruption_and_nonce_mismatch_fail(self):
        run = self.make("ETH"); self.start(run)
        run.accept_udp(packet("ETH", run.token ^ 1, 1, True), 1, 1)
        self.assertEqual(run.phase, "FAILED")

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
        self.assertEqual(run.phase, "COMPLETE")

    def test_rc_wrong_channel_pattern_never_passes(self):
        run = self.make("DSM"); self.start(run)
        for n in range(1, 6):
            self.sample(run, "probe", n)
            self.sample(run, "source", n, channels=[1500] * 7)
        self.assertEqual(run.phase, "RUNNING")

    def test_cpp_codecs_match_python_and_px4_channel_scaling(self):
        root = HERE.parents[2]
        source = r'''
#include "protocol_codec.hpp"
#include <stdio.h>
#include <assert.h>
#include <math.h>
int main() {
    using namespace protocol_test;
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
