// SPDX-License-Identifier: BSD-3-Clause
// Dedicated bench adapter. Never linked into the flight or GPIO-only images.
#include "board_config.h"
#include "protocol_test.hpp"
#include <px4_platform_common/i2c.h>
#include <px4_platform/gpio.h>
#include <nuttx/irq.h>
#include <nuttx/i2c/i2c_master.h>
#include <arch/board/board.h>
#include <hardware/stm32_i2c.h>
#include <hardware/stm32_uart.h>
#include <hardware/stm32_rcc.h>
#include "arm_internal.h"
#include <errno.h>
#include <unistd.h>

#if defined(CONFIG_GPIO_TEST_RECEIVER) && defined(CONFIG_ARCH_HIPRI_INTERRUPT)
extern "C" {
#include "ram_vectors.h"
}
#include <arch/armv7-m/nvicpri.h>
#include <hardware/stm32_exti.h>
#include <hardware/stm32_gpio.h>
#include <hardware/stm32_tim.h>
#if HRT_TIMER != 8
#error "SBUS fast capture requires the PixNova 1 MHz HRT on TIM8"
#endif
ProtocolTest *ProtocolTest::_sbus_owner = nullptr;
#endif

#ifndef CONFIG_GPIO_TEST_RECEIVER
#include <drivers/drv_adc.h>
#include <px4_arch/adc.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <nuttx/net/mii.h>
#include <nuttx/net/ioctl.h>
#include <nuttx/can.h>
#include <hardware/stm32_ethernet.h>
#include <hardware/stm32_fdcan.h>
#include <fcntl.h>
#include "stm32_fdcan_sock.h"
#include "stm32_ethernet.h"
#include <modules/px4iofirmware/protocol.h>
#include "io_pwm_policy.hpp"
#endif

using protocol_test::Mode;
namespace
{
constexpr hrt_abstime LEASE_US = 2000000;
constexpr hrt_abstime REPORT_US = 1000000;
constexpr hrt_abstime STEP_US = 100000;
constexpr uint32_t INPUT = GPIO_INPUT | GPIO_FLOAT;
constexpr uint32_t SIM_TX = GPIO_PORTE | GPIO_PIN1;
constexpr uint32_t IO_TX = GPIO_PORTC | GPIO_PIN6;
constexpr uint32_t IO_RX = GPIO_PORTC | GPIO_PIN7;
constexpr uint32_t SBUS_OUT_RECEIVE = GPIO_PORTG | GPIO_PIN5; // Fixture P8.2, flight-controller SBUS OUT.
constexpr uint32_t ADC6V6_STIMULUS = GPIO_PORTF | GPIO_PIN13;
constexpr uint32_t ADC3V3_STIMULUS = GPIO_PORTG | GPIO_PIN10;
constexpr uint32_t ADC6V6_INPUT = GPIO_PORTC | GPIO_PIN2;
constexpr uint32_t ADC3V3_INPUT = GPIO_PORTC | GPIO_PIN3;
constexpr unsigned ADC6V6_CHANNEL = 12;
constexpr unsigned ADC3V3_CHANNEL = 13;
constexpr uint32_t IO_RECEIVE_PINS[] {
	GPIO_PORTI | GPIO_PIN0, GPIO_PORTH | GPIO_PIN12, GPIO_PORTH | GPIO_PIN11, GPIO_PORTH | GPIO_PIN10,
	GPIO_PORTD | GPIO_PIN13, GPIO_PORTD | GPIO_PIN14, GPIO_PORTH | GPIO_PIN6, GPIO_PORTH | GPIO_PIN9
}; // Fixture P21 pins 8..1; manually switch the complete harness after FMU PWM.

#ifdef CONFIG_GPIO_TEST_RECEIVER
uint32_t adc_stimulus(Mode mode)
{
	return protocol_test::is_adc_3v3(mode) ? ADC3V3_STIMULUS : ADC6V6_STIMULUS;
}
#endif

#ifndef CONFIG_GPIO_TEST_RECEIVER
int interface_flags(int fd, const char *name, bool up)
{
	ifreq request{};
	strncpy(request.ifr_name, name, IFNAMSIZ - 1);

	if (ioctl(fd, SIOCGIFFLAGS, reinterpret_cast<unsigned long>(&request)) < 0) { return -errno; }

	request.ifr_flags = up ? request.ifr_flags | IFF_UP : request.ifr_flags & ~IFF_UP;
	return ioctl(fd, SIOCSIFFLAGS, reinterpret_cast<unsigned long>(&request)) < 0 ? -errno : 0;
}

int can_receive_fifo0(uintptr_t base)
{
	// The socket driver enables filter lists without initializing their RAM.
	// This bench validates IDs/data in software. Disable both lists so retained
	// or uninitialized entries cannot reject packets or route them to FIFO1.
	constexpr hrt_abstime CONFIGURE_TIMEOUT_US = 10000;
	const hrt_abstime deadline = hrt_absolute_time() + CONFIGURE_TIMEOUT_US;
	modifyreg32(base + STM32_FDCAN_CCCR_OFFSET, 0, FDCAN_CCCR_INIT);

	while (!(getreg32(base + STM32_FDCAN_CCCR_OFFSET) & FDCAN_CCCR_INIT)) {
		if (hrt_absolute_time() >= deadline) { return -ETIMEDOUT; }

		usleep(100);
	}

	modifyreg32(base + STM32_FDCAN_CCCR_OFFSET, 0, FDCAN_CCCR_CCE);
	modifyreg32(base + STM32_FDCAN_SIDFC_OFFSET, FDCAN_SIDFC_LSS_MASK, 0);
	modifyreg32(base + STM32_FDCAN_XIDFC_OFFSET, FDCAN_XIDFC_LSE_MASK, 0);
	// With no list entries, all frames use FIFO0. Unexpected traffic fails the
	// normal protocol checks rather than silently disappearing in a filter.
	putreg32(0, base + STM32_FDCAN_GFC_OFFSET);
	const bool configured = !(getreg32(base + STM32_FDCAN_SIDFC_OFFSET) & FDCAN_SIDFC_LSS_MASK)
				&& !(getreg32(base + STM32_FDCAN_XIDFC_OFFSET) & FDCAN_XIDFC_LSE_MASK)
				&& getreg32(base + STM32_FDCAN_GFC_OFFSET) == 0;
	modifyreg32(base + STM32_FDCAN_CCCR_OFFSET, FDCAN_CCCR_CCE | FDCAN_CCCR_INIT, 0);

	while (getreg32(base + STM32_FDCAN_CCCR_OFFSET) & FDCAN_CCCR_INIT) {
		if (hrt_absolute_time() >= deadline) { return -ETIMEDOUT; }

		usleep(100);
	}

	return configured ? 0 : -EIO;
}
#endif
}

