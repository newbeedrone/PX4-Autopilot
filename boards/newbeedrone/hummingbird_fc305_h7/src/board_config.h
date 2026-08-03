/****************************************************************************
 *
 *   Copyright (C) 2026 PX4 Development Team. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in
 *    the documentation and/or other materials provided with the
 *    distribution.
 * 3. Neither the name PX4 nor the names of its contributors may be
 *    used to endorse or promote products derived from this software
 *    without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
 * FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 * COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
 * INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 * BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS
 * OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
 * AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
 * ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 *
 ****************************************************************************/

/**
 * @file board_config.h
 *
 * Hummingbird FC305 H7 board definitions.
 */

#pragma once

#include <px4_platform_common/px4_config.h>
#include <nuttx/compiler.h>
#include <stdint.h>

#include <stm32_gpio.h>

/* Status LEDs are active low. */
#define GPIO_nLED_BLUE             /* PE3 */ (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_50MHz | GPIO_OUTPUT_SET | GPIO_PORTE | GPIO_PIN3)
#define GPIO_nLED_GREEN            /* PE4 */ (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_50MHz | GPIO_OUTPUT_SET | GPIO_PORTE | GPIO_PIN4)

#define BOARD_HAS_CONTROL_STATUS_LEDS 1
#define BOARD_OVERLOAD_LED             LED_BLUE
#define BOARD_ARMED_STATE_LED          LED_GREEN

/* ADC1 inputs from the Betaflight target. */
#define ADC1_CH(_n)                 (_n)
#define PX4_ADC_GPIO \
	GPIO_ADC123_INP10, /* PC0: battery 1 voltage */ \
	GPIO_ADC123_INP11, /* PC1: battery 1 current */ \
	GPIO_ADC12_INP5,   /* PB1: battery 2 voltage */ \
	GPIO_ADC12_INP9,   /* PB0: battery 2 current */ \
	GPIO_ADC12_INP4,   /* PC4: external analog */ \
	GPIO_ADC12_INP8    /* PC5: analog RSSI */

#define ADC_BATTERY1_VOLTAGE_CHANNEL  ADC1_CH(10)
#define ADC_BATTERY1_CURRENT_CHANNEL  ADC1_CH(11)
#define ADC_BATTERY2_VOLTAGE_CHANNEL  ADC1_CH(5)
#define ADC_BATTERY2_CURRENT_CHANNEL  ADC1_CH(9)
#define ADC_EXTERNAL1_CHANNEL         ADC1_CH(4)
#define ADC_RSSI_IN_CHANNEL           ADC1_CH(8)

#define ADC_CHANNELS \
	((1 << ADC_BATTERY1_VOLTAGE_CHANNEL) | \
	 (1 << ADC_BATTERY1_CURRENT_CHANNEL) | \
	 (1 << ADC_BATTERY2_VOLTAGE_CHANNEL) | \
	 (1 << ADC_BATTERY2_CURRENT_CHANNEL) | \
	 (1 << ADC_EXTERNAL1_CHANNEL) | \
	 (1 << ADC_RSSI_IN_CHANNEL))

/* Both battery sense pairs are analog inputs without separate valid pins. */
#define BOARD_NUMBER_BRICKS          2
#define BOARD_ADC_BRICK1_VALID       true
#define BOARD_ADC_BRICK2_VALID       true

/* Eight motor outputs plus two standard PWM servo outputs. */
#define DIRECT_PWM_OUTPUT_CHANNELS  10
#define DIRECT_INPUT_TIMER_CHANNELS  10
#define BOARD_HAS_PWM                DIRECT_PWM_OUTPUT_CHANNELS
#define BOARD_NUM_IO_TIMERS          3

/* Passive beeper: PA15, TIM2_CH1. The tone alarm driver owns TIM2 directly,
 * so CONFIG_STM32H7_TIM2 must remain disabled in the NuttX configuration.
 * Betaflight marks the beeper as inverted, therefore its idle GPIO level is
 * high. GPIO-only tone mode is unsuitable because it does not generate the
 * target's configured 5.4 kHz carrier.
 */
