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
  go-e firmware itself. The go-e's own algorithm computes and regulates the
  actual charge current live - this script does not compute a current itself,
  it only ensures `amp` (a ceiling, not the live value - see below) is raised
  to the device's maximum whenever Auto mode is entered, so the Eco algorithm
  has its full regulation range available.
- **Scheduled mode:** activates the go-e's own "Daily Trip" mode (`lmo=5`) -
  target energy amount, target time, and tariff settings remain fully defined
  in the go-e app; this script only switches the top-level mode, exactly like
  Auto/Manual. **Not** to be confused with the go-e's separate weekly on/off
  timer feature (`sch_week`/`sch_satur`/`sch_sund`, available under Basic
  mode) - an earlier version of this fork mapped "Scheduled" to that timer
  instead, which is a different, independent feature and does not match what
  the go-e app calls "Daily Trip". If you are running an older build, update
  it - the previous mapping is easy to confuse with Daily Trip since both are
  reachable from a "Scheduled"-sounding selector, but behave very differently.
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
(firmware 60.5 and 59.4, HW V4) and are **not**, or only incompletely,
described in the official go-e API docs. Behaviour may differ on other
firmware versions.

### `amp` is a CEILING, not the live-regulated current - this was the root cause of many hours of confusion

**This is the single most important finding in this document.** Extensive
testing initially suggested that the go-e's Eco algorithm does not regulate
the charge current from `pGrid`/`pPv`/`pAkku` at all - `amp` stayed fixed at
6 no matter how large the reported surplus was (`-1800` up to `-4000`), across
two firmware versions, with every documented prerequisite (`lmo=4`, `fup=true`,
`acp=true`, `frm`, tariff configuration, negative `pgt`) correctly set, and
with no HTTP errors, no competing automation, and no timing issues.

**The actual cause: `amp` is not the live-regulated value at all - it is a
ceiling that the Eco algorithm will never exceed.** The real, live-regulated
current is reflected in `nrg[4]` (Amps) / `nrg[11]` (Watts), never in `amp`
itself. Since `amp` had been left at 6 (e.g. from an earlier manual test, or
from Manual mode), the Eco algorithm was silently capped there the entire
time - it may well have been working correctly throughout, just within an
artificially low ceiling that looked, from the outside, exactly like "no
regulation happening at all".

**Confirmed live:** with `amp` explicitly raised to 16, a simulated ~2070W
surplus (corresponding to 9A) made the real current (`nrg[4]`) climb from
~5.6A to ~8.2A and still rising within 40 seconds - clear, genuine regulation
that had been invisible in every prior test simply because the ceiling itself
was the bottleneck being measured, not the algorithm's willingness to
regulate.

