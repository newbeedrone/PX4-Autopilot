// SPDX-License-Identifier: BSD-3-Clause
// Actual FMU adapter functions and IO mixer, with a byte-level UART substitute.
#include <array>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <vector>
#include <unistd.h>
#include <modules/px4iofirmware/protocol.h>

using hrt_abstime = uint64_t;
using irqstate_t = unsigned;
static hrt_abstime clock_us = 1000;
struct hrt_call { bool active{}; };
namespace px4 { using atomic_bool = std::atomic_bool; }
inline hrt_abstime hrt_absolute_time() { return clock_us; }
inline hrt_abstime hrt_elapsed_time(const hrt_abstime *since) { return clock_us - *since; }
inline irqstate_t enter_critical_section() { return 0; }
inline void leave_critical_section(irqstate_t) {}
inline void hrt_cancel(hrt_call *call) { call->active = false; }
inline void hrt_call_every(hrt_call *call, uint32_t, uint32_t, void (*)(void *), void *) { call->active = true; }
inline void up_disable_irq(int) {}
inline void irq_detach(int) {}
inline int px4_arch_configgpio(uint32_t) { return 0; }
inline int interface_flags(int, const char *, bool) { return 0; }

static uint16_t io_setup[256] {}, io_status[16] {};
static uint16_t r_page_servos[8] {}, r_page_direct_pwm[8] {}, r_page_servo_disarmed[8] {}, r_page_servo_failsafe[8] {};
static unsigned protocol_version = PX4IO_PROTOCOL_VERSION, actuator_count = 8;
static bool physical_armed{}, uart_enabled{}, drop_responses{}, corrupt_response{}, corrupt_readback{};
static bool ignore_rate_restore{};
static std::array<uint16_t, 8> physical_pwm{};
static unsigned writes{}, sbus_packets{};
static std::vector<uint8_t> tx;
static std::deque<uint8_t> rx;
static uint64_t mixer_at{};
static unsigned mixer_period = 1000;
static int _sbus_fd = -1;
static struct { hrt_abstime fmu_data_received_time{}; } system_state;
#define PX4IO_SERVO_COUNT 8
#define r_status_flags io_status[PX4IO_P_STATUS_FLAGS]
#define r_setup_arming io_setup[PX4IO_P_SETUP_ARMING]
#define r_setup_features io_setup[PX4IO_P_SETUP_FEATURES]
#define r_setup_flighttermination io_setup[PX4IO_P_SETUP_ENABLE_FLIGHTTERMINATION]
#define isr_debug(...) ((void)0)
inline void atomic_modify_or(uint16_t *word, uint16_t mask) { *word |= mask; }
inline void atomic_modify_clear(uint16_t *word, uint16_t mask) { *word &= ~mask; }
inline void up_pwm_servo_arm(bool armed, unsigned) { physical_armed = armed; if (!armed) { physical_pwm.fill(0); } }
inline void up_pwm_servo_set(unsigned channel, uint16_t value) { assert(channel < 8); physical_pwm[channel] = physical_armed ? value : 0; }
inline void sbus1_output(int, uint16_t *, unsigned) { ++sbus_packets; }
inline void sbus2_output(int, uint16_t *, unsigned) { ++sbus_packets; }

// This include is generated directly from the repository's mixer.cpp, unchanged.
#include "io_mixer_under_test.inc"

static void advance(uint64_t duration)
{
	const auto end = clock_us + duration;

	while (mixer_at + mixer_period <= end) {
		mixer_at += mixer_period; clock_us = mixer_at; mixer_tick();
	}

	clock_us = end;
}

static bool zeros(const uint16_t *data)
{
	for (unsigned i = 0; i < 8; ++i) { if (data[i]) { return false; } }

	return true;
}

