// SPDX-License-Identifier: BSD-3-Clause
// Manufacturing test endpoint. Never included in a flight configuration.

#include <px4_platform_common/px4_config.h>
#include <px4_platform_common/log.h>
#include <px4_platform_common/tasks.h>
#include <px4_platform_common/atomic.h>
#include <px4_platform_common/board_common.h>
#include <px4_platform/gpio.h>
#include <drivers/drv_hrt.h>
#include <lib/version/version.h>
#include <board_config.h>
#include <nuttx/irq.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

#include "gpio_test_profile.h"
#include "board_self_check.hpp"
#include "edge_counter.hpp"
#include "gpio_test.hpp"
#ifdef CONFIG_GPIO_TEST_PROTOCOLS
#include "protocol_test.hpp"
#endif

#if defined(CONFIG_MODULES_COMMANDER) || defined(CONFIG_DRIVERS_PWM_OUT) || defined(CONFIG_DRIVERS_PX4IO)
# error "gpio_test requires a dedicated configuration without flight/actuator applications"
#endif

namespace
{
px4::atomic_bool task_running{false};
px4::atomic_bool task_exit{false};
char device_path[64] = "/dev/ttyACM0";
constexpr hrt_abstime CHECK_VALID_US = 30000000;
constexpr unsigned LINE_LENGTH = 160;

#ifdef CONFIG_GPIO_TEST_RECEIVER
constexpr bool RECEIVER = true;
constexpr const char *ROLE = "probe";
#else
constexpr bool RECEIVER = false;
constexpr const char *ROLE = "source";
#endif

uint32_t test_pin(unsigned index, bool output)
{
	const auto &channel = GPIO_TEST_CHANNELS[index];
	output = output && channel.waveform;
	const uint32_t port = RECEIVER ? channel.rx_port : channel.tx_port;
	const uint32_t number = RECEIVER ? channel.rx_pin : channel.tx_pin;
	const uint32_t pull = !RECEIVER || channel.rx_pull == GpioTestPull::None ? GPIO_FLOAT :
			      channel.rx_pull == GpioTestPull::Up ? GPIO_PULLUP : GPIO_PULLDOWN;
	return (port << GPIO_PORT_SHIFT) | (number << GPIO_PIN_SHIFT)
	       | (output ? (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz | GPIO_OUTPUT_CLEAR) : (GPIO_INPUT | pull));
}

class Endpoint
{
public:
	~Endpoint() { stop(); }
	void run();

private:
	uint32_t pin(unsigned index, bool output) const;
	void stop();
	bool configure(bool output);
	void tick();
	void command(char *line);
	void report_window();
	void power_on_check();
	void send_power_on();
	void check_interfaces(const char *session, const char *mode = nullptr);
	bool send(const char *format, ...) __attribute__((format(printf, 2, 3)));
	static void callback(void *context) { static_cast<Endpoint *>(context)->tick(); }
	static void check_report(void *context, const char *name, bool passed, int detail);
	static void power_report(void *context, const char *name, bool passed, int detail);
	struct PowerResult {
		char name[40] {};
		int detail{0};
		bool passed{false};
	};
	static constexpr unsigned POWER_RESULT_CAPACITY = 12;
	PowerResult _power_results[POWER_RESULT_CAPACITY] {};
	unsigned _power_count{0};
	bool _power_done{false};
	bool _power_ok{false};
	bool _power_invalid{false};
	hrt_abstime _power_time{0};

	int _fd{-1};
	hrt_call _call{};
	GpioTestEdges _edges[GPIO_TEST_COUNT] {};
	bool _levels[GPIO_TEST_COUNT] {};
	bool _active{false};
	bool _checked{false};
	bool _io_ok{true};
	px4::atomic_bool _lease_expired{false};
	char _session[33] {};
	hrt_abstime _epoch{0};
	hrt_abstime _last_feed{0};
	hrt_abstime _last_report{0};
	hrt_abstime _check_time{0};
	hrt_abstime _last_tick{0};
	uint32_t _late_ticks{0};
	uint32_t _sequence{0};
#ifdef CONFIG_GPIO_TEST_PROTOCOLS
	ProtocolTest _protocol {};
	bool _protocol_checked{false};
	protocol_test::Mode _checked_mode{protocol_test::Mode::None};
#endif
};

uint32_t Endpoint::pin(unsigned index, bool output) const
{
	return test_pin(index, output);
}

bool Endpoint::configure(bool output)
{
	bool ok = true;

	for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
		ok = (px4_arch_configgpio(pin(i, output)) == 0) && ok;
	}

