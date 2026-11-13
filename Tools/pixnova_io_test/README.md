# PixNova GPIO Test Station

This tool checks external signal continuity by generating distinct square-wave frequencies on PixNova and measuring them independently on `PX4 TEST BOARD_RevA`.
Both boards connect to the same computer over USB CDC.
It is a dedicated manufacturing configuration, separate from the flight firmware and `pixnova_doc` branch.

## UART And Buzzer Frequency Tests

All of the following signals use GPIO square-wave output on the flight controller
and frequency measurement on the fixture, with per-pin results in the main GUI:

| Connector | Signals measured by frequency |
| --- | --- |
| TELEM1, TELEM2, TELEM3 | TX, RX, CTS, RTS; all four signals on each connector |
| GPS1 | TX, RX, safety input, safety LED and BUZZER |
| GPS2 | TX and RX |
| UART4/I2C3 | TX, RX and NFC GPIO |

GPS1 BUZZER uses PF9 at approximately 47.170 Hz, received at fixture P19 pin 2
(PF9), with a weak pull-up on that fixture input. Bench measurements require
this input bias: floating and pull-down configurations did not detect edges,
while the pull-up produced the expected frequency. The firmware applies
`probe_pull: up` from the profile only to the receiver input; it does not drive
the fixture pin as an output. GPS2 TX uses PE1 at approximately 51.546 Hz, received at P3 pin 5 (PE0);
GPS2 RX uses PE0 at approximately 56.180 Hz, received at P3 pin 4 (PE1).
The confirmed TELEM1, TELEM2, GPS2 and UART4 harnesses cross TX and RX:

| Flight-controller signal | Fixture receive position | Expected frequency |
| --- | --- | --- |
| TELEM1 TX, PE8 | P15.5, PF6, UART7 RX | 10.000 Hz |
| TELEM1 RX, PF6 | P15.4, PE8, UART7 TX | 10.893 Hz |
| TELEM2 TX, PC12 | P18.5, PD2, UART5 RX | 14.124 Hz |
| TELEM2 RX, PD2 | P18.4, PC12, UART5 TX | 15.385 Hz |
| GPS2 TX, PE1 | P3.5, PE0, UART8 RX | 51.546 Hz |
| GPS2 RX, PE0 | P3.4, PE1, UART8 TX | 56.180 Hz |
| UART4 TX, PH13 | P17.6, PH14, UART4 RX | 60.976 Hz |
| UART4 RX, PH14 | P17.5, PH13, UART4 TX | 66.667 Hz |

The GPIO tests follow this fixed mapping in [wiring.csv](wiring.csv).
Each GUI row names the flight-controller signal and displays its fixture GPIO
and pin. The fixture connector diagrams retain their schematic signal names.
GPS1 UART, TELEM3 and CTS/RTS keep their existing mappings. Acceptance still
requires the assigned signal's frequency within tolerance for three consecutive
windows; a different signal's frequency fails and is identified when possible.
This checks digital signal continuity, without transmitting UART data or testing
an attached buzzer's sound output.

Only I2C SCL/SDA use simulated-slave transactions. The GPS1/GPS2 I2C cards do not
replace the frequency tests for the UART or buzzer pins on the same connector.

## ADC6V6 Voltage Test

ADC6V6 is a flight-controller input with an approximately 1:2 resistor divider.
The fixture drives P10.7/PF13 first low, then high at its nominal 3.3 V GPIO level.
PixNova keeps PC2_C as an ADC input and samples ADC1 channel 12. The low test
accepts 0–0.100 V; the high test accepts 1.500–1.800 V around the expected 1.65 V.
Both boards must provide fresh readback for three report windows per level.
The GUI shows both measured voltages, and the existing ADC6V6 pin turns red or
green according to the combined result. **Test ADC** runs the two levels alone;
the normal **Start test** includes them after the I2C tests.

This is an external input/divider check at 0 V and nominal 3.3 V stimulus. It does
not calibrate ADC accuracy or test the full 6.6 V range. The flight controller
never drives PC2 as a waveform output. Fixture STOP, USB/heartbeat expiry and
test completion return PF13 to an input. Both firmware images advertise
`adc_levels: 1`; the complete station rejects older images before starting.

