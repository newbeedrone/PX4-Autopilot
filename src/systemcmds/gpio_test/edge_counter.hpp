#pragma once

#include <stdint.h>

// Updated only in the sampling interrupt; snapshots require an interrupt lock.
struct GpioTestEdges {
	uint64_t first_us{0};
	uint64_t last_us{0};
	uint32_t edges{0};
	bool previous{false};
	bool initialized{false};

	void sample(uint64_t now, bool high)
	{
		if (initialized && high && !previous) {
			if (edges == 0) {
				first_us = now;
			}

			last_us = now;
			edges++;
		}

		previous = high;
		initialized = true;
	}

	uint32_t frequency_millihz() const
	{
		return (edges >= 2 && last_us > first_us)
		       ? static_cast<uint32_t>((static_cast<uint64_t>(edges - 1) * 1000000000ULL) / (last_us - first_us)) : 0;
	}
};