int ProtocolTest::start(Mode mode, uint32_t token)
{
	if (active() || _io_needs_cleanup || mode == Mode::None) { return -EINVAL; }

#ifdef CONFIG_GPIO_TEST_RECEIVER

	if (mode == Mode::Can || mode == Mode::Ethernet) { return -ENOTSUP; }

#endif
	_mode = mode;
	_token = token;
	_sample = {};
	_sample.detail = -EAGAIN;
	_expired.store(false);
	_starting.store(true);
	_slave_errors = 0;
	_i2c_reading = false;
	_slave_sequence = 0;
	_have_rc_frames = false;

	for (auto &edges : _io_edges) { edges = {}; }

	_io_capture_time = 0;
	_io_timing_errors = 0;
	memset(_can_rx, 0, sizeof(_can_rx));
	memset(_can_sequence, 0, sizeof(_can_sequence));
	memset(_pending, 0, sizeof(_pending));
	_feed = _step = _report = hrt_absolute_time();
	hrt_call_every(&_watchdog, 1000, 1000, watchdog, this);
	int result = mode == Mode::SbusOut ? sbus_out_start() : protocol_test::is_io_pwm(mode) ? io_pwm_start() : protocol_test::is_i2c(
			     mode) ? i2c_start() :
		     protocol_test::is_adc(mode) ? adc_start() :
		     (mode == Mode::Sbus || mode == Mode::Dsm) ? uart_start() : network_start();

	const hrt_abstime startup_limit = mode == Mode::Ethernet ? 20000000 : LEASE_US;

	if (result >= 0 && (_expired.load() || hrt_absolute_time() - _feed > startup_limit)) { result = -ETIMEDOUT; }

	if (result < 0) { stop(); return result; }

	const irqstate_t state = enter_critical_section();
	_feed = hrt_absolute_time();
	_starting.store(false);
	leave_critical_section(state);

	return 0;
}

void ProtocolTest::watchdog(void *context)
{
	auto *self = static_cast<ProtocolTest *>(context);

#if defined(CONFIG_GPIO_TEST_RECEIVER) && defined(CONFIG_ARCH_HIPRI_INTERRUPT)

	if (self->_mode == Mode::SbusOut && self->_sbus_irq_attached) {
		up_disable_irq(STM32_IRQ_EXTI95);
		self->_sbus_clock.advance(getreg16(STM32_TIM8_CNT));
		up_enable_irq(STM32_IRQ_EXTI95);
	}

#endif

	// PHY reset/link negotiation can block inside the network driver. No test
	// datagrams are sent until STARTED; the normal running lease remains 2 s.
	const hrt_abstime lease = self->_starting.load() && self->_mode == Mode::Ethernet ? 20000000 : LEASE_US;

	if (!self->_expired.load() && hrt_absolute_time() - self->_feed > lease) {
		self->_expired.store(true);
		self->disable_outputs();
	}
}

bool ProtocolTest::feed()
{
	const irqstate_t state = enter_critical_section();
	const bool valid = active() && !_expired.load() && hrt_absolute_time() - _feed <= LEASE_US;

	if (valid) { _feed = hrt_absolute_time(); }

	leave_critical_section(state);
	return valid;
}

void ProtocolTest::disable_outputs()
{
	if (protocol_test::is_adc(_mode)) {
#ifdef CONFIG_GPIO_TEST_RECEIVER
		px4_arch_configgpio(adc_stimulus(_mode) | INPUT);
#else
		px4_arch_configgpio((protocol_test::is_adc_3v3(_mode) ? ADC3V3_INPUT : ADC6V6_INPUT) | INPUT);
#endif
	}

	if (_i2c) { putreg32(0, _i2c + STM32_I2C_CR1_OFFSET); }

	if (_gpio_sda) { px4_arch_configgpio(_gpio_sda | INPUT); }

	if (_uart) {
		putreg32(0, _uart + STM32_USART_CR1_OFFSET);
#ifdef CONFIG_GPIO_TEST_RECEIVER
		px4_arch_configgpio(SIM_TX | INPUT);
#else
		px4_arch_configgpio(IO_TX | INPUT);
		px4_arch_configgpio(IO_RX | INPUT);
#endif
	}

#ifndef CONFIG_GPIO_TEST_RECEIVER

	if (_mode == Mode::Can && (_network_up[0] || _network_up[1])) {
		// CCCR.INIT stops transmissions even if the USB task cannot run.
		modifyreg32(STM32_FDCAN1_CCCR, 0, FDCAN_CCCR_INIT_MASK);
		modifyreg32(STM32_FDCAN2_CCCR, 0, FDCAN_CCCR_INIT_MASK);
	}

	if (_mode == Mode::Ethernet && _network_up[0]) { modifyreg32(STM32_ETH_MACCR, ETH_MACCR_TE, 0); }

#endif
}

bool ProtocolTest::stop()
{
	hrt_cancel(&_watchdog);
	hrt_cancel(&_io_sampler);
	_expired.store(true);
	_starting.store(false);
	const bool io_stopped = !_io_needs_cleanup || io_pwm_stop();
	disable_outputs();

	if (_irq_event_attached) { up_disable_irq(_i2c_event); irq_detach(_i2c_event); _irq_event_attached = false; }

	if (_irq_error_attached) { up_disable_irq(_i2c_error); irq_detach(_i2c_error); _irq_error_attached = false; }

#ifdef CONFIG_GPIO_TEST_RECEIVER

	if (_sbus_irq_attached) { sbus_out_stop(); }

	if (_gpio_scl) {
		px4_arch_gpiosetevent(_gpio_scl, false, false, false, nullptr, nullptr);
		px4_arch_gpiosetevent(_gpio_sda, false, false, false, nullptr, nullptr);
		px4_arch_configgpio(_gpio_scl | INPUT); px4_arch_configgpio(_gpio_sda | INPUT);
		_gpio_scl = _gpio_sda = 0;
	}

	if (_i2c) {
		const unsigned bus = protocol_test::fixture_i2c(_mode);
		const uint32_t scl = bus == 1 ? GPIO_PORTB | GPIO_PIN8 : bus == 2 ? GPIO_PORTF | GPIO_PIN1 : bus == 3 ? GPIO_PORTA | GPIO_PIN8 : GPIO_PORTF
				     | GPIO_PIN14;
		const uint32_t sda = bus == 1 ? GPIO_PORTB | GPIO_PIN9 : bus == 2 ? GPIO_PORTF | GPIO_PIN0 : bus == 3 ? GPIO_PORTH | GPIO_PIN8 : GPIO_PORTF
				     | GPIO_PIN15;
		px4_arch_configgpio(scl | INPUT); px4_arch_configgpio(sda | INPUT);
	}

	if (_uart) { px4_arch_configgpio(SIM_TX | INPUT); }

#else

	for (unsigned i = 0; i < 2; ++i) {
		if (_socket[i] >= 0) {
			if (_network_up[i]) { interface_flags(_socket[i], _mode == Mode::Ethernet ? "eth0" : i == 0 ? "can0" : "can1", false); }

			_network_up[i] = false;
			close(_socket[i]); _socket[i] = -1;
		}
	}

#endif
	_i2c = _uart = 0;
	_mode = Mode::None;
	return io_stopped;
}

int ProtocolTest::adc_start()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	const bool high = protocol_test::is_adc_high(_mode);
	const uint32_t pin = adc_stimulus(_mode);
	const int result = px4_arch_configgpio(pin | GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz
					       | (high ? GPIO_OUTPUT_SET : GPIO_OUTPUT_CLEAR));

	if (result >= 0 && px4_arch_gpioread(pin) != high) {
		px4_arch_configgpio(pin | INPUT);
		return -EIO;
	}

	return result;
#else
	const int result = px4_arch_configgpio(protocol_test::is_adc_3v3(_mode) ? GPIO_ADC12_INP13 : GPIO_ADC123_INP12);

	if (result < 0) { return result; }

	modifyreg32(STM32_RCC_AHB1ENR, 0, RCC_AHB1ENR_ADC12EN);
	return px4_arch_adc_init(SYSTEM_ADC_BASE);
#endif
}

