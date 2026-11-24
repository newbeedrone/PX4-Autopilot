# PixNova GPIO Test Station

This tool checks external signal continuity by generating distinct square-wave frequencies on PixNova and measuring them independently on `PX4 TEST BOARD_RevA`.
Both boards connect to the same computer over USB CDC.
It is a dedicated manufacturing configuration, separate from the flight firmware and `pixnova_doc` branch.

## Complete Workflow: FMU, IOV2 PWM And Ethernet

The normal **Start test** now runs these stages:

1. Select the **CSV folder**. If the run should restore flight firmware afterwards, select a normal PixNova `.px4` through **Normal firmware → Browse…**. The station validates the board ID, application vectors, size and package before freezing the selected image. Leave the path blank for a measurement-only run.
2. Connect both USB ports and wait for **USB ready**. The boards also report their cached power-on checks during connection. Click **Start test** to run fresh sensor and storage checks and display the results before testing connectors.
3. Test the six simulated-slave I2C connectors, both ADC inputs at two levels, the physical CAN1↔CAN2 bus, **SBUS OUT** at fixture P8.2 / PG5, and the ordinary GPIO groups. PPM is a 156.25 Hz continuity signal from PixNova. GPS2 stays connected. The separate SBUS/DSM receiver simulators are outside the automatic sequence. A failed measurement is recorded and the sequence continues once shutdown is confirmed.
4. After both ordinary GPIO STOP replies, test Ethernet. Select the PC adapter connected to PixNova. If the temporary `192.168.77.1/24` address is missing, click **Configure IP** and approve the Windows prompt once. PixNova uses `192.168.77.2`, UDP port `14570`. If adapter or wiring setup is incomplete, **Retest Ethernet** resumes the waiting stage once it is ready.
5. After Ethernet, measure the eight FMU PWM channels in a fresh GPIO session. After its results and both GPIO STOP replies, move the P21 harness from FMU PWM to IOV2 and click **Confirm IOV2 wiring**. The eight IOV2 outputs are the final measurements.
6. After every stage and its STOP confirmations finish, save a unique CSV containing all signal, protocol, onboard and summary results. If a normal firmware image was selected, restore it over USB after saving the CSV. The source test firmware must advertise `usb_restore: 1`. The USB bootloader board ID and MCU serial must match the tested flight controller before erase. Programming, CRC verification and application reboot are required. Restoration results are added to the CSV. A save failure prevents flashing. A flash or final CSV update failure is displayed and the original measurement CSV is retained.

The test firmware permits an identity-bound `BOOTLOADER` request only on the idle flight controller after successful cleanup. The fixture rejects this command. The bootloader and fixture firmware are retained. Firmware restoration runs in a background worker; the GUI remains responsive. An operator cancellation or unconfirmed STOP never triggers automatic restoration. In DEMO, the same complete sequence and CSV save run, but no USB firmware is changed.

Use **Update Firmware** to update the flight controller independently while the station is idle. Select a valid normal `.px4` and confirm the PixNova device shown in the dialog. The update uses the same USB identity and CRC checks as automatic restoration. **All ports** expands its diagrams in the lower results pane. Use the pane's scrollbar to reach the last port and the event log while the connection and firmware controls remain visible.

Partial connector tests retain signals outside their selected scope as **NOT RUN** and never trigger normal-firmware restoration. They do not claim completion of the full manufacturing suite. When returning from normal firmware to test firmware, fully power-cycle the flight controller and fixture before IOV2/SBUS OUT tests: a USB application reboot does not remove the separate IO MCU's previous setup. A rejected IO idle-state check is retained as a failure; no arming or safety-register override is used.

**Start test** validates the selected normal firmware and CSV folder in a background worker before starting hardware tests. The GUI remains responsive during slow file access; **Stop** cancels preparation. Preparation times out after 30 seconds, and a cancelled, timed-out or disconnected preparation cannot start tests when file access eventually returns. Missing USB connections and preparation errors appear in the main status area.

Manual GPIO cancellation, restart, self-check, connector switching and window handoff also wait for both session-bound STOP replies. Missing replies or cleanup failures block further tests until USB recovery; cancellation and disconnect revoke pending actions. Closing an active station waits for its protocol shutdown as well. Normal-firmware restoration uses the USB endpoints that owned the completed test, even if the port selection boxes were changed during the run.

