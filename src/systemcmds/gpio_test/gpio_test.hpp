// SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include <stdint.h>

// Board startup/reset must leave every allowlisted test signal undriven.
bool gpio_test_contains_pin(uint32_t config);
bool gpio_test_initialize_inputs();