void ProtocolTest::adc_step()
{
	_sample.tx++;
#ifdef CONFIG_GPIO_TEST_RECEIVER
	const bool high = px4_arch_gpioread(adc_stimulus(_mode));
	_sample.detail = high ? 3300 : 0;
	const bool valid = high == protocol_test::is_adc_high(_mode);
#else
	const unsigned channel = protocol_test::is_adc_3v3(_mode) ? ADC3V3_CHANNEL : ADC6V6_CHANNEL;
	const uint32_t raw = px4_arch_adc_sample(SYSTEM_ADC_BASE, channel);
	const uint32_t full = px4_arch_adc_dn_fullcount();
	const uint32_t reference_mv = static_cast<uint32_t>(px4_arch_adc_reference_v() * 1000.0f + 0.5f);
	_sample.detail = raw != UINT32_MAX && full > 0 ? static_cast<int>((uint64_t(raw) * reference_mv) / full) : -EIO;
	const bool valid = protocol_test::adc_voltage_valid(_mode, _sample.detail);
#endif

	if (valid) { _sample.rx++; }

	else {
		_sample.errors++;
#ifdef CONFIG_GPIO_TEST_RECEIVER
		disable_outputs();
#endif
	}

	_sample.passed = _sample.rx >= 10 && _sample.errors == 0;
}

int ProtocolTest::i2c_start()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	const unsigned bus = protocol_test::fixture_i2c(_mode);

	if (!bus) {
		// GPS1: measured PB8 SCL reaches PH3/P19.7, PB9 SDA reaches PH4/P19.6.
		// Shared UART4/I2C3: P17.4/PG1 SCL and P17.3/PG2 SDA.
		_gpio_scl = _mode == Mode::Gps1 ? GPIO_PORTH | GPIO_PIN3 : GPIO_PORTG | GPIO_PIN1;
		_gpio_sda = _mode == Mode::Gps1 ? GPIO_PORTH | GPIO_PIN4 : GPIO_PORTG | GPIO_PIN2;
		_gpio_i2c.start(_mode, _token);

		if (px4_arch_configgpio(_gpio_scl | INPUT) < 0
		    || px4_arch_configgpio(_gpio_sda | GPIO_OUTPUT | GPIO_OPENDRAIN | GPIO_OUTPUT_SET | GPIO_SPEED_2MHz) < 0) { return -EIO; }

		if (!px4_arch_gpioread(_gpio_scl) || !px4_arch_gpioread(_gpio_sda)) { return -EBUSY; }

		int result = px4_arch_gpiosetevent(_gpio_scl, true, true, false, gpio_i2c_irq, this);

		if (result < 0) { return result; }

		// Input mode is required to route EXTI to this GPIO port. The event
		// helper also reconfigures MODER, so restore open-drain output after
		// attaching the interrupt. The input buffer still observes SDA edges.
		result = px4_arch_gpiosetevent(_gpio_sda | INPUT, true, true, false, gpio_i2c_irq, this);

		if (result < 0) { return result; }

		return px4_arch_configgpio(_gpio_sda | GPIO_OUTPUT | GPIO_OPENDRAIN | GPIO_OUTPUT_SET | GPIO_SPEED_2MHz);
	}

	_i2c = bus == 1 ? STM32_I2C1_BASE : bus == 2 ? STM32_I2C2_BASE : bus == 3 ? STM32_I2C3_BASE : STM32_I2C4_BASE;
	_i2c_event = bus == 1 ? STM32_IRQ_I2C1EV : bus == 2 ? STM32_IRQ_I2C2EV : bus == 3 ? STM32_IRQ_I2C3EV : STM32_IRQ_I2C4EV;
	_i2c_error = bus == 1 ? STM32_IRQ_I2C1ER : bus == 2 ? STM32_IRQ_I2C2ER : bus == 3 ? STM32_IRQ_I2C3ER : STM32_IRQ_I2C4ER;
	const uint32_t clock = bus == 1 ? RCC_APB1LENR_I2C1EN : bus == 2 ? RCC_APB1LENR_I2C2EN : bus == 3 ? RCC_APB1LENR_I2C3EN :
			       RCC_APB4ENR_I2C4EN;
	modifyreg32(bus == 4 ? STM32_RCC_APB4ENR : STM32_RCC_APB1LENR, 0, clock);
	putreg32(0, _i2c + STM32_I2C_CR1_OFFSET);
	const uint32_t scl = bus == 1 ? GPIO_I2C1_SCL : bus == 2 ? GPIO_I2C2_SCL : bus == 3 ? GPIO_I2C3_SCL : GPIO_I2C4_SCL;
	const uint32_t sda = bus == 1 ? GPIO_I2C1_SDA : bus == 2 ? GPIO_I2C2_SDA : bus == 3 ? GPIO_I2C3_SDA : GPIO_I2C4_SDA;

	if (px4_arch_configgpio(scl) < 0 || px4_arch_configgpio(sda) < 0) { return -EIO; }

	int result = irq_attach(_i2c_event, i2c_irq, this);

	if (result < 0) { return result; }

	_irq_event_attached = true;
	result = irq_attach(_i2c_error, i2c_irq, this);

	if (result < 0) { return result; }

	_irq_error_attached = true;
	_i2c_sim.start(_mode, _token);
	putreg32(0, _i2c + STM32_I2C_CR2_OFFSET);
	putreg32(0, _i2c + STM32_I2C_OAR2_OFFSET);
	putreg32(0, _i2c + STM32_I2C_TIMINGR_OFFSET);
	putreg32((protocol_test::I2C_ADDRESS << I2C_OAR1_OA1_7_SHIFT) | I2C_OAR1_OA1EN, _i2c + STM32_I2C_OAR1_OFFSET);
	putreg32(I2C_ICR_CLEARMASK, _i2c + STM32_I2C_ICR_OFFSET);
	putreg32(I2C_CR1_PE | I2C_CR1_RXIE | I2C_CR1_TXIE | I2C_CR1_ADDRIE | I2C_CR1_STOPIE | I2C_CR1_NACKIE | I2C_CR1_ERRIE,
		 _i2c + STM32_I2C_CR1_OFFSET);
	up_enable_irq(_i2c_event); up_enable_irq(_i2c_error);
#endif
	return 0;
}

int ProtocolTest::gpio_i2c_irq(int, void *, void *context)
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	auto *self = static_cast<ProtocolTest *>(context);

	if (!self->_expired.load() && self->_gpio_scl) {
		const bool release = self->_gpio_i2c.edge(px4_arch_gpioread(self->_gpio_scl), px4_arch_gpioread(self->_gpio_sda), hrt_absolute_time());

		if (!self->_expired.load()) { px4_arch_gpiowrite(self->_gpio_sda, release); }
	}

#else
	(void)context;
#endif
	return 0;
}