**Fix implemented:** whenever Auto mode is (re-)entered, `amp` is raised to
the device's configured maximum (`ama`, exposed on D-Bus as `/MaxCurrent`) -
see `_applyChargeMode()`. This is deliberately **not** the script computing or
choosing a charge current itself - it only removes an artificial constraint
(a leftover low ceiling, e.g. from Manual mode) so the go-e's own Eco
algorithm has its full intended regulation range available, exactly as
apparently expected by the community integrations that report this working
without any external current calculation (e.g.
[marq24/ha-goecharger-api2](https://github.com/marq24/ha-goecharger-api2/blob/main/docs/PVSURPLUS.md)).

One related, unresolved side note: the API key `frm` ("Strommengen Handling"
in the app) has a documented value `2 = PreferPowerToGrid`, which at least one
community report associates with the go-e deliberately choosing a lower
current than the surplus would allow. This device had `frm=2` throughout most
testing; it was changed to `frm=1` ("Standard") before the `amp`-ceiling cause
was found. Whether `frm=2` would have worked fine once the ceiling was also
raised was not isolated/retested - if PV surplus charging ever behaves overly
conservatively going forward, `frm` is worth checking again independently of
the ceiling fix above.

### API endpoint quirks

- `lmo`, `fup`, `frc`, `amp`, `pgt` can be set via a **direct query parameter**
  (`GET /api/set?lmo=4`) - works reliably.
- `pGrid`, `pPv`, `pAkku` and the scheduler objects (`sch_week` etc.) must
  instead be set via `ids={"key":value}`. Important: the `ids` parameter must
  be properly URL-encoded (e.g. via `requests`' `params=` mechanism or
  `curl --data-urlencode`) - a manually built, non-encoded query string results
  in `"value must be null or JsonObject"` errors.
- `alw` returns an HTTP 500 ("tried to set api key without setter") when set
  via a direct query parameter - unlike every other key tested. It works via
  `ids={"alw":...}`, but is then unreliable: while the Eco algorithm considers
  charging justified, it silently reverts `alw` back to `true` within well
  under a second. **`frc`** (force state: 0=Neutral, 1=Off, 2=On), set via a
  direct query parameter, reliably overrides this and is used throughout this
  fork instead of `alw` for starting/stopping charging.
- **`frc` physically clicks the charging contactor/relay on every write**
  (confirmed live - an audible click on the charger itself). This means `frc`
  writes must be minimized, not just for API politeness but to avoid
  unnecessary relay wear. Two real bugs caused by this were found and fixed,
  and the underlying pattern was then generalized:
  1. When entering Auto mode, the code used to unconditionally release
     charging (`frc=0`) and only then, on the very next cycle, evaluate
     battery priority - if the battery SOC was already below the configured
     threshold at that moment, this caused an immediate `frc=0` followed by
     `frc=1` (release, then re-lock within seconds) - two audible clicks for a
     single mode switch. Fixed by evaluating battery priority *before* writing
     any `frc` value when entering Auto mode.
  2. When entering Manual mode, the code used to unconditionally force
     charging off (`frc=1`) as a "safe default" - this stopped an
     already-active charging session (e.g. one running via PV surplus in Auto
     mode) and clicked the relay, even though the intent of switching to
     Manual was only to hand control over to `SetCurrent`/`StartStop` going
     forward, not to stop an ongoing charge. Fixed to use `frc=0` (neutral)
     instead.
  3. **Generalized fix:** every `frc` write anywhere in the script now goes
     through a single `_setFrc()` helper, which tracks the last value
     commanded *globally, across all modes* (`self._lastCommandedFrc`) and
     only actually writes (and only clicks the relay) when the desired value
     differs from that tracked value - regardless of which mode or code path
     is asking for it. E.g. switching Auto -> Manual -> Scheduled -> Auto
     while `frc` stays logically `0` throughout now produces zero writes and
     zero clicks, not one per mode switch.
- The older `/mqtt?payload=key=value` endpoint (used by the original script
  for `amp`/`alw`/`ama`) worked fine for `amp` in testing, but repeatedly and
  persistently failed for `alw` (error status/no response) - the exact cause
  was never conclusively determined. This fork uses `/api/set` consistently.
- **Scheduler enable call (`sch_week`/`sch_satur`/`sch_sund`):** writing the
  full object back via `ids={...}` (needed since these are nested JSON
  objects, not simple scalars - see above) initially failed with
  `ESP_ERR_HTTPD_RESULT_TRUNC` ("URL too long" - the go-e's small ESP32 HTTP
  server has a limited request buffer). Fixed by encoding the JSON compactly
  (`json.dumps(..., separators=(',', ':'))`, no spaces after `:`/`,`), saving
  ~30 characters per call - enough to stay under the buffer limit in testing.
  If this still fails on a device with more/longer configured time ranges (the
  URL length depends on how many ranges are configured in the go-e app), the
  object may simply be too long regardless of compact encoding. **Note:**
  since "Scheduled" now activates Daily Trip (`lmo=5`, see above) instead, all
  three Venus OS modes (Auto/Scheduled/Manual) explicitly *disable* this
  weekly timer - none of them turn it on. If you want to use the go-e's
  separate weekly on/off timer feature, it needs to be managed directly in the
  go-e app; this fork's mode selector does not expose it.
- `amx` (an API v1-only key, documented as *not* persisted to flash, "for PV
  regulation") does not exist on this device's API v2 firmware at all -
  confirmed absent both via a filtered and a full, unfiltered status dump.
  This is not a bug: an official go-e developer confirmed in
  [API-v2#112](https://github.com/goecharger/go-eCharger-API-v2/issues/112)
  that flash write-cycle limitations were fully resolved across the board
  ("NVS with flash wear leveling", introduced ~2 years ago starting with V3
  chargers) - `amp` can simply be used directly on API v2, exactly as
  [evcc's own source code does](https://github.com/evcc-io/evcc/blob/main/charger/go-e.go).

### Timing behaviour (PV surplus push)

Measured while continuously sending `pGrid`/`pPv`/`pAkku` every 5 seconds:

| Event | Measured response time |
|---|---|
| Charging starts once surplus is reported (from a stable idle state) | ~30-35 seconds |
| Brief interruption of surplus (at least up to 30s) | no reaction - tolerated |
| Charging stops when surplus is persistently absent | ~2 minutes (120-125s) |
| Current (`nrg[4]`/`nrg[11]`) ramps up once surplus increases (with `amp` ceiling raised) | visible increase within ~35-40 seconds |
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
is calculated. (This test predates the `amp`-ceiling discovery above and was
incidentally not affected by it, since 6A happened to be at/below whatever
ceiling was in effect at the time.)

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

⚠️ After any change to `.py` file, or to `Deviceinstance`/`Host`/`HardwareVersion`/
`AcPosition`/`EnableChargeControl` in `config.ini`: run `restart.sh`.

Note: the Auto-mode tuning values (`PvGridTarget`, `BatteryPriorityMinSoc`,
`BatteryPriorityHysteresis`, `BatterySupportMinSoc`, `BatterySupportPower`,
`BatterySupportHysteresis`) are re-read directly from `config.ini` on every
Auto-mode cycle (every 5s while in Auto mode) and on every mode switch -
editing them in `config.ini` takes effect on the very next cycle, without a
restart. An earlier version of this fork exposed these values as writable
D-Bus paths under `/Settings/*` for VRM-based editing; this was removed after
testing showed the Venus OS GUI (Remote Console as well as the VRM portal)
only ever renders the fixed set of paths it already knows for the
`evcharger` role - custom paths like these are simply not displayed anywhere,
making that mechanism pointless in practice. Editing `config.ini` directly is
therefore the only supported way to change these values.

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
