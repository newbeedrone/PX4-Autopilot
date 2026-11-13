// SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include <stdint.h>
#include <string.h>

namespace protocol_test
{
enum class Mode : uint8_t { None, I2c3, Gps2, Sbus, Dsm, Can, Ethernet, Gps1, Power1, Power2, SharedI2c3, AdcLow, AdcHigh };
constexpr unsigned PACKET_SIZE = 32;
constexpr unsigned RC_CHANNELS = 7;
constexpr uint8_t I2C_ADDRESS = 0x42;
constexpr uint16_t UDP_PORT = 14570;

inline Mode mode(const char *name)
{
	return strcmp(name, "ADC6V6_LOW") == 0 ? Mode::AdcLow : strcmp(name, "ADC6V6_HIGH") == 0 ? Mode::AdcHigh :
	       strcmp(name, "GPS1_I2C") == 0 ? Mode::Gps1 : strcmp(name, "POWER1_I2C") == 0 ? Mode::Power1 :
	       strcmp(name, "POWER2_I2C") == 0 ? Mode::Power2 : strcmp(name, "UART4_I2C3") == 0 ? Mode::SharedI2c3 :
	       strcmp(name, "I2C3") == 0 ? Mode::I2c3 : strcmp(name, "GPS2_I2C") == 0 ? Mode::Gps2 :
	       strcmp(name, "SBUS") == 0 ? Mode::Sbus : strcmp(name, "DSM") == 0 ? Mode::Dsm :
	       strcmp(name, "CAN") == 0 ? Mode::Can : strcmp(name, "ETH") == 0 ? Mode::Ethernet : Mode::None;
}

inline const char *name(Mode mode)
{
	switch (mode) {
	case Mode::AdcLow: return "ADC6V6_LOW";

	case Mode::AdcHigh: return "ADC6V6_HIGH";

	case Mode::Gps1: return "GPS1_I2C";

	case Mode::Power1: return "POWER1_I2C";

	case Mode::Power2: return "POWER2_I2C";

	case Mode::SharedI2c3: return "UART4_I2C3";

	case Mode::I2c3: return "I2C3";

	case Mode::Gps2: return "GPS2_I2C";

	case Mode::Sbus: return "SBUS";

	case Mode::Dsm: return "DSM";

	case Mode::Can: return "CAN";

	case Mode::Ethernet: return "ETH";

	default: return "NONE";
	}
}

inline bool is_i2c(Mode mode)
{
	return mode == Mode::I2c3 || mode == Mode::Gps2 || mode == Mode::Gps1 || mode == Mode::Power1 || mode == Mode::Power2
	       || mode == Mode::SharedI2c3;
}
inline bool is_adc(Mode mode) { return mode == Mode::AdcLow || mode == Mode::AdcHigh; }
inline bool adc_voltage_valid(Mode mode, int millivolts)
{
	return mode == Mode::AdcLow ? millivolts >= 0 && millivolts <= 100 :
	       mode == Mode::AdcHigh && millivolts >= 1500 && millivolts <= 1800;
}
inline unsigned source_i2c(Mode mode)
{
	return mode == Mode::Gps1 || mode == Mode::Power1 ? 1 : mode == Mode::Gps2 || mode == Mode::Power2 ? 2 : 3;
}
inline unsigned fixture_i2c(Mode mode)
{
	return mode == Mode::Power1 ? 1 : mode == Mode::Power2 ? 2 : mode == Mode::Gps2 ? 4 : mode == Mode::I2c3 ? 3 : 0;
}

inline uint16_t crc16(const uint8_t *data, unsigned length)
{
	uint16_t crc = 0xffff;

	for (unsigned i = 0; i < length; ++i) {
		crc ^= static_cast<uint16_t>(data[i]) << 8;

		for (unsigned bit = 0; bit < 8; ++bit) {
			crc = (crc << 1) ^ ((crc & 0x8000) ? 0x1021 : 0);
		}
	}

	return crc;
}

inline void put32(uint8_t *p, uint32_t value)
{
	for (unsigned i = 0; i < 4; ++i) { p[i] = value >> (8 * i); }
}

inline uint32_t get32(const uint8_t *p)
{
	return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}

inline void packet(uint8_t *p, Mode mode, uint32_t token, uint32_t sequence, bool response)
{
	p[0] = 'P'; p[1] = 'N'; p[2] = 1; p[3] = static_cast<uint8_t>(mode);
	put32(p + 4, token); put32(p + 8, sequence);
	p[12] = response ? 0xa5 : 0x5a;

	for (unsigned i = 13; i < PACKET_SIZE - 2; ++i) {
		p[i] = (token >> (8 * (i % 4))) ^ sequence ^ (i * 37) ^ p[12];
	}

	const uint16_t crc = crc16(p, PACKET_SIZE - 2);
	p[30] = crc; p[31] = crc >> 8;
}

inline bool valid_packet(const uint8_t *p, unsigned length, Mode mode, uint32_t token, uint32_t sequence,
			 bool response)
{
	uint8_t expected[PACKET_SIZE];
	packet(expected, mode, token, sequence, response);
	return length == PACKET_SIZE && memcmp(p, expected, PACKET_SIZE) == 0;
}

// Different channels and run tokens produce different low/mid/high values.
inline uint16_t rc_value(uint32_t token, unsigned channel)
{
	return 1100 + 400 * ((token + channel) % 3);
}

inline unsigned sbus(uint8_t *frame, uint32_t token)
{
	memset(frame, 0, 25);
	frame[0] = 0x0f;

	for (unsigned channel = 0; channel < 16; ++channel) {
		// PX4 sbus.cpp: factor 0.625, integer offset 874 microseconds.
		const uint16_t raw = ((rc_value(token, channel) - 874) * 8u + 2) / 5;

		for (unsigned bit = 0; bit < 11; ++bit) {
			const unsigned position = channel * 11 + bit;
			frame[1 + position / 8] |= ((raw >> bit) & 1) << (position % 8);
		}
	}

	return 25;
}

inline unsigned dsm(uint8_t *frame, uint32_t token)
{
	frame[0] = 0; frame[1] = 0x12; // DSMX 11-bit, seven channels.

	for (unsigned channel = 0; channel < RC_CHANNELS; ++channel) {
		// PX4 remaps DSM throttle/roll/pitch (0/1/2) to 2/0/1.
		const unsigned mapped = channel == 0 ? 2 : channel == 1 ? 0 : channel == 2 ? 1 : channel;
		// PX4 dsm.cpp uses round(raw * 0.583) + 903 microseconds.
		const uint16_t raw = ((rc_value(token, mapped) - 903) * 1000u + 291) / 583;
		const uint16_t word = (channel << 11) | raw;
		frame[2 + channel * 2] = word >> 8;
		frame[3 + channel * 2] = word;
	}

	return 16;
}
}