#define TONE_ALARM_TIMER             2
#define TONE_ALARM_CHANNEL           1
#define GPIO_TONE_ALARM_IDLE         /* PA15 */ (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz | GPIO_OUTPUT_SET | GPIO_PORTA | GPIO_PIN15)
#define GPIO_TONE_ALARM              GPIO_TIM2_CH1OUT_2

/* High-resolution timer. TIM8 is not connected to an output in this target. */
#define HRT_TIMER                    8
#define HRT_TIMER_CHANNEL            1

/* ICM42688P external clock: PE9, TIM1_CH1.
 * TIM1's 240 MHz input clock produces 32.051 kHz with the integer divider
 * used by spix_sync.c, so the sensor startup argument is -C 32051.
 */
#define BOARD_SPIX_SYNC_FREQ          32000
#define GPIO_SPIX_SYNC               /* PE9 */ (GPIO_INPUT | GPIO_PULLDOWN | GPIO_PORTE | GPIO_PIN9)

/* USB OTG FS uses PA11/PA12 for DM/DP. The board has no VBUS sense input;
 * board_read_VBUS_state() therefore reports USB available unconditionally.
 * The common bootloader deinit path still requires a harmless GPIO placeholder.
 */
#define BOARD_USB_VBUS_SENSE_DISABLED 1
#define GPIO_OTGFS_VBUS              /* unused PA9 */ (GPIO_INPUT | GPIO_FLOAT | GPIO_PORTA | GPIO_PIN9)

/* Betaflight PINIO outputs. PINIO1 is the active-high 10 V BEC enable and
 * must default on. PINIO2/PINIO3 also retain the target's default high state;
 * their connected functions are not identified in the hardware definition.
 */
#define GPIO_PINIO1_10V_EN           /* PC15 */ (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz | GPIO_OUTPUT_SET | GPIO_PORTC | GPIO_PIN15)
#define GPIO_PINIO2                  /* PC14 */ (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz | GPIO_OUTPUT_SET | GPIO_PORTC | GPIO_PIN14)
#define GPIO_PINIO3                  /* PC13 */ (GPIO_OUTPUT | GPIO_PUSHPULL | GPIO_SPEED_2MHz | GPIO_OUTPUT_SET | GPIO_PORTC | GPIO_PIN13)

#define VDD_10V_BEC_EN(on_true)      px4_arch_gpiowrite(GPIO_PINIO1_10V_EN, (on_true))

/* SDMMC1 has no card-detect pin. */
#define SDIO_SLOTNO                  0
#define SDIO_MINOR                   0

/* UART5 is the bidirectional CRSF receiver port. */
#define RC_SERIAL_PORT               "/dev/ttyS4"
#define BOARD_SUPPORTS_RC_SERIAL_PORT_OUTPUT

#define BOARD_DMA_ALLOC_POOL_SIZE    5120
#define BOARD_HAS_ON_RESET           1
#define BOARD_ENABLE_CONSOLE_BUFFER

/* The application linker script reserves sector 15 for parameters. */
#define FLASH_BASED_PARAMS

/* Serial RGB LED: PE5, TIM15_CH1, DMA1 */
#if defined(USE_S_RGB_LED_DMA)
#define BOARD_HAS_N_S_RGB_LED       1
#define BOARD_MAX_LEDS              BOARD_HAS_N_S_RGB_LED
#define S_RGB_LED_DMA               DMAMAP_TIM15_CH1
#define S_RGB_LED_TIMER             15
#define S_RGB_LED_CHANNEL           1
#define S_RGB_LED_TIM_GPIO          GPIO_TIM15_CH1OUT_2
#endif

#define PX4_GPIO_INIT_LIST { \
		PX4_ADC_GPIO, \
		GPIO_nLED_BLUE, \
		GPIO_nLED_GREEN, \
		GPIO_TONE_ALARM_IDLE, \
		GPIO_SPIX_SYNC, \
		GPIO_PINIO1_10V_EN, \
		GPIO_PINIO2, \
		GPIO_PINIO3, \
	}

#ifndef __ASSEMBLY__
__BEGIN_DECLS

int stm32_sdio_initialize(void);
extern void stm32_spiinitialize(void);
extern void stm32_usbinitialize(void);
extern void board_peripheral_reset(int ms);

#include <px4_platform_common/board_common.h>

__END_DECLS
#endif
