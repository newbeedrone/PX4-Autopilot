// SPDX-License-Identifier: BSD-3-Clause

#include <px4_platform_common/px4_config.h>
#include <px4_platform_common/i2c.h>
#include <px4_platform/gpio.h>
#include <px4_arch/micro_hal.h>
#include <drivers/drv_hrt.h>
#include <nuttx/i2c/i2c_master.h>
#include <nuttx/spi/spi.h>
#include <board_config.h>
#include <errno.h>
#include <unistd.h>
#include "board_self_check.hpp"

#if !defined(CONFIG_GPIO_TEST_RECEIVER)
#include <px4_platform_common/px4_mtd.h>
#include <px4_platform_common/px4_manifest.h>
#include <nuttx/mtd/mtd.h>
#include <nuttx/fs/fs.h>
#include <nuttx/fs/ioctl.h>
#include <sys/mount.h>
#include <limits.h>
namespace
{
constexpr uint32_t I2C_FREQUENCY = 100000;
constexpr uint8_t BMM150_ADDRESS = 0x10;
constexpr uint8_t BMP388_ADDRESS = 0x76;
constexpr uint8_t ICP20100_ADDRESS = 0x63;
constexpr uint8_t BMM150_CHIP_ID = 0x32;
constexpr uint8_t BMP388_CHIP_ID = 0x50;
constexpr uint8_t ICP20100_CHIP_ID = 0x63;
constexpr uint8_t ICM45686_CHIP_ID = 0xE9;
constexpr uint8_t ICM45686_WHO_AM_I = 0x72;
constexpr uint8_t SPI_READ = 0x80;

bool read_register(i2c_master_s *bus, uint8_t address, uint8_t reg, uint8_t &value)
{
	if (!bus) {
		return false;
	}

	i2c_msg_s messages[2] {};
	messages[0].frequency = messages[1].frequency = I2C_FREQUENCY;
	messages[0].addr = messages[1].addr = address;
	messages[0].buffer = &reg;
	messages[0].length = 1;
	messages[1].flags = I2C_M_READ;
	messages[1].buffer = &value;
	messages[1].length = 1;
	return I2C_TRANSFER(bus, messages, 2) >= 0;
}

bool sensor_id(GpioTestReport report, void *context, const char *name, unsigned number, uint8_t address,
	       uint8_t reg, uint8_t expected, bool wake_bmm = false)
{
	auto *bus = px4_i2cbus_initialize(number);
	uint8_t value = 0;
	bool ok = bus != nullptr;

	if (ok && wake_bmm) {
		// BMM150 starts in suspend mode. Enable its power-control bit before reading CHIP_ID.
		uint8_t command[] = {0x4B, 0x01};
		i2c_msg_s message{};
		message.frequency = I2C_FREQUENCY;
		message.addr = address;
		message.buffer = command;
		message.length = sizeof(command);
		ok = I2C_TRANSFER(bus, &message, 1) >= 0;
		usleep(3000);
	}

	ok = ok && read_register(bus, address, reg, value) && value == expected;

	if (bus) {
		px4_i2cbus_uninitialize(bus);
	}

	report(context, name, ok, value);
	return ok;
}

bool empty_external_bus(GpioTestReport report, void *context)
{
	auto *bus = px4_i2cbus_initialize(1);
	bool ok = bus != nullptr;
	int found = -1;
	const hrt_abstime deadline = hrt_absolute_time() + 5000000;

	for (uint8_t address = 0x08; bus && address <= 0x77; address++) {
		uint8_t value{};
		i2c_msg_s message{};
		message.frequency = I2C_FREQUENCY;
		message.addr = address;
		message.flags = I2C_M_READ;
		message.buffer = &value;
		message.length = 1;

		const int result = I2C_TRANSFER(bus, &message, 1);

		if (result >= 0 || result == -ECOMM) {
			found = address;
			ok = false;
			break;
		}

		if (result != -EADDRNOTAVAIL && result != -ENXIO) {
			// A stuck bus or transfer fault is not evidence that the bus is empty.
			found = result;
			ok = false;
			break;
		}

		if (hrt_absolute_time() > deadline) {
			found = -2;
			ok = false;
			break;
		}
	}

	if (bus) {
		px4_i2cbus_uninitialize(bus);
	}

	report(context, "i2c1_no_attached_devices", ok, found);
	return ok;
}

bool imu_id(GpioTestReport report, void *context, const char *name, unsigned number, uint32_t chip_select)
{
	auto *bus = px4_spibus_initialize(number);
	uint8_t received[2] {};
	const uint8_t command[2] = {ICM45686_WHO_AM_I | SPI_READ, 0};
	const uint32_t cs = chip_select | GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz | GPIO_OUTPUT_SET;
	bool ok = bus != nullptr;

	if (bus) {
		px4_arch_configgpio(cs);
		SPI_LOCK(bus, true);
		SPI_SETFREQUENCY(bus, 1000000);
		SPI_SETMODE(bus, SPIDEV_MODE3);
		SPI_SETBITS(bus, 8);
		px4_arch_gpiowrite(cs, false);
		SPI_EXCHANGE(bus, command, received, sizeof(received));
		px4_arch_gpiowrite(cs, true);
		SPI_LOCK(bus, false);
		ok = received[1] == ICM45686_CHIP_ID;
	}

	report(context, name, ok, received[1]);
	return ok;
}

bool eeprom_read(GpioTestReport report, void *context, const char *name, unsigned number, uint8_t address)
{
	auto *bus = px4_i2cbus_initialize(number);
	unsigned bytes = 0;
	uint8_t data[16] {};
	constexpr uint16_t offsets[] = {0, 8192 - sizeof(data)};

	// 24LC64T has no chip-ID register. Sample both ends without writing data.
	for (uint16_t offset : offsets) {
		if (!bus) {
			break;
		}

		uint8_t word_address[2] = {static_cast<uint8_t>(offset >> 8), static_cast<uint8_t>(offset)};
		i2c_msg_s messages[2] {};
		messages[0].frequency = messages[1].frequency = I2C_FREQUENCY;
		messages[0].addr = messages[1].addr = address;
		messages[0].buffer = word_address;
		messages[0].length = sizeof(word_address);
		messages[1].flags = I2C_M_READ;
		messages[1].buffer = data;
		messages[1].length = sizeof(data);

		if (I2C_TRANSFER(bus, messages, 2) < 0) {
			break;
		}

		bytes += sizeof(data);
	}

	if (bus) {
		px4_i2cbus_uninitialize(bus);
	}

	const bool ok = bytes == 2 * sizeof(data);
	report(context, name, ok, bytes);
	return ok;
}

bool fram_read(GpioTestReport report, void *context)
{
	auto *bus = px4_spibus_initialize(5);
	uint8_t identity[9] {};

	if (bus) {
		SPI_LOCK(bus, true);
		SPI_SETFREQUENCY(bus, 1000000);
		SPI_SETMODE(bus, SPIDEV_MODE3);
		SPI_SETBITS(bus, 8);
		SPI_SELECT(bus, SPIDEV_FLASH(0), true);
		SPI_SEND(bus, 0x9F); // RAMTRON RDID, followed by six manufacturer bytes and three part bytes.
		SPI_RECVBLOCK(bus, identity, sizeof(identity));
		SPI_SELECT(bus, SPIDEV_FLASH(0), false);
		SPI_LOCK(bus, false);
	}

	// Match the FM25V02 / FM25V02A entries in the RAMTRON driver. Read the ID
	// on every check: geometry is cached, and SPI memory reads always return a length.
	const bool identified = bus && identity[7] == 0x22 && (identity[8] == 0x00 || identity[8] == 0x08);
	unsigned count = 0;
	auto **instances = px4_mtd_get_instances(&count);
	bool ok = false;
	int capacity = 0;

	for (unsigned i = 0; instances && i < count; i++) {
		if (!instances[i] || instances[i]->devid != SPIDEV_FLASH(0) || !instances[i]->mtd_dev) {
			continue;
		}

		auto *device = instances[i]->mtd_dev;
		mtd_geometry_s geometry{};
		uint8_t data[16] {};

		if (identified && device->ioctl(device, MTDIOC_GEOMETRY, reinterpret_cast<unsigned long>(&geometry)) == 0) {
			const uint64_t size = static_cast<uint64_t>(geometry.erasesize) * geometry.neraseblocks;

			if (size == 32768) {
				capacity = size;
				ok = MTD_READ(device, 0, sizeof(data), data) == static_cast<ssize_t>(sizeof(data))
				     && MTD_READ(device, size - sizeof(data), sizeof(data), data) == static_cast<ssize_t>(sizeof(data));
			}
		}

		break;
	}

	report(context, "fram_spi5", ok, ok ? capacity : 0);
	return ok;
}

bool sdcard_read(GpioTestReport report, void *context)
{
	static bool initialised = false;
	bool ok = false;
	int capacity_mib = 0;

	if (!initialised) {
		initialised = stm32_sdio_initialize() == 0;
	}

	const hrt_abstime deadline = hrt_absolute_time() + 3000000;

	while (initialised && hrt_absolute_time() < deadline) {
		inode *device = nullptr;

		if (open_blockdriver("/dev/mmcsd0", MS_RDONLY, &device) == 0) {
			geometry info{};
			alignas(32) uint8_t sector[512] {};
			const auto *operations = device->u.i_bops;

			if (operations && operations->geometry && operations->read
			    && operations->geometry(device, &info) == 0 && info.geo_available
			    && info.geo_nsectors > 0 && info.geo_sectorsize == sizeof(sector)) {
				const uint64_t mib = static_cast<uint64_t>(info.geo_nsectors) * info.geo_sectorsize / (1024 * 1024);

				if (mib > 0 && mib <= INT_MAX) {
					ok = operations->read(device, sector, 0, 1) == 1
					     && operations->read(device, sector, info.geo_nsectors - 1, 1) == 1;
					capacity_mib = ok ? mib : 0;
				}
			}

			close_blockdriver(device);
		}

		if (ok) {
			break;
		}

		usleep(50000);
	}

	report(context, "sdcard", ok, capacity_mib);
	return ok;
}
}
#endif

