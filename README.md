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

## Fixes compared to the original

- **Lifetime energy fixed:** `/Ac/Energy/Forward` incorrectly used `wh` (session
  energy) instead of `eto` (lifetime energy) when `HardwareVersion >= 4` in the
  original. Both values were effectively swapped/wrong.
- **Session energy/time added:** `/Session/Energy` and `/Session/Time` were
  missing entirely. The Venus OS GUI/VRM reads these paths for the "Session"
  display - `/ChargingTime` alone (the only thing the original sets) is marked
  deprecated in the official Victron docs and is no longer used by the GUI for
  the session display.
- **`/Connected` now reflects actual reachability.** The original (and this
  fork, until now) only ever set `/Connected = 1` once at startup and never
  updated it again. `/Connected` (and `/Status`) now correctly switch to
  "disconnected" if the go-e becomes unreachable (e.g. a mobile wallbox on a
  different network) and back once it responds again - see "Findings" below
  for the full story.

## What this fork adds

Everything below is **disabled by default** except `/AutoStart`. Without
`EnableChargeControl`, this script behaves exactly like the original -
monitoring only, `/Mode` stays read-only.

- **Real Auto/Manual/Scheduled charge mode**, controllable directly from the
  Venus OS "Charge mode" page (in the original, this selector has no effect at
  all - see Restrictions below).
- **PV surplus push:** reads PV/grid/battery power from Venus OS and forwards
  it to the go-e's own Eco mode (`pGrid`/`pPv`/`pAkku`), including automatic
  phase switching handled by the go-e firmware itself. The go-e's own
  algorithm computes and regulates the actual charge current live - this
  script does not compute a current itself.
- **Scheduled mode:** activates the go-e's own "Daily Trip" mode (`lmo=5`) -
  target energy, time, and tariff settings remain defined in the go-e app;
  this script only switches the top-level mode.
- **Grid target (`pgt`):** read (not written) to optionally compensate for its
  effect on the battery buffer feature below - configure the actual value
  directly in the go-e app.
- **Battery priority:** the EV only charges once the home battery reaches a
  configurable minimum SOC (with hysteresis against flapping).
- **Battery as a charging buffer, with a force-start tier:** above a
  configurable SOC, additional battery power is reported as virtual surplus
  (optionally gated on real PV also being present); above a second, higher
  SOC, this applies unconditionally even with zero PV.
- **`/AutoStart` repurposed as a configurable phase-switching override** -
  see its own section below (works independently of `EnableChargeControl`).
- **Sync on external change:** if the mode (or, for `/AutoStart`, `psm`) is
  changed directly in the go-e app, the corresponding Venus OS path follows
  automatically.

See `config.ini.example` and "Configuration reference" below for every
option, and "Findings" for the reasoning and live-testing evidence behind
each of these.

## `/AutoStart` repurposed as a phase-switching override

Like `/StartStop` and `/SetCurrent`, this works regardless of the
`EnableChargeControl` setting.

Victron's official `evcharger` dbus spec documents `/AutoStart` as "start
automatically when a vehicle is connected" - a concept the go-e has no direct
equivalent for. Since this path is otherwise permanently non-functional
(neither the original script nor this fork historically wrote anything to
it, leaving the GUI button greyed out with nothing behind it), it is
deliberately **repurposed** here for something genuinely useful instead:
manual override of go-e's phase-switching logic (`psm`).

**What the two toggle positions actually do is configurable** via
`AutoStartMode` in `config.ini` - read once at startup only (a config edit
needs a service restart to take effect, since this defines the meaning of a
control path rather than a tuning value):

- `AutoStartMode = 0` (**default**): disabled - the button has no function at
  all, `psm` is never touched. This is the default deliberately: a repurposed
  control path should never be silently active on an installation that
  didn't ask for it, matching how `EnableChargeControl` also defaults to off.
- `AutoStartMode = 1` ("1P-Auto"): `/AutoStart = 0` -> `psm = 1`
  (force single-phase); `/AutoStart = 1` -> `psm = 0` (**Auto** - go-e's own
  live, surplus-based 1-/3-phase switching).
- `AutoStartMode = 2` ("3P-Auto"): `/AutoStart = 0` -> `psm = 2` (force
  three-phase); `/AutoStart = 1` -> `psm = 0` (Auto).
- `AutoStartMode = 3` ("1P-3P"): `/AutoStart = 0` -> `psm = 1` (force
  single-phase); `/AutoStart = 1` -> `psm = 2` (force three-phase) - `psm = 0`
  (Auto) is never used in this mode.

An invalid or unrecognized value (e.g. `true`, or a number outside 0-3) logs
a warning and falls back to `0` rather than preventing the service from
starting.

This was added because a household with little PV surplus most of the time
may prefer to default to forced single-phase and only occasionally check
whether enough surplus exists for 3-phase, rather than constantly running
Auto - modes 1 and 3 both suit that use case, depending on whether the
"probing" toggle position should hand control back to Auto or force 3-phase
outright.

**Important caveat, by direct analogy with the `frc` relay-click findings
below:** phase switching is reported by other users to involve a real, timed
contactor changeover (~10s to complete), not a soft parameter - so this
control goes through the same "only write if the value actually changed"
tracking as `frc` does, to avoid unnecessary physical switching. **Confirmed
live** that the toggle correctly forces single-phase charging via the go-e
app.

**External change detection:** if `psm` is changed directly in the go-e app
instead of via the Venus OS toggle, this is detected the same way external
`lmo` changes are, and `/AutoStart` is updated to match - the exact mapping
depends on `AutoStartMode` (mode 0 leaves `/AutoStart` untouched entirely,
since the toggle has no function to reflect).

## Restrictions