## Connector Protocol Tests

The **Protocols** button opens a separate protocol window after releasing the
frequency station's USB connections. Only one window may own the boards at a time.
The protocol window's **IO ports** button returns to the main station, where GPS1,
GPS2, TELEM, SPI6, PWM and the other frequency-test connectors remain available.
Switching stops test outputs and releases USB before changing pages.
You can also launch it directly:

```sh
python Tools/pixnova_io_test/app.py --protocols
python Tools/pixnova_io_test/app.py --protocols --demo
```

Protocol tests require a dedicated firmware pair; the GPIO-only images do not
advertise this capability. Build both using:

```sh
python Tools/pixnova_io_test/build_firmware.py --protocols
```

The targets are `newbeedrone_pixnova_protocol-test` (flight controller, application
at `0x08020000`) and `newbeedrone_pixnova_protocol-probe` (fixture, at `0x08000000`).
They retain the corresponding USB identities and add a versioned protocol-test
capability. The simulator reports `probe_protocol_firmware` instead of claiming
to be an input-only image. It starts with undriven test signals; simulator outputs
are enabled only by a checked command. No commander, actuator or PX4IO driver runs.
The RC observer sends only read requests to the already-installed PX4IO firmware.
Fixture images disable the Ethernet MAC so startup cannot remap P4's PG13/PG12
RC signals to RMII outputs. Source network interfaces initialise only when their
specific checked protocol test starts.
The protocol fixture also supports GPIO frequency measurements in receive mode.
The IO station verifies its advertised capability and matching firmware marker,
then requires a fresh GPIO `CHECK` before starting. Switching between these tests
does not require reflashing the fixture.

The exact wiring and acceptance criteria appear beside each GUI test and in
[protocols.json](protocols.json):

| Test | Physical method |
| --- | --- |
| I2C3 | Flight-controller master PA8/PH8 to fixture PA8/PH8, P7 pins 3/2. The shared UART4/I2C3 connector has a separate slave endpoint on P17 PG1/PG2. |
| GPS2 I2C | Flight-controller I2C2 PF1/PF0 to fixture I2C4 PF14/PF15, P3 pins 3/2. |
| SBUS / DSM | Fixture UART8 TX PE1, P3 pin 4, temporarily jumpered to the appropriate flight-controller RC input. Disconnect this pin's normal GPS2 UART connection first. P4 PG13/PG12 cannot transmit hardware UART directly. |
| CAN1 / CAN2 | Flight-controller CAN1 H/L connected externally to CAN2 H/L with correct bus termination and common ground. Disconnect the fixture's CAN headers. Classic CAN at 500 kbit/s; two distinct controllers must receive fresh frames. |
| Ethernet | Correct PixNova Ethernet adapter to a real PC Ethernet interface. Flight controller `192.168.77.2/24`, PC `192.168.77.1/24`, UDP port `14570`. Configure the PC interface separately; the GUI does not change its network settings. Disconnect fixture P2. |

The six I2C tests use address `0x42`, with a selected hardware controller or an edge-driven, open-drain software slave:

| Connector | Flight-controller bus | Fixture endpoint | Tested clock |
| --- | --- | --- | --- |
| GPS1 | I2C1 PB8/PB9 | P19.7 PH3 SCL, P19.6 PH4 SDA; software slave | 10 kHz |
| POWER1 | I2C1 PB8/PB9 | P5.3 PB8 SCL, P5.4 PB9 SDA; I2C1 | 100 kHz |
| GPS2 | I2C2 PF1/PF0 | P3.3 PF14 SCL, P3.2 PF15 SDA; I2C4 | 100 kHz |
| POWER2 | I2C2 PF1/PF0 | P6.3 PF1 SCL, P6.4 PF0 SDA; I2C2 | 100 kHz |
| I2C3 | I2C3 PA8/PH8 | P7.3 PA8 SCL, P7.2 PH8 SDA; I2C3 | 100 kHz |
| UART4/I2C3 | I2C3 PA8/PH8 | P17.4 PG1 SCL, P17.3 PG2 SDA; software slave | 10 kHz |

