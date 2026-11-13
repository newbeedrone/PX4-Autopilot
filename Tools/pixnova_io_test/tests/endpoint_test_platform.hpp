// SPDX-License-Identifier: BSD-3-Clause
#pragma once

// Host substitutes for GPIO, HRT and PX4 services. The endpoint implementation
// and its wire formatting are compiled unchanged by endpoint_test.cpp.
#include <array>
#include <atomic>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <poll.h>
#include <termios.h>
#include <unistd.h>
#include <sys/socket.h>
#include <string>
#include "board_self_check.hpp"

#define __EXPORT
#define PX4_INFO(...) ((void)0)
#define PX4_ERR(...) ((void)0)
#define PX4_GUID_FORMAT_SIZE 40
#define SCHED_DEFAULT 0
#define SCHED_PRIORITY_DEFAULT 0
#define GPIO_PORT_SHIFT 4
#define GPIO_PIN_SHIFT 0
#define GPIO_PORT_MASK 0xf0u
#define GPIO_PIN_MASK 0x0fu
#define GPIO_OUTPUT 0x10000u
#define GPIO_INPUT 0u
#define GPIO_PUSHPULL 0u
#define GPIO_FLOAT 0u
#define GPIO_PULLUP 0x200u
#define GPIO_PULLDOWN 0x400u
#define GPIO_SPEED_2MHz 0x100u
#define GPIO_OUTPUT_CLEAR 0u

using hrt_abstime = uint64_t;
using irqstate_t = unsigned;
static hrt_abstime test_now = 1000;
static bool test_board_ok = true;
static bool test_external_ok = true;
static int test_bad_pin = -1;
static unsigned test_read_cost_us = 0;
static std::array<uint32_t, 256> test_modes{};
static std::array<bool, 256> test_levels{};

namespace px4
{
class atomic_bool
{
public:
	explicit atomic_bool(bool value) : _value(value) {}
	bool load() const { return _value.load(); }
	void store(bool value) { _value.store(value); }
	bool compare_exchange(bool *expected, bool desired) { return _value.compare_exchange_strong(*expected, desired); }
private:
	std::atomic<bool> _value;
};
}

struct hrt_call {
	bool active{false};
};
inline hrt_abstime hrt_absolute_time() { return test_now; }
inline void hrt_cancel(hrt_call *call) { call->active = false; }
inline void hrt_call_every(hrt_call *call, uint32_t, uint32_t, void (*)(void *), void *) { call->active = true; }
inline irqstate_t enter_critical_section() { return 0; }
inline void leave_critical_section(irqstate_t) {}
inline int px4_arch_configgpio(uint32_t config)
{
	const unsigned pin = config & (GPIO_PORT_MASK | GPIO_PIN_MASK);
	test_modes[pin] = config;
	return static_cast<int>(pin) == test_bad_pin ? -1 : 0;
}
inline void px4_arch_gpiowrite(uint32_t config, bool high) { test_levels[config & 255] = high; }
inline bool px4_arch_gpioread(uint32_t config)
{
	test_now += test_read_cost_us;
	return test_levels[config & 255];
}
inline void board_get_px4_guid_formated(char *guid, unsigned size) { snprintf(guid, size, "%032x", 1); }
inline uint64_t px4_firmware_version_binary() { return 1; }
inline int px4_task_spawn_cmd(const char *, int, int, int, int (*)(int, char **), void *) { return 1; }
inline bool board_gpio_test_self_check(GpioTestReport report, void *context, bool)
{
#if defined(CONFIG_GPIO_TEST_RECEIVER)
	report(context, "probe_input_only_firmware", test_board_ok, test_board_ok ? 1 : 0);
#else
	const char *names[] = {"bmm150_i2c4", "bmp388_i2c4", "icp20100_i2c2", "icm45686_spi1", "icm45686_spi2", "icm45686_spi3", "eeprom_base_i2c3", "eeprom_imu_i2c4", "fram_spi5", "sdcard"};
	const int ids[] = {0x32, 0x50, 0x63, 0xE9, 0xE9, 0xE9, 32, 32, 32768, 16384};

	for (unsigned i = 0; i < 10; i++) {
		report(context, names[i], test_board_ok, test_board_ok ? ids[i] : 0);
	}

#endif
	return test_board_ok;
}
inline int test_usleep(useconds_t duration) { test_now += duration; return 0; }
inline bool board_gpio_test_external_check(GpioTestReport report, void *context, bool gpio_waveform)
{
#if !defined(CONFIG_GPIO_TEST_RECEIVER)

	if (gpio_waveform) {
		report(context, "i2c1_no_attached_devices", test_external_ok, test_external_ok ? 0 : 1);
		return test_external_ok;
	}

#else
	(void)report;
	(void)context;
	(void)gpio_waveform;
#endif
	return true;
}