	return ok;
}

void Endpoint::stop()
{
#ifdef CONFIG_GPIO_TEST_PROTOCOLS
	_protocol.stop();
	_protocol_checked = false;
	_checked_mode = protocol_test::Mode::None;
#endif
	hrt_cancel(&_call);
	_active = false;
	_checked = false;
	configure(false);
}

bool Endpoint::send(const char *format, ...)
{
	char buffer[384];
	va_list args;
	va_start(args, format);
	const int size = vsnprintf(buffer, sizeof(buffer), format, args);
	va_end(args);

	if (size < 0 || static_cast<unsigned>(size) >= sizeof(buffer) || _fd < 0) {
		_io_ok = false;
		return false;
	}

	unsigned offset = 0;
	const hrt_abstime deadline = hrt_absolute_time() + 200000;

	while (offset < static_cast<unsigned>(size)) {
		const ssize_t written = write(_fd, buffer + offset, size - offset);

		if (written > 0) {
			offset += written;

		} else if ((written < 0 && errno != EAGAIN && errno != EINTR) || hrt_absolute_time() >= deadline) {
			_io_ok = false;
			return false;

		} else {
			pollfd pending{_fd, POLLOUT, 0};
			poll(&pending, 1, 10);
		}
	}

	return true;
}

void Endpoint::tick()
{
	const hrt_abstime now = hrt_absolute_time();

	if (_lease_expired.load()) {
		return;
	}

	if (now - _last_feed > GPIO_TEST_LEASE_US) {
		// Enforce the lease in the IRQ as well, even if the USB task stops running.
		if (!RECEIVER) {
			configure(false);
		}

		_lease_expired.store(true);
		return;
	}

	const uint32_t interval = RECEIVER ? GPIO_TEST_SAMPLE_US : GPIO_TEST_WAVE_US;

	if (_last_tick != 0 && now - _last_tick > interval * 3) {
		_late_ticks++;
	}

	_last_tick = now;

	for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
		if (!GPIO_TEST_CHANNELS[i].waveform) { continue; }

		if (RECEIVER) {
			_edges[i].sample(now, px4_arch_gpioread(pin(i, false)));

		} else {
			const bool high = ((now - _epoch) / GPIO_TEST_CHANNELS[i].half_period_us) & 1;

			if (high != _levels[i]) {
				px4_arch_gpiowrite(pin(i, true), high);
				_levels[i] = high;
			}
		}
	}

	if (hrt_absolute_time() - now >= interval) {
		// A callback that consumes its entire period cannot sustain the sampling
		// rate. Report it even if the next callback starts before the gap limit.
		_late_ticks++;
	}
}

void Endpoint::check_report(void *context, const char *name, bool passed, int detail)
{
	auto *self = static_cast<Endpoint *>(context);
	self->send("{\"type\":\"check\",\"session\":\"%s\",\"test\":\"%s\",\"pass\":%s,\"detail\":%d}\n",
		   self->_session, name, passed ? "true" : "false", detail);
}

void Endpoint::power_report(void *context, const char *name, bool passed, int detail)
{
	auto *self = static_cast<Endpoint *>(context);

	if (!name || strlen(name) >= sizeof(PowerResult::name) || self->_power_count >= POWER_RESULT_CAPACITY) {
		self->_power_invalid = true;
		return;
	}

	for (const char *c = name; *c; c++) {
		if (!isalnum(static_cast<unsigned char>(*c)) && *c != '_') {
			self->_power_invalid = true;
			return;
		}
	}

	auto &result = self->_power_results[self->_power_count++];
	strcpy(result.name, name);
	result.detail = detail;
	result.passed = passed;
	self->_power_invalid = self->_power_invalid || !passed;
}