GPS1 routing follows the measured cable path; the former GPIO-profile SCL/SDA expectations were reversed. Software slaves never drive SCL and only pull SDA low or release it. The master timeout reserves 2000 microseconds per byte so a full 10 kHz write/repeated-START/read transaction is not truncated by the faster-bus default. Requests contain a new run token and sequence; replies contain transformed data and CRC. Repeated START, address ACK and both directions are checked. This validates communication at the listed speed, without claiming a physical sensor or a full IST8310 emulation.

All simulated slaves use address `0x42` on their respective fixture controllers.
Disconnect other external devices and ensure that address belongs only to the
fixture. EEPROM `0x51` and ICP-20100 `0x63` retain their normal bus modes. Requests
carry a fresh run token and increasing sequence; the simulator returns transformed
data protected by CRC-16/CCITT. No EEPROM data is written.

SBUS uses inverted 100000 baud 8E2; DSM uses 115200 baud 8N1 and 11-bit frames.
Seven distinct channel values are verified through PX4IO, along with protocol type,
advancing frame count, no incoming failsafe/frame-drop flag and no armed outputs.
After three fresh valid windows, the simulator stops and the flight controller must
revoke RC validity in two additional windows. No actuator outputs are enabled.

CAN acceptance requires at least 20 verified frames in each direction, plus three
fresh report windows without errors. Internal controller loopback and socket local
echo are disabled. Ethernet requires physical PHY link and independently verified
request/response packets at both the PC and flight controller. The PC waits for the
physical link before sending test traffic. Neither test uses ADC waveforms as proof
of communication; the fixture CAN and Ethernet pins have no ADC channels.

CAN startup explicitly disables both hardware acceptance-filter lists and routes
unmatched frames to FIFO0. The socket driver otherwise enables filter entries in
uninitialised or retained message RAM, which can reject traffic or send it to the
unconfigured FIFO1. This policy is applied after each interface start; unexpected
frames still fail the software ID, run-token and sequence checks.

Onboard diagnostics and external-interface eligibility are separate. Sensor,
EEPROM, FRAM and SD failures remain red in the GUI and reports but do not prevent
external tests. Every protocol start requires a fresh `PCHECK` for that exact mode,
with valid clock, GPIO and fixture firmware evidence. A GPIO `CHECK` cannot enable
a protocol test, or vice versa. All I2C buses retain their peripheral function during GPIO tests; responding devices do not prevent measurement of unrelated digital signals.
The source advertises `independent_external_checks`; update older source firmware
before using this host. The fixture is started before the source where needed.
GPIO frequency output and protocol output cannot run together. STOP, disconnects
and the two-second lease revoke test activity; an IRQ watchdog also disables UART
and I2C output and requests CAN initialisation if the USB task stops running.

Reports distinguish completed measurement snapshots from untested ports. Demo
exports are marked simulated and never claim hardware validation. A failed I2C
transaction or paired CAN test identifies the failing connection, not which
individual wire or component caused it. ADC front-end design and physical waveform
quality measurements are outside these protocol tests.

The profile covers 43 signals: 42 GPIO frequency tests and one ADC6V6 voltage test. All I2C SCL/SDA signals are excluded from waveform generation, including GPS1 and POWER1.
It includes TELEM1–3, GPS UARTs, UART4, NFC, external SPI signals, PPM, eight FMU PWM signals, capture/armed signals, and the two external ADC signal paths.
ADC3V3 is tested as a digital connection; ADC6V6 uses the separate input voltage test above.
GPS1 and POWER1 share the flight controller’s I2C1 bus and use separate simulated-slave endpoints on the fixture. GPS2 and POWER2 similarly share flight-controller I2C2. Only the selected fixture slave responds at address `0x42`.

## Wiring And Electrical Scope