int ProtocolTest::i2c_irq(int, void *, void *context)
{
	auto *self = static_cast<ProtocolTest *>(context);

	if (self->_expired.load()) { return 0; }

	const uintptr_t base = self->_i2c;
	const uint32_t status = getreg32(base + STM32_I2C_ISR_OFFSET);
	const hrt_abstime now = hrt_absolute_time();

	if (status & I2C_ISR_ERRORMASK) {
		self->_slave_errors++;
		putreg32(status & I2C_ISR_ERRORMASK, base + STM32_I2C_ICR_OFFSET);
	}

// Drain RXDR before a repeated START switches the slave to transmit mode.
	if (status & I2C_ISR_RXNE) { self->_i2c_sim.write(getreg32(base + STM32_I2C_RXDR_OFFSET), now); }

	if (status & I2C_INT_ADDR) {
		putreg32(I2C_ISR_TXE, base + STM32_I2C_ISR_OFFSET);
		self->_i2c_reading = status & I2C_ISR_DIR;

		if (self->_i2c_reading) {
			self->_i2c_sim.begin_read(now);
			putreg32(self->_i2c_sim.read(now), base + STM32_I2C_TXDR_OFFSET);

		} else { self->_i2c_sim.begin_write(); }

		putreg32(I2C_INT_ADDR, base + STM32_I2C_ICR_OFFSET);
	}

	if ((status & I2C_ISR_TXIS) && !(status & (I2C_INT_ADDR | I2C_INT_NACK | I2C_INT_STOP))) {
		putreg32(self->_i2c_sim.read(now), base + STM32_I2C_TXDR_OFFSET);
	}

	if (status & I2C_INT_NACK) { putreg32(I2C_INT_NACK, base + STM32_I2C_ICR_OFFSET); }

	if (status & I2C_INT_STOP) {
		if (self->_i2c_reading) { self->_i2c_sim.end_read(); }

		else { self->_i2c_sim.end_write(); }

		putreg32(I2C_INT_STOP, base + STM32_I2C_ICR_OFFSET);
	}

	return 0;
}

void ProtocolTest::i2c_step()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	const irqstate_t state = enter_critical_section();
	auto &sim = _gpio_scl ? _gpio_i2c.simulator() : _i2c_sim;
	_sample.tx = sim.transmitted(); _sample.rx = sim.received(); _sample.errors = _slave_errors + sim.errors();
	_sample.detail = _sample.rx ? 0 : -EAGAIN;
	leave_critical_section(state);
#else
	auto *bus = px4_i2cbus_initialize(protocol_test::source_i2c(_mode));

	if (!bus) { _sample.errors++; _sample.detail = -ENODEV; return; }

	uint8_t request[protocol_test::PACKET_SIZE], response[protocol_test::PACKET_SIZE] {};
	const uint32_t sequence = ++_slave_sequence;
	protocol_test::packet(request, _mode, _token, sequence, false);
	i2c_msg_s messages[2] {};
	const uint32_t frequency = protocol_test::fixture_i2c(_mode) ? 100000 : 10000;
	messages[0].frequency = frequency; messages[0].addr = protocol_test::I2C_ADDRESS; messages[0].buffer = request;
	messages[0].length = sizeof(request);
	messages[1] = messages[0]; messages[1].flags = I2C_M_READ; messages[1].buffer = response; messages[1].length = sizeof(response);
	int result = I2C_TRANSFER(bus, messages, 2);

	if (result >= 0) {
		_sample.tx++;

		if (protocol_test::valid_packet(response, sizeof(response), _mode, _token, sequence, true)) { _sample.rx++; }

		else { result = -EBADMSG; }
	}

	px4_i2cbus_uninitialize(bus);
	_sample.detail = result;

	if (result < 0) { _sample.errors++; }

#endif
	_sample.passed = _sample.tx >= 10 && _sample.rx >= 10 && _sample.errors == 0;
}

int ProtocolTest::uart_start()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	_uart = STM32_UART8_BASE;
	modifyreg32(STM32_RCC_APB1LENR, 0, RCC_APB1LENR_UART8EN);
	putreg32(0, _uart + STM32_USART_CR1_OFFSET);
	const bool sbus = _mode == Mode::Sbus;
	putreg32(sbus ? USART_CR2_TXINV | USART_CR2_STOP2 : 0, _uart + STM32_USART_CR2_OFFSET);
	putreg32(0, _uart + STM32_USART_CR3_OFFSET);
	putreg32((STM32_PCLK1_FREQUENCY + (sbus ? 50000 : 57600)) / (sbus ? 100000 : 115200), _uart + STM32_USART_BRR_OFFSET);
	putreg32(0, _uart + STM32_USART_PRESC_OFFSET);

	if (px4_arch_configgpio(GPIO_UART8_TX) < 0) { return -EIO; }

	putreg32(USART_CR1_TE | USART_CR1_UE | (sbus ? USART_CR1_M0 | USART_CR1_PCE : 0), _uart + STM32_USART_CR1_OFFSET);
#else
	_uart = STM32_USART6_BASE;
	modifyreg32(STM32_RCC_APB2ENR, 0, RCC_APB2ENR_USART6EN);
	putreg32(0, _uart + STM32_USART_CR1_OFFSET);
	putreg32(0, _uart + STM32_USART_CR2_OFFSET); putreg32(0, _uart + STM32_USART_CR3_OFFSET);
	putreg32((STM32_PCLK2_FREQUENCY + 750000) / 1500000, _uart + STM32_USART_BRR_OFFSET);
	putreg32(0, _uart + STM32_USART_PRESC_OFFSET);

	if (px4_arch_configgpio(GPIO_USART6_TX) < 0 || px4_arch_configgpio(GPIO_USART6_RX) < 0) { return -EIO; }

	putreg32(USART_CR1_TE | USART_CR1_RE | USART_CR1_FIFOEN | USART_CR1_UE, _uart + STM32_USART_CR1_OFFSET);
#endif
	return 0;
}

bool ProtocolTest::uart_write(const uint8_t *data, unsigned length)
{
	const hrt_abstime deadline = hrt_absolute_time() + 10000;

	for (unsigned i = 0; i < length; ++i) {
		while (!(getreg32(_uart + STM32_USART_ISR_OFFSET) & USART_ISR_TXE)) {
			if ((!_io_cleaning && _expired.load()) || hrt_absolute_time() > deadline) { return false; }
		}

		if (!_io_cleaning && _expired.load()) { return false; }

		putreg32(data[i], _uart + STM32_USART_TDR_OFFSET);
	}

	while (!(getreg32(_uart + STM32_USART_ISR_OFFSET) & USART_ISR_TC)) {
		if ((!_io_cleaning && _expired.load()) || hrt_absolute_time() > deadline) { return false; }
	}

	return true;
}

bool ProtocolTest::io_read(uint8_t page, uint8_t offset, uint16_t *data, unsigned count)
{
#ifndef CONFIG_GPIO_TEST_RECEIVER
	const bool pwm_page = (protocol_test::is_io_pwm(_mode) || _mode == Mode::SbusOut || _io_cleaning) &&
			      (page == PX4IO_PAGE_SETUP || page == PX4IO_PAGE_SERVOS || page == PX4IO_PAGE_DIRECT_PWM || page == PX4IO_PAGE_DISARMED_PWM);

	if (!pwm_page && page != PX4IO_PAGE_CONFIG && page != PX4IO_PAGE_STATUS && page != PX4IO_PAGE_RAW_RC_INPUT) { return false; }

	return io_transfer(page, offset, data, count, false);
#else
	(void)page; (void)offset; (void)data; (void)count;
	return false;
#endif
}