void Endpoint::power_on_check()
{
	stop();
	_power_count = 0;
	_power_invalid = false;
	const bool ok = board_gpio_test_self_check(power_report, this, false);
	_power_ok = ok && !_power_invalid && _power_count > 0;
	_power_time = hrt_absolute_time();
	_power_done = true;
}

void Endpoint::send_power_on()
{
	for (unsigned i = 0; i < _power_count && _io_ok; i++) {
		const auto &result = _power_results[i];
		send("{\"type\":\"power_check\",\"test\":\"%s\",\"pass\":%s,\"detail\":%d}\n",
		     result.name, result.passed ? "true" : "false", result.detail);
	}

	send("{\"type\":\"power_checked\",\"pass\":%s,\"count\":%u,\"checked_us\":%llu,\"uptime_us\":%llu}\n",
	     _power_done && _power_ok ? "true" : "false", _power_count,
	     static_cast<unsigned long long>(_power_time), static_cast<unsigned long long>(hrt_absolute_time()));
}

void Endpoint::command(char *line)
{
	if (!_io_ok) {
		stop();
		return;
	}

	char verb[16] {}, session[33] {}, profile[65] {}, extra{};
	const int fields = sscanf(line, "%15s %32s %64s %c", verb, session, profile, &extra);

#ifdef CONFIG_GPIO_TEST_PROTOCOLS

	if (strcmp(verb, "PCHECK") == 0) {
		char mode[16] {};
		const int count = sscanf(line, "%15s %32s %64s %15s %c", verb, session, profile, mode, &extra);
		bool valid = count == 4 && strlen(session) == 32 && strcmp(profile, GPIO_TEST_PROFILE_SHA) == 0
			     && protocol_test::mode(mode) != protocol_test::Mode::None;

		for (const char *p = session; *p; ++p) { valid = valid && isxdigit(static_cast<unsigned char>(*p)); }

		if (!valid) {
			stop();
			send("{\"type\":\"error\",\"reason\":\"invalid_protocol_check\"}\n");
			return;
		}

		check_interfaces(session, mode);
		return;
	}

	if (strcmp(verb, "PROTO") == 0) {
		char mode[16] {}, token_text[9] {};
		const int count = sscanf(line, "%15s %32s %64s %15s %8s %c", verb, session, profile, mode, token_text, &extra);
		bool valid = count == 5 && strlen(token_text) == 8;

		for (const char *p = token_text; *p; ++p) { valid = valid && isxdigit(static_cast<unsigned char>(*p)); }

		if (!valid || strcmp(profile, GPIO_TEST_PROFILE_SHA) != 0 || strcmp(session, _session) != 0
		    || !_power_done || (RECEIVER && !_power_ok) || !_protocol_checked || _checked_mode != protocol_test::mode(mode) || _active
		    || _protocol.active()
		    || hrt_absolute_time() - _check_time > CHECK_VALID_US) {
			stop();
			send("{\"type\":\"error\",\"reason\":\"protocol_self_check_required\"}\n");
			return;
		}

		const auto selected = protocol_test::mode(mode);
		const int result = _protocol.start(selected, strtoul(token_text, nullptr, 16));
		_checked = false;
		_protocol_checked = false;

		if (result < 0) {
			stop();
			send("{\"type\":\"error\",\"reason\":\"protocol_setup_failed\",\"detail\":%d}\n", result);

		} else {
			send("{\"type\":\"started\",\"session\":\"%s\",\"mode\":\"%s\",\"role\":\"%s\"}\n", _session, mode, ROLE);
		}

		return;
	}

	if (fields == 2 && strcmp(verb, "PING") == 0 && _protocol.active() && strcmp(session, _session) == 0) {
		if (!_protocol.feed()) {
			stop();
			send("{\"type\":\"stopped\",\"session\":\"%s\",\"reason\":\"heartbeat_timeout\"}\n", _session);

		} else {
			send("{\"type\":\"heartbeat\",\"session\":\"%s\",\"running\":true}\n", _session);
		}

		return;
	}

#endif

	if (fields == 1 && strcmp(verb, "HELLO") == 0) {
		stop();
		char guid[PX4_GUID_FORMAT_SIZE] {};
		board_get_px4_guid_formated(guid, sizeof(guid));
		send("{\"type\":\"hello\",\"role\":\"%s\",\"protocol\":%u,\"profile\":\"%s\",\"channels\":%u,\"device_id\":\"%s\",\"firmware\":\"%016llx\""
#ifdef CONFIG_GPIO_TEST_PROTOCOLS
		     ",\"protocol_tests\":1,\"i2c_simulators\":2,\"adc_levels\":1"
#endif
		     ",\"independent_external_checks\":1}\n",
		     ROLE, GPIO_TEST_PROTOCOL, GPIO_TEST_PROFILE_SHA, GPIO_TEST_COUNT, guid,
		     static_cast<unsigned long long>(px4_firmware_version_binary()));
		send_power_on();
		return;
	}

	if (fields == 1 && strcmp(verb, "STOP") == 0) {
		stop();
		send("{\"type\":\"stopped\",\"session\":\"%s\",\"reason\":\"requested\"}\n", _session);
		return;
	}

	if (fields == 2 && strcmp(verb, "PING") == 0 && _active && strcmp(session, _session) == 0) {
		const irqstate_t state = enter_critical_section();
		const hrt_abstime now = hrt_absolute_time();
		const bool valid_lease = !_lease_expired.load() && now - _last_feed <= GPIO_TEST_LEASE_US;

		if (valid_lease) {
			_last_feed = now;
		}

		leave_critical_section(state);

		if (!valid_lease) {
			stop();
			send("{\"type\":\"stopped\",\"session\":\"%s\",\"reason\":\"heartbeat_timeout\"}\n", _session);
			return;
		}

		send("{\"type\":\"heartbeat\",\"session\":\"%s\",\"running\":true}\n", _session);
		return;
	}

	bool valid_session = strlen(session) == 32;

	for (const char *c = session; *c; c++) {
		valid_session = valid_session && isxdigit(static_cast<unsigned char>(*c));
	}

	if (fields != 3 || !valid_session || strcmp(profile, GPIO_TEST_PROFILE_SHA) != 0) {
		stop();
		send("{\"type\":\"error\",\"reason\":\"invalid_command_or_profile\"}\n");
		return;
	}

	if (strcmp(verb, "CHECK") == 0) {
		check_interfaces(session);
		return;
	}

	if (strcmp(verb, "START") != 0 || !_power_done || (RECEIVER && !_power_ok) || !_checked || _active || strcmp(session, _session) != 0
	    || hrt_absolute_time() - _check_time > CHECK_VALID_US) {
		stop();
		send("{\"type\":\"error\",\"reason\":\"self_check_required\"}\n");
		return;
	}

	if (!configure(!RECEIVER)) {
		stop();
		send("{\"type\":\"error\",\"reason\":\"gpio_configuration_failed\"}\n");
		return;
	}

	for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
		_edges[i] = {};
		_levels[i] = false;
	}

	_sequence = 0;
	_late_ticks = 0;
	_last_tick = 0;
	_epoch = _last_report = _last_feed = hrt_absolute_time();
	_active = true;
	_checked = false;
	_lease_expired.store(false);
	const uint32_t interval = RECEIVER ? GPIO_TEST_SAMPLE_US : GPIO_TEST_WAVE_US;
	hrt_call_every(&_call, interval, interval, callback, this);
	// Also enables the IRQ timestamp check in the host's startup sequence.
	send("{\"type\":\"started\",\"session\":\"%s\",\"role\":\"%s\"}\n", _session, ROLE);
}

