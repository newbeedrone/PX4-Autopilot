// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include "protocol_codec.hpp"

namespace protocol_test
{
class I2CSimulator
{
public:
	static constexpr uint8_t ADDRESS = I2C_ADDRESS;
	void start(Mode mode, uint32_t token) { _mode = mode; _token = token; _sequence = _tx = _rx = _errors = 0; _valid = false; begin_write(); }
	void begin_write() { _received = 0; _valid = false; _committed = false; }
	void write(uint8_t byte, uint64_t) { if (_received < PACKET_SIZE) { _request[_received++] = byte; } else { _received = PACKET_SIZE + 1; } }
	void end_write()
	{
		if (_committed) { return; }

		_committed = true;
		const uint32_t sequence = get32(_request + 8);
		_valid = valid_packet(_request, _received, _mode, _token, sequence, false) && sequence > _sequence;

		if (_valid) { _sequence = sequence; packet(_response, _mode, _token, sequence, true); _rx++; } else { _errors++; }
	}
	void begin_read(uint64_t) { end_write(); _sent = 0; }
	uint8_t read(uint64_t) { return _valid && _sent < PACKET_SIZE ? _response[_sent++] : 0xff; }
	void end_read() { if (_valid && _sent == PACKET_SIZE) { _tx++; _valid = false; } }
	uint32_t received() const { return _rx; }
	uint32_t transmitted() const { return _tx; }
	uint32_t errors() const { return _errors; }
private:
	Mode _mode{Mode::None};
	uint32_t _token{0}, _sequence{0}, _tx{0}, _rx{0}, _errors{0};
	uint8_t _request[PACKET_SIZE] {}, _response[PACKET_SIZE] {};
	unsigned _received{0}, _sent{0};
	bool _valid{false}, _committed{false};
};

// Edge-driven, open-drain I2C slave for fixture connectors whose GPIOs have no
// matching hardware I2C pair. SCL is never driven. The master runs at 10 kHz.
class GpioI2CSlave
{
public:
	void start(Mode mode, uint32_t token) { _simulator.start(mode, token); _state = State::Idle; _scl = _sda = _release = true; _reading = false; _accept = false; _bits = _byte = 0; }
	bool edge(bool scl, bool sda, uint64_t now)
	{
		const bool rising = !_scl && scl, falling = _scl && !scl;
		const bool start = _scl && scl && _sda && !sda;
		const bool stop = _scl && scl && !_sda && sda;
		_scl = scl; _sda = sda;

		if (start || stop) {
			if (_reading) { _simulator.end_read(); }

			else if (_state != State::Idle && _state != State::Address) { _simulator.end_write(); }

			_reading = false; _release = true; _bits = _byte = 0;
			_state = start ? State::Address : State::Idle;
			return _release;
		}

		if (_state == State::Address || _state == State::Write) {
			if (rising) {
				_byte = (_byte << 1) | sda;

				if (++_bits == 8) {
					if (_state == State::Address) {
						_accept = (_byte >> 1) == I2CSimulator::ADDRESS; _reading = _accept && (_byte & 1);

						if (_accept) {

							if (_reading) { _simulator.begin_read(now); } else { _simulator.begin_write(); }
						}

					} else { _accept = true; _simulator.write(_byte, now); }

					_state = State::AckSetup;
				}
			}

		} else if (_state == State::AckSetup && falling) { _release = !_accept; _state = State::AckClock; }

		else if (_state == State::AckClock && rising) { _state = State::AckEnd; }

		else if (_state == State::AckEnd && falling) {
			_release = true; _bits = _byte = 0;

			if (!_accept) { _state = State::Idle; }

			else if (_reading) { _byte = _simulator.read(now); _release = _byte & 0x80; _state = State::Read; }

			else { _state = State::Write; }

		} else if (_state == State::Read) {
			if (rising && ++_bits == 8) { _state = State::MasterAckSetup; }

			else if (falling) { _release = (_byte << _bits) & 0x80; }

		} else if (_state == State::MasterAckSetup && falling) { _release = true; _state = State::MasterAck; }

		else if (_state == State::MasterAck && rising) { _accept = !sda; _state = State::MasterAckEnd; }

		else if (_state == State::MasterAckEnd && falling) {
			if (_accept) { _byte = _simulator.read(now); _bits = 0; _release = _byte & 0x80; _state = State::Read; }

			else { _release = true; _simulator.end_read(); _reading = false; _state = State::Idle; }
		}

		return _release;
	}

	I2CSimulator &simulator() { return _simulator; }

private:
	enum class State { Idle, Address, Write, AckSetup, AckClock, AckEnd, Read, MasterAckSetup, MasterAck, MasterAckEnd };
	I2CSimulator _simulator{};
	State _state{State::Idle};
	bool _scl{true}, _sda{true}, _release{true}, _accept{false}, _reading{false};
	unsigned _bits{0};
	uint8_t _byte{0};
};
}