bool board_gpio_test_external_check(GpioTestReport report, void *context, bool gpio_waveform)
{
// All I2C pins are outside the GPIO waveform table. Attached I2C devices
// neither authorize nor block tests of the remaining digital connections.
	(void)report; (void)context; (void)gpio_waveform;
	return true;
}

bool board_gpio_test_self_check(GpioTestReport report, void *context, bool check_external_bus)
{
#if defined(CONFIG_GPIO_TEST_RECEIVER)
	(void)check_external_bus;
#if defined(CONFIG_GPIO_TEST_PROTOCOLS)
	report(context, "probe_protocol_firmware", true, 1);
#else
	report(context, "probe_input_only_firmware", true, 1);
#endif
	return true;
#else
	bool ok = sensor_id(report, context, "bmm150_i2c4", 4, BMM150_ADDRESS, 0x40, BMM150_CHIP_ID, true);
	ok = sensor_id(report, context, "bmp388_i2c4", 4, BMP388_ADDRESS, 0x00, BMP388_CHIP_ID) && ok;
	ok = sensor_id(report, context, "icp20100_i2c2", 2, ICP20100_ADDRESS, 0x0C, ICP20100_CHIP_ID) && ok;
	ok = imu_id(report, context, "icm45686_spi1", 1, GPIO_PORTI | GPIO_PIN9) && ok;
	ok = imu_id(report, context, "icm45686_spi2", 2, GPIO_PORTH | GPIO_PIN5) && ok;
	ok = imu_id(report, context, "icm45686_spi3", 3, GPIO_PORTI | GPIO_PIN4) && ok;
	ok = eeprom_read(report, context, "eeprom_base_i2c3", 3, 0x51) && ok;
	ok = eeprom_read(report, context, "eeprom_imu_i2c4", 4, 0x50) && ok;
	ok = fram_read(report, context) && ok;
	ok = sdcard_read(report, context) && ok;

	if (check_external_bus) {
		ok = empty_external_bus(report, context) && ok;
	}

	return ok;
#endif
}