void Endpoint::check_interfaces(const char *session, const char *mode)
{
	stop();
	strcpy(_session, session);
	const hrt_abstime before = hrt_absolute_time();
	usleep(5000);
	const hrt_abstime elapsed = hrt_absolute_time() - before;
	const bool clock_ok = elapsed >= 4000 && elapsed < 100000;
	check_report(this, "hrt_clock", clock_ok, static_cast<int>(elapsed));
	const bool pins_ok = configure(false);
	check_report(this, "gpio_inputs", pins_ok, GPIO_TEST_COUNT);
	const bool onboard_ok = board_gpio_test_self_check(check_report, this, false);
	const bool external_ok = board_gpio_test_external_check(check_report, this, mode == nullptr);
	const bool ready = clock_ok && pins_ok && external_ok && (!RECEIVER || onboard_ok) && _io_ok;
	_check_time = hrt_absolute_time();
	_checked = mode == nullptr && ready;
#ifdef CONFIG_GPIO_TEST_PROTOCOLS
	_protocol_checked = mode != nullptr && ready;
	_checked_mode = mode ? protocol_test::mode(mode) : protocol_test::Mode::None;
#endif
	send("{\"type\":\"checked\",\"session\":\"%s\",\"mode\":\"%s\",\"pass\":%s,\"onboard_pass\":%s}\n",
	     _session, mode ? mode : "GPIO", ready ? "true" : "false", onboard_ok ? "true" : "false");
}