// Simulated IO register/serial endpoint. CRC and READ/WRITE replies follow the
// installed protocol. PWM generation and FMU-loss behavior use the real mixer.
static void transact()
{
	IOPacket request{};
	memcpy(&request, tx.data(), tx.size());
	const auto crc = request.crc; request.crc = 0;
	assert(crc_packet(&request) == crc);
	IOPacket reply{}; reply.page = request.page; reply.offset = request.offset;
	const unsigned count = PKT_COUNT(request);

	if (PKT_CODE(request) == PKT_CODE_WRITE) {
		++writes;

		if (request.page == PX4IO_PAGE_DIRECT_PWM) {
			assert(request.offset == 0 && count == 8);
			memcpy(r_page_direct_pwm, request.regs, sizeof(r_page_direct_pwm));
			system_state.fmu_data_received_time = clock_us;
			r_status_flags |= PX4IO_P_STATUS_FLAGS_RAW_PWM;

		} else if (request.page == PX4IO_PAGE_DISARMED_PWM) {
			assert(request.offset == 0 && count == 8 && zeros(request.regs));
			memcpy(r_page_servo_disarmed, request.regs, sizeof(r_page_servo_disarmed));
			r_status_flags |= PX4IO_P_STATUS_FLAGS_INIT_OK;

		} else if (request.page == PX4IO_PAGE_SETUP) {
			assert((request.offset == PX4IO_P_SETUP_PWM_RATES && count == 3) ||
			       ((request.offset == PX4IO_P_SETUP_FEATURES || request.offset == PX4IO_P_SETUP_SBUS_RATE) && count == 1));

			if (!(ignore_rate_restore && request.offset == PX4IO_P_SETUP_PWM_RATES && request.regs[1] == 50)) {
				memcpy(io_setup + request.offset, request.regs, count * sizeof(uint16_t));
			}

		} else if (request.page == PX4IO_PAGE_STATUS) {
			assert(request.offset == PX4IO_P_STATUS_FLAGS && count == 1);

			if (!(r_status_flags & PX4IO_P_STATUS_FLAGS_ARM_SYNC)) { r_status_flags = request.regs[0]; }

		} else { assert(false && "Unexpected IO write"); }

		reply.count_code = PKT_CODE_SUCCESS;

	} else {
		reply.count_code = PKT_CODE_SUCCESS | count;

		for (unsigned i = 0; i < count; ++i) {
			const unsigned offset = request.offset + i;

			if (request.page == PX4IO_PAGE_CONFIG) {
				reply.regs[i] = offset == PX4IO_P_CONFIG_PROTOCOL_VERSION ? protocol_version : offset == PX4IO_P_CONFIG_ACTUATOR_COUNT ? actuator_count : 0;

			} else if (request.page == PX4IO_PAGE_SETUP) { reply.regs[i] = io_setup[offset]; }

			else if (request.page == PX4IO_PAGE_STATUS) { reply.regs[i] = io_status[offset]; }

			else if (request.page == PX4IO_PAGE_DISARMED_PWM) { reply.regs[i] = r_page_servo_disarmed[offset]; }

			else if (request.page == PX4IO_PAGE_DIRECT_PWM) { reply.regs[i] = corrupt_readback ? 0 : r_page_direct_pwm[offset]; }

			else if (request.page == PX4IO_PAGE_SERVOS) { reply.regs[i] = r_page_servos[offset]; }

			else { assert(false && "Unexpected IO read"); }
		}
	}

	reply.crc = crc_packet(&reply);

	if (corrupt_response) { reply.crc ^= 1; corrupt_response = false; }

	if (!drop_responses) {
		const auto *bytes = reinterpret_cast<const uint8_t *>(&reply);

		for (unsigned i = 0; i < PKT_SIZE(reply); ++i) { rx.push_back(bytes[i]); }
	}

	tx.clear();
}

constexpr uintptr_t STM32_USART6_BASE = 0x1000, STM32_RCC_APB2ENR = 0x2000;
constexpr uintptr_t STM32_USART_CR1_OFFSET = 0, STM32_USART_CR2_OFFSET = 4, STM32_USART_CR3_OFFSET = 8;
constexpr uintptr_t STM32_USART_BRR_OFFSET = 12, STM32_USART_PRESC_OFFSET = 16, STM32_USART_ISR_OFFSET = 20;
constexpr uintptr_t STM32_USART_TDR_OFFSET = 24, STM32_USART_RDR_OFFSET = 28, STM32_USART_ICR_OFFSET = 32;
constexpr uint32_t USART_CR1_TE = 1, USART_CR1_RE = 2, USART_CR1_FIFOEN = 4, USART_CR1_UE = 8;
constexpr uint32_t USART_ISR_TXE = 1, USART_ISR_TC = 2, USART_ISR_RXNE = 4;
constexpr uint32_t USART_ISR_ORE = 8, USART_ISR_FE = 16, USART_ISR_NE = 32, USART_ISR_PE = 64;
constexpr uint32_t GPIO_USART6_TX = 6, GPIO_USART6_RX = 7, STM32_PCLK2_FREQUENCY = 120000000, RCC_APB2ENR_USART6EN = 1;

