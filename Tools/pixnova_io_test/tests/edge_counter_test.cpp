#include "edge_counter.hpp"
#include <assert.h>

int main()
{
	GpioTestEdges edges;
	// An initial high level is not an edge. This avoids a false first period.
	edges.sample(0, true);
	assert(edges.edges == 0);
	edges.sample(1000, false);
	edges.sample(2000, true);
	assert(edges.frequency_millihz() == 0);
	edges.sample(3000, false);
	edges.sample(4000, true);
	assert(edges.frequency_millihz() == 500000);
	// A long uptime must not overflow timestamps or period arithmetic.
	GpioTestEdges uptime;
	const uint64_t start = 0x100000000ULL;
	uptime.sample(start, false);
	uptime.sample(start + 1000, true);
	uptime.sample(start + 2000, false);
	uptime.sample(start + 3000, true);
	assert(uptime.frequency_millihz() == 500000);
	return 0;
}