void Endpoint::report_window()
{
	GpioTestEdges snapshot[GPIO_TEST_COUNT];
	const irqstate_t state = enter_critical_section();

	for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
		snapshot[i] = _edges[i];
		const bool previous = _edges[i].previous;
		_edges[i] = {};
		_edges[i].previous = previous;
		_edges[i].initialized = true;
	}

	const uint32_t late = _late_ticks;
	_late_ticks = 0;
	leave_critical_section(state);
	_sequence++;

	for (unsigned i = 0; i < GPIO_TEST_COUNT && _io_ok; i++) {
		if (!GPIO_TEST_CHANNELS[i].waveform) { continue; }

		const auto &edge = snapshot[i];
		send("{\"type\":\"sample\",\"session\":\"%s\",\"seq\":%lu,\"channel\":%u,\"frequency_millihz\":%lu,\"edges\":%lu,\"span_us\":%llu,\"timing_errors\":%lu}\n",
		     _session, static_cast<unsigned long>(_sequence), i,
		     static_cast<unsigned long>(edge.frequency_millihz()), static_cast<unsigned long>(edge.edges),
		     static_cast<unsigned long long>(edge.last_us - edge.first_us), static_cast<unsigned long>(late));
	}
}

void Endpoint::run()
{
	configure(false);
	// Cache peripheral identity checks before a host connects. This never authorises START.
	power_on_check();
	char line[LINE_LENGTH] {};
	unsigned used = 0;

	while (!task_exit.load()) {
		if (_fd < 0) {
			_fd = open(device_path, O_RDWR | O_NONBLOCK | O_NOCTTY);

			if (_fd < 0) {
				usleep(100000);
				continue;
			}

			termios settings{};

			if (tcgetattr(_fd, &settings) == 0) {
				cfmakeraw(&settings);
				tcsetattr(_fd, TCSANOW, &settings);
			}

			_io_ok = true;
			used = 0;
		}

		pollfd incoming{_fd, POLLIN, 0};
		const int ready = poll(&incoming, 1, 20);

		if ((ready < 0 && errno != EINTR) || (incoming.revents & (POLLERR | POLLHUP | POLLNVAL))) {
			_io_ok = false;
		}

		if (_io_ok && (incoming.revents & POLLIN)) {
			char bytes[128];
			const ssize_t count = read(_fd, bytes, sizeof(bytes));

			if (count <= 0 && (count == 0 || (errno != EAGAIN && errno != EINTR))) {
				_io_ok = false;
			}

			for (ssize_t i = 0; i < count; i++) {
				if (bytes[i] == '\n') {
					line[used] = '\0';
					command(line);
					used = 0;

				} else if (bytes[i] != '\r') {
					if (used + 1 >= sizeof(line)) {
						stop();
						_io_ok = false;
						break;
					}

					line[used++] = bytes[i];
				}
			}
		}

		const hrt_abstime now = hrt_absolute_time();

#ifdef CONFIG_GPIO_TEST_PROTOCOLS

		if (_protocol.active()) {
			if (_protocol.expired()) {
				stop();
				send("{\"type\":\"stopped\",\"session\":\"%s\",\"reason\":\"heartbeat_timeout\"}\n", _session);

			} else {
				ProtocolSample sample{};

				if (_protocol.update(sample)) {
					send("{\"type\":\"protocol_sample\",\"session\":\"%s\",\"mode\":\"%s\",\"seq\":%lu,\"tx\":%lu,\"rx\":%lu,\"errors\":%lu,\"detail\":%d,\"pass\":%s,\"channels\":[%u,%u,%u,%u,%u,%u,%u]}\n",
					     _session, protocol_test::name(_protocol.mode()), static_cast<unsigned long>(sample.sequence),
					     static_cast<unsigned long>(sample.tx), static_cast<unsigned long>(sample.rx),
					     static_cast<unsigned long>(sample.errors), sample.detail, sample.passed ? "true" : "false",
					     sample.channels[0], sample.channels[1], sample.channels[2], sample.channels[3],
					     sample.channels[4], sample.channels[5], sample.channels[6]);
				}
			}
		}

#endif

		if (_active && (_lease_expired.load() || now - _last_feed > GPIO_TEST_LEASE_US)) {
			stop();
			send("{\"type\":\"stopped\",\"session\":\"%s\",\"reason\":\"heartbeat_timeout\"}\n", _session);
		}

		if (_active && RECEIVER && now - _last_report >= GPIO_TEST_REPORT_US) {
			_last_report = now;
			report_window();
		}

		if (!_io_ok) {
			stop();
			close(_fd);
			_fd = -1;
		}
	}

	stop();

	if (_fd >= 0) {
		close(_fd);
	}
}