**Export report** is disabled during active measurement and output shutdown, so a native file dialog cannot interrupt the heartbeat. Manual CSV export uses the same complete schema as automatic saving: signals, protocols, onboard and power-on checks, startup details, restoration and summary rows. GPIO STOP confirmation and errors are included. A failed or aborted run can still be exported after its shutdown attempt; it retains its failure and never triggers normal-firmware restoration.

CSV separates `measurement_status`, `finalization_status` and `workflow_status`. If all measurements pass, the initial CSV remains `PENDING` until normal-firmware restoration and the final CSV update finish. A restoration/save failure produces a failed overall summary even when every interface measurement passed. Measurement failures remain failed when restoration succeeds.

The left connector list shows a green **PASS** after every measurement for that connector passes and a red failure badge if any measurement fails. A mixed connector such as GPS1 includes both its GPIO and I2C results. **IOV2 PWM** opens the P21 diagram and eight output inspectors. **SBUS OUT** opens the P8 / PG5 diagram. **All ports** shows both diagrams alongside the FMU ports, and the pin totals include all 52 FMU/ADC, IOV2 and SBUS signals. The IOV2 PWM and Ethernet tabs also support separate retests.

PC network setup runs in the GUI under Windows Python. **Refresh** lists physical wired adapters with their link state and current addresses. Select the adapter connected to PixNova. If the test IP is missing, click **Configure IP** once and approve Windows authorization. Starting or repeating a test reuses an assigned test address without requesting authorization. A test waiting for the address continues when it becomes ready after configuration or **Refresh**. Configuration retains DHCP, existing IP addresses, the default gateway and DNS. It adds a temporary secondary test address, binds test traffic to that adapter and records ownership by adapter GUID. **Restore** removes only the address created by this application, including after restarting the GUI. A preexisting or externally replaced address is retained. Failure rollback repeats the same ownership checks as Restore; an address replaced or edited during Windows address verification is retained, and an unverified cleanup keeps its journal for a later retry. The address remains available for later tests until restored or cleared by Windows, for example after a restart or adapter reset. Configuration and the administrator helper run in the background so USB heartbeat processing remains responsive; cancelling permission prevents the test from starting. An unplugged cable can be configured, but testing waits for the link and address verification. No Windows network-settings dialog or manual IP command is required. A per-adapter process lock covers the complete configuration or restore transaction, including the ownership journal; a concurrent station receives a busy error and can retry after the first operation finishes. The same controls are available in the protocol station. DEMO uses a simulated adapter and never changes PC settings.

The current paired firmware must advertise `io_pwm: 1`, `sbus_out: 1` and `session_stop: 1`. Update both boards together; the already-installed IO-v2 firmware uses its standard serial register protocol and is not reflashed. Start from idle IO firmware with zero arming, zero disarmed outputs and no enabled auxiliary output features. The normal RSSI ADC default is allowed and retained; SBUS output is enabled only during its dedicated test. Power-cycle after installing the flight-controller test image if the previous flight firmware left IO configured.

| IOV2 output | IO-v2 MCU pin | Fixture input | Test frequency |
| --- | --- | --- | --- |
| PWM1 | PA0 | P21.8 / PI0 | 50 Hz |
| PWM2 | PA1 | P21.7 / PH12 | 60 Hz |
| PWM3 | PB8 | P21.6 / PH11 | 75 Hz |
| PWM4 | PB9 | P21.5 / PH10 | 90 Hz |
| PWM5 | PA6 | P21.4 / PD13 | 110 Hz |
| PWM6 | PA7 | P21.3 / PD14 | 130 Hz |
| PWM7 | PB0 | P21.2 / PH6 | 160 Hz |
| PWM8 | PB1 | P21.1 / PH9 | 200 Hz |