static uint32_t getreg32(uintptr_t address)
{
	advance(5);

	if (address == STM32_USART6_BASE + STM32_USART_ISR_OFFSET) { return uart_enabled ? USART_ISR_TXE | USART_ISR_TC | (rx.empty() ? 0 : USART_ISR_RXNE) : 0; }

	if (address == STM32_USART6_BASE + STM32_USART_RDR_OFFSET) { assert(!rx.empty()); const auto byte = rx.front(); rx.pop_front(); return byte; }

	return 0;
}
static void putreg32(uint32_t value, uintptr_t address)
{
	if (address == STM32_USART6_BASE + STM32_USART_CR1_OFFSET) { uart_enabled = value & USART_CR1_UE; }

	if (address == STM32_USART6_BASE + STM32_USART_TDR_OFFSET && uart_enabled) {
		tx.push_back(value);

		if (tx.size() >= 4 && tx.size() == 4u + 2u * (tx[0] & PKT_COUNT_MASK)) { transact(); }
	}
}
static void modifyreg32(uintptr_t, uint32_t, uint32_t) {}
static void test_usleep(unsigned duration) { advance(duration); }
#define usleep test_usleep

#define private public
#include "protocol_test.hpp"
#undef private
#include "io_pwm_policy.hpp"
using protocol_test::Mode;
constexpr hrt_abstime LEASE_US = 2000000, REPORT_US = 1000000, STEP_US = 100000;
int ProtocolTest::i2c_start() { return -ENOTSUP; }
int ProtocolTest::adc_start() { return -ENOTSUP; }
int ProtocolTest::network_start() { return -ENOTSUP; }
void ProtocolTest::i2c_step() {}
void ProtocolTest::adc_step() {}
void ProtocolTest::rc_step() {}
void ProtocolTest::network_step() {}
void ProtocolTest::disable_outputs() { uart_enabled = false; }

// These methods are extracted from protocol_test_board.cpp without substitutions.
#include "io_adapter_under_test.inc"

static void reset_io()
{
	clock_us = mixer_at = 1000;
	mixer_period = 1000;
	memset(io_setup, 0, sizeof(io_setup)); memset(io_status, 0, sizeof(io_status));
	memset(r_page_servos, 0, sizeof(r_page_servos)); memset(r_page_direct_pwm, 0, sizeof(r_page_direct_pwm));
	memset(r_page_servo_disarmed, 0, sizeof(r_page_servo_disarmed)); memset(r_page_servo_failsafe, 0, sizeof(r_page_servo_failsafe));
	r_setup_features = PX4IO_P_SETUP_FEATURES_ADC_RSSI; // Standard IO power-on default.
	io_setup[PX4IO_P_SETUP_SBUS_RATE] = 72;
	io_setup[PX4IO_P_SETUP_PWM_DEFAULTRATE] = 50; io_setup[PX4IO_P_SETUP_PWM_ALTRATE] = 200;
	physical_armed = uart_enabled = drop_responses = corrupt_response = corrupt_readback = false;
	ignore_rate_restore = false;
	physical_pwm.fill(0); tx.clear(); rx.clear(); writes = sbus_packets = 0;
	protocol_version = PX4IO_PROTOCOL_VERSION; actuator_count = 8;
	system_state.fmu_data_received_time = 0;
	mixer_servos_armed = should_arm = should_arm_nothrottle = should_always_enable_pwm = false;
	new_fmu_data = false; last_fmu_update = 0;
}