Use [wiring.csv](wiring.csv) for the exact source GPIO, test-board connector/pin and receiver GPIO.
The mapping was extracted from `PX4 TEST BOARD_RevA.SchDoc`; its SHA256 is recorded in [profile.json](profile.json).
The schematic remains an untracked input and is not bundled into the application.
In particular, the fixture's GPS1 I2C inputs go to PH4/PH3, rather than PB8/PB9, and its GPS2 I2C inputs go to PF14/PF15.
Do not assume that corresponding signals use the same GPIO on both boards.
The schematic labels P21 as an I/O header, but its nets are `FMU_CH1`–`FMU_CH8`; use the net/pin mapping rather than that label.

Connect only the listed signal lines and a common ground for this phase.
Both boards are powered through their own USB connections.
Leave connector power pins disconnected from the test harness; GPIO frequency measurements do not test the power rails.
For GPIO measurements, disconnect receivers, GNSS modules, actuators and other external peripherals from the GPIO signals under test.
Existing I2C devices at other addresses may remain connected; I2C pins stay in protocol mode and the fixture reserves only address `0x42`.
The test expects 3.3 V logic.
Do not connect 12 V servo power, USB D+/D−, CAN H/L or Ethernet pairs to MCU inputs.

The following lines are protected and never configured as waveform outputs:

- I2C2 PF0/PF1: attached ICP-20100, including the shared GPS2/POWER2 I2C signals.
- I2C1 PB8/PB9: GPS1/POWER1 slave-simulation tests only.
- I2C3 PA8/PH8: base EEPROM configured at address `0x51`, including the shared UART4/I2C3 connector.
- I2C4 PF14/PF15: BMM150, BMP388 and IMU EEPROM.
- PC2/PC2_C: ADC6V6 input/divider test only.
- SPI1/2/3 IMUs, SPI5 FRAM and PE9 sensor synchronisation.
- PX4IO UART, eight PX4IO PWM channels, DSM/SBUS, SBUS output and RSSI.
- SWD, reset, BOOT0, debug UART, oscillators, USB, storage and power-control nets.

## Build The Two Firmware Images

Use the PX4 build environment and the repository's `build-px4` workflow.
The helper builds the roles sequentially, clears ignored NuttX object/archive caches when changing configurations and stages commit-labelled artifacts and checksums in `/tmp/px4-firmware`:

```sh
python Tools/pixnova_io_test/build_firmware.py
```

The helper locks its shared build workspace and rejects another simultaneous helper invocation.
Do not run a separate PX4 build in the same checkout while it is running.
Changing the profile or Git revision during either role's build aborts the pair.
The staged PX4 package must decompress to exactly the validated BIN and use the expected board ID.
The helper also checks the generated NuttX configuration for startup-script and USB CDC support,
including a startup mount that matches the compressed ROMFS image.
The test defconfigs use the current `CONFIG_ETC_ROMFS` and `CONFIG_ETC_CROMFS` options;
obsolete `CONFIG_NSH_ROMFSETC` options leave the startup script disabled on current NuttX.

The normal station uses these complete test targets:

```sh
make newbeedrone_pixnova_protocol-test
make newbeedrone_pixnova_protocol-probe
```

`--gpio-only` builds the legacy `io-test`/`io-probe` diagnostic targets instead;
these do not provide I2C slave simulation for the complete test button.

If a previous NuttX apps checkout left an autogenerated `gpsutils/Kconfig` referring to a missing `minmea/Kconfig`, regenerate that local menu with `bash ../tools/mkkconfig.sh -m 'GPS Utilities'` from `platforms/nuttx/NuttX/apps/gpsutils` before building.
This is a generated build-environment file; do not stage it as a test-tool source change.

`io-test` is the PixNova waveform source.
Its image retains the normal PixNova bootloader offset at `0x08020000`.
`io-probe` is an input-only image for the supplied STM32H753IIK6 test board with a 16 MHz crystal and USB FS on PA11/PA12.
The probe ELF/BIN is linked at `0x08000000` for programming through SWD without requiring a PX4 bootloader.
Program the probe ELF using the board's SWD connector and your normal programmer.
Do not use the probe image as flight-controller firmware.
The source and probe products enumerate as `PixNova IO Test` and `PixNova IO Probe` respectively.
The application also verifies the firmware role before starting.
Protocol 3 rejects earlier test images; update both boards together with the host application.
The dedicated linker scripts align the end of BSS to eight bytes, and both configurations use a 1024-byte idle stack.
The build helper rejects images with misaligned/out-of-SRAM startup stacks, incorrect reset vectors, stale profiles or mismatched USB products before staging them.