P21.9 is common ground; leave power pins disconnected and remove all actuators for this manufacturing test. Because IO timers share rate groups, channels are tested sequentially, each with a session-derived pulse width. The fixture measures actual frequency within ±2% over three fresh report windows and rejects activity on unselected inputs. Each accepted source window requires a distinct receiver report with advancing sequence and edge count. A missing receiver report or loss of signal resets the consecutive-window count. The GUI shows the IO MCU pin, fixture pin and measured frequency; failed outputs turn red. Click an output card for its frequency error, verified-window count and failure reason; **Retest** runs only that output and counts only its current result. The IOV2 tab works independently after the two USB handshakes, without running the FMU test first.

The source checks both direct-command storage and the applied servo page, PWM rates and running flags. It allows the first write to settle until IO's next mixer tick; subsequent mismatches fail. A measured nonzero frequency outside tolerance fails immediately rather than waiting for the overall protocol timeout.

To open directly at either independent test page:

```sh
python Tools/pixnova_io_test/app.py --iov2
python Tools/pixnova_io_test/app.py --ethernet
```

Both commands open the ordinary station and wait for USB connection and the operator's test action; they do not enable outputs automatically.

Only the IOV2 test adapter can write the selected direct PWM channel, zero disarmed values and PWM rates. Arming, safety-switch, mixer, failsafe and bootloader writes are rejected. STOP zeros the outputs, clears the temporary direct-PWM state, restores saved rates and reads back all three rate registers before confirming shutdown. A write acknowledgement without matching readback retains the pending cleanup and blocks the next test until cleanup succeeds. If the FMU stops running, the zero disarmed values suppress pulses after the IO firmware's 500 ms FMU timeout; loss of host heartbeat has the two-second FMU lease plus this IO timeout.

Ethernet PHY startup has a bounded 20-second negotiation period; no application datagrams are sent before STARTED. The normal running heartbeat lease remains two seconds. The first valid UDP reply establishes communication and may follow unanswered startup probes while the network resolves the peer address. Measurements still require at least 20 valid exchanges, three consecutive report windows and confirmed shutdown. An unanswered measurement packet, bad packet or link loss fails the test. Reports include `iov2_tests`, `iov2_passed`, `ethernet_test`, `ethernet_passed`, `pc_ethernet` and `extended_suite`.

## SBUS OUT On Fixture PG5

Connect the flight-controller **SBUS OUT** signal to fixture **P8.2 / PG5** and ground to **P8.3**; leave P8.1 power disconnected. The RevA schematic calls this PG5 net `I2C2_DRDY_BAR1`, but P8 is the SBUS OUT connector. This test is separate from the **SBUS receiver input** simulator, which sends in the opposite direction through fixture UART8.

The left connector list includes **SBUS OUT**, with its own PASS/FAIL status. Selecting it opens the P8 pin diagram, PG5 inspector and IOV2 test controls; selecting a port does not start outputs. The same P8 diagram appears in **All ports** and, after a failure, **Failed pins**. **Retest SBUS OUT** runs it independently after both USB handshakes. The inspector shows IO PB10 → P8.2 / PG5, measured frame rate, decoded frame count, verification windows and failure reason. Failed or missing frames turn the sidebar entry, connector pin, card and signal path red.

The flight controller requests SBUS1 at 50 frames/s through PX4IO, continually confirms the feature/rate/applied servo registers and keeps all eight PWM commands zero. PG5 remains input-only. Its EXTI edge timestamps decode inverted 100000 baud 8E2; the 50 μs GPIO/PWM sampler is not used. Every 25-byte frame must have valid parity and stop bits, header, flags, footer and a complete zero-output payload. Supported IO encodings are all sixteen raw channels zero, or the legacy first eight raw channels 0x7ff with the remaining eight zero; mixed or arbitrary channel patterns are rejected. A requested 50 Hz SBUS rate accepts 40–55 measured frames/s to allow the installed IO mixer's scheduling. Each complete report window must also contain 40–55 valid frames per second over its actual elapsed duration, with one boundary frame allowed for quantization. Snapshot and window reset are atomic and occur at report time, including when a report falls between the 100 ms processing callbacks. Three fresh valid windows are required; a short 50 Hz burst followed by silence cannot pass.