int endpoint_task(int, char *[])
{
	auto *endpoint = new Endpoint;

	if (!endpoint) {
		task_running.store(false);
		return -ENOMEM;
	}

	endpoint->run();
	delete endpoint;
	task_running.store(false);
	return 0;
}
}

bool gpio_test_contains_pin(uint32_t config)
{
	constexpr uint32_t mask = GPIO_PORT_MASK | GPIO_PIN_MASK;

	for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
		if ((test_pin(i, false) & mask) == (config & mask)) {
			return true;
		}
	}

	return false;
}

bool gpio_test_initialize_inputs()
{
	bool ok = true;

	for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
		ok = (px4_arch_configgpio(test_pin(i, false)) == 0) && ok;
	}

	return ok;
}

extern "C" __EXPORT int gpio_test_main(int argc, char *argv[])
{
	if (argc >= 2 && strcmp(argv[1], "start") == 0) {
		bool expected = false;

		if (!task_running.compare_exchange(&expected, true)) {
			PX4_ERR("already running");
			return -1;
		}

		if (argc == 4 && strcmp(argv[2], "-d") == 0 && strlen(argv[3]) < sizeof(device_path)) {
			strcpy(device_path, argv[3]);

		} else if (argc != 2) {
			task_running.store(false);
			return -1;
		}

		task_exit.store(false);
		const int task = px4_task_spawn_cmd("gpio_test", SCHED_DEFAULT, SCHED_PRIORITY_DEFAULT, 6000, endpoint_task, nullptr);

		if (task < 0) {
			task_running.store(false);
			return -1;
		}

		return 0;
	}

	if (argc == 2 && strcmp(argv[1], "stop") == 0) {
		task_exit.store(true);
		return 0;
	}

	if (argc == 2 && strcmp(argv[1], "status") == 0) {
		PX4_INFO("%s: %s", ROLE, task_running.load() ? "running" : "stopped");
		return 0;
	}

	PX4_INFO("gpio_test {start [-d /dev/ttyACM0]|stop|status}");
	return -1;
}