These configurations use an isolated ROMFS.
They do not start commander, MAVLink, actuator drivers or sensor applications.
The probe skips PixNova's power-control, PWM-reset, storage and sensor initialisation, so its measurement inputs are not accidentally driven during application startup.
Both roles initialise their test pins as inputs during early startup and reset.
The source uses floating inputs; the fixture applies the profile's receiver bias,
including the BUZZER weak pull-up.
The source filters those pins out of the flight board's default GPIO initialisation, and test configurations omit SPI6 peripheral initialisation so its chip selects stay undriven until START.
The source retains the board power and sensor-bus initialisation needed for self-checks.

## Run The GUI

Python 3.10 or later with Tk is required.
On Windows, install Python with Tcl/Tk enabled.
On Linux, install the distribution's `python3-tk` package if necessary.

```sh
python -m pip install -r Tools/pixnova_io_test/requirements.txt
python Tools/pixnova_io_test/app.py
```

On Windows, double-click `start_test_station.bat` to open the ordinary test station.

Choose PixNova as the source. The probe port is optional when viewing flight-controller peripheral checks; two distinct USB ports are required for GPIO frequency tests.
After a disconnect, stop the active operation and use **Connect USB** again; messages from the previous connection cannot change the new session.
Select **Connect**, then **Start test**.
The ordinary station’s **Start test** runs a six-second GPIO measurement, freezes that result, then tests all six I2C connectors sequentially using the simulated slaves and checks the two ADC6V6 voltage levels. Individual **Test I2C** and **Test ADC** buttons are available on the main page. GPIO waveforms stop before connector protocol tests begin; SCL/SDA and PC2 are never waveform outputs.

Every start repeats interface checks and collects fresh onboard diagnostics before output is enabled.
Onboard faults remain visible while the external interface test proceeds; a failed
clock, GPIO configuration or fixture safety check still prevents output.
The GUI displays expected frequency, measured frequency, relative error and a result for each fixture input.
The dashboard groups signals by connector and draws each connector's numbered pins.
Click a pin or signal row to inspect the PixNova GPIO, probe GPIO, connector pin and measurement details.
Failed pins turn red together with their port card and source-to-probe signal path; passing pins are green and pending pins are grey or amber.
Use **Failed pins** to show only ports with failures, or select a connector in the sidebar.
Pins outside the test profile remain grey, including power, ground and protected sensor signals.
Diagrams follow probe schematic pin numbering, not the physical mating orientation of a connector.
Display positions are stored in `connectors.json`, separately from the firmware profile.
The default acceptance criterion is ±2% for three consecutive, distinct measurement windows.
Windows arriving as a backlog burst or after an excessive gap restart the consecutive-window count.
USB receive timestamps, rather than GUI delivery times, determine measurement and heartbeat freshness.
Missing edges, wrong frequency, sampling overruns, stale sessions and USB/heartbeat timeouts cannot produce a pass.
Reports can be exported to JSON or CSV, including profile digest, session, self-check state and per-channel results.
Exports freeze a freshness-checked snapshot before opening the file dialog, including individual startup checks and both device identities. Suite exports include `i2c_tests`, `test_suite_completed`, `gpio_measurement_completed`, `i2c_passed` and `external_suite_passed`; untested connectors cannot produce an overall I2C pass.
Reports are written to a temporary file beside the destination and replaced only after a complete write, so a write error preserves the previous report.
Export a passing report while the run is active and every channel is green; a stopped or disconnected run is not reported as an active pass.

Preview the interface without hardware:

```sh
python Tools/pixnova_io_test/app.py --demo
```