STOP restores the saved IO features (including the original RSSI selection), SBUS rate and PWM configuration, verifies the readback and waits for both USB acknowledgements. Cleanup failures block further runs until STOP succeeds or the USB endpoints are reconnected. No arming/safety/mixer/bootloader writes are permitted. With a stopped FMU, the installed IO firmware can continue transmitting its zero-output SBUS frames until the feature is disabled or IO loses power; an absent STOP acknowledgement cannot pass. Reports include `sbus_out_test` and `sbus_out_passed`; the full workflow requires this result as well as the eight PWM results and Ethernet.

The fixture protocol image uses NuttX RAM vectors in uncached DTCM and a direct high-priority EXTI5–9 handler for PG5, so ordinary RTOS critical sections do not delay its edges. The handler makes no RTOS, USB, floating-point or HRT API calls; it extends the existing 1 MHz TIM8 counter, also refreshed by the 1 ms watchdog during quiet periods. It owns the shared IRQ only during SBUS OUT, rejects other active EXTI6–9 owners, briefly masks its NVIC line for snapshots, and restores the normal vector and priority on STOP. The decoder follows each UART byte's bit clock rather than accumulating edge-to-edge timestamp rounding. Failed frames remain failures; reports retain capture diagnostics (error code, byte, bit, interval and phase) for timing, missing-edge, parity, stop-bit, length and payload faults. The fixture RTC uses internal LSI to avoid an unavailable external LSE clock blocking USB startup; frequency measurements continue to use TIM8, independently of RTC.

The probe configuration excludes DTCM from the system heap (`CONFIG_STM32H7_DTCMEXCLUDE=y`) so allocator metadata cannot overwrite the vector table. The build checks both this setting and the ELF vector-table address/alignment before staging a protocol probe image. Flight-controller firmware retains its existing memory and RTC configuration.

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
The confirmed TELEM1, TELEM2, TELEM3, GPS2 and UART4 harnesses cross TX and RX:

| Flight-controller signal | Fixture receive position | Expected frequency |
| --- | --- | --- |
| TELEM1 TX, PE8 | P15.5, PF6, UART7 RX | 10.000 Hz |
| TELEM1 RX, PF6 | P15.4, PE8, UART7 TX | 10.893 Hz |
| TELEM2 TX, PC12 | P18.5, PD2, UART5 RX | 14.124 Hz |
| TELEM2 RX, PD2 | P18.4, PC12, UART5 TX | 15.385 Hz |
| TELEM3 TX, PD5 | P20.5, PA3, USART2 RX | 19.920 Hz |
| TELEM3 RX, PA3 | P20.4, PD5, USART2 TX | 21.739 Hz |
| GPS2 TX, PE1 | P3.5, PE0, UART8 RX | 51.546 Hz |
| GPS2 RX, PE0 | P3.4, PE1, UART8 TX | 56.180 Hz |
| UART4 TX, PH13 | P17.6, PH14, UART4 RX | 60.976 Hz |
| UART4 RX, PH14 | P17.5, PH13, UART4 TX | 66.667 Hz |

The GPIO tests follow this fixed mapping in [wiring.csv](wiring.csv).
Each GUI row names the flight-controller signal and displays its fixture GPIO
and pin. The fixture connector diagrams retain their schematic signal names.
GPS1 UART and CTS/RTS keep their existing mappings. Acceptance still
requires the assigned signal's frequency within tolerance for three consecutive
windows; a different signal's frequency fails and is identified when possible.
This checks digital signal continuity, without transmitting UART data or testing
an attached buzzer's sound output.

Only I2C SCL/SDA use simulated-slave transactions. The GPS1/GPS2 I2C cards do not
replace the frequency tests for the UART or buzzer pins on the same connector.

SPI6 also exposes `SPIX_SYNC`, PE9, at fixture P9 pin 7. It is protected as shared sensor synchronization and is outside the eight SPI6 GPIO frequency channels. The GUI labels it **Shared sensor sync · NOT TESTED**; the grey state is not a passing measurement.

## ADC3V3 And ADC6V6 Voltage Tests

Both external ADC signals are tested as flight-controller inputs. The fixture
applies 0 V and nominal 3.3 V through the existing ADIO harness; PixNova samples
the selected input using ADC1. Neither PC2 nor PC3 generates GPIO waveforms.