bool ProtocolTest::io_transfer(uint8_t page, uint8_t offset, uint16_t *data, unsigned count, bool write)
{
#ifndef CONFIG_GPIO_TEST_RECEIVER

	if (count > PKT_MAX_REGS || count == 0) { return false; }

	IOPacket packet{};
	packet.count_code = count | (write ? PKT_CODE_WRITE : PKT_CODE_READ); packet.page = page; packet.offset = offset;

	if (write) { memcpy(packet.regs, data, count * sizeof(uint16_t)); }

	packet.crc = crc_packet(&packet);

	while (getreg32(_uart + STM32_USART_ISR_OFFSET) & USART_ISR_RXNE) { (void)getreg32(_uart + STM32_USART_RDR_OFFSET); }

	putreg32(0xffffffff, _uart + STM32_USART_ICR_OFFSET);
	const unsigned length = PKT_SIZE(packet);

	if (!uart_write(reinterpret_cast<uint8_t *>(&packet), length)) { return false; }

	unsigned received = 0;
	const hrt_abstime deadline = hrt_absolute_time() + 10000;
	auto *bytes = reinterpret_cast<uint8_t *>(&packet);

	const unsigned reply_length = write ? 4 : length;

	while (received < reply_length && (_io_cleaning || !_expired.load()) && hrt_absolute_time() < deadline) {
		const uint32_t status = getreg32(_uart + STM32_USART_ISR_OFFSET);

		if (status & (USART_ISR_ORE | USART_ISR_FE | USART_ISR_NE | USART_ISR_PE)) { return false; }

		if (status & USART_ISR_RXNE) { bytes[received++] = getreg32(_uart + STM32_USART_RDR_OFFSET); }
	}

	const uint8_t crc = packet.crc; packet.crc = 0;

	if (received != reply_length || PKT_COUNT(packet) != (write ? 0 : count) || PKT_CODE(packet) != PKT_CODE_SUCCESS
	    || packet.page != page || packet.offset != offset || crc_packet(&packet) != crc) { return false; }

	if (!write) { memcpy(data, packet.regs, count * sizeof(uint16_t)); }

	return true;
#else
	(void)page; (void)offset; (void)data; (void)count; (void)write;
	return false;
#endif
}

bool ProtocolTest::io_write(uint8_t page, uint8_t offset, uint16_t *data, unsigned count)
{
#ifndef CONFIG_GPIO_TEST_RECEIVER
	return (protocol_test::io_pwm_write_allowed(_mode, _token, _io_cleaning, _io_needs_cleanup, _io_saved_rates, page, offset, data, count)
		|| protocol_test::io_sbus_write_allowed(_io_sbus_cleanup ? Mode::SbusOut : _mode, _io_cleaning, _io_needs_cleanup, _io_saved_features,
				_io_saved_sbus_rate, page, offset, data, count))
	       && io_transfer(page, offset, data, count, true);
#else
	(void)page; (void)offset; (void)data; (void)count;
	return false;
#endif
}

int ProtocolTest::io_pwm_start()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER

	for (const uint32_t pin : IO_RECEIVE_PINS) {
		if (px4_arch_configgpio(pin | INPUT) < 0) { return -EIO; }
	}

	hrt_call_every(&_io_sampler, 50, 50, io_pwm_capture, this);
	return 0;
#else

	if (uart_start() < 0) { return -EIO; }

	uint16_t version{}, actuators{}, flags{}, arming{}, features{}, disarmed[8] {};

	if (!io_read(PX4IO_PAGE_CONFIG, PX4IO_P_CONFIG_PROTOCOL_VERSION, &version, 1) || version != PX4IO_PROTOCOL_VERSION ||
	    !io_read(PX4IO_PAGE_CONFIG, PX4IO_P_CONFIG_ACTUATOR_COUNT, &actuators, 1) || actuators != 8 ||
	    !io_read(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) ||
	    !io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_ARMING, &arming, 1) || arming != 0 ||
	    !io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_FEATURES, &features, 1) ||
	    (features & ~PX4IO_P_SETUP_FEATURES_ADC_RSSI) != 0 ||
	    flags & (PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED | PX4IO_P_STATUS_FLAGS_ARM_SYNC | PX4IO_P_STATUS_FLAGS_RAW_PWM) ||
	    !io_read(PX4IO_PAGE_DISARMED_PWM, 0, disarmed, 8) ||
	    !io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_PWM_RATES, _io_saved_rates, 3)) { return -EACCES; }

	for (const auto value : disarmed) { if (value != 0) { return -EACCES; } }

	if (_mode == Mode::SbusOut && !io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_SBUS_RATE, &_io_saved_sbus_rate, 1)) { return -EIO; }

	_io_saved_features = features;
	_io_sbus_cleanup = _mode == Mode::SbusOut;
	_io_needs_cleanup = true;
	uint16_t rates[] {0, static_cast<uint16_t>(protocol_test::io_pwm_hz(_mode)), 200};

	if (!io_write(PX4IO_PAGE_DISARMED_PWM, 0, disarmed, 8)) { return -EIO; }

	if (_mode == Mode::SbusOut) {
		uint16_t sbus_rate = protocol_test::SBUS_OUT_RATE, sbus_features = PX4IO_P_SETUP_FEATURES_SBUS1_OUT;

		if (!io_write(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_SBUS_RATE, &sbus_rate, 1) ||
		    !io_write(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_FEATURES, &sbus_features, 1)) { return -EIO; }

	} else if (!io_write(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_PWM_RATES, rates, 3)) { return -EIO; }

	// Only io_pwm_step enables a selected channel after the simulator is ready.
	return 0;
#endif
}

void ProtocolTest::io_pwm_capture(void *context)
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	auto *self = static_cast<ProtocolTest *>(context);

	if (self->_expired.load()) { return; }

	const hrt_abstime now = hrt_absolute_time();

	if (self->_io_capture_time && now - self->_io_capture_time > 150) { self->_io_timing_errors++; }

	self->_io_capture_time = now;

	for (unsigned i = 0; i < 8; ++i) { self->_io_edges[i].sample(now, px4_arch_gpioread(IO_RECEIVE_PINS[i])); }

#else
	(void)context;
#endif
}

void ProtocolTest::io_pwm_step()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	const irqstate_t state = enter_critical_section();
	const unsigned selected = protocol_test::io_pwm_channel(_mode);
	const GpioTestEdges edges = _io_edges[selected];
	const uint32_t timing = _io_timing_errors;
	bool isolated = true;

	for (unsigned i = 0; i < 8; ++i) { isolated = isolated && (i == selected || _io_edges[i].edges == 0); }

	leave_critical_section(state);
	_sample.tx++;
	_sample.detail = edges.frequency_millihz();
	const unsigned expected = protocol_test::io_pwm_hz(_mode) * 1000;
	const bool fresh = edges.last_us && hrt_absolute_time() - edges.last_us < 3000000 / protocol_test::io_pwm_hz(_mode);
	const bool good = edges.edges >= 10 && fresh && isolated && timing == 0 &&
			  static_cast<unsigned>(_sample.detail) >= expected * 98 / 100 && static_cast<unsigned>(_sample.detail) <= expected * 102 / 100;

	if (!isolated || timing) { _sample.errors++; }

	if (good) { _sample.rx++; }

	_sample.passed = good && _sample.rx >= 10;
#else
	uint16_t pwm[8] {}, observed[8] {}, applied[8] {}, flags {}, rates[3] {};
	pwm[protocol_test::io_pwm_channel(_mode)] = protocol_test::io_pwm_width(_token);
	_sample.tx++;
	const bool transferred = io_write(PX4IO_PAGE_DIRECT_PWM, 0, pwm, 8) &&
				 io_read(PX4IO_PAGE_DIRECT_PWM, 0, observed, 8) && memcmp(pwm, observed, sizeof(pwm)) == 0 &&
				 io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_PWM_RATES, rates, 3) && rates[0] == 0 && rates[1] == protocol_test::io_pwm_hz(_mode) &&
				 io_read(PX4IO_PAGE_SERVOS, 0, applied, 8) &&
				 io_read(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) && !(flags & PX4IO_P_STATUS_FLAGS_FAILSAFE);
	const uint16_t running = PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED | PX4IO_P_STATUS_FLAGS_FMU_OK | PX4IO_P_STATUS_FLAGS_RAW_PWM |
				 PX4IO_P_STATUS_FLAGS_INIT_OK;
	const bool good = transferred && (flags & running) == running && memcmp(pwm, applied, sizeof(pwm)) == 0;
	bool initial_zero = _sample.tx == 1 && transferred;

	for (const auto value : applied) { initial_zero = initial_zero && value == 0; }

	_sample.detail = good ? protocol_test::io_pwm_hz(_mode) * 1000 : initial_zero ? -EAGAIN : -EIO;

	// The first write can precede IO's next mixer tick. Later reads must prove
	// actual servo values and running state, not only direct-command storage.

	if (good) { _sample.rx++; } else if (!initial_zero) { _sample.errors++; }

	_sample.passed = good && _sample.rx >= 10 && _sample.errors == 0;