The demo is visibly marked and can simulate a wrong-frequency connection.
Its exported reports are explicitly marked as simulated.
The demo fault switch injects a wrong-frequency signal on TELEM1 pin 4 (PE8), so the red pin and path can be previewed without hardware.

## PixNova Flight-Controller Power-On Checks

The flight controller automatically checks its onboard devices when the test firmware starts, before the USB host connects.
It caches the individual results and replays them after HELLO, so connecting the GUI later does not lose the power-on result.
The measurement board does not test EEPROM, SD or SPI storage; it remains an input-only frequency probe.

The GUI's **PixNova onboard checks** panel has separate **Sensors** and **Storage** tabs.
Each device has its own green/red result, bus location and observed ID or read/capacity value.
Click a device for details, including the immutable power-on result and the latest pre-test result.
Only the flight controller USB connection is needed to view or rerun these checks.

| Flight-controller device | Bus | Read-only check |
| --- | --- | --- |
| ICM45686 #1 / #2 / #3 | SPI1 / SPI2 / SPI3 | Individual WHO_AM_I reads, expected `0xE9` |
| BMM150 | I2C4 / `0x10` | CHIP_ID `0x32` |
| BMP388 | I2C4 / `0x76` | CHIP_ID `0x50` |
| ICP-20100 | I2C2 / `0x63` | DEVICE_ID `0x63` |
| Base EEPROM, 24LC64T | I2C3 / `0x51` | 16-byte samples at offsets 0 and 8176 of the configured 8 KiB device |
| IMU EEPROM, 24LC64T | I2C4 / `0x50` | The same two read samples; calibration data is not modified |
| SPI FRAM, FM25V02/A family | SPI5 / CS PG7 | Live RDID on every check, 32 KiB driver geometry, and reads at both ends |
| Flight-controller microSD | SDMMC2 | Card capacity and reads of the first and last 512-byte sector |

The board configuration identifies the SPI device as FRAM, not NOR flash.
EEPROM, FRAM and SD tests do not write or erase stored data, and do not format or mount the SD filesystem.
EEPROM samples do not prove full capacity or validate calibration contents; the SD check does not prove filesystem integrity or write performance.
Sensor ID checks do not replace MEMS built-in self-test, calibration or accuracy measurements.

Insert the SD card before powering the flight controller for the manufacturing run.
A failed onboard power-on check remains in the report and its device stays red;
external GPIO and protocol tests can still run. Correct the onboard fault and
power-cycle the flight controller to refresh the immutable power-on result.
Manual self-checks update the latest result without rewriting the cached power-on history.
Missing sensor/storage diagnostics cannot produce an onboard pass. Missing or
invalid interface evidence prevents external tests even if a summary claims success.
Reports include both sets of results and definitions of their units.
JSON reports record `external_passed` (or `hardware_protocols_passed`) separately
from `onboard_passed`; the GPIO report's `all_passed` still requires both domains.
Pre-test failures are collected through the final summaries so all device results remain visible.
No outputs are enabled while these results are being collected; any failure or missing result blocks START.
Cancelling or timing out a check returns devices without a result to waiting, while preserving completed results.

## Mandatory Startup Self-Check

Before enabling any waveform, both endpoints must agree with the GUI's profile SHA256 and protocol version.
The firmware then checks:

1. The HRT clock advances and every test pin can be configured as an input.
2. BMM150 on I2C4/`0x10` reports CHIP_ID `0x32`.
3. BMP388 on I2C4/`0x76` reports CHIP_ID `0x50`.
4. ICP-20100 on I2C2/`0x63` reports DEVICE_ID `0x63`.
5. All three ICM45686 devices on SPI1/2/3 report WHO_AM_I `0xE9`.
6. Both EEPROMs, the SPI5 FRAM and the flight-controller SD card pass the read checks above.
7. I2C1/2/3/4 SCL and SDA are excluded from the frequency-output pin table.

The BMM150 is woken from suspend mode for its identity read.
The protected sensor buses remain in peripheral mode and are never toggled by the waveform engine.
Self-check results are printed in the GUI log.
A failed check blocks **Start** in the GUI and in firmware.
The firmware requires a matching, recent check session and does not accept an unchecked `START` command.
The probe starts first, followed by the source.

