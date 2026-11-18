// SPDX-License-Identifier: BSD-3-Clause
#include "sbus_capture.hpp"
#include <cassert>
#include <cmath>
#include <cstring>
#include <vector>
using hrt_abstime = uint64_t;
static hrt_abstime encoder_time = 21000, last_txframe_time = 0;
static unsigned sbus1_frame_delay = 20000;
constexpr unsigned SBUS_FRAME_SIZE = 25;
static std::vector<uint8_t> encoded;
static hrt_abstime hrt_absolute_time() { return encoder_time; }
static int capture_write(int, const void *buffer, unsigned size)
{
	const auto *bytes = static_cast<const uint8_t *>(buffer);
	encoded.assign(bytes, bytes + size);
	return size;
}
#define write capture_write
// Actual PX4 sbus1_output, including its scaling definitions, unchanged.
#include "sbus_encoder_under_test.inc"
#undef write

static void waveform(protocol_test::SbusCapture &capture, const std::vector<uint8_t> &data, uint64_t start,
		     bool &level, unsigned bit_us = 10, bool inverted = true, int parity_fault = -1,
		     bool stop_fault = false, bool jitter = false, unsigned jitter_us = 2, unsigned edge_seed = 0)
{
	uint64_t now = start;
	unsigned edges = edge_seed;

	for (unsigned byte = 0; byte < data.size(); ++byte) {
		unsigned parity = 0;

		for (unsigned bit = 0; bit < 12; ++bit) {
			bool logical = bit >= 10;

			if (bit >= 1 && bit <= 8) { logical = (data[byte] >> (bit - 1)) & 1; parity ^= logical; }

			if (bit == 9) { logical = parity ^ (int(byte) == parity_fault); }

			if (stop_fault && byte == 5 && bit == 10) { logical = false; }

			const bool wire = inverted ? !logical : logical;

			if (level != wire) { capture.edge(now + (jitter ? (++edges % 3) * jitter_us / 2 : 0), wire); level = wire; }

			now += bit_us;
		}
	}

	capture.flush(now + 1000, level);
}

int main()
{
	{
		protocol_test::SbusClock clock;
		clock.reset(64000, 64000);
		assert(clock.advance(65000) == 65000);
		assert(clock.advance(464) == 66000);

		for (uint64_t t = 67000; t < 2067000; t += 1000) { assert(clock.advance(t & 0xffff) == t); }
	}

	uint16_t zeros[8] {};
	sbus1_output(-1, zeros, 8);
	assert(encoded.size() == 25 && encoded[0] == 0x0f);

	for (unsigned i = 1; i < 25; ++i) { assert(encoded[i] == (i <= 11 ? 0xff : 0)); }

	for (unsigned scenario = 0; scenario < 6; ++scenario) {
		// Interrupt delivery can vary by 4 us while the physical bit time is
		// exactly 10 us. Edge-to-edge rounding must not turn that bounded
		// latency into a corrupt frame or allow cumulative baud drift.
		auto frame = scenario < 3 ? encoded : std::vector<uint8_t>(25, 0);
		frame[0] = 0x0f;
		protocol_test::SbusCapture receiver;
		bool level = false;
		receiver.reset(0, level);

		for (unsigned i = 0; i < 100; ++i) { waveform(receiver, frame, 2000 + 20000 * i, level, 10, true, -1, false, true, 4, scenario % 3); }

		assert(receiver.statistics().frames == 100 && receiver.statistics().errors == 0);
	}

	{
		// Installed IO firmware reports disabled outputs as raw zero.
		std::vector<uint8_t> disabled(25, 0);
		disabled[0] = 0x0f;
		protocol_test::SbusCapture receiver;
		bool level = false;
		receiver.reset(0, level);

		for (unsigned i = 0; i < 48; ++i) { waveform(receiver, disabled, 2000 + 21000 * i, level, 10, true, -1, false, true); }

		assert(receiver.statistics().frames == 48 && receiver.statistics().errors == 0);
		// A hybrid of the two encodings or arbitrary channel data is rejected.
		disabled[1] = 0xff;
		waveform(receiver, disabled, 2000 + 21000 * 48, level);
		assert(receiver.statistics().frames == 48 && receiver.statistics().errors == 1);
	}

	{
		protocol_test::SbusCapture receiver;
		bool level = false;
		receiver.reset(0, level);

		for (unsigned i = 0; i < 48; ++i) { waveform(receiver, encoded, 2000 + 21000 * i, level, 10, true, -1, false, true); }

		auto stats = receiver.statistics();
		assert(stats.frames == 48 && stats.errors == 0 && stats.window_frames == 48);
		assert(stats.frequency_millihz >= 47618 && stats.frequency_millihz <= 47620);
		receiver.reset_window();
		assert(receiver.statistics().window_frames == 0 && receiver.statistics().frequency_millihz == 0);

		for (unsigned i = 48; i < 96; ++i) { waveform(receiver, encoded, 2000 + 21000 * i, level); }

		assert(receiver.statistics().frames == 96 && receiver.statistics().window_frames == 48);
	}

	for (unsigned fault = 0; fault < 7; ++fault) {
		protocol_test::SbusCapture receiver;
		bool level = fault == 5;
		receiver.reset(0, level);
		auto frame = encoded;

		if (fault == 0) { frame[4] ^= 1; } // Different data, even with valid UART parity.

		if (fault == 1) { frame[23] = 8; } // Failsafe flag.

		if (fault == 2) { frame.resize(24); }

		if (fault == 3) { frame.push_back(0); }

		for (unsigned i = 0; i < 5; ++i) {
			waveform(receiver, frame, 2000 + 21000 * i, level, fault == 6 ? 12 : 10,
				 fault != 5, fault == 4 ? 5 : -1);
		}

		assert(receiver.statistics().frames == 0);
	}

	{
		protocol_test::SbusCapture receiver;
		bool level = false;
		receiver.reset(0, false);
		waveform(receiver, encoded, 2000, level, 10, true, -1, true);
		assert(receiver.statistics().frames == 0 && receiver.statistics().errors == 1);
		waveform(receiver, encoded, 23000, level);
		assert(receiver.statistics().frames == 1); // Resynchronize after a bad frame.
	}

	{
		protocol_test::SbusCapture receiver;
		receiver.reset(0, false);
		receiver.flush(1000000, false);
		assert(receiver.statistics().frames == 0); // Disconnected/stuck input cannot pass.
		receiver.edge(1000010, true);
		receiver.flush(1100000, true);
		assert(receiver.statistics().frames == 0 && receiver.statistics().errors == 1);
	}
}
