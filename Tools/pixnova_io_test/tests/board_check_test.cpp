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
static std::string fault;
static unsigned fram_ids = 0;
static unsigned sd_initialisations = 0;
static int external_result = -ENXIO;
static std::vector<unsigned> eeprom_offsets;
static std::vector<off_t> sd_sectors;
static std::vector<off_t> fram_offsets;

struct i2c_master_s { unsigned number; };
struct spi_dev_s { unsigned number; };
static i2c_master_s i2c_buses[] = {{0}, {1}, {2}, {3}, {4}};
static spi_dev_s spi_buses[] = {{0}, {1}, {2}, {3}, {4}, {5}};
struct i2c_msg_s {
	uint32_t frequency;
	uint16_t addr;
	uint16_t flags;
	uint8_t *buffer;
	int length;
};

static hrt_abstime hrt_absolute_time() { return clock_us; }
static int test_usleep(useconds_t duration) { clock_us += duration; return 0; }
static i2c_master_s *px4_i2cbus_initialize(unsigned n) { return &i2c_buses[n]; }
static int px4_i2cbus_uninitialize(i2c_master_s *) { return 0; }
static spi_dev_s *px4_spibus_initialize(unsigned n) { return &spi_buses[n]; }
static int px4_arch_configgpio(uint32_t) { return 0; }
static void px4_arch_gpiowrite(uint32_t, bool) {}
static void SPI_LOCK(spi_dev_s *, bool) {}
static void SPI_SETFREQUENCY(spi_dev_s *, unsigned) {}
static void SPI_SETMODE(spi_dev_s *, unsigned) {}
static void SPI_SETBITS(spi_dev_s *, unsigned) {}
static void SPI_SELECT(spi_dev_s *, unsigned device, bool) { assert(device == SPIDEV_FLASH(0)); }
static void SPI_SEND(spi_dev_s *bus, unsigned command)
{
	assert(bus->number == 5 && command == 0x9F);
	fram_ids++;
}
static void SPI_RECVBLOCK(spi_dev_s *, void *data, size_t count)
{
	assert(count == 9);
	auto *identity = static_cast<uint8_t *>(data);
	memset(identity, 0xFF, count);

	if (fault != "fram_spi5") {
		identity[7] = 0x22;
		identity[8] = 0x08;
	}
}
static void SPI_EXCHANGE(spi_dev_s *bus, const void *command, void *data, size_t count)
{
	assert(count == 2 && static_cast<const uint8_t *>(command)[0] == (0x72 | 0x80));
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

	if (count == 1) {
		assert(address == 0x10 && messages[0].length == 2);
		assert(messages[0].buffer[0] == 0x4B && messages[0].buffer[1] == 1);
		return fault == name ? -ENXIO : 0;
	}

	assert(count == 2 && messages[1].flags == I2C_M_READ && messages[1].addr == address);

	if (address == 0x51 || address == 0x50) {
		assert(bus->number == (address == 0x51 ? 3u : 4u));
		assert(messages[0].length == 2 && messages[1].length == 16);
		eeprom_offsets.push_back((messages[0].buffer[0] << 8) | messages[0].buffer[1]);
	}

	memset(messages[1].buffer, address == 0x10 ? 0x32 : address == 0x76 ? 0x50 : 0x63, messages[1].length);
	return fault == name ? -ENXIO : 0;
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

	for (const char *name : devices) {
		fault = name;
		results.clear();
		const unsigned previous_ids = fram_ids;
		assert(!board_gpio_test_self_check(record, &results, false));
		assert(results.size() == 10 && fram_ids == previous_ids + 1);

		for (const auto &result : results) {
			assert(result.passed == (result.name != fault));
		}
	}

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
