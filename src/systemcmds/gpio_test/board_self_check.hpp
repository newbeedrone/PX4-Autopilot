#pragma once

using GpioTestReport = void (*)(void *context, const char *name, bool passed, int detail);

// A board adapter must provide checks before GPIO outputs can be enabled.
bool board_gpio_test_self_check(GpioTestReport report, void *context, bool check_external_bus = true);
