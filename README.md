# dbus-goecharger (fork with extended charge control)

Integrate go-eCharger into Victron Energies Venus OS.

This is a fork of [vikt0rm/dbus-goecharger](https://github.com/vikt0rm/dbus-goecharger)
that additionally enables optional, active charge control (Auto/Manual/Scheduled
mode with PV surplus) directly from the Venus OS GUI.

## Purpose

With the scripts in this repo it should be easy possible to install, uninstall, restart
a service that connects the go-eCharger to the VenusOS and GX devices from Victron.
Idea is inspired on @fabian-lauer and @trixing project linked below, many thanks for
sharing the knowledge:

- https://github.com/fabian-lauer/dbus-shelly-3em-smartmeter
- https://github.com/trixing/venus.dbus-twc3

## What this fork adds

### Fixes compared to the original

- **Lifetime energy fixed:** `/Ac/Energy/Forward` incorrectly used `wh` (session
  energy) instead of `eto` (lifetime energy) when `HardwareVersion >= 4` in the
  original. Both values were effectively swapped/wrong.
- **Session energy/time added:** `/Session/Energy` and `/Session/Time` were
  missing entirely. The Venus OS GUI/VRM reads these paths for the "Session"
  display - `/ChargingTime` alone (the only thing the original sets) is marked
  deprecated in the official Victron docs and is no longer used by the GUI for
  the session display.

### New, optional features (via `EnableChargeControl = true` in `config.ini`)

Everything below is **disabled by default**. Without this setting, the script
behaves exactly like the original - monitoring only, `/Mode` stays read-only.

- **Real Auto/Manual/Scheduled charge mode**, controllable directly from the
  Venus OS "Charge mode" page (in the original, this selector has no effect at
  all, see Restrictions below)
- **PV surplus push:** reads PV/grid/battery power from Venus OS
  (`com.victronenergy.system`) and forwards it to the go-e's own Eco mode
  (`pGrid`/`pPv`/`pAkku`), including automatic phase switching handled by the
  go-e firmware itself
- **Scheduled mode:** only enables/disables the go-e's own weekly schedule
  (`sch_week`/`sch_satur`/`sch_sund`) - the time windows themselves continue to
  be managed in the go-e app, not by this script
- **Grid target (`pgt`):** for a battery reserve while PV-surplus charging
- **Battery priority:** the EV only charges once the home battery reaches a
  configurable minimum SOC (with hysteresis against flapping)
- **Battery as a charging buffer:** above a configurable SOC, additional
  battery power is reported as virtual surplus (with hysteresis)
- **Sync on external change:** if the mode is changed directly in the go-e
  app, Venus OS `/Mode` follows automatically

See `config.ini.example` for all options with explanations.

## Empirically tested go-e control behaviour (not officially documented)

The following findings come from systematic tests against a real go-e charger
(firmware 60.5, HW V4) and are **not**, or only incompletely, described in the
official go-e API docs. Behaviour may differ on other firmware versions.

### API endpoint quirks

- `lmo`, `fup`, `pgt` can be set via a **direct query parameter**
  (`GET /api/set?lmo=4`) - works reliably.
- `pGrid`, `pPv`, `pAkku` and the scheduler objects (`sch_week` etc.) must
  instead be set via `ids={"key":value}`. Important: the `ids` parameter must
  be properly URL-encoded (e.g. via `requests`' `params=` mechanism or
  `curl --data-urlencode`) - a manually built, non-encoded query string results
  in `"value must be null or JsonObject"` errors.
- The older `/mqtt?payload=key=value` endpoint (used by the original script
  for `amp`/`alw`/`ama`) worked fine for `amp` in testing, but repeatedly and
  persistently failed for `alw` (error status/no response) - the exact cause
  was never conclusively determined (no correlation with the device's MQTT
  enable/disable setting, which was constant throughout testing). This fork
  therefore uses `/api/set` consistently, which worked reliably for every key
  tested.

### Timing behaviour (PV surplus push)

Measured while continuously sending `pGrid`/`pPv`/`pAkku` every 5 seconds:

| Event | Measured response time |
|---|---|
| Charging starts once surplus is reported (from a stable idle state) | ~30-35 seconds |
| Brief interruption of surplus (at least up to 30s) | no reaction - tolerated |
| Charging stops when surplus is persistently absent | ~2 minutes (120-125s) |
| **Watchdog:** no new values arrive -> stop after | ~6 seconds, after which `pgrid`/`ppv`/`pakku` revert to `null` |

**Important side observation:** After a watchdog gap (>6s without new values),
the *next* incoming value - regardless of its sign - triggers an immediate
session restart (`car` changes within 1-2s). The actual surplus evaluation
(start after ~30s / stop after ~2min) only kicks in on the cycles that follow.
During continuous, gap-free sending (as this script does every 5s), this effect
does not occur during normal operation - only after a restart of the script/service.

**Consequence for `PauseBetweenRequests`:** The interval should stay noticeably
below the 6-second watchdog threshold (recommended: 5000ms, as in the
original), so that a single delayed HTTP request does not already trigger an
unwanted pause.

### The grid target (`pgt`) acts continuously

`pgt` is a persistent config value and continuously feeds into the Eco mode's
current calculation - not just when it is set. Example: with `pGrid=-1800`
(1800W surplus) and `pgt=-200` (200W reserve), the go-e charges at around 6A
instead of the ~7-8A one would expect at a full 1800W, since 200W is subtracted
as a buffer before the resulting charge current (rounded down to whole amps)
is calculated.

## Restrictions

Controlling `/SetCurrent`, `/StartStop` and `/MaxCurrent` works as in the
original. The native Venus OS "Auto" mode for third-party charging stations
(as opposed to the official Victron EVCS hardware) is not supported by the
platform's architecture - Venus OS does not forward any computed values for
this. **This fork solves that** by independently reading the PV surplus values
from Venus OS and passing them directly to the go-e's own Eco mode (see above)
- entirely independent of the (for third-party chargers ineffective) native
Venus OS automatic mode.

Phase switching (1/3-phase) is deliberately left to the go-e's own firmware
logic (`psm`, `spl3`) and is not manipulated by this script.

## Install & Configuration

Get the code:

```
wget https://github.com/Gesiima/dbus-goecharger/archive/refs/heads/main.zip
unzip main.zip "dbus-goecharger-main/*" -d /data
mv /data/dbus-goecharger-main /data/dbus-goecharger
chmod a+x /data/dbus-goecharger/install.sh
/data/dbus-goecharger/install.sh
rm main.zip
```

Check `config.ini` afterwards - most important is `Deviceinstance` and `Host`.
For extended charge control, see `config.ini.example` for all options.

⚠️ After any change to `config.ini` or the `.py` file: run `restart.sh`.

## Used documentation

- https://github.com/goecharger/go-eCharger-API-v2
- https://github.com/victronenergy/venus/wiki/dbus
- https://github.com/victronenergy/venus/wiki/dbus-api

## Attribution

This project is built collaboratively: the coding is done by Claude (Anthropic),
while the features are developed together. All requirements, architectural
decisions, real-hardware testing, and every correction and refinement come from
Gesiima; Claude turns them into code iteratively. No code was written manually -
the implementation happens entirely through step-by-step instructions and joint
review.

## Credits

Based on the work of [vikt0rm](https://github.com/vikt0rm/dbus-goecharger),
inspired by [fabian-lauer](https://github.com/fabian-lauer/dbus-shelly-3em-smartmeter)
and [trixing](https://github.com/trixing/venus.dbus-twc3).
