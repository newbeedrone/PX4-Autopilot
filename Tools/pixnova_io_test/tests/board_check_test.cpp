// SPDX-License-Identifier: BSD-3-Clause
// Compile the actual board checks with deterministic bus and storage substitutes.
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
#include <errno.h>
#include <string>
#include <vector>

#define GPIO_PORTI 0x80
#define GPIO_PORTH 0x70
#define GPIO_PIN9 9
#define GPIO_PIN5 5
#define GPIO_PIN4 4
#define GPIO_OUTPUT 0x100
#define GPIO_PUSHPULL 0
#define GPIO_SPEED_2MHz 0
#define GPIO_OUTPUT_SET 0
#define I2C_M_READ 1
#define SPIDEV_MODE3 3
#define SPIDEV_FLASH(n) (0x100u + (n))
#define MTDIOC_GEOMETRY 1
#define MTD_READ(d, o, n, b) ((d)->read(d, o, n, b))

using hrt_abstime = uint64_t;
static hrt_abstime clock_us = 1000;
static hrt_abstime bmm_wake_at;
static std::string fault;
static unsigned fram_ids = 0;
static unsigned sd_initialisations = 0;
static int external_result = -ENXIO;
static std::vector<unsigned> eeprom_offsets;
static std::vector<off_t> sd_sectors;
static std::vector<off_t> fram_offsets;
static std::vector<unsigned> icp_transactions;

struct i2c_master_s { unsigned number; };
struct spi_dev_s { unsigned number; };
static i2c_master_s i2c_buses[] = {{0}, {1}, {2}, {3}, {4}};
static spi_dev_s spi_buses[] = {{0}, {1}, {2}, {3}, {4}, {5}};
static spi_dev_s *locked_spi;
struct i2c_msg_s {
	uint32_t frequency;
	uint16_t addr;
	uint16_t flags;
	uint8_t *buffer;
	int length;
};

static hrt_abstime hrt_absolute_time() { return clock_us; }
static int test_usleep(useconds_t duration) { clock_us += duration; return 0; }
static i2c_master_s *px4_i2cbus_initialize(unsigned n)
{
	if (n == 2) { icp_transactions.clear(); }

	return &i2c_buses[n];
}
static int px4_i2cbus_uninitialize(i2c_master_s *) { return 0; }
static spi_dev_s *px4_spibus_initialize(unsigned n) { return &spi_buses[n]; }
static int px4_arch_configgpio(uint32_t) { return fault == "imu_cs_failed" ? -EINVAL : 0; }
static void px4_arch_gpiowrite(uint32_t, bool) {}
static int SPI_LOCK(spi_dev_s *bus, bool lock)
{
	if (lock) {
		assert(!locked_spi);

		if (fault == "spi_lock_failed") { return -EBUSY; }

		locked_spi = bus;

	} else {
		assert(locked_spi == bus);
		locked_spi = nullptr;
	}

	return 0;
}
static void SPI_SETFREQUENCY(spi_dev_s *, unsigned) {}
static void SPI_SETMODE(spi_dev_s *, unsigned) {}
static void SPI_SETBITS(spi_dev_s *, unsigned) {}
static void SPI_SELECT(spi_dev_s *, unsigned device, bool) { assert(device == SPIDEV_FLASH(0)); }
static void SPI_SEND(spi_dev_s *bus, unsigned command)
{
	assert(locked_spi == bus && bus->number == 5 && command == 0x9F);
	fram_ids++;
}
static void SPI_RECVBLOCK(spi_dev_s *, void *data, size_t count)
{
	assert(count == 9);
	auto *identity = static_cast<uint8_t *>(data);
	memset(identity, 0xFF, count);

	if (fault != "fram_spi5") {
		memset(identity, 0x7F, 6);
		identity[0] = fault == "fram_bad_continuation" ? 0 : 0x7F;
		identity[6] = fault == "fram_bad_manufacturer" ? 0 : 0xC2;
		identity[7] = 0x22;
		identity[8] = fault == "fram_fm25v02" ? 0x00 : 0x08;
	}
}
static void SPI_EXCHANGE(spi_dev_s *bus, const void *command, void *data, size_t count)
{
	assert(locked_spi == bus && count == 2 && static_cast<const uint8_t *>(command)[0] == (0x72 | 0x80));
	assert(fault != "imu_cs_failed");
	static_cast<uint8_t *>(data)[1] = fault == "icm45686_spi" + std::to_string(bus->number) ? 0xFF : 0xE9;
}