int main()
{
	{
		reset_io();
		ProtocolTest source;
		assert(source.start(Mode::IoPwm5, 1234) == 0);
		ProtocolSample sample;

		for (unsigned i = 0; i < 12; ++i) { advance(100000); source.update(sample); }

		assert(source._sample.passed);
		assert(io_setup[PX4IO_P_SETUP_PWM_DEFAULTRATE] == 110);
		// A valid write ACK does not prove that the device restored its rates.
		ignore_rate_restore = true;
		assert(!source.stop());
		assert(source._io_needs_cleanup && source._io_saved_rates[1] == 50);
		assert(io_setup[PX4IO_P_SETUP_PWM_DEFAULTRATE] == 110);
		assert(source.start(Mode::IoPwm1, 1234) < 0);
		ignore_rate_restore = false;
		assert(source.stop());
		assert(!source._io_needs_cleanup);
		assert(io_setup[PX4IO_P_SETUP_PWM_DEFAULTRATE] == 50 && io_setup[PX4IO_P_SETUP_PWM_ALTRATE] == 200);
		assert(!physical_armed && zeros(r_page_direct_pwm) && zeros(r_page_servos));
	}

	{
		reset_io();
		ProtocolTest source;
		assert(source.start(Mode::SbusOut, 1234) == 0);
		assert(r_setup_features == PX4IO_P_SETUP_FEATURES_SBUS1_OUT);
		assert(io_setup[PX4IO_P_SETUP_SBUS_RATE] == 50);
		ProtocolSample sample;

		for (unsigned i = 0; i < 12; ++i) { advance(100000); source.update(sample); }

		assert(source._sample.passed && sbus_packets > 20);
		assert(zeros(r_page_direct_pwm) && zeros(r_page_servos));
		assert((physical_pwm == std::array<uint16_t, 8> {}));
		// A denied cleanup must retain SBUS restoration state after mode=None.
		drop_responses = true;
		assert(!source.stop());
		assert(source._io_sbus_cleanup && source._io_needs_cleanup);
		assert(source.start(Mode::IoPwm1, 1234) < 0);
		drop_responses = false;
		assert(source.stop());
		assert(!source._io_sbus_cleanup && !source._io_needs_cleanup);
		assert(r_setup_features == PX4IO_P_SETUP_FEATURES_ADC_RSSI);
		assert(io_setup[PX4IO_P_SETUP_SBUS_RATE] == 72);
		assert(!physical_armed && r_setup_arming == 0);
		const unsigned stopped_packets = sbus_packets;
		advance(100000);
		assert(sbus_packets == stopped_packets);
	}

	for (unsigned channel = 0; channel < 8; ++channel) {
		reset_io();
		mixer_period = channel % 2 ? 10000 : 1000;
		ProtocolTest source;
		const Mode mode = static_cast<Mode>(unsigned(Mode::IoPwm1) + channel);
		assert(source.start(mode, 1234 + channel) == 0);
		assert((physical_pwm == std::array<uint16_t, 8> {}));
		ProtocolSample sample;

		for (unsigned i = 0; i < 12; ++i) { advance(100000); source.update(sample); }

		advance(2000);

		for (unsigned i = 0; i < 8; ++i) { assert(physical_pwm[i] == (i == channel ? protocol_test::io_pwm_width(1234 + channel) : 0)); }

		if (!source._sample.passed) { fprintf(stderr, "channel=%u tx=%u rx=%u errors=%u flags=%u servo=%u\n", channel, source._sample.tx, source._sample.rx, source._sample.errors, r_status_flags, r_page_servos[channel]); }

		assert(source._sample.passed);
		assert(source._sample.rx >= 10);
		assert(io_setup[PX4IO_P_SETUP_PWM_DEFAULTRATE] == protocol_test::io_pwm_hz(mode));
		assert(source.stop());
		assert((physical_pwm == std::array<uint16_t, 8> {}));
		assert(!physical_armed);
		assert(zeros(r_page_direct_pwm) && zeros(r_page_servo_disarmed));
		assert(io_setup[PX4IO_P_SETUP_PWM_DEFAULTRATE] == 50 && io_setup[PX4IO_P_SETUP_PWM_ALTRATE] == 200);
		assert(r_setup_features == PX4IO_P_SETUP_FEATURES_ADC_RSSI && r_setup_arming == 0 && sbus_packets == 0);
	}

	for (unsigned fault = 0; fault < 5; ++fault) {
		reset_io();

		if (fault == 0) { r_setup_arming = PX4IO_P_SETUP_ARMING_FMU_ARMED; }

		if (fault == 1) { r_setup_features |= PX4IO_P_SETUP_FEATURES_SBUS1_OUT; }

		if (fault == 2) { protocol_version = 4; }

		if (fault == 3) { actuator_count = 4; }

		if (fault == 4) { r_page_servo_disarmed[0] = 1000; }

		ProtocolTest source;
		assert(source.start(Mode::IoPwm1, 1234) < 0);
		assert((physical_pwm == std::array<uint16_t, 8> {}));
		assert(writes == 0);
	}

	{
		reset_io(); corrupt_response = true;
		ProtocolTest source;
		assert(source.start(Mode::IoPwm1, 1234) < 0);
		assert(writes == 0);
	}

	{
		reset_io(); ProtocolTest source;
		assert(source.start(Mode::IoPwm1, 1234) == 0);
		ProtocolSample sample;
		advance(100000); corrupt_readback = true; source.update(sample);
		assert(source._sample.errors > 0 && !source._sample.passed);
		corrupt_readback = false;
		assert(source.stop());
		assert((physical_pwm == std::array<uint16_t, 8> {}));
	}

	{
		reset_io(); ProtocolTest source;
		assert(source.start(Mode::IoPwm8, 1234) == 0);
		ProtocolSample sample;

		for (unsigned i = 0; i < 19; ++i) { advance(100000); source.update(sample); }

		assert(physical_pwm[7]);
		advance(200000); ProtocolTest::watchdog(&source);
		assert(source.expired());
		assert(!source.update(sample));
		advance(500001);
		assert((physical_pwm == std::array<uint16_t, 8> {}));
		assert(source.stop());
	}

	{
		reset_io(); ProtocolTest source;
		assert(source.start(Mode::IoPwm1, 1234) == 0);
		ProtocolSample sample;
		advance(100000); source.update(sample);
		drop_responses = true;
		assert(!source.stop());
		assert(source._io_needs_cleanup);
		drop_responses = false;
		assert(source.stop());
		assert(!source._io_needs_cleanup);
	}
}