Controlling `/SetCurrent`, `/StartStop` and `/MaxCurrent` works as in the
original. The native Venus OS "Auto" mode for third-party charging stations
(as opposed to the official Victron EVCS hardware) is not supported by the
platform's architecture - Venus OS does not forward any computed values for
this. **This fork solves that** by independently reading the PV surplus
values from Venus OS and passing them directly to the go-e's own Eco mode -
entirely independent of the (for third-party chargers ineffective) native
Venus OS automatic mode.

Phase switching (1/3-phase) is deliberately left to the go-e's own firmware
logic (`psm`, `spl3`) and is not manipulated by this script beyond the
`/AutoStart` override above.

**Visible startup confirmation even at `Logging = WARN`:** at `WARN`,
essentially all other logging is filtered out - a restarted service left no
confirmation that it had actually started successfully. One line is logged
at `WARNING` level specifically so it remains visible regardless of the
configured level: `"dbus-goecharger started successfully..."`. (A periodic
heartbeat at `WARNING` was tried too, but reverted - that level is meant for
something actually noteworthy, not routine "still running" confirmation.
Switch `Logging` to `INFO`/`DEBUG` temporarily for periodic confirmation.)

## Configuration reference

`config.ini.example` intentionally only has short comments - full
explanations for every option are here instead, grouped the same way as in
the file.

### Basic (unchanged from the original)

**`AccessType`, `SignOfLifeLog`, `Deviceinstance`, `HardwareVersion`,
`AcPosition`, `Host`, `PauseBetweenRequests` are all required** - there is no
code-level fallback for any of them, so the service will crash on startup if
one is missing. Every other setting in this document has a code-level
default and can be safely omitted.

- **`AccessType`** *(restart)*: always `OnPremise`.
- **`SignOfLifeLog`** *(restart)*: minutes between periodic status log entries.
- **`Deviceinstance`** *(restart)*: this device's Venus OS D-Bus instance number.
- **`HardwareVersion`** *(restart)*: go-e hardware generation. Only affects the
  temperature reading (`/MCU/Temperature`) - energy calculation (`eto`) is
  handled uniformly via the API v2 unit (Wh) in this fork, since
  `/api/status` always returns API v2 data.
- **`AcPosition`** *(restart)*: `0` = AC Output (critical loads), `1` = AC Input.
- **`Logging`** *(restart)*: `DEBUG`/`INFO`/`WARN`/etc. At `WARN`, one line is still
  logged on successful startup regardless of this setting - see
  "Restrictions" above.

### Charge control master switch

- **`EnableChargeControl`** *(restart)*: when `false` or omitted, this script behaves
  exactly like the original - monitoring only, `/Mode` not writable, no
  writes to `lmo`/`fup`/scheduler, no PV surplus push. Every option below
  this line has no effect in that case.

### Battery priority (Auto mode only)

The EV only starts charging once the home battery has reached a configured
SOC.

- **`BatteryPriorityMinSoc`** *(live)*: `0` or omit = feature disabled (default `0`).
- **`BatteryPriorityHysteresis`** *(live)*: in percentage points. Charging pauses
  below `BatteryPriorityMinSoc` and is only released again once SOC reaches
  `BatteryPriorityMinSoc + hysteresis` - prevents flapping right at the
  threshold. Default `2`.

### Battery as a charging buffer (Auto mode only)

Above a second, higher SOC, a configurable amount of home battery power is
allowed to help charge the EV too (virtual surplus added to `pGrid`).

- **`BatterySupportMinSoc`** *(live)*: `0` or omit = feature disabled (default `0`).
- **`BatterySupportPower`** *(live)*: additional power in W reported to the go-e as
  virtual surplus. Should not exceed the system's actual discharge limit,
  otherwise the difference is drawn from the grid instead. Default `0`
  (meaning: no effect until you set both this and `BatterySupportMinSoc`).
- **`BatterySupportHysteresis`** *(live)*: in percentage points - the buffer stays
  active down to `BatterySupportMinSoc - hysteresis`. Default `2`.
- **`BatterySupportCompensatePgt`** *(live)*: `1` (not `true` - this is read as a
  number) compensates for `pgt`'s continuous reserve while the buffer is
  active, by adding `pgt`'s magnitude on top of `BatterySupportPower` -
  without this, `pgt` (configured in the go-e app, see below) reduces the
  amount that actually reaches the charge current calculation below what you
  configured here. The exact relationship between `pgt` and the resulting
  current reduction has only been empirically observed, not confirmed as a
  precise formula - this is a reasonable approximation, not a guaranteed
  exact match. `0` or omit = disabled (default).