static int I2C_TRANSFER(i2c_master_s *bus, i2c_msg_s *messages, int count)
{
	if (bus->number == 1) {
		assert(count == 1 && messages[0].flags == I2C_M_READ);
		return external_result;
	}

	const unsigned address = messages[0].addr;
	const std::string name = address == 0x51 ? "eeprom_base_i2c3" : address == 0x50 ? "eeprom_imu_i2c4" :
				 address == 0x10 ? "bmm150_i2c4" : address == 0x76 ? "bmp388_i2c4" : "icp20100_i2c2";

	if (address == 0x63) {
		assert(bus->number == 2 && messages[0].frequency == 100000);
		const uint8_t reg = messages[0].buffer[0];

		if (count == 1) {
			assert(reg == 0xBE && messages[0].length == 2 && messages[0].buffer[1] == 0);
			icp_transactions.push_back(reg);
			// Cold clocking writes can abort at the address, before the register byte.
			return fault == name || (fault != "icp_revision_a" && icp_transactions.size() <= 2) ? -ENXIO : 0;
		}

		assert(count == 2 && messages[1].flags == I2C_M_READ && messages[1].addr == address);
		assert(messages[0].length == 1 && messages[1].length == 1);
		icp_transactions.push_back(0x100 | reg);
		auto &value = messages[1].buffer[0];

		switch (reg) {
		case 0x0C:
			value = icp_transactions.size() < 3 || fault == "icp_bad_id" ? 0x40 : 0x63;
			break;

		case 0xD3:
			value = fault == "icp_bad_version" ? 0xFF : (fault == "icp_revision_a" ? 0x00 : 0xB2);
			break;

		case 0x00:
			value = 0;

			if (fault == "icp_dummy_nack") { return -ENXIO; }

			break;

		default: assert(false);
		}

		return fault == name ? -ENXIO : 0;
	}

	if (count == 1) {
		assert(address == 0x10 && messages[0].length == 2);
		assert(messages[0].buffer[0] == 0x4B && messages[0].buffer[1] == 1);
		bmm_wake_at = clock_us;
		return fault == name ? -ENXIO : 0;
	}

	assert(count == 2 && messages[1].flags == I2C_M_READ && messages[1].addr == address);

	if (address == 0x51 || address == 0x50) {
		assert(bus->number == (address == 0x51 ? 3u : 4u));
		assert(messages[0].length == 2 && messages[1].length == 16);
		eeprom_offsets.push_back((messages[0].buffer[0] << 8) | messages[0].buffer[1]);

	} else if (address == 0x10) {
		assert(messages[0].buffer[0] == 0x40 && clock_us - bmm_wake_at >= 3000);
	}

	if (fault == name) { return -ENXIO; }

	const uint8_t value = address == 0x10 ? (fault == "bmm_zero_id" ? 0 : 0x32) :
			      address == 0x76 ? (fault == "bmp390_id" ? 0x60 : 0x50) : 0;
	memset(messages[1].buffer, value, messages[1].length);
	return 0;
}

struct mtd_geometry_s { uint32_t erasesize; uint32_t neraseblocks; };
struct mtd_dev_s {
	int (*ioctl)(mtd_dev_s *, int, unsigned long);
	ssize_t (*read)(mtd_dev_s *, off_t, size_t, uint8_t *);
};
static int mtd_geometry(mtd_dev_s *, int command, unsigned long arg)
{
	assert(command == MTDIOC_GEOMETRY);
	*reinterpret_cast<mtd_geometry_s *>(arg) = {32, 1024};
	return 0;
}
static ssize_t mtd_read(mtd_dev_s *, off_t offset, size_t count, uint8_t *)
{
	fram_offsets.push_back(offset);
	// Like RAMTRON's SPI read, this succeeds even with a disconnected device.
	return count;
}
static mtd_dev_s fram = {mtd_geometry, mtd_read};
struct mtd_instance_s { unsigned devid; mtd_dev_s *mtd_dev; };
static mtd_instance_s instance = {SPIDEV_FLASH(0), &fram};
static mtd_instance_s *instances[] = {&instance};
static mtd_instance_s **px4_mtd_get_instances(unsigned *count) { *count = 1; return instances; }

struct geometry { bool geo_available; size_t geo_nsectors; size_t geo_sectorsize; };
struct inode;
struct block_operations {
	int (*geometry)(inode *, struct geometry *);
	ssize_t (*read)(inode *, uint8_t *, off_t, unsigned);
};
struct inode { struct { const block_operations *i_bops; } u; };
static int sd_geometry(inode *, struct geometry *info)
{
	*info = {true, 32768, 512};
	return 0;
}
static ssize_t sd_read(inode *, uint8_t *, off_t sector, unsigned count)
{
	assert(count == 1);
	sd_sectors.push_back(sector);
	return fault == "sdcard" ? -EIO : 1;
}
static block_operations sd_ops = {sd_geometry, sd_read};
static inode sd_device = {{&sd_ops}};
static int stm32_sdio_initialize() { sd_initialisations++; return 0; }
static int open_blockdriver(const char *path, int flags, inode **device)
{
	assert(strcmp(path, "/dev/mmcsd0") == 0 && flags == 1); // MS_RDONLY
	*device = &sd_device;
	return 0;
}
static int close_blockdriver(inode *) { return 0; }