#endif
}

bool ProtocolTest::io_pwm_stop()
{
#ifndef CONFIG_GPIO_TEST_RECEIVER
	_io_cleaning = true;
	bool ok = uart_start() >= 0;

	if (_io_sbus_cleanup) {
		uint16_t features{}, sbus_rate{};
		ok = io_write(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_FEATURES, &_io_saved_features, 1) && ok;
		ok = io_write(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_SBUS_RATE, &_io_saved_sbus_rate, 1) && ok;
		ok = io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_FEATURES, &features, 1) && features == _io_saved_features && ok;
		ok = io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_SBUS_RATE, &sbus_rate, 1) && sbus_rate == _io_saved_sbus_rate && ok;
	}

	uint16_t zeros[8] {}, flags{};
	ok = io_write(PX4IO_PAGE_DIRECT_PWM, 0, zeros, 8) && ok;
	ok = io_write(PX4IO_PAGE_DISARMED_PWM, 0, zeros, 8) && ok;

	if (io_read(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) && !(flags & PX4IO_P_STATUS_FLAGS_ARM_SYNC)) {
		flags &= ~(PX4IO_P_STATUS_FLAGS_RAW_PWM | PX4IO_P_STATUS_FLAGS_INIT_OK | PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED | PX4IO_P_STATUS_FLAGS_FMU_OK);
		ok = io_write(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) && ok;

	} else { ok = false; }

	ok = io_write(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_PWM_RATES, _io_saved_rates, 3) && ok;
	const hrt_abstime deadline = hrt_absolute_time() + 100000;
	bool confirmed = false;

	do {
		usleep(2000); // The IO mixer runs independently of the FMU.
		uint16_t observed[8] {}, restored_rates[3] {};
		confirmed = io_read(PX4IO_PAGE_SERVOS, 0, observed, 8) && memcmp(zeros, observed, sizeof(zeros)) == 0 &&
			    io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_PWM_RATES, restored_rates, 3) &&
			    memcmp(restored_rates, _io_saved_rates, sizeof(restored_rates)) == 0 &&
			    io_read(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) && !(flags & PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED);
	} while (!confirmed && hrt_absolute_time() < deadline);

	ok = ok && confirmed;
	_io_cleaning = false;
	_io_needs_cleanup = !ok;

	if (ok) { _io_sbus_cleanup = false; }

	return ok;
#else
	return true;
#endif
}

int ProtocolTest::sbus_out_start()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER

	if (px4_arch_configgpio(SBUS_OUT_RECEIVE | INPUT | GPIO_EXTI) < 0) { return -EIO; }

	const irqstate_t state = enter_critical_section();
#ifdef CONFIG_ARCH_HIPRI_INTERRUPT
	constexpr uint32_t other_lines = (0x1fu << 5) & ~(1u << 5);

	if (_sbus_owner || (getreg32(STM32_EXTI_CPUIMR1) & other_lines)) {
		leave_critical_section(state);
		return -EBUSY;
	}

#endif
	_sbus_capture.reset(hrt_absolute_time(), px4_arch_gpioread(SBUS_OUT_RECEIVE));
	int result = px4_arch_gpiosetevent(SBUS_OUT_RECEIVE, true, true, false, sbus_out_irq, this);
	_sbus_irq_attached = result >= 0;
#ifdef CONFIG_ARCH_HIPRI_INTERRUPT

	// This direct vector owns the shared EXTI5..9 IRQ only during SBUS OUT.
	// Other connector simulators must have released their EXTI lines first.
	if (result >= 0) {
		up_disable_irq(STM32_IRQ_EXTI95);
		_sbus_clock.reset(hrt_absolute_time(), getreg16(STM32_TIM8_CNT));
		_sbus_owner = this;
		result = arm_ramvec_attach(STM32_IRQ_EXTI95, sbus_out_fast_irq);

		if (result >= 0) { result = up_prioritize_irq(STM32_IRQ_EXTI95, NVIC_SYSH_HIGH_PRIORITY); }

		if (result >= 0) { up_enable_irq(STM32_IRQ_EXTI95); }
	}

#endif
	leave_critical_section(state);
	return result;
#else
	return io_pwm_start();
#endif
}

void ProtocolTest::sbus_out_stop()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
#ifdef CONFIG_ARCH_HIPRI_INTERRUPT
	up_disable_irq(STM32_IRQ_EXTI95);
	_sbus_owner = nullptr;
	arm_ramvec_attach(STM32_IRQ_EXTI95, nullptr);
	up_prioritize_irq(STM32_IRQ_EXTI95, NVIC_SYSH_PRIORITY_DEFAULT);
#endif
	px4_arch_gpiosetevent(SBUS_OUT_RECEIVE, false, false, false, nullptr, nullptr);
	_sbus_irq_attached = false;
	px4_arch_configgpio(SBUS_OUT_RECEIVE | INPUT);
#endif
}

int ProtocolTest::sbus_out_irq(int, void *, void *context)
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	auto *self = static_cast<ProtocolTest *>(context);

	if (!self->_expired.load()) {
		self->_sbus_capture.edge(hrt_absolute_time(), px4_arch_gpioread(SBUS_OUT_RECEIVE));
	}

#else
	(void)context;
#endif
	return 0;
}

void ProtocolTest::sbus_out_fast_irq()
{
#if defined(CONFIG_GPIO_TEST_RECEIVER) && defined(CONFIG_ARCH_HIPRI_INTERRUPT)
	constexpr uint32_t line = 1u << 5;

	if (getreg32(STM32_EXTI_CPUPR1) & line) {
		putreg32(line, STM32_EXTI_CPUPR1);
		auto *self = _sbus_owner;

		// No RTOS, HRT API, USB or floating-point calls from this direct IRQ.
		if (self && !self->_expired.load()) {
			const uint64_t now = self->_sbus_clock.advance(getreg16(STM32_TIM8_CNT));
			self->_sbus_capture.edge(now, (getreg32(STM32_GPIOG_IDR) & line) != 0);
		}
	}

#endif
}

void ProtocolTest::sbus_out_step()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	const irqstate_t state = enter_critical_section();
#ifdef CONFIG_ARCH_HIPRI_INTERRUPT
	// BASEPRI deliberately leaves the fast IRQ enabled; mask its NVIC line
	// explicitly for this short, consistent snapshot and window reset.
	up_disable_irq(STM32_IRQ_EXTI95);
#endif
	const hrt_abstime now = hrt_absolute_time();
	_sbus_capture.flush(now, px4_arch_gpioread(SBUS_OUT_RECEIVE));
	const auto stats = _sbus_capture.statistics();
	_sbus_capture.reset_window();
#ifdef CONFIG_ARCH_HIPRI_INTERRUPT
	up_enable_irq(STM32_IRQ_EXTI95);