| Input | Fixture stimulus | Flight-controller ADC | Low acceptance | High acceptance |
| --- | --- | --- | --- | --- |
| ADC3V3 | P10.6 / PG10 | PC3_C / ADC1 channel 13 | 0–0.100 V | 3.000–3.400 V, nominal 3.3 V |
| ADC6V6 | P10.7 / PF13 | PC2_C / ADC1 channel 12 | 0–0.100 V | 1.500–1.800 V, nominal 1.65 V after the 1:2 divider |

Both boards must provide fresh readback for three report windows at each level.
Each input has its own GUI card, low/high readbacks and pin status; a failure on
one input does not mark the other input as passed or failed. **Retest ADC** on a
card runs that input's two levels. The normal **Start test** runs all four levels
after the six I2C tests. Reports require both inputs for `adc_passed`.

These checks verify the external input path at 0 V and nominal 3.3 V stimulus.
They do not calibrate ADC accuracy or test the full ADC6V6 input range. Fixture
STOP, USB/heartbeat expiry and test completion release the selected stimulus
pin to an input. Both firmware images advertise `adc_levels: 2`; the complete
station rejects the older single-input voltage firmware before starting.

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
| Ethernet | Correct PixNova Ethernet adapter to a real PC Ethernet interface. Flight controller `192.168.77.2/24`, PC `192.168.77.1/24`, UDP port `14570`. Choose the adapter in the GUI and click Configure IP if the test address is missing. Retest Ethernet reuses the configured address. Disconnect fixture P2. |

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

The profile covers 43 signals: 41 GPIO frequency tests and two ADC input voltage tests. All I2C SCL/SDA signals are excluded from waveform generation, including GPS1 and POWER1.
It includes TELEM1–3, GPS UARTs, UART4, NFC, external SPI signals, PPM, eight FMU PWM signals, capture/armed signals, and the two external ADC signal paths.
ADC3V3 and ADC6V6 both use the separate input voltage tests above.
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
- PC2/PC2_C and PC3/PC3_C: ADC6V6 and ADC3V3 input voltage tests only.
- SPI1/2/3 IMUs, SPI5 FRAM and PE9 sensor synchronisation.
- PX4IO UART and PWM: owned through the checked IOV2 protocol test, never FMU GPIO waveforms. DSM/SBUS receiver input and SBUS OUT each use a separate protocol test. RSSI remains excluded.
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
After a disconnect, stop the active operation and use **Connect USB** again. Reconnecting clears the displayed results and event log. The selected firmware, CSV folder and saved reports remain available. Messages from a previous connection cannot change the new session.
Select **Connect**, then **Start test**.
The ordinary station’s **Start test** runs the sequence above. Each GPIO connector group is assessed for up to six seconds before its result is retained. Individual connector buttons remain available. SCL/SDA, PC2 and PC3 are never waveform outputs.

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
The complete workflow ends with **TEST COMPLETE**, showing the signal count,
connector-check count and onboard-check count separately. A GPIO failure cannot
be hidden by passing I2C/ADC checks. Onboard faults remain independent and visible.
Each connector test enters `STOPPING` after measurement. It reports PASS and starts
the next test only after every participating board returns a fresh `stopped`
reply for that session with reason `requested`. Heartbeats cease during this
three-second wait. Missing replies abort the queue with **STOP unconfirmed**;
reconnect USB before testing again. Operator cancellation also waits for these
replies. The main window's Stop button controls the protocol window when it owns USB.
Select a connector in the sidebar to access its **Retest** controls. GPIO retests
assess one connector at a time and retain the other results. A completed GPIO
group can also be retested while the full workflow waits for Ethernet or the
P21 harness change. Connect P21 to FMU PWM before retesting that group.
I2C, ADC, CAN, SBUS OUT, Ethernet and IOV2 have their own retest buttons.
**Retest Onboard** refreshes the flight controller's onboard checks together.
Exports identify the latest protocol batch in `selected_connector_modes` and
`selected_connector_results`.
The completed report is frozen with its measurement timestamp. Disconnecting USB
after completion updates the connection indicators and appends an offline notice;
it does not turn recorded measurements into failures. Exports include the current
`usb_connected` state while retaining the completed measurement values. A new
connection, self-check or test clears the completed snapshot and requires fresh
checks. Disconnects during an active measurement still stop outputs and fail that
run; incomplete measurements cannot be converted into completed passes.

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
The measurement board receives waveforms in GPIO frequency mode. Its dedicated I2C and ADC modes emulate an I2C slave or supply a test voltage.

