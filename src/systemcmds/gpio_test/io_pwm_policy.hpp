// SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include "protocol_codec.hpp"
#include "sbus_capture.hpp"
#include <modules/px4iofirmware/protocol.h>

namespace protocol_test
{
// The dedicated test adapter never permits arming, safety, mixer or boot writes.
inline bool io_pwm_write_allowed(Mode mode, uint32_t token, bool cleaning, bool configured,
				 const uint16_t *saved_rates, uint8_t page, uint8_t offset, const uint16_t *data, unsigned count)
{
	if ((!is_io_pwm(mode) && mode != Mode::SbusOut && !cleaning) || !configured) { return false; }

	if ((page == PX4IO_PAGE_DIRECT_PWM || page == PX4IO_PAGE_DISARMED_PWM) && offset == 0 && count == 8) {
		for (unsigned i = 0; i < 8; ++i) {
			const unsigned expected = !cleaning && is_io_pwm(mode) && page == PX4IO_PAGE_DIRECT_PWM
						  && i == io_pwm_channel(mode) ? io_pwm_width(token) : 0;

			if (data[i] != expected) { return false; }
		}

		return true;
	}

	if (page == PX4IO_PAGE_SETUP && offset == PX4IO_P_SETUP_PWM_RATES && count == 3) {
		return cleaning ? memcmp(data, saved_rates, 3 * sizeof(uint16_t)) == 0 : is_io_pwm(mode) && data[0] == 0 && data[1] == io_pwm_hz(mode)
		       && data[2] == 200;
	}

	return cleaning && page == PX4IO_PAGE_STATUS && offset == PX4IO_P_STATUS_FLAGS && count == 1 &&
	       !(data[0] & (PX4IO_P_STATUS_FLAGS_RAW_PWM | PX4IO_P_STATUS_FLAGS_INIT_OK | PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED |
			    PX4IO_P_STATUS_FLAGS_ARM_SYNC | PX4IO_P_STATUS_FLAGS_FMU_OK));
}

inline bool io_sbus_write_allowed(Mode mode, bool cleaning, bool configured, uint16_t features, uint16_t rate,
				  uint8_t page, uint8_t offset, const uint16_t *data, unsigned count)
{
	if (mode != Mode::SbusOut || !configured || page != PX4IO_PAGE_SETUP || count != 1) { return false; }

	return offset == PX4IO_P_SETUP_FEATURES ? data[0] == (cleaning ? features : PX4IO_P_SETUP_FEATURES_SBUS1_OUT) :
	       offset == PX4IO_P_SETUP_SBUS_RATE && data[0] == (cleaning ? rate : SBUS_OUT_RATE);
}
}
