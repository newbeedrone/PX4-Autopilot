// SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include "protocol_codec.hpp"
#include "i2c_simulator.hpp"
#include "edge_counter.hpp"
#include "sbus_capture.hpp"
#include <drivers/drv_hrt.h>
#include <px4_platform_common/atomic.h>

struct ProtocolSample {
	uint32_t sequence{0};
	uint32_t tx{0};
	uint32_t rx{0};
	uint32_t errors{0};
	int detail{0};
	bool passed{false};
	uint16_t channels[protocol_test::RC_CHANNELS] {};
	int32_t capture[5] {};
};

// The board adapter owns peripheral routing. No flight or actuator drivers run.
class ProtocolTest
{
public:
	~ProtocolTest() { stop(); }
	int start(protocol_test::Mode mode, uint32_t token);
	bool stop();
	bool feed();
	bool active() const { return _mode != protocol_test::Mode::None; }
	bool expired() const { return _expired.load(); }
	protocol_test::Mode mode() const { return _mode; }
	bool update(ProtocolSample &sample);

private:
	static int i2c_irq(int, void *, void *context);
	static int gpio_i2c_irq(int, void *, void *context);
	static void watchdog(void *context);
	void disable_outputs();
	int i2c_start();
	void i2c_step();
	int adc_start();
	void adc_step();
	int uart_start();
	bool uart_write(const uint8_t *data, unsigned length);
	bool io_read(uint8_t page, uint8_t offset, uint16_t *data, unsigned count);
	bool io_transfer(uint8_t page, uint8_t offset, uint16_t *data, unsigned count, bool write);
	bool io_write(uint8_t page, uint8_t offset, uint16_t *data, unsigned count);
	int io_pwm_start();
	void io_pwm_step();
	bool io_pwm_stop();
	static void io_pwm_capture(void *context);
	void rc_step();
	int sbus_out_start();
	void sbus_out_stop();
	void sbus_out_step();
	static int sbus_out_irq(int, void *, void *context);
	static void sbus_out_fast_irq();
	static ProtocolTest *_sbus_owner;
	int network_start();
	void network_step();
	bool ethernet_link();
	protocol_test::Mode _mode{protocol_test::Mode::None};
	uint32_t _token{0};
	ProtocolSample _sample{};
	hrt_abstime _feed{0}, _step{0}, _report{0};
	hrt_call _watchdog{};
	px4::atomic_bool _expired{false};
	px4::atomic_bool _starting{false};
	uintptr_t _i2c{0}, _uart{0};
	int _i2c_event{0}, _i2c_error{0};
	bool _irq_event_attached{false}, _irq_error_attached{false};
	protocol_test::I2CSimulator _i2c_sim{};
	protocol_test::GpioI2CSlave _gpio_i2c{};
	uint32_t _gpio_scl{0}, _gpio_sda{0};
	bool _i2c_reading{false};
	uint32_t _slave_sequence{0};
	volatile uint32_t _slave_errors{0};
	int _socket[2] {-1, -1};
	bool _network_up[2] {};
	uint32_t _can_rx[2] {}, _can_sequence[2] {};
	bool _pending[2] {};
	hrt_abstime _pending_time[2] {};
	uint16_t _rc_frames{0};
	bool _have_rc_frames{false};
	hrt_call _io_sampler{};
	GpioTestEdges _io_edges[8] {};
	hrt_abstime _io_capture_time{0};
	uint32_t _io_timing_errors{0};
	uint16_t _io_saved_rates[3] {};
	bool _io_needs_cleanup{false}, _io_cleaning{false};
	uint16_t _io_saved_features{0}, _io_saved_sbus_rate{0};
	bool _io_sbus_cleanup{false};
	protocol_test::SbusCapture _sbus_capture{};
	protocol_test::SbusClock _sbus_clock{};
	bool _sbus_irq_attached{false};
};
