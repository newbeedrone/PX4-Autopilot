if(NOT CONFIG_GPIO_TEST_PROFILE)
	message(FATAL_ERROR "gpio_test requires an explicit allowlisted wiring profile")
endif()
set(profile_file "${PX4_SOURCE_DIR}/${CONFIG_GPIO_TEST_PROFILE}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
	"${profile_file}" "${PX4_SOURCE_DIR}/Tools/pixnova_io_test/generate_profile.py")
execute_process(
	COMMAND ${PYTHON_EXECUTABLE} ${PX4_SOURCE_DIR}/Tools/pixnova_io_test/generate_profile.py
		--profile "${profile_file}" --header "${CMAKE_CURRENT_BINARY_DIR}/gpio_test_profile.h"
	RESULT_VARIABLE profile_result
)
if(NOT profile_result EQUAL 0)
	message(FATAL_ERROR "Invalid GPIO test profile")
endif()
# The minimal probe still uses manifest queries through the parameter library.
# Declare their hardware-info dependency without relying on a flight module to pull it in.
cmake_policy(PUSH)
cmake_policy(SET CMP0079 NEW)
target_link_libraries(px4_platform arch_board_hw_info)
cmake_policy(POP)
px4_add_module(
	MODULE systemcmds__gpio_test
	MAIN gpio_test
	SRCS gpio_test.cpp
	INCLUDES ${CMAKE_CURRENT_BINARY_DIR}
	DEPENDS
		perf
		px4_work_queue
		version
		arch_board_hw_info
)