- **`BatterySupportMinPv`** *(live)*: minimum real PV production (`pPv`, in W)
  required alongside the SOC threshold before the virtual surplus boost is
  actually applied. Without this, the SOC threshold alone is sufficient to
  trigger the boost - a fully-charged battery sitting above the threshold at
  night, with no PV production at all, would still make the go-e think there
  is solar surplus and start charging purely because of a high SOC, with no
  sun involved. This does not affect the SOC-based eligibility flag itself
  (`_batterySupportActive`, which still only uses the hysteresis logic above,
  so it doesn't flap due to momentary PV fluctuations) - it only gates
  whether the boost is actually sent on a given cycle. `0` or omit = disabled
  (default, SOC-only behaviour, matching the original implementation) - this
  reliably means "no gating at all" even if real `pPv` happens to read
  slightly negative.
- **`BatteryForceStartSoc`** *(live)*: a third, higher SOC threshold (evcc calls the
  equivalent setting on its Battery page "bufferStartSoc") - above this
  level, `BatterySupportMinPv`'s gate is bypassed entirely and the boost
  applies unconditionally, even with zero real PV. Tracked with its own
  hysteresis state (reusing `BatterySupportHysteresis`), independent of the
  `BatterySupportMinSoc` eligibility flag. Together, the three thresholds
  form a coherent tier system, e.g. `BatteryPriorityMinSoc=60`,
  `BatterySupportMinSoc=90` (needs real PV too), `BatteryForceStartSoc=95`
  (starts regardless of PV): below 60% the EV doesn't charge at all; 60-90%
  is normal PV-only Auto behaviour; 90-95% additionally allows the battery to
  help, but only alongside genuine PV production; above 95% the battery
  alone is considered enough to start charging even in complete darkness.
  `0` or omit = disabled (default).

### Rolling-average reset after leaving Auto mode

- **`PvAverageResetCycles`** *(live)*: how many cycles of pushing `pGrid`/`pPv`/`pAkku`=0
  to send after leaving Auto mode (see "Findings" below for the full
  background). `0` or omit = disabled (default) - the go-e's rolling
  averages then simply stay frozen at whatever they were until real values
  are pushed again. `5` was confirmed reliable in real Auto-mode operation
  when this was tested, but defaults to off, consistent with every other
  feature in this fork defaulting to disabled until explicitly enabled - and
  because the exact mechanism behind why the go-e's averages behave the way
  they do remains a partial black box (see "Findings" below), so a value
  that worked in one specific test shouldn't be silently active everywhere.

### Grid target (`pgt`)

Not configured here - set it directly in the go-e app instead (App: PV
surplus -> Grid target / Power preference). See "Findings" below for the
reasoning. This script still reads the current value (only for
`BatterySupportCompensatePgt` above), never writes it.

### `/AutoStart` button function

- **`AutoStartMode`** *(restart)*: see the dedicated section above for the full
  enumeration and reasoning. Read once at startup only - **a config.ini edit
  here needs a service restart**, unlike every other option on this page.

### Preventing home battery discharge during Manual charging

- **`PreventBatteryDischarge`** *(live)*: `0` or omit = disabled (default).
  When `1`, the battery's current SOC is temporarily written to
  `/Settings/CGwacs/BatteryLife/MinimumSocLimit` while the car is actually
  charging (go-e `car==2`) in Manual **or** Scheduled mode, restoring the
  original value once charging stops or the option is disabled - the same
  mechanism evcc uses for Victron systems. **Tied to actually charging, not
  merely to the mode itself** - switching to Manual is also commonly used
  just to disable Eco mode without charging at all (e.g. to use
  `/AutoStart`'s phase-switching override), and the lock must not engage in
  that case (found live - see "Findings" below). Applies in both Manual and
  Scheduled, since Scheduled (the go-e's own Daily Trip) can draw on the
  battery outside genuine PV surplus too; Auto is deliberately excluded, as
  it already has its own PV-aware battery logic
  (`BatterySupportMinSoc`/`BatteryForceStartSoc`). See "Findings" below for
  more reasoning, why this mechanism was chosen over other candidates, and
  **importantly, a real, repeatedly-reported failure mode from evcc's own
  equivalent feature that applies here too** - read this before enabling.
  Checked every cycle, so it correctly reacts either via the Venus OS
  `/Mode` switch or an externally-detected go-e app mode change. A manual
  change to `MinimumSocLimit` directly in the Venus OS GUI while the lock
  is already active is detected and adopted as the new
  value to restore to later, rather than being silently overwritten.
- **`CheckEssMinSocAtStartup`** *(restart)*: `0` or omit = disabled (default). When `1`,
  `ExpectedEssMinSoc` (below) is compared once at every service start
  against the actual `/Settings/CGwacs/BatteryLife/MinimumSocLimit`; a mismatch is treated
  as evidence the lock above was left stuck active after a crash/unclean
  shutdown (see "Findings" below) and is corrected automatically. A
  one-time startup check only, not a continuous watchdog - deliberately
  kept simple rather than building more elaborate monitoring. Follows this
  fork's normal enable/disable pattern, unlike `ExpectedEssMinSoc` itself,
  which can't (`0` is a genuinely valid `MinimumSocLimit` on some systems).
- **`ExpectedEssMinSoc`** *(restart)*: the value to compare against - only consulted
  when `CheckEssMinSocAtStartup=1`. If the switch is on but this isn't set,
  a warning is logged and the check is skipped rather than failing.

### `[ONPREMISE]`

Both required (no code-level fallback).

- **`Host`** *(restart)*: the go-e's IP address.
- **`PauseBetweenRequests`** *(restart)*: poll interval in ms. Must stay at or below
  5000, since the go-e expects `pGrid`/`pPv`/`pAkku` to be updated at least
  every 5 seconds in Auto mode (see "Findings" below).

### What needs a restart, and what doesn't

Every option above is now individually marked *(live)* or *(restart)*. In
short: `AutoStartMode`, `CheckEssMinSocAtStartup`, and `ExpectedEssMinSoc`
need a restart because each defines the meaning of a control path or is
only ever consulted once, at genuine startup. `AccessType`, `Deviceinstance`,
`HardwareVersion`, `AcPosition`, `Logging`, `Host`, `PauseBetweenRequests`,
and `EnableChargeControl` are also read once at startup - these still need
`restart.sh` after editing, since nothing re-reads them mid-run. Everything
else is actively re-read every Auto-mode cycle (or, for
`PreventBatteryDischarge`, every cycle regardless of mode) without
needing a restart at all.

## Findings

Everything below was found through live testing against one specific device
(go-e V4, firmware 60.6) and isn't officially documented by go-e - some of it
may differ on other firmware/hardware versions. Grouped by subsystem, in
roughly the order these were discovered.

### `amp` is a CEILING, not the live-regulated current

**The single most important finding in this document.** Extensive testing
initially suggested that the go-e's Eco algorithm does not regulate the
charge current from `pGrid`/`pPv`/`pAkku` at all - `amp` stayed fixed at 6 no
matter how large the reported surplus was, across two firmware versions,
with every documented prerequisite correctly set.

**The actual cause: `amp` is not the live-regulated value at all - it is a
ceiling the Eco algorithm will never exceed.** The real, live-regulated
current is reflected in `nrg[4]` (Amps) / `nrg[11]` (Watts), never in `amp`
itself. Since `amp` had been left at 6 (e.g. from Manual mode), the Eco
algorithm was silently capped there the entire time - it may well have been
working correctly throughout, just within an artificially low ceiling that
looked, from the outside, exactly like "no regulation happening at all".

**Confirmed live:** with `amp` explicitly raised to 16, a simulated ~2070W
surplus made the real current (`nrg[4]`) climb from ~5.6A to ~8.2A and still
rising within 40 seconds - clear, genuine regulation that had been invisible
simply because the ceiling itself was the bottleneck being measured.

**Fix implemented:** whenever Auto mode is (re-)entered, `amp` is raised to
the device's configured maximum (`ama`, exposed on D-Bus as `/MaxCurrent`).
This is deliberately **not** the script computing or choosing a charge
current itself - it only removes an artificial constraint so the go-e's own
Eco algorithm has its full intended regulation range available, matching
what community integrations expect (e.g.
[marq24/ha-goecharger-api2](https://github.com/marq24/ha-goecharger-api2/blob/main/docs/PVSURPLUS.md)).
**Independently confirmed** by another fork,
[gonzo7734/dbus-goecharger](https://github.com/gonzo7734/dbus-goecharger),
which does the exact same `amp = MaxCurrent` fix for the exact same reason.

**Related, unresolved side note:** the API key `frm` ("Strommengen Handling"
in the app, documented value `2 = PreferPowerToGrid`) is at least anecdotally
associated by the community with the go-e choosing a lower current than
surplus would allow. This device had `frm=2` throughout most testing; it was
changed to `frm=1` ("Standard") before the ceiling cause was found, and
whether `frm=2` would work fine once the ceiling is also raised was not
retested - worth checking if PV surplus charging ever seems overly
conservative.

**A latent bug found later during a full code review:** `/SetCurrent`
writes the same `amp` key that the Auto-mode ceiling logic manages, but did
so without updating the ceiling tracker. Changing the charge current in the
Venus OS GUI while in Auto mode therefore silently and permanently
re-introduced the capped-Eco-algorithm problem above. Verified reproducible,
fixed by keeping the tracker in sync on every `/SetCurrent` write too.

### `frc` physically clicks the charging contactor/relay on every write

Confirmed live via an audible click on the charger itself. This means `frc`
writes must be minimized, not just for API politeness but to avoid
unnecessary relay wear. Several real bugs caused by this were found and
fixed, and the underlying pattern was then generalized:

1. **Entering Auto used to click twice.** The code used to unconditionally
   release charging (`frc=0`) and only then, on the next cycle, evaluate
   battery priority - if SOC was already below threshold, this caused an
   immediate release followed by a re-lock within seconds. Fixed by
   evaluating battery priority *before* writing any `frc` value on entry.
2. **Entering Manual used to interrupt active charging, then over-corrected.**
   The code used to unconditionally force charging off (`frc=1`) as a "safe
   default" - stopping an already-active PV-surplus session just to hand
   control to `SetCurrent`/`StartStop`. First fixed to use `frc=0` (neutral)
   instead - but Basic mode (`lmo=3`) has no PV-surplus gating of its own, so
   `frc=0` made it start charging immediately at the full `amp` ceiling the
   moment a vehicle was connected, regardless of whether it was actually
   charging right before the switch. **Corrected again:** the go-e's actual
   `car` state is now checked right before switching to Manual - only if it
   shows `2` (actively charging) does `frc` stay at `0`; otherwise `frc=1` is
   used, taking over whatever state was genuinely active instead of always
   releasing.
3. **Generalized fix:** every `frc` write anywhere in the script now goes
   through a single `_setFrc()` helper, tracking the last value commanded
   *globally, across all modes*, and only actually writing (and clicking)
   when the desired value differs from that tracked value. Switching
   Auto -> Manual -> Scheduled -> Auto while `frc` stays logically `0`
   throughout now produces zero writes and zero clicks.

### PV/grid reading: explicit L1+L2+L3 summing, no aggregate "Total" path

`pPv` sums AC-coupled production (`/Ac/PvOnGrid/L1/Power` + `L2` + `L3`,
`L1` required, `L2`/`L3` optional/contributing `0` if absent) and
`/Dc/Pv/Power` (DC-coupled, already system-wide). `pGrid` sums
`/Ac/Grid/L1/Power` + `L2` + `L3` the same way - relevant whenever the grid
**connection point** is three-phase even if only one phase is actually
managed by a single-phase inverter (this fork's own setup): load or
PV/battery-driven feed-in on the other two phases would otherwise be
invisible to `pGrid` entirely, even though it directly affects the go-e's
surplus decision. An earlier version of this fork read only `L1` for
`pGrid`, matching
[gonzo7734/dbus-goecharger](https://github.com/gonzo7734/dbus-goecharger)'s
explicit `L1+L2+L3` summing for PV but missing the same treatment for grid.

**Bug found and fixed:** an earlier version used `/Ac/PvOnGrid/Total/Power`
(an aggregate that looked like it should exist) instead of explicit summing -
this path does **not** exist on this system (confirmed live: the dbus CLI
itself raised `AttributeError`, i.e. no such object). This silently degraded
to `0` via this fork's own defensive value-reading (`_safeGetValue()`, see
below) - a real, unnoticed loss of the *entire* AC-coupled contribution to
`pPv` (confirmed live: ~1700W of real production was missing, leaving only
~650W DC-coupled, which happened to look plausible enough on its own not to
be immediately obviously wrong). Reverted to explicit summing, matching the
approach already used for `pGrid`.

**Related crash, found and fixed shortly after:** a `DBusException` ("was
not provided by any .service files") from a `.get_value()` call - not just
at import/construction time, which was already wrapped - crashed the entire
service if the underlying Venus system service was momentarily unavailable,
or a newly added path wasn't served on a given Venus OS version. Every read
of a Venus system item now goes through a dedicated `_safeGetValue()`
wrapper, so any single missing/unavailable value degrades gracefully instead
of taking down the whole process.

### Detailed `/Status` reporting and `/Connected`

While a vehicle is connected but not charging, the official Venus OS
`evcharger` status enum distinguishes several reasons (e.g. "waiting for
sun", "waiting for RFID") that go-e's `car` alone cannot tell apart. This
fork additionally reads go-e's `modelStatus` to pick the correct Venus
status. **Corrected after live testing:** the disambiguation applies when
`car==4`, NOT `car==3` as the official state names alone would suggest - on
this device, go-e reports `car==4` ("charging finished, vehicle still
connected") for both a genuinely completed session *and* paused/force-off
states. Confirmed live: `modelStatus 4` (this fork's `frc=1` hard-stop) ->
Venus `6`; `modelStatus 17` (soft pause, insufficient surplus) -> Venus `4`
("waiting for sun"); anything else falls back to "Charged" (`3`).

**Also added:** `car==5` ("Error") was previously unhandled and silently
fell through to `/Status=0` ("Disconnected"), hiding a real error behind a
misleading display. go-e's `err` key now picks the closest matching Venus
error status where reasonably confident (none of these specific mappings
have been live-tested - no real error occurred during development, please
verify against the app if this ever triggers). `car` can also be `null` on
an internal error per the docs - handled defensively (`/Status=0`) instead
of crashing on `int(None)`.

**`/Connected` now reflects actual reachability, not just a startup value.**
Previously set to `1` once at startup and never updated - if the go-e became
unreachable, every dbus path just kept showing its last known value forever,
with no indication anything was wrong. Now set to `0` (and `/Status` to `0`)
as soon as a poll cycle fails, back to `1` once it responds again - only
written when it actually changes. Relevant for a mobile wallbox that isn't
always on the GX device's network. Relatedly: `/FirmwareVersion` and
`/Serial` used to only be registered if the go-e answered within the first
few hundred milliseconds of startup - since D-Bus paths can only be added
before the service registers, they'd otherwise be permanently missing for
the whole process lifetime if the charger was briefly unreachable at that
exact moment. Now always registered with placeholder values and a warning
when unavailable.

### "Scheduled" mode vs. the go-e's weekly timer - two different features

"Scheduled" activates the go-e's own "Daily Trip" mode (`lmo=5`). This is
**not** the same as the go-e's separate weekly on/off timer feature
(`sch_week`/`sch_satur`/`sch_sund`, available under Basic mode) - an earlier
version of this fork mapped "Scheduled" to that timer instead, which behaves
very differently despite both being reachable from a "Scheduled"-sounding
selector. All three Venus OS modes now explicitly *disable* the weekly timer
- if you want to use it, it must be managed directly in the go-e app.

(Writing the timer object back, when it was still used, initially failed
with `ESP_ERR_HTTPD_RESULT_TRUNC` - the go-e's ESP32 HTTP server has a
limited request buffer. Fixed by encoding the JSON compactly, saving ~30
characters per call.)

**Not extensively live-tested** - unlike Auto and Manual, which were both
tested extensively over many live sessions, Scheduled mode has no real use
case for this fork's own installation and has therefore not been verified
in real day-to-day use beyond confirming the basic mode switch itself
works. If you rely on Scheduled/Daily Trip, please verify carefully and
report back if anything doesn't behave as documented here.

### Fresh PV data is pushed *before* activating Eco mode, not after

**Found live:** switching Auto (charging on genuine surplus) -> Manual ->
Auto again later, once it had actually gone dark, caused the go-e to
immediately resume charging as if the old surplus still applied. The go-e
appears to keep its last-received `pGrid`/`pPv`/`pAkku` around indefinitely
while `lmo != 4` (Eco isn't evaluating them, so nothing invalidates the old
reading). **Fixed** by reordering `_applyChargeMode()`'s Auto branch to push
fresh values *before* setting `lmo=4`, closing the window where Eco could
start evaluating on stale data.

**This alone turned out to be insufficient.** Checking `pvopt_averagePGrid`
etc. (internal rolling averages, undocumented but readable via
`/api/status`) while in Manual revealed why: these stay frozen indefinitely,
even though the raw instantaneous fields correctly go `null` - and appear to
be what the Eco algorithm's decisions are actually driven by. A single fresh
sample on re-entering Auto cannot override several minutes of accumulated
average.

A single `{"pGrid":0,"pPv":0,"pAkku":0}` reset push on leaving Auto was tried
first and confirmed live to not work - the averages stayed frozen even ~40s
later. An initial theory (averages ending in `.0`/`.3`/`.7` suggesting a
3-sample window) led to pushing zeros over several consecutive cycles
instead of just one, which **was** confirmed live to reliably bring the
averages to 0 and keep them there. However, a later isolated test - pushing
a known sequence of values directly via `curl`, bypassing this script
entirely - produced averages a plain mean cannot explain (e.g. a *positive*
average after only negative/zero pushes, mathematically impossible for a
simple average). The 3-sample theory therefore doesn't fully hold; something
else (possibly a tariff/price signal this fork doesn't control) appears to
factor in too. The exact mechanism remains a partial black box - what's
confirmed from real operation is that pushing zeros for enough cycles
reliably works. The count is configurable (`PvAverageResetCycles`) rather
than hardcoded for exactly this reason, and defaults to `0` (disabled) -
given the mechanism isn't fully understood, a value confirmed to work in one
specific test shouldn't be silently active by default everywhere.

This reset, when enabled, is **also applied once after a service restart**,
not just on a live mode switch - the go-e keeps its averages regardless of
whether this script is running, so a restart while sitting on stale
averages from hours earlier would otherwise never get flushed. Cancelled
immediately if the
first cycle finds the go-e already in Auto, so real values are never
overwritten.

**The same averaging lag also shows up while continuously staying in Auto**,
not just when leaving and re-entering it - confirmed live with a clean,
concrete example: `BatterySupportPower` was increased while already
charging in Auto. The real `pGrid` reflected the larger boost immediately
(`-3886` to `-3963`), but `pvopt_averagePGrid` stayed frozen at its old value
(`-2934.7`) for a full ~40s (6 cycles) before catching up to `-3950.7` - the
charge current only started climbing (5.7A -> 7.1A -> 7.4A and rising) in
the very same cycle the average finally updated, not when the real value
changed. This confirms the averaging behaviour is a general property of how
the Eco algorithm evaluates surplus, not something specific to the Auto/
Manual transition case above.

### Battery buffer: two bugs found after adding `BatterySupportMinSoc`

- **Could discharge at night with no PV involved at all.** The SOC-based
  eligibility check considered only charge level - a fully-charged battery
  above the threshold at night, with zero PV, would still trigger the boost
  and make the go-e think there was solar surplus. Fixed via
  `BatterySupportMinPv`, requiring real PV production alongside the SOC
  check (see Configuration reference above) - and its own follow-up bug: the
  gate was implemented as `pPv < minPvForSupport`, so at the disabled
  default of `0` a slightly negative real `pPv` reading (e.g. inverter idle
  standby draw at night) would still incorrectly block the boost. Fixed with
  an explicit `minPvForSupport > 0` check first.
- **Flag could get stuck "active" if disabled mid-session.** Editing
  `BatterySupportMinSoc`/`Power` to `0` while the buffer was already active
  never reset the `_batterySupportActive` flag, since the code that would
  normally clear it lived entirely inside the now-skipped guard. Didn't
  cause a wrong `pGrid` adjustment (that's inside the same skipped block),
  but misleadingly kept implying the feature was engaged. Fixed by
  explicitly resetting the flag when found disabled while previously active.

**Worth noting, found via evcc's own documentation of a related
limitation:** even evcc - despite directly deciding start/stop and current,
unlike this fork's approach of only feeding data to the go-e's own Eco
algorithm - cannot prevent a home battery from covering a real-time
shortfall unless the battery/inverter exposes "active battery control" (a
direct hold/charge/discharge mode command, e.g. via Modbus). Without that,
any system's ESS will physically balance a momentary production/demand gap
from the battery regardless of what any external controller "intends" -
this fork's settings control what *this script itself* signals to the go-e,
not the underlying physical behaviour of the Multiplus/ESS system.

### Preventing battery discharge during Manual charging: three mechanisms considered

Requested to mirror an evcc option that keeps the home battery from being
drawn on while manually charging the car. Three candidate mechanisms were
investigated before picking one:

- **VE.Bus "Switch" register (`/Mode` on `com.victronenergy.vebus.*`,
  Modbus register 33; 1=Charger Only, 2=Inverter Only, 3=On, 4=Off).**
  **Rejected as a general default:** multiple independent sources confirm
  "Charger Only" mode disables AC-Out entirely, regardless of grid
  presence - not "battery left alone, everything else runs as normal", but
  a full loss of AC output for anything connected there. Only safe if
  nothing is actually wired to AC-Out (confirmed to be the case for this
  fork's own installation) - **not implemented, since this fork is used by
  others whose wiring can't be assumed.**
- **ESS-level `/Settings/CGwacs/MaxDischargePower` set to `0`.** Confirmed
  via a real community project
  ([t0bias-r/venusos_acload_prioritize](https://github.com/t0bias-r/venusos_acload_prioritize))
  to stop discharge while still allowing PV to be used directly. Not
  chosen: evcc's own maintainers, in their tracking issue for this exact
  feature, flag this as unconfirmed whether it also disables Peak Shaving.
- **DVCC `/Info/MaxDischargeCurrent` (the "DCL" a BMS normally publishes),
  artificially set to `0`.** Confirmed behaviour: at DCL=0, the system
  drops to grid passthrough (AC-Out stays powered) if the grid is present,
  or turns the inverter off if it isn't - conceptually the cleanest
  option, but requires either overriding an existing real BMS's own
  published value (risky) or registering an entirely new virtual "battery"
  D-Bus service just to report this one artificial limit. Not implemented,
  given the added architectural complexity.

**What was implemented instead, matching evcc's own actual mechanism for
Victron systems:** temporarily raising `/Settings/CGwacs/BatteryLife/MinimumSocLimit` to
the battery's current SOC while in Manual mode (confirmed via evcc's own
GitHub issue tracker: "The current Victron integration manages the battery
discharge during fast charging by setting minimum SOC = current SOC").
Chosen for being simple, requiring no new D-Bus service, and - unlike
Switch-Mode - not touching AC-Out at all while the grid is present.

**Read this before enabling `PreventBatteryDischarge` - a real,
repeatedly-reported failure mode from evcc's own implementation of this
exact mechanism applies here too:** if the restore step fails (a transient
D-Bus error, or this script crashing/being killed while the lock is
active), the raised `MinimumSocLimit` can be left stuck in place until
manually corrected. This is not a hypothetical risk - it is independently
confirmed multiple times in evcc's own issue tracker, e.g.
[evcc-io/evcc#16326](https://github.com/evcc-io/evcc/issues/16326) ("the
battery is essentially crippled... it will not discharge any further until
I change the MinSoc manually" - triggered there by a plain Modbus i/o
timeout during the restore step) and a German-language report of the exact
same pattern
([evcc-io/evcc#23557](https://github.com/evcc-io/evcc/discussions/23557)).
A third, related evcc/Victron report describes the inverter getting stuck
in a "Maintenance" state after the lock was lifted, recoverable only by
power-cycling the Multiplus
([evcc-io/evcc#27837](https://github.com/evcc-io/evcc/issues/27837)).

**Deliberately not addressed by a full watchdog/timeout mechanism** - this
was considered but rejected as disproportionate, given evcc itself doesn't
solve this either. Two lighter-weight improvements were added instead,
without going as far as continuous monitoring:

- **External changes to `MinimumSocLimit` while the lock is active are
  detected and respected**, rather than silently being overwritten by the
  stale value recorded when the lock was first applied - if you (or
  something else) manually change it mid-session, that new value becomes
  what gets restored later, and the lock is re-applied on top of it using
  the current SOC.
- **`CheckEssMinSocAtStartup` + `ExpectedEssMinSoc`**: an optional,
  one-time check at every service start - if the actual `MinimumSocLimit`
  doesn't match what you've configured as your normal value, this is
  treated as evidence of exactly the stuck-lock scenario described above
  (e.g. after a crash) and is corrected automatically. This is not a
  continuous watchdog - it only runs once, at startup - but directly
  catches the most common trigger for this failure mode (the service being
  killed/crashing and then simply being restarted, which happens
  automatically under daemontools).

If enabling `PreventBatteryDischarge`, checking
`/Settings/CGwacs/BatteryLife/MinimumSocLimit` after any unclean restart or crash while a
Manual charging session was active is still worth doing manually too - the
above catches the most common cases, not every conceivable one (e.g. this
script itself functioning normally throughout, but the lock never being
left in the first place due to some other, unrelated logic error).

**Found live: tying the lock to the mode alone (Manual) rather than to
actually charging was wrong.** Switching to Manual mode is also commonly
used simply to disable Eco mode without charging at all - e.g. to use the
`/AutoStart` phase-switching override, or just to stop the go-e's own
surplus evaluation temporarily - and the lock must not engage purely
because of that mode switch. **Fixed**: the lock now additionally requires
`car==2` (go-e reports "actively charging"), checked alongside the mode, so
it only engages while a car is genuinely drawing current. **Also extended
to Scheduled mode** (not just Manual) at the same time, since the go-e's
own Daily Trip charging (Scheduled) is just as able to draw on the battery
outside of genuine PV surplus as Manual charging is - Auto remains
deliberately excluded, since it already has its own PV-aware battery logic
via `BatterySupportMinSoc`/`BatteryForceStartSoc`.

**Found live: the initially-used path, `/Settings/Ess/MinimumSocLimit`, was
wrong for this fork's own installation (and for classic VE.Bus systems in
general).** That path only exists under `com.victronenergy.acsystem` -
the newer Multi RS / "acsystem" product line, per the official
[victronenergy/venus dbus wiki](https://github.com/victronenergy/venus/wiki/dbus)
(confirmed live: querying `com.victronenergy.acsystem` on a classic
Multiplus-II GX system raises `ServiceUnknown` - that service simply
doesn't exist there). The correct path for classic VE.Bus systems (Multi,
Quattro, MultiPlus-II) is `/Settings/CGwacs/BatteryLife/MinimumSocLimit`
under `com.victronenergy.settings`, confirmed live by reading back the
exact SOC value that had been set manually beforehand.

**Confirmed live: `MinimumSocLimit` stores fractional values exactly, no
rounding to whole percent.** Writing `62.7` directly and reading it back
returned exactly `62.7`, not rounded to `62`/`63`. This confirms this
fork's approach - writing whatever `/Dc/Battery/Soc` reports, which is
itself already limited to one decimal place by Venus OS - is safe as-is,
with no additional rounding needed before writing it.

### Phase-switch anti-flapping lock (`mptwt`) can cause a stop/start loop

Not fixed/handled by this fork, but worth understanding: the go-e has a
minimum phase-toggle wait time (community-documented key `mptwt`, reportedly
600s/10 minutes by default) that prevents switching between 1-phase and
3-phase more often than this interval, regardless of current `pGrid`/`pPv`.
If the go-e switches to 3-phase while genuine surplus exists and conditions
then drop below what 3-phase's minimum current requires before this lockout
expires, it cannot fall back to 1-phase - the home battery may then be drawn
on to sustain the session, and once that stops being sustainable the go-e
may stop, briefly retry (still locked to 3-phase), stop again, repeating
until the lockout window elapses. This is inherent go-e firmware behaviour,
not something this fork's PV surplus push can influence.

### The grid target (`pgt`) acts continuously

`pgt` is a persistent value (configured directly in the go-e app) and
continuously feeds into the Eco mode's current calculation, not just when
set. Example: with `pGrid=-1800` (1800W surplus) and `pgt=-200`, the go-e
charges at around 6A instead of the ~7-8A one would expect at a full 1800W,
since 200W is subtracted as a buffer before the resulting current is
calculated.

**`pgt` is intentionally never written by this fork - configure it directly
in the go-e app.** An earlier version managed `pgt` via `config.ini`,
including a fairly involved mechanism to reconcile file edits with direct
app changes (file "winning" on a change, an app-side change otherwise
adopted in-memory) - this added real complexity and, once, a real bug: an
app-side change could be silently reverted on the very next cycle, because
the comparison used to detect "did the file change" couldn't distinguish a
genuine edit from the in-memory value having previously diverged. Given
`pgt` is just as easy to set once directly where it takes effect, this fork
now simply reads the current value (only for `BatterySupportCompensatePgt`)
and never writes it.

### API endpoint quirks

- `lmo`, `fup`, `frc`, `amp`, `pgt` can be set via a **direct query
  parameter** (`GET /api/set?lmo=4`) - works reliably.
- `pGrid`, `pPv`, `pAkku` and the scheduler objects must instead be set via
  `ids={"key":value}`, properly URL-encoded - a manually built, non-encoded
  query string results in `"value must be null or JsonObject"` errors.
- `alw` returns HTTP 500 via a direct query parameter, unlike every other
  key tested; via `ids={...}` it works but is unreliable (silently reverts
  to `true` under a second while the Eco algorithm considers charging
  justified). `frc` reliably overrides this and is used throughout instead.
- The older `/mqtt?payload=key=value` endpoint worked fine for `amp` but
  repeatedly failed for `alw` (cause never conclusively determined). This
  fork uses `/api/set` consistently.
- `amx` (an API v1-only key, documented as not persisted to flash) does not
  exist on this device's API v2 firmware at all - confirmed absent via both
  a filtered and a full status dump. Not a bug: an official go-e developer
  confirmed in [API-v2#112](https://github.com/goecharger/go-eCharger-API-v2/issues/112)
  that flash write-cycle limitations were fully resolved across the board -
  `amp` can simply be used directly on API v2, exactly as
  [evcc's own source code does](https://github.com/evcc-io/evcc/blob/main/charger/go-e.go).

### HTTP connection handling (tried Session reuse, reverted)

A shared `requests.Session()` was tried for connection reuse (HTTP
keep-alive), to avoid a full TCP handshake on every call to the go-e's
small ESP32 web server. **Confirmed live: the go-e closes its socket after
every response without declaring `Connection: close`** - so `urllib3`'s pool
doesn't know the connection is dead, attempts to reuse it anyway, discovers
the drop, and only then opens a fresh connection (logged as "Resetting
dropped connection") - strictly *worse* than opening fresh immediately. An
explicit `Connection: close` header was then tried to stop the pool from
attempting reuse at all - **confirmed live that this didn't work either**,
since that header only asks the *server* to close; it doesn't reliably
affect this client's own pooling behaviour. **Reverted entirely** back to
plain module-level `requests.get(...)` calls, confirmed to not exhibit the
"attempt reuse, discover dead, reconnect" pattern at all.

### Timing behaviour (measured while continuously sending every 5 seconds)

| Event | Measured response time |
|---|---|
| Charging starts once surplus is reported (from stable idle) | ~30-35 seconds |
| Brief interruption of surplus (up to ~30s) | no reaction - tolerated |
| Charging stops when surplus is persistently absent | ~2 minutes (120-125s) |
| Current ramps up once surplus increases (`amp` ceiling raised) | visible increase within ~35-40s |
| **Watchdog:** no new values arrive -> | stop after ~6s, `pgrid`/`ppv`/`pakku` revert to `null` |

**Side observation:** after a watchdog gap (>6s), the *next* incoming value -
regardless of sign - triggers an immediate session restart (`car` changes
within 1-2s); the actual surplus evaluation (start/stop timing above) only
kicks in on the cycles that follow. During continuous, gap-free sending this
doesn't occur - only after a restart of the script/service.

**Consequence for `PauseBetweenRequests`:** stay noticeably below the
6-second watchdog threshold (recommended: 5000ms, as in the original), so a
single delayed request doesn't already trigger an unwanted pause.

### Other robustness findings from a full code review

- **A single config.ini typo could break every cycle.** `_getSetting()`
  called `int()` unguarded, so writing e.g. `true` instead of `1` raised
  `ValueError` on every subsequent cycle, since config.ini is re-read live.
  Now logs a warning and falls back to the default.
- **`AutoStartMode`/`EnableChargeControl` could prevent the service from
  starting**, for the same reason (`getint()`/`getboolean()` raise on
  anything unparseable). Both now log a warning and fall back to their (off)
  default.
- **Log rotation was silently disabled.** `RotatingFileHandler` had
  `maxBytes` but no `backupCount`; at its default of `0`, Python never
  rotates at all and the file grows unbounded (verified: a 1000-byte limit
  produced a 25kB file). Fixed by adding `backupCount=3` and raising
  `maxBytes` from 10kB to 2MB.
- **`HardwareVersion` was re-read from disk twice per cycle, unguarded** -
  an invalid/removed value would raise continuously during normal
  operation, not just at startup. It only requires a restart to change
  anyway, so it's now read once at startup and cached.
- Also cleaned up: `import sys` appeared three times (inherited from the
  original), and a bare `except:` was narrowed to the exceptions it
  actually needs to catch.

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

Check `config.ini` afterwards - most important is `Deviceinstance` and
`Host`. See "Configuration reference" above for every option and what
does/doesn't need a restart.

⚠️ After any change to the `.py` file, or to anything in `config.ini` that
isn't re-read live (see "What needs a restart, and what doesn't" above): run
`restart.sh`.

(An earlier version of this fork exposed the Auto-mode tuning values as
writable D-Bus paths under `/Settings/*` for VRM-based editing; this was
removed after testing showed the Venus OS GUI - Remote Console as well as
the VRM portal - only ever renders the fixed set of paths it already knows
for the `evcharger` role, making custom paths like these invisible anywhere.
Editing `config.ini` directly is therefore the only supported way to change
these values.)

## Used documentation

- https://github.com/goecharger/go-eCharger-API-v2
- https://github.com/victronenergy/venus/wiki/dbus
- https://github.com/victronenergy/venus/wiki/dbus-api

## Credits & Attribution

Based on the work of [vikt0rm](https://github.com/vikt0rm/dbus-goecharger),
inspired by [fabian-lauer](https://github.com/fabian-lauer/dbus-shelly-3em-smartmeter)
and [trixing](https://github.com/trixing/venus.dbus-twc3).

This project is built collaboratively: the coding is done by Claude
(Anthropic), while the features are developed together. All requirements,
architectural decisions, real-hardware testing, and every correction and
refinement come from Gesiima; Claude turns them into code iteratively. No
code was written manually - the implementation happens entirely through
step-by-step instructions and joint review.
