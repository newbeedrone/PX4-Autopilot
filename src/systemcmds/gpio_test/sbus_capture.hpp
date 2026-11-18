// SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include <stdint.h>

namespace protocol_test
{
constexpr unsigned SBUS_OUT_RATE = 50;
constexpr unsigned SBUS_OUT_MIN_MILLIHZ = 40000;
constexpr unsigned SBUS_OUT_MAX_MILLIHZ = 55000;

// Extend the existing 1 MHz HRT timer without entering the RTOS from the
// fast interrupt. A 1 ms watchdog refresh also covers quiet input periods.
class SbusClock
{
public:
	void reset(uint64_t now, uint16_t counter) { _now = now; _counter = counter; }
	uint64_t advance(uint16_t counter)
	{
		_now += static_cast<uint16_t>(counter - _counter);
		_counter = counter;
		return _now;
	}

private:
	uint64_t _now{0};
	uint16_t _counter{0};
};

// PG5 has no UART receive route. Decode inverted 100 kbaud 8E2 from EXTI
// timestamps, never from the 50 us PWM sampler. Called under the IRQ lock.
class SbusCapture
{
public:

	struct Statistics {
		uint32_t frames{0}, errors{0}, window_frames{0}, frequency_millihz{0};
		uint64_t last_frame_us{0};
		int32_t diagnostic[5] {}; // Code, byte, bit, edge interval, phase error.
	};

	void reset(uint64_t now, bool high)
	{
		*this = {};
		_edge_us = now;
		_high = high;
	}

	void edge(uint64_t now, bool high)
	{
		const uint64_t elapsed = now - _edge_us;

		if (elapsed >= GAP_US) {
			finish_gap();

			if (!_high && high) {
				_active = true;
				_start_us = _phase_us = now;
				_bytes = _bit = _byte = _parity = 0;
				_bad = false;
			}

		} else if (_active && !_bad) {
			// Quantize against the byte's bit clock, not the previous IRQ's
			// delivery time. Bounded IRQ latency must not accumulate rounding
			// errors. Re-synchronize at each UART start bit, as a UART does.
			const uint64_t phase_elapsed = now - _phase_us;
			const unsigned bits = (phase_elapsed + BIT_US / 2) / BIT_US;
			const int error = static_cast<int>(phase_elapsed) - static_cast<int>(bits * BIT_US);

			if (high == _high || bits == 0 || bits > 12 || error <= -int(BIT_US / 2) || error >= int(BIT_US / 2)) {
				bad(high == _high ? 2 : 1, elapsed, error);
			}

			else {
				for (unsigned i = 0; i < bits; ++i) { bit(!_high); }

				_phase_us += bits * BIT_US;

				if (!_bad && _bit == 0 && !_high && high) { _phase_us = now; }
			}
		}

		_edge_us = now;
		_high = high;
	}

	void flush(uint64_t now, bool high)
	{
		// Do not flush while a new start edge is pending in EXTI.
		if (high == _high && now - _edge_us >= GAP_US) { finish_gap(); }
	}

	Statistics statistics() const
	{
		return {_frames, _errors, _window_frames,
			(_window_frames >= 2 && _last_start > _first_start) ?
			static_cast<uint32_t>(uint64_t(_window_frames - 1) * 1000000000ULL / (_last_start - _first_start)) : 0,
			_last_start, {_diagnostic[0], _diagnostic[1], _diagnostic[2], _diagnostic[3], _diagnostic[4]}};
	}

	void reset_window() { _window_frames = 0; _first_start = _last_start = 0; }

private:
	static constexpr unsigned BIT_US = 10;
	static constexpr unsigned GAP_US = 500;

	void bad(int code, uint64_t elapsed = 0, int phase_error = 0)
	{
		if (_bad) { return; }

		_diagnostic[0] = code; _diagnostic[1] = _bytes; _diagnostic[2] = _bit;
		_diagnostic[3] = elapsed; _diagnostic[4] = phase_error;
		_bad = true;
	}

	void bit(bool value)
	{
		if (_bad) { return; }

		if (_bit == 0) {
			if (value) { return; } // Additional idle stop time between bytes.

			if (_bytes >= 25) { bad(5); return; }

			_bit = 1; _byte = _parity = 0;
			return;
		}

		if (_bit <= 8) {
			_byte |= static_cast<unsigned>(value) << (_bit - 1);
			_parity ^= value;

		} else if (_bit == 9) {
			if (value != bool(_parity)) { bad(3); }

		} else if (!value) { bad(4); }

		if (++_bit == 12) {
			_frame[_bytes++] = _byte;
			_bit = 0;
		}
	}

	bool zero_output_frame() const
	{
		if (_frame[0] != 0x0f) { return false; }

		// IO firmware can encode disabled PWM as raw zero, while the legacy
		// PX4 encoder clamps its first eight channels to 0x7ff. Require one
		// complete pattern, including zero unused channels, flags and footer.
		bool zero = true, legacy = true;

		for (unsigned i = 1; i < 25; ++i) {
			zero = zero && _frame[i] == 0;
			legacy = legacy && _frame[i] == (i <= 11 ? 0xff : 0);
		}

		return zero || legacy;
	}

	void finish_gap()
	{
		if (!_active) { return; }

		if (!_high && !_bad && _bit) {
			const unsigned remaining = 12 - _bit;

			for (unsigned i = 0; i < remaining; ++i) { bit(true); }
		}

		if (!_bad && (_high || _bytes != 25 || _bit != 0 || !zero_output_frame())) {
			bad(_high || _bytes != 25 || _bit != 0 ? 5 : 6);
		}

		if (_bad) { ++_errors; }

		else {
			++_frames;

			if (_window_frames++ == 0) { _first_start = _start_us; }

			_last_start = _start_us;
		}

		_active = false;
	}

	uint64_t _edge_us{0}, _phase_us{0}, _start_us{0}, _first_start{0}, _last_start{0};
	uint32_t _frames{0}, _errors{0}, _window_frames{0};
	unsigned _bytes{0}, _bit{0}, _byte{0}, _parity{0};
	bool _high{false}, _active{false}, _bad{false};
	uint8_t _frame[25] {};
	int32_t _diagnostic[5] {};
};
}