#endif
	leave_critical_section(state);
	const uint64_t window_us = now - _report;
	constexpr uint64_t frame_scale = 1000000000ULL; // Frames/us to millihertz.
	// Allow one boundary frame for quantization, but count over the entire
	// report period. A short burst with correct frame spacing cannot pass.
	const bool complete_window = window_us >= REPORT_US &&
				     (uint64_t(stats.window_frames) + 1) * frame_scale >= protocol_test::SBUS_OUT_MIN_MILLIHZ * window_us &&
				     uint64_t(stats.window_frames) * frame_scale <= protocol_test::SBUS_OUT_MAX_MILLIHZ * window_us + frame_scale;
	_sample.tx = stats.frames + stats.errors;
	_sample.rx = stats.frames;
	_sample.errors = stats.errors;
	memcpy(_sample.capture, stats.diagnostic, sizeof(_sample.capture));
	_sample.detail = stats.frequency_millihz;
	_sample.passed = complete_window && stats.last_frame_us &&
			 now - stats.last_frame_us < 100000 && stats.frequency_millihz >= protocol_test::SBUS_OUT_MIN_MILLIHZ &&
			 stats.frequency_millihz <= protocol_test::SBUS_OUT_MAX_MILLIHZ && stats.errors == 0;
#else
	uint16_t zeros[8] {}, observed[8] {}, applied[8] {}, features {}, rate {}, flags {};
	_sample.tx++;
	const bool good = io_write(PX4IO_PAGE_DIRECT_PWM, 0, zeros, 8) &&
			  io_read(PX4IO_PAGE_DIRECT_PWM, 0, observed, 8) && memcmp(zeros, observed, sizeof(zeros)) == 0 &&
			  io_read(PX4IO_PAGE_SERVOS, 0, applied, 8) && memcmp(zeros, applied, sizeof(zeros)) == 0 &&
			  io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_FEATURES, &features, 1) && features == PX4IO_P_SETUP_FEATURES_SBUS1_OUT &&
			  io_read(PX4IO_PAGE_SETUP, PX4IO_P_SETUP_SBUS_RATE, &rate, 1) && rate == protocol_test::SBUS_OUT_RATE &&
			  io_read(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) && !(flags & PX4IO_P_STATUS_FLAGS_FAILSAFE);
	const uint16_t required = PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED | PX4IO_P_STATUS_FLAGS_FMU_OK | PX4IO_P_STATUS_FLAGS_RAW_PWM |
				  PX4IO_P_STATUS_FLAGS_INIT_OK;
	const bool running = good && (flags & required) == required;
	_sample.detail = running ? protocol_test::SBUS_OUT_RATE * 1000 : -EAGAIN;

	if (running) { _sample.rx++; }

	else if (_sample.tx > 1 || !good) { _sample.errors++; _sample.detail = -EIO; }

	_sample.passed = running && _sample.rx >= 10 && _sample.errors == 0;
#endif
}

void ProtocolTest::rc_step()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	uint8_t frame[25];
	const unsigned count = _mode == Mode::Sbus ? protocol_test::sbus(frame, _token) : protocol_test::dsm(frame, _token);

	if (uart_write(frame, count)) { _sample.tx++; _sample.detail = count; }

	else { _sample.errors++; _sample.detail = -EIO; }

	// This is transmission status, never a simulated receiver PASS.
	_sample.passed = false;
#else
	uint16_t version {}, flags {}, raw[6 + protocol_test::RC_CHANNELS] {};

	if (!io_read(PX4IO_PAGE_CONFIG, PX4IO_P_CONFIG_PROTOCOL_VERSION, &version, 1) || version != PX4IO_PROTOCOL_VERSION ||
	    !io_read(PX4IO_PAGE_STATUS, PX4IO_P_STATUS_FLAGS, &flags, 1) ||
	    !io_read(PX4IO_PAGE_RAW_RC_INPUT, 0, raw, sizeof(raw) / sizeof(raw[0]))) {
		_sample.detail = -EIO; _sample.errors++; _sample.passed = false; return;
	}

	_sample.tx++;
	const unsigned type_flag = _mode == Mode::Sbus ? PX4IO_P_STATUS_FLAGS_RC_SBUS : PX4IO_P_STATUS_FLAGS_RC_DSM;
	bool good = (flags & PX4IO_P_STATUS_FLAGS_RC_OK) && (flags & type_flag) && !(flags & PX4IO_P_STATUS_FLAGS_OUTPUTS_ARMED)
		    && raw[PX4IO_P_RAW_RC_COUNT] >= protocol_test::RC_CHANNELS
		    && (raw[PX4IO_P_RAW_RC_FLAGS] & PX4IO_P_RAW_RC_FLAGS_RC_OK)
		    && !(raw[PX4IO_P_RAW_RC_FLAGS] & (PX4IO_P_RAW_RC_FLAGS_FAILSAFE | PX4IO_P_RAW_RC_FLAGS_FRAME_DROP));

	for (unsigned i = 0; i < protocol_test::RC_CHANNELS; ++i) {
		_sample.channels[i] = raw[PX4IO_P_RAW_RC_BASE + i];
		const int difference = int(raw[PX4IO_P_RAW_RC_BASE + i]) - protocol_test::rc_value(_token, i);
		good = good && difference >= -5 && difference <= 5;
	}

	const uint16_t frames = raw[PX4IO_P_RAW_FRAME_COUNT];
	const uint16_t advance = frames - _rc_frames;
	good = good && _have_rc_frames && advance > 0 && advance < 100;
	_rc_frames = frames; _have_rc_frames = true;
	_sample.detail = flags | (uint32_t(raw[PX4IO_P_RAW_RC_FLAGS]) << 16);

	if (good) { _sample.rx++; }

	_sample.passed = good && _sample.rx >= 10 && _sample.errors == 0;
#endif
}

bool ProtocolTest::update(ProtocolSample &sample)
{
	if (!active() || _expired.load()) { return false; }

	const hrt_abstime now = hrt_absolute_time();
	const hrt_abstime interval = _mode == Mode::Sbus || _mode == Mode::Dsm ? 20000 : STEP_US;

	if (now - _step >= interval) {
		_step = now;

		if (protocol_test::is_i2c(_mode)) { i2c_step(); }

		else if (protocol_test::is_adc(_mode)) { adc_step(); }

		else if (protocol_test::is_io_pwm(_mode)) { io_pwm_step(); }

		else if (_mode == Mode::SbusOut) {
#ifndef CONFIG_GPIO_TEST_RECEIVER
			sbus_out_step();
#endif
		}

		else if (_mode == Mode::Sbus || _mode == Mode::Dsm) { rc_step(); }

		else { network_step(); }
	}

	if (_expired.load() || now - _report < REPORT_US) { return false; }

#ifdef CONFIG_GPIO_TEST_RECEIVER

	// Snapshot, validate and reset SBUS reception once per full report period,
	// even if this update is between two regular STEP_US callbacks.
	if (_mode == Mode::SbusOut) { sbus_out_step(); }

#endif
	_report = now; _sample.sequence++; sample = _sample;
#ifdef CONFIG_GPIO_TEST_RECEIVER

	if (protocol_test::is_io_pwm(_mode)) {
		const irqstate_t state = enter_critical_section();

		for (auto &edges : _io_edges) {
			const bool previous = edges.previous;
			const bool initialized = edges.initialized;
			edges = {}; edges.previous = previous; edges.initialized = initialized;
		}

		leave_critical_section(state);
	}

#endif
	return true;
}

