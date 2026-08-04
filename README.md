# Voltcraft SEM6000 / SPB012BLE Integration for Home Assistant

An extended, independently maintained Home Assistant custom integration for the
**Voltcraft SEM6000** and protocol-compatible **SPB012BLE** Bluetooth Low Energy
power plugs.

> [!IMPORTANT]
> This repository is an independently maintained fork of
> [Anty0/homeassistant-voltcraft_sem6000_spb012ble-integration](https://github.com/Anty0/homeassistant-voltcraft_sem6000_spb012ble-integration).
> It retains the original Git history, license and attribution, but releases and
> issue handling for this extended version are maintained here.

## Project status

Version **2.0.0** is the first stable release of the extended integration. It has
been validated with a Voltcraft SEM6000 using Home Assistant and an ESPHome
Bluetooth proxy. SPB012BLE support is inherited from the upstream integration and
remains best effort; compatibility reports are welcome.

## Features

### Outlet control and live measurements

- Automatic Bluetooth discovery and UI-based setup
- Outlet on/off control and state reporting
- Power, voltage, current and frequency
- Calculated power factor
- Persistent total-energy reading from the plug
- Automatic recovery after temporary disconnects or power loss

### Device settings

- Automatic device-time synchronization after an authenticated connection
- Device name
- Four-digit device PIN entry during setup
- GUI-based device PIN change and reset with post-change login verification
- Night mode / LED ring control
- Over-power limit from 1 to 4000 W
- Over-power protection enable/disable control
- Normal and reduced electricity tariffs
- Reduced-tariff start and end times

### Timers, schedules and random mode

- Read, start and stop the plug's countdown timer
- Start a timer after a duration or at a selected date and time
- Read, add, edit and remove schedules stored in the plug
- Random mode enable/disable control
- Random-mode start and end times
- Individual weekday switches for random mode

### History and diagnostics

- Total energy suitable for Home Assistant long-term statistics
- Device energy history for the last 24 hours, 30 days and 12 months
- Compatibility handling for the observed SEM6000 history-frame checksum quirk
- Vendor, firmware and hardware information when reported by the plug
- BLE connection mode and negotiated ATT MTU
- Diagnostics for the app-compatible initialization sequence

Some history and diagnostic entities are disabled by default and can be enabled
from the Home Assistant entity registry when needed.

## Known limitations

- Development and device validation have primarily used one Voltcraft SEM6000.
- The tested SEM6000 does not expose a usable serial number over either the
  standard Bluetooth Device Information Service or the documented proprietary
  command. The integration therefore uses the Bluetooth MAC address as its stable
  device identifier and does not create a serial-number entity.
- The tested settings response does not reliably report the current
  over-power-protection state. The integration preserves the last
  command-confirmed state instead of forcing the switch back to off.
- Calibration and firmware updates are not implemented.
- The official Voltcraft app and Home Assistant should not control the plug at
  the same time because the device normally accepts only one active BLE client.

## Requirements

- Home Assistant with the Bluetooth integration enabled
- A local Bluetooth adapter or a supported Bluetooth proxy
- Voltcraft SEM6000 or compatible SPB012BLE power plug
- HACS for the recommended installation method

## Installation

### HACS custom repository

1. Open HACS in Home Assistant.
2. Add the following repository as a custom **Integration** repository:

   ```text
   https://github.com/citizenserious/homeassistant-voltcraft_sem6000_spb012ble-integration
   ```

3. Install **Voltcraft SEM6000 / SPB012BLE**.
4. Restart Home Assistant.
5. Open **Settings -> Devices & services** and configure the discovered plug.

### Manual installation

1. Download the desired release archive.
2. Copy the directory below into the Home Assistant configuration directory:

   ```text
   custom_components/voltcraft_sem6000_spb012ble/
   ```

3. Restart Home Assistant.
4. Configure the integration through **Settings -> Devices & services**.

## Migrating from the upstream integration

This fork deliberately keeps the same Home Assistant integration domain:

```text
voltcraft_sem6000_spb012ble
```

The upstream integration and this fork therefore **cannot be installed in
parallel**.

Before migrating, create a Home Assistant backup. Then replace the upstream HACS
custom-repository entry with this repository, install the selected release and
restart Home Assistant. Do not remove the configured Home Assistant integration
unless normal replacement fails, because removing it can also remove registry
entries and entity customizations.

Check entity IDs, automations and the Energy dashboard after the first restart.

## Configuration and advanced controls

The integration is configured entirely through the Home Assistant UI. Setup asks
for the plug's four-digit PIN in a masked password field. The factory default is
`0000`; only ASCII decimal digits are accepted.

Open **Settings -> Devices & services -> Voltcraft SEM6000 / SPB012BLE ->
Configure** for the advanced control menu:

- **Device access**: set the PIN Home Assistant uses to log in, change the PIN on
  the plug, or reset the plug PIN to `0000`.
- **Timer**: create a countdown timer, schedule a switch operation for a date and
  time, inspect the active timer, or stop it.
- **Schedules**: add, edit and remove schedules stored in the plug, including a
  normal weekday multi-selection.
- **Maintenance**: reset consumption data or restore factory settings using
  explicit warning and confirmation forms.

Random mode, tariffs, night mode, over-power settings and outlet control remain
normal entities on the device page. Device-time synchronization and complete
state refresh run automatically after an authenticated connection and are not
exposed as manual buttons.

Version 2.0.0 registers **no Voltcraft-specific services** under **Developer
tools -> Actions** and no Voltcraft button platform. Standard Home Assistant
entity actions such as `switch.turn_on` remain available for normal entities.

### PIN transaction behavior

Some SEM6000 firmware applies a PIN change without returning the expected
acknowledgement before the command timeout. The integration therefore treats the
resulting device state as authoritative:

1. send the PIN-changing command;
2. disconnect and allow the plug to settle;
3. authenticate through a fresh BLE connection using the candidate PIN;
4. store the candidate PIN only after that login succeeds;
5. if it fails, test the previous PIN and leave the stored value unchanged.

The same verified transaction is used for PIN reset and factory reset. A missing
acknowledgement is no longer reported as a definitive failure when the new PIN
actually works.

The stored PIN is never returned to the browser. **Set the PIN used by Home
Assistant** is a repair function only; it does not change the PIN on the plug.
The PIN is stored in the Home Assistant config entry and is included in Home
Assistant backups. UI masking prevents casual disclosure but is not additional
encryption.

### History-frame compatibility

The tested SEM6000 returns the 24-hour and 12-month history in 55-byte responses
and the 30-day history in a 127-byte response. Their checksums do not follow the
normal protocol formula. Version 2.0.0 accepts this exception only for read-only
history commands `0x0A`, `0x0B` and `0x0C` when the header, declared length,
subcommand and `FFFF` suffix are valid.

Authentication, settings, switching and all state-changing commands keep strict
checksum validation. Each history range is requested independently, so a timeout
for one range does not suppress the other two.

### Bluetooth startup recovery

An ESPHome Bluetooth proxy can briefly reject notification subscription while its
scanner or another BLE connection is still settling after a Home Assistant
restart. One matching `GATT Protocol Error: Unlikely Error` during initial setup
is treated as transient and retried after a short delay. Repeated failures are
still reported normally.

## Debug logging

Add the following to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.voltcraft_sem6000_spb012ble: debug
```

Complete BLE notification frames are redacted by the integration before they are
written to the log. Downloaded Home Assistant logs can still contain unrelated
integration data, hostnames, usernames and device identifiers. Review and redact
complete logs before posting them publicly.

## Reporting issues

When reporting a problem, include:

- Integration version
- Home Assistant Core version
- Device model, hardware version and firmware version when available
- Bluetooth path used: local adapter or proxy
- Relevant debug log section with private data removed
- Exact GUI operation or entity action that triggered the problem

Please report issues in this repository, not in the upstream repository, when
they concern features or releases provided only by this fork.

## AI-assisted development

Development of this fork was assisted by generative AI tools for code analysis,
implementation, debugging and documentation. AI-assisted changes are reviewed
and adapted by the maintainer and must pass project tests and device validation
before release. Responsibility for the code, releases and project maintenance
remains with the maintainer.

## Upstream relationship and credits

This project is based on the work by **Jiri Kuchynka (Anty)** in the original
integration. The original Git history and MIT license are retained.

The SEM6000 protocol implementation also builds on publicly available reverse
engineering and additional Android Bluetooth HCI captures used during development
of this fork.

Useful prior protocol work:

- [amasson/hass-voltcraft-sem6000](https://gitlab.youmi-lausanne.ch/amasson/hass-voltcraft-sem6000)

## Contributing

Bug reports, protocol captures, device-compatibility reports and focused pull
requests are welcome. Changes should preserve existing entity unique IDs unless
a documented migration is provided.

## License

Licensed under the MIT License. See [LICENSE](LICENSE).

## Disclaimer

This is an unofficial community integration. It is not affiliated with or
endorsed by Voltcraft or the device manufacturer. Device-control and protection
functions should be tested with a non-critical load before relying on them.
