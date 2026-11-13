// SPDX-License-Identifier: BSD-3-Clause
#include "edge_counter.hpp"
#include "gpio_test_profile.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <initializer_list>

int main()
{
	unsigned windows = 0;

	for (const auto &channel : GPIO_TEST_CHANNELS) {
		if (!channel.waveform) { continue; }

		for (const int clock_ppm : {-100, 100}) {
			for (const unsigned phase_us : {7u, 31u, 49u}) {
				GpioTestEdges edges;
				unsigned report = 1;

				for (uint64_t now = phase_us; now < 3 * GPIO_TEST_REPORT_US; now += GPIO_TEST_SAMPLE_US) {
					// Independent clocks, different sampling phases, and bounded sampling jitter.
					const uint64_t sampled_at = now + (now / GPIO_TEST_SAMPLE_US) % 7;
					const uint64_t source_time = (sampled_at + 13) * (1000000 + clock_ppm) / 1000000;
					const uint64_t source_tick = source_time / GPIO_TEST_WAVE_US * GPIO_TEST_WAVE_US;
					edges.sample(sampled_at, (source_tick / channel.half_period_us) & 1);

					if (now + GPIO_TEST_SAMPLE_US >= report * GPIO_TEST_REPORT_US) {
						assert(edges.edges >= 3);
						const double expected = 500000000.0 / channel.half_period_us;
						assert(fabs(edges.frequency_millihz() - expected) / expected < 0.02);
						const bool previous = edges.previous;
						edges = {};
						edges.previous = previous;
						edges.initialized = true;
						report++;
						windows++;
					}
				}
			}
		}
	}

	printf("%u waveform measurement windows passed across %u profile channels\n", windows, GPIO_TEST_COUNT);
	return 0;
}
