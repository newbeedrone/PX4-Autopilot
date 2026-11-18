// SPDX-License-Identifier: BSD-3-Clause
// Actual fast receiver adapter; replace only registers, NVIC and vector-table APIs.
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <cerrno>
using hrt_abstime = uint64_t;
using irqstate_t = unsigned;
struct hrt_call {};
namespace px4 { using atomic_bool = std::atomic_bool; }
#define CONFIG_GPIO_TEST_RECEIVER 1
#define CONFIG_ARCH_HIPRI_INTERRUPT 1
#define private public
#include "protocol_test.hpp"
#undef private
using protocol_test::Mode;
constexpr unsigned SBUS_OUT_RECEIVE = 5, INPUT = 0, GPIO_EXTI = 1;
constexpr unsigned STM32_IRQ_EXTI95 = 23, NVIC_SYSH_PRIORITY_DEFAULT = 128, NVIC_SYSH_HIGH_PRIORITY = 96;
constexpr unsigned STM32_EXTI_CPUIMR1 = 1, STM32_EXTI_CPUPR1 = 2, STM32_TIM8_CNT = 3, STM32_GPIOG_IDR = 4;
constexpr uint64_t LEASE_US = 2000000, REPORT_US = 1000000;
static uint64_t clock_us = 1000;
static uint32_t mask = 0, pending = 0;
static bool level = false, irq_enabled = false, fast_context = false;
static unsigned priority = NVIC_SYSH_PRIORITY_DEFAULT;
static int attach_result = 0;
static void (*vector)() = nullptr;
static hrt_abstime hrt_absolute_time() { assert(!fast_context); return clock_us; }
static irqstate_t enter_critical_section() { assert(!fast_context); return 0; }
static void leave_critical_section(irqstate_t) { assert(!fast_context); }
static int px4_arch_configgpio(unsigned) { return 0; }
static bool px4_arch_gpioread(unsigned) { return level; }
static uint16_t getreg16(unsigned reg) { assert(reg == STM32_TIM8_CNT); return clock_us & 0xffff; }
static uint32_t getreg32(unsigned reg)
{
	return reg == STM32_EXTI_CPUIMR1 ? mask : reg == STM32_EXTI_CPUPR1 ? pending : level ? 1u << 5 : 0;
}
static void putreg32(uint32_t value, unsigned reg) { assert(reg == STM32_EXTI_CPUPR1); pending &= ~value; }
static void up_disable_irq(unsigned irq) { assert(irq == STM32_IRQ_EXTI95); irq_enabled = false; }
static void up_enable_irq(unsigned irq) { assert(irq == STM32_IRQ_EXTI95); irq_enabled = true; }
static int up_prioritize_irq(unsigned irq, unsigned value) { assert(irq == STM32_IRQ_EXTI95); priority = value; return 0; }
static int arm_ramvec_attach(unsigned irq, void (*handler)())
{
	assert(irq == STM32_IRQ_EXTI95 && !irq_enabled);

	if (attach_result) { return attach_result; }

	vector = handler;
	return 0;
}
static int px4_arch_gpiosetevent(unsigned, bool rise, bool, bool, int (*)(int, void *, void *), void *)
{
	mask = rise ? mask | (1u << 5) : mask & ~(1u << 5);
	irq_enabled = rise;
	return 0;
}
ProtocolTest *ProtocolTest::_sbus_owner = nullptr;
void ProtocolTest::disable_outputs() {}
bool ProtocolTest::stop() { if (_sbus_irq_attached) { sbus_out_stop(); } return true; }
#include "sbus_fast_under_test.inc"

static void edge(bool high)
{
	if (level == high) { return; }

	level = high;
	pending |= 1u << 5;
	assert(irq_enabled && vector && priority == NVIC_SYSH_HIGH_PRIORITY);
	fast_context = true;
	vector(); // Must work even while ordinary RTOS IRQ processing is masked.
	fast_context = false;
	assert(!pending);
}

static void frame(uint64_t begin)
{
	for (unsigned byte = 0; byte < 25; ++byte) {
		unsigned data = byte == 0 ? 0x0f : 0, parity = 0;

		for (unsigned bit = 0; bit < 12; ++bit) {
			clock_us = begin + (byte * 12 + bit) * 10;
			bool logical = bit >= 10;

			if (bit > 0 && bit < 9) { logical = (data >> (bit - 1)) & 1; parity ^= logical; }

			if (bit == 9) { logical = parity; }

			edge(!logical);
		}
	}
}

int main()
{
	ProtocolTest probe;
	probe._mode = Mode::SbusOut;
	mask = 1u << 8; irq_enabled = true;
	assert(probe.sbus_out_start() == -EBUSY && irq_enabled && !vector && !ProtocolTest::_sbus_owner);
	mask = 0; attach_result = -EIO;
	assert(probe.sbus_out_start() == -EIO);
	attach_result = 0;
	probe.sbus_out_stop();
	assert(!irq_enabled && !vector && priority == NVIC_SYSH_PRIORITY_DEFAULT && !ProtocolTest::_sbus_owner);
	assert(probe.sbus_out_start() == 0);
	probe._report = clock_us;

	for (unsigned window = 0; window < 3; ++window) {
		const uint64_t begin = clock_us;

		for (unsigned n = 0; n < 50; ++n) {
			frame(begin + 2000 + n * 20000);
			probe._feed = clock_us;
			ProtocolTest::watchdog(&probe);
		}

		clock_us = begin + REPORT_US;
		probe.sbus_out_step();
		probe._report = clock_us;
		assert(probe._sample.passed && probe._sample.errors == 0 && probe._sample.rx == (window + 1) * 50);
		assert(irq_enabled && priority == NVIC_SYSH_HIGH_PRIORITY);
	}

	probe.sbus_out_stop();
	assert(!irq_enabled && !vector && priority == NVIC_SYSH_PRIORITY_DEFAULT && !ProtocolTest::_sbus_owner && !mask);
	assert(probe.sbus_out_start() == 0); // Repeated tests restore and reacquire the vector.
	probe.sbus_out_stop();
}
