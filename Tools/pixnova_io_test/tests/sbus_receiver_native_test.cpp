// SPDX-License-Identifier: BSD-3-Clause
// Production decoder and report adapter; only the GPIO/clock/IRQ boundary is replaced.
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <vector>
using hrt_abstime = uint64_t;
using irqstate_t = unsigned;
struct hrt_call {};
namespace px4 { using atomic_bool = std::atomic_bool; }
static uint64_t clock_us = 1000;
static uint64_t hrt_absolute_time() { return clock_us; }
static irqstate_t enter_critical_section() { return 0; }
static void leave_critical_section(irqstate_t) {}
static bool px4_arch_gpioread(unsigned) { return false; }
constexpr unsigned SBUS_OUT_RECEIVE = 5;
constexpr uint64_t REPORT_US = 1000000, STEP_US = 100000;
#define CONFIG_GPIO_TEST_RECEIVER 1
#define private public
#include "protocol_test.hpp"
#undef private
using protocol_test::Mode;
bool ProtocolTest::stop() { return true; }
void ProtocolTest::i2c_step() {}
void ProtocolTest::adc_step() {}
void ProtocolTest::io_pwm_step() {}
void ProtocolTest::rc_step() {}
void ProtocolTest::network_step() {}
#include "sbus_receiver_under_test.inc"
#include "sbus_waveform_under_test.inc"

int main()
{
	for (unsigned scenario = 0; scenario < 7; ++scenario) {
		clock_us = 1000;
		ProtocolTest probe;
		probe._mode = protocol_test::Mode::SbusOut;
		probe._report = probe._step = clock_us;
		bool level = false;
		probe._sbus_capture.reset(clock_us, level);
		std::vector<uint8_t> frame(25, 0);
		frame[0] = 0x0f;

		for (unsigned window = 1; window <= 7; ++window) {
			const bool burst = scenario == 3 || scenario == 4 || (scenario == 5 && window <= 3);
			const unsigned hz = scenario == 0 ? 40 : scenario == 2 ? 55 : 50;
			const uint64_t duration = scenario == 4 || scenario == 6 ? 1500000 : REPORT_US;
			const unsigned count = burst ? 5 : hz * duration / REPORT_US;
			const uint64_t period = (REPORT_US + hz - 1) / hz;
			const uint64_t begin = clock_us, first = burst ? duration - 100000 : 2000;
			unsigned sent = 0;
			ProtocolSample sample;

			// The report falls just before the next 100 ms task callback.
			for (uint64_t tick = 100500; tick < duration; tick += STEP_US) {
				while (sent < count && first + sent * period + 4000 <= tick) {
					waveform(probe._sbus_capture, frame, begin + first + sent++ * period, level);
				}

				clock_us = begin + tick;

				if (tick < REPORT_US) { assert(!probe.update(sample)); }
			}

			while (sent < count) { waveform(probe._sbus_capture, frame, begin + first + sent++ * period, level); }

			clock_us = begin + duration + 17;
			assert(probe.update(sample));
			assert(sample.passed == !burst);
			assert(sample.errors == 0);
			assert(sample.sequence == window);
			assert(probe._sbus_capture.statistics().window_frames == 0);
			printf("%u %u %u %u %d %u\n", scenario, window, sample.rx, sample.errors, sample.detail, sample.passed);
		}
	}
}