**STOP**, USB errors and an expired two-second heartbeat lease stop sampling/waveforms and return test signals to inputs.
Source inputs float; fixture inputs retain their configured receiver bias.
Closing the GUI requests STOP from both endpoints.
The firmware lease is also effective if the application crashes or the host stops responding.
An expired lease cannot be renewed by a late PING, and an endpoint with a USB error cannot execute a buffered START.
STOP discards queued START/heartbeat commands, and closing the station does not execute pending START commands.
If the GUI event loop stalls beyond the heartbeat lease, the station invalidates the run and requires a new startup check instead of replaying old samples as fresh results.

## Protocol And Profile Changes

USB CDC carries newline-delimited JSON responses and bounded ASCII commands:

```text
HELLO
CHECK <32-character-hex-session> <profile-sha256>
START <32-character-hex-session> <profile-sha256>
PING <32-character-hex-session>
STOP
```

HELLO is followed by cached `power_check` records and a `power_checked` summary.
Responses also include `check`, `checked`, `started`, `heartbeat`, `sample`, `stopped` or `error` frames.
Samples include session, sequence, channel, frequency in millihertz, rising-edge count, measurement span in microseconds and interrupt-overrun count.
The GUI independently checks that frequency agrees with edge count and span.
Firmware uses measured HRT timestamps, rather than treating nominal polling intervals as elapsed time.

Edit the single `profile.json` source, then regenerate the wiring table and rebuild both images:

```sh
python Tools/pixnova_io_test/generate_profile.py --wiring Tools/pixnova_io_test/wiring.csv
```

The build generates its pin table from that same profile.
Validation rejects pins outside the reviewed external FMU signals, duplicate receiver pins or connector positions, conflicting frequencies on shared output pins, identical frequencies on independent nets, non-finite values and overlapping frequency acceptance bands.
Adding a new source GPIO requires reviewing and updating the source allowlist as well as the profile.
Protocol 3 uses fixed 1000 ms report windows and a 2000 ms heartbeat lease.

## Local Verification

```sh
python -m unittest discover -s Tools/pixnova_io_test/tests -v
g++ -std=c++14 -Wall -Wextra -Werror -Isrc/systemcmds/gpio_test Tools/pixnova_io_test/tests/edge_counter_test.cpp -o /tmp/pixnova-edge-test
/tmp/pixnova-edge-test
```

On a POSIX host with `g++`, the Python suite also compiles the actual firmware endpoint for both roles against simulated GPIO/HRT services.
It verifies self-check gating, input-only GPIO measurement behaviour, GPIO configuration failures, STOP, USB errors and heartbeat expiry, and passes 126 real endpoint frequency frames through the Python assessment model.
It also compiles the actual board checks with fault-injected buses, including a lost FRAM that still has cached geometry.
The probe flags a sampling callback that takes 50 microseconds or longer as a timing error; that report window cannot pass.
At the current settings, 42 frequency channels require 840,000 GPIO reads per second and roughly 8 KB/s of USB sample payload. ADC voltage samples use a separate, slower protocol phase.
The highest test frequency is about 454.5 Hz, with at least 22 samples per waveform half period.
USB bandwidth and sampling budget must still be verified on the physical fixture; the host demo does not measure MCU execution time.

With a desktop display available, check the interactive diagrams and fault highlighting:

```sh
python Tools/pixnova_io_test/tests/gui_smoke.py
```

To simulate every profile waveform across independent clock offsets and sampling phases:

```sh
python Tools/pixnova_io_test/generate_profile.py --header /tmp/pixnova-waveform/gpio_test_profile.h
g++ -std=c++14 -Wall -Wextra -Werror -Isrc/systemcmds/gpio_test -I/tmp/pixnova-waveform Tools/pixnova_io_test/tests/profile_waveform_test.cpp -o /tmp/pixnova-waveform-test
/tmp/pixnova-waveform-test
```

The software tests and demo do not replace validation on the assembled fixture.
Record the physical wiring, both firmware versions and the exported measurement report for each tested unit.