The GUI's **PixNova onboard checks** panel has separate **Sensors** and **Storage** tabs.
Each device has its own green/red result, bus location and observed ID or read/capacity value.
Click a device for details, including the immutable power-on result and the latest pre-test result.
Only the flight controller USB connection is needed to view or rerun these checks.

| Flight-controller device | Bus | Read-only check |
| --- | --- | --- |
| ICM45686 #1 / #2 / #3 | SPI1 / SPI2 / SPI3 | Individual WHO_AM_I reads, expected `0xE9` |
| BMM150 | I2C4 / `0x10` | CHIP_ID `0x32` |
| BMP388 | I2C4 / `0x76` | CHIP_ID `0x50` |
| ICP-20100 | I2C2 / `0x63` | Initialise the I2C interface, verify DEVICE_ID `0x63` and revision `0x00` or `0xB2`, then read `0x00` to finish |
| Base EEPROM, 24LC64T | I2C3 / `0x51` | 16-byte samples at offsets 0 and 8176 of the configured 8 KiB device |
| IMU EEPROM, 24LC64T | I2C4 / `0x50` | The same two read samples; calibration data is not modified |
| SPI FRAM, FM25V02/A family | SPI5 / CS PG7 | Live nine-byte manufacturer and device ID, 32 KiB driver geometry, and reads at both ends |
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
Outputs stay disabled while checks are running. External interface eligibility depends on the clock, GPIO setup and fixture checks. The flight controller's sensor and storage results still determine the onboard verdict.
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
A failed or missing external interface check blocks output in the GUI and in firmware.
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
STOP <32-character-hex-request-session>
```

HELLO is followed by cached `power_check` records and a `power_checked` summary.
Responses also include `check`, `checked`, `started`, `heartbeat`, `sample`, `stopped` or `error` frames.
The session-bearing STOP acknowledges the request session even if a queued PCHECK was cancelled before firmware executed it. A cleanup failure returns `cleanup_failed` and cannot be accepted as confirmed shutdown.
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
It verifies self-check gating, input-only GPIO measurement behaviour, GPIO configuration failures, STOP, USB errors and heartbeat expiry, and passes 123 real endpoint frequency frames through the Python assessment model.
It also compiles the actual board checks with fault-injected buses, including a lost FRAM that still has cached geometry.
The probe flags a sampling callback that takes 50 microseconds or longer as a timing error; that report window cannot pass.
At the current settings, 41 frequency channels require 820,000 GPIO reads per second and roughly 8 KB/s of USB sample payload. ADC voltage samples use a separate, slower protocol phase.
The highest test frequency is about 454.5 Hz, with at least 22 samples per waveform half period.
USB bandwidth and sampling budget must still be verified on the physical fixture; the host demo does not measure MCU execution time.

With a desktop display available, check the interactive diagrams and fault highlighting:

```sh
python Tools/pixnova_io_test/tests/gui_smoke.py
python Tools/pixnova_io_test/tests/protocol_gui_smoke.py
python Tools/pixnova_io_test/tests/extended_gui_smoke.py
python Tools/pixnova_io_test/tests/iov2_gui_smoke.py
python Tools/pixnova_io_test/tests/sbus_out_gui_smoke.py
```

To simulate every profile waveform across independent clock offsets and sampling phases:

```sh
python Tools/pixnova_io_test/generate_profile.py --header /tmp/pixnova-waveform/gpio_test_profile.h
g++ -std=c++14 -Wall -Wextra -Werror -Isrc/systemcmds/gpio_test -I/tmp/pixnova-waveform Tools/pixnova_io_test/tests/profile_waveform_test.cpp -o /tmp/pixnova-waveform-test
/tmp/pixnova-waveform-test
```

The software tests and demo do not replace validation on the assembled fixture.
Record the physical wiring, both firmware versions and the exported measurement report for each tested unit.
