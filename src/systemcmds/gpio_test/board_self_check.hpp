#pragma once

using GpioTestReport = void (*)(void *context, const char *name, bool passed, int detail);

// Onboard diagnostics are reported separately from interface eligibility.
bool board_gpio_test_self_check(GpioTestReport report, void *context, bool check_external_bus = true);
bool board_gpio_test_external_check(GpioTestReport report, void *context, bool gpio_waveform);
