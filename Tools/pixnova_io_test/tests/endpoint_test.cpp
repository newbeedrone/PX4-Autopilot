// SPDX-License-Identifier: BSD-3-Clause
#include "endpoint_test_platform.hpp"
#include <assert.h>

// Exercise the actual endpoint state machine with a deterministic HRT clock.
#define private public
#define usleep test_usleep
#include "../../../src/systemcmds/gpio_test/gpio_test.cpp"
#undef usleep
#undef private

static const char *SESSION = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Fixture {
	Endpoint endpoint;
	int sockets[2] {};

	Fixture(bool power_check = true)
	{
		assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
		endpoint._fd = sockets[0];
		test_now = 1000;
		test_board_ok = true;
		test_bad_pin = -1;
		test_read_cost_us = 0;
		test_modes.fill(0);
		test_levels.fill(false);

		if (power_check) {
			endpoint.power_on_check();
		}
	}

	~Fixture()
	{
		endpoint.stop();
		close(sockets[0]);
		close(sockets[1]);
	}

	std::string command(const char *verb, bool with_profile = true)
	{
		char line[160] {};
		snprintf(line, sizeof(line), with_profile ? "%s %s %s" : "%s", verb, SESSION, GPIO_TEST_PROFILE_SHA);
		endpoint.command(line);
		return drain();
	}

	std::string drain()
	{
		std::string output;
		char response[4096] {};
		ssize_t count;

		while ((count = recv(sockets[1], response, sizeof(response), MSG_DONTWAIT)) > 0) {
			output.append(response, count);
		}

		return output;
	}

	void inputs_only() const
	{
		for (const uint32_t config : test_modes) {
			assert((config & GPIO_OUTPUT) == 0);
		}
	}
};

int main(int argc, char *argv[])
{
	if (argc > 1 && strcmp(argv[1], "hello") == 0) {
		Fixture f;
		const std::string output = f.command("HELLO", false);
		fwrite(output.data(), 1, output.size(), stdout);
		return 0;
	}

	if (argc > 1 && strcmp(argv[1], "stream") == 0 && RECEIVER) {
		Fixture f;
		f.command("CHECK");
		f.command("START");

		for (unsigned elapsed = GPIO_TEST_SAMPLE_US; elapsed <= 3 * GPIO_TEST_REPORT_US; elapsed += GPIO_TEST_SAMPLE_US) {
			test_now += GPIO_TEST_SAMPLE_US;

			if (elapsed % 400000 == 0) {
				f.endpoint._last_feed = test_now;
			}

			for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
				test_levels[test_pin(i, false) & 255] = (elapsed / GPIO_TEST_CHANNELS[i].half_period_us) & 1;
			}

			f.endpoint.tick();

			if (elapsed % GPIO_TEST_REPORT_US == 0) {
				f.endpoint.report_window();
				const std::string output = f.drain();
				fwrite(output.data(), 1, output.size(), stdout);
			}
		}

		return 0;
	}

	{
		Fixture f(false);
		assert(!f.endpoint._power_done);
		f.command("CHECK");
		assert(f.command("START").find("self_check_required") != std::string::npos);
		f.endpoint.power_on_check();
		assert(f.endpoint._power_done && f.endpoint._power_ok);
		f.inputs_only();
		assert(f.command("START").find("self_check_required") != std::string::npos);
	}

	{
		Fixture f(false);
		test_board_ok = false;
		f.endpoint.power_on_check();
		assert(!f.endpoint._power_ok);
		test_board_ok = true;
		f.command("CHECK");
		assert(f.command("START").find("self_check_required") != std::string::npos);
		f.inputs_only();
	}

	{
		Fixture f;
		assert(f.command("START").find("self_check_required") != std::string::npos);
		f.inputs_only();
		assert(f.command("CHECK").find("\"pass\":true") != std::string::npos);
		assert(f.command("START").find("\"type\":\"started\"") != std::string::npos);
		assert(f.endpoint._active && f.endpoint._call.active);
		assert(!f.endpoint._checked);

		for (unsigned i = 0; i < GPIO_TEST_COUNT; i++) {
			assert(static_cast<bool>(test_modes[test_pin(i, false) & 255] & GPIO_OUTPUT) == !RECEIVER);
		}

		assert(f.command("STOP", false).find(SESSION) != std::string::npos);
		f.inputs_only();
		assert(!f.endpoint._active && !f.endpoint._call.active);
	}

	{
		Fixture f;
		test_board_ok = false;
		assert(f.command("CHECK").find("\"pass\":false") != std::string::npos);
		assert(f.command("START").find("self_check_required") != std::string::npos);
		f.inputs_only();
	}

	{
		Fixture f;
		f.command("CHECK");
		test_bad_pin = test_pin(0, false) & 255;
		assert(f.command("START").find("gpio_configuration_failed") != std::string::npos);
		assert(!f.endpoint._active);
		f.inputs_only();
	}

	{
		Fixture f;
		f.command("CHECK");
		f.command("START");
		char ping[64] {};
		snprintf(ping, sizeof(ping), "PING %s", SESSION);
		test_now += GPIO_TEST_LEASE_US / 2;
		assert(f.command(ping, false).find("heartbeat") != std::string::npos);
		assert(f.endpoint._active && f.endpoint._last_feed == test_now);
		// Expiry also applies before the IRQ gets a chance to mark the lease.
		test_now += GPIO_TEST_LEASE_US + 1;
		assert(f.command(ping, false).find("heartbeat_timeout") != std::string::npos);
		assert(!f.endpoint._active);
		f.inputs_only();
	}

	{
		Fixture f;
		f.command("CHECK");
		test_now += CHECK_VALID_US + 1;
		assert(f.command("START").find("self_check_required") != std::string::npos);
		f.inputs_only();
	}

	{
		Fixture f;
		f.command("CHECK");
		f.endpoint._io_ok = false;
		f.command("START");
		assert(!f.endpoint._active);
		f.inputs_only();
	}

	if (RECEIVER) {
		Fixture f;
		f.command("CHECK");
		f.command("START");
		// 47 reads taking 2 us each exceed the 50 us sampling period, even
		// though the callback gap has not yet reached the late-tick threshold.
		test_read_cost_us = 2;
		f.endpoint.tick();
		assert(f.endpoint._late_ticks == 1);
		f.endpoint.report_window();
		assert(f.drain().find("\"timing_errors\":1") != std::string::npos);
	}

	{
		Fixture f;
		f.command("CHECK");
		f.command("START");
		test_now += GPIO_TEST_LEASE_US + 1;
		f.endpoint.tick();
		f.inputs_only();
		assert(f.endpoint._lease_expired.load());
		char ping[64] {};
		snprintf(ping, sizeof(ping), "PING %s", SESSION);
		f.endpoint.command(ping);
		assert(!f.endpoint._active);
		f.inputs_only();
	}

	printf("%s endpoint startup, self-check, STOP, USB-error and expired-lease checks passed\n", ROLE);
	return 0;
}