int ProtocolTest::network_start()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	return -ENOTSUP;
#else
#if defined(CONFIG_STM32H7_FDCAN_LOOPBACK)
# error "Protocol CAN testing must use two physical external interfaces"
#endif

	if (_mode == Mode::Can) {
		static bool initialized[2] {};

		for (unsigned i = 0; i < 2; ++i) {
			if (!initialized[i]) {
				const int result = stm32_fdcansockinitialize(i);

				if (result < 0) { return result; }

				initialized[i] = true;
			}

			_socket[i] = socket(PF_CAN, SOCK_RAW, CAN_RAW);

			if (_socket[i] < 0) { return -errno; }

			ifreq request{};
			strncpy(request.ifr_name, i == 0 ? "can0" : "can1", IFNAMSIZ - 1);

			if (ioctl(_socket[i], SIOCGIFINDEX, reinterpret_cast<unsigned long>(&request)) < 0) { return -errno; }

			sockaddr_can address{};
			address.can_family = AF_CAN; address.can_ifindex = request.ifr_ifindex;

			if (bind(_socket[i], reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0) { return -errno; }

			const int loopback = 0;
			const can_err_mask_t errors = CAN_ERR_MASK;

			if (setsockopt(_socket[i], SOL_CAN_RAW, CAN_RAW_LOOPBACK, &loopback, sizeof(loopback)) < 0 ||
			    setsockopt(_socket[i], SOL_CAN_RAW, CAN_RAW_ERR_FILTER, &errors, sizeof(errors)) < 0) { return -errno; }

			if (fcntl(_socket[i], F_SETFL, O_NONBLOCK) < 0) { return -errno; }

			_network_up[i] = true; // Also clean up a partially failed interface start.
			const int result = interface_flags(_socket[i], request.ifr_name, true);

			if (result < 0) { return result; }

			const int receive_result = can_receive_fifo0(i == 0 ? STM32_FDCAN1_BASE : STM32_FDCAN2_BASE);

			if (receive_result < 0) { return receive_result; }

			if (_expired.load()) { return -ETIMEDOUT; }
		}

		return 0;
	}

	static bool ethernet_initialized = false;

	if (!ethernet_initialized) {
		const int result = stm32_ethinitialize(0);

		if (result < 0) { return result; }

		ethernet_initialized = true;
	}

	_socket[0] = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (_socket[0] < 0) { return -errno; }

	ifreq request{};
	strncpy(request.ifr_name, "eth0", IFNAMSIZ - 1);
	sockaddr_in address{};
	address.sin_family = AF_INET; address.sin_addr.s_addr = inet_addr("192.168.77.2");
	memcpy(&request.ifr_addr, &address, sizeof(address));

	if (ioctl(_socket[0], SIOCSIFADDR, reinterpret_cast<unsigned long>(&request)) < 0) { return -errno; }

	address.sin_addr.s_addr = inet_addr("255.255.255.0");
	memcpy(&request.ifr_netmask, &address, sizeof(address));

	if (ioctl(_socket[0], SIOCSIFNETMASK, reinterpret_cast<unsigned long>(&request)) < 0) { return -errno; }

	_network_up[0] = true;
	const int result = interface_flags(_socket[0], "eth0", true);

	if (result < 0) { return result; }

	if (_expired.load()) { return -ETIMEDOUT; }

	address.sin_port = htons(protocol_test::UDP_PORT); address.sin_addr.s_addr = inet_addr("192.168.77.2");

	if (bind(_socket[0], reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0 ||
	    fcntl(_socket[0], F_SETFL, O_NONBLOCK) < 0) { return -errno; }

	return 0;
#endif
}

bool ProtocolTest::ethernet_link()
{
#ifdef CONFIG_GPIO_TEST_RECEIVER
	return false;
#else
	ifreq request {};
	strncpy(request.ifr_name, "eth0", IFNAMSIZ - 1);

	if (ioctl(_socket[0], SIOCGMIIPHY, reinterpret_cast<unsigned long>(&request)) < 0) { return false; }

	request.ifr_mii_reg_num = MII_MSR;

	// BMSR link is latched low: read twice to observe the current state.
	if (ioctl(_socket[0], SIOCGMIIREG, reinterpret_cast<unsigned long>(&request)) < 0 ||
	    ioctl(_socket[0], SIOCGMIIREG, reinterpret_cast<unsigned long>(&request)) < 0) { return false; }

	return request.ifr_mii_val_out & MII_MSR_LINKSTATUS;
#endif
}

void ProtocolTest::network_step()
{
#ifndef CONFIG_GPIO_TEST_RECEIVER
	const hrt_abstime now = hrt_absolute_time();

	if (_mode == Mode::Can) {
		for (unsigned i = 0; i < 2; ++i) {
			for (unsigned limit = 0; limit < 16; ++limit) {
				can_frame frame{};
				const ssize_t count = read(_socket[i], &frame, sizeof(frame));

				if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) { break; }

				if (count != sizeof(frame) || (frame.can_id & CAN_ERR_FLAG)) { _sample.errors++; break; }

				const unsigned peer = 1 - i;

				if (!_pending[peer] || frame.can_id != 0x5a0 + peer || frame.can_dlc != 8 ||
				    protocol_test::get32(frame.data) != _token || protocol_test::get32(frame.data + 4) != _can_sequence[peer]) {
					_sample.errors++; continue;
				}

				_pending[peer] = false; _can_rx[peer]++; _sample.rx++;
			}
		}

		for (unsigned i = 0; i < 2; ++i) {
			if (_pending[i] && now - _pending_time[i] > 500000) { _sample.errors++; _pending[i] = false; }

			if (!_pending[i]) {
				can_frame frame{};
				frame.can_id = 0x5a0 + i; frame.can_dlc = 8;
				protocol_test::put32(frame.data, _token); protocol_test::put32(frame.data + 4, ++_can_sequence[i]);

				if (write(_socket[i], &frame, sizeof(frame)) == sizeof(frame)) {
					_sample.tx++; _pending[i] = true; _pending_time[i] = now;

				} else { _sample.errors++; }
			}
		}

		_sample.detail = (_can_rx[0] >= 20 ? 1 : 0) | (_can_rx[1] >= 20 ? 2 : 0);
		_sample.passed = _sample.detail == 3 && _sample.errors == 0;
		return;
	}

	bool received = false;

	for (unsigned limit = 0; limit < 16; ++limit) {
		uint8_t packet[protocol_test::PACKET_SIZE + 1] {};
		sockaddr_in peer{}; socklen_t size = sizeof(peer);
		const ssize_t count = recvfrom(_socket[0], packet, sizeof(packet), 0, reinterpret_cast<sockaddr *>(&peer), &size);

		if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) { break; }

		const uint32_t sequence = protocol_test::get32(packet + 8);

		if (peer.sin_addr.s_addr != inet_addr("192.168.77.1") || sequence <= _slave_sequence ||
		    !protocol_test::valid_packet(packet, count > 0 ? count : 0, _mode, _token, sequence, false)) {
			_sample.errors++; continue;
		}

		_slave_sequence = sequence; _sample.rx++; received = true;
		protocol_test::packet(packet, _mode, _token, sequence, true);

		if (sendto(_socket[0], packet, protocol_test::PACKET_SIZE, 0, reinterpret_cast<sockaddr *>(&peer), size) == protocol_test::PACKET_SIZE) { _sample.tx++; }

		else { _sample.errors++; }
	}

	_sample.detail = ethernet_link() ? 1 : 0;

	if (received) { _pending_time[0] = now; }

	_sample.passed = _sample.detail == 1 && _sample.tx >= 20 && _sample.rx >= 20 && _sample.errors == 0
			 && _pending_time[0] != 0 && now - _pending_time[0] < 1000000;
#endif
}