#define usleep test_usleep
#include "../../../boards/newbeedrone/pixnova/src/gpio_test_board.cpp"
#undef usleep

struct Result { std::string name; bool passed; int detail; };
static void record(void *context, const char *name, bool passed, int detail)
{
	static_cast<std::vector<Result> *>(context)->push_back({name, passed, detail});
}

int main()
{
	const char *devices[] = {"bmm150_i2c4", "bmp388_i2c4", "icp20100_i2c2", "icm45686_spi1", "icm45686_spi2", "icm45686_spi3", "eeprom_base_i2c3", "eeprom_imu_i2c4", "fram_spi5", "sdcard"};
	std::vector<Result> results;
	assert(board_gpio_test_self_check(record, &results, false));
	assert(results.size() == 10 && fram_ids == 1 && sd_initialisations == 1);
	assert((eeprom_offsets == std::vector<unsigned> {0, 8176, 0, 8176}));
	assert((sd_sectors == std::vector<off_t> {0, 32767}));
	assert((fram_offsets == std::vector<off_t> {0, 32752}));

	for (const char *name : {"fram_bad_continuation", "fram_bad_manufacturer", "fram_fm25v02", "spi_lock_failed"}) {
		fault = name;
		results.clear();
		const size_t prior_reads = fram_offsets.size();
		const unsigned prior_ids = fram_ids;
		const bool expected_pass = fault == "fram_fm25v02";
		assert(fram_read(record, &results) == expected_pass);
		assert(results.size() == 1 && results[0].detail == (expected_pass ? 32768 : 0));
		assert(fram_offsets.size() == prior_reads + (expected_pass ? 2 : 0));
		assert(fram_ids == prior_ids + (fault != "spi_lock_failed"));
		assert(!locked_spi);
	}

	for (const char *name : {"spi_lock_failed", "imu_cs_failed"}) {
		fault = name;
		results.clear();
		assert(!imu_id(record, &results, "icm45686_spi1", 1, GPIO_PORTI | GPIO_PIN9));
		assert(results.size() == 1 && !results[0].passed);
		assert(results[0].detail == (fault == "spi_lock_failed" ? -EBUSY : -EINVAL));
		assert(!locked_spi);
	}

	const std::vector<unsigned> expected_icp_transactions{0xBE, 0xBE, 0x10C, 0x1D3, 0x100};
	assert(icp_transactions == expected_icp_transactions);

	for (const char *name : {"icp_bad_id", "icp_bad_version", "icp_dummy_nack", "icp_revision_a"}) {
		fault = name;
		results.clear();
		const bool revision_a = fault == "icp_revision_a";
		assert(board_gpio_test_self_check(record, &results, false) == revision_a);
		assert(results[2].name == "icp20100_i2c2" && results[2].passed == revision_a);
		assert(results[2].detail == (fault == "icp_bad_id" ? 0x40 : 0x63));
		assert(icp_transactions == expected_icp_transactions);
	}

	for (const char *name : devices) {
		fault = name;
		results.clear();
		const unsigned previous_ids = fram_ids;
		assert(!board_gpio_test_self_check(record, &results, false));
		assert(results.size() == 10 && fram_ids == previous_ids + 1);

		for (const auto &result : results) {
			assert(result.passed == (result.name != fault));

			if (result.name == fault && (fault == "bmm150_i2c4" || fault == "bmp388_i2c4")) {
				assert(result.detail == -ENXIO);
			}
		}
	}

	fault = "bmm_zero_id";
	results.clear();
	assert(!board_gpio_test_self_check(record, &results, false));
	assert(results[0].name == "bmm150_i2c4" && !results[0].passed && results[0].detail == 0);

	fault = "bmp390_id";
	results.clear();
	assert(!board_gpio_test_self_check(record, &results, false));
	assert(results[1].name == "bmp388_i2c4" && !results[1].passed && results[1].detail == 0x60);

	fault.clear();

	for (int error : {-ENXIO, -EADDRNOTAVAIL, -EIO, -ECOMM, 0}) {
		external_result = error;
		results.clear();
		assert(board_gpio_test_self_check(record, &results, true) == (error == -ENXIO || error == -EADDRNOTAVAIL));
		assert(results.size() == 11 && results.back().name == "i2c1_no_attached_devices");
	}

	assert(sd_initialisations == 1);
	return 0;
}
