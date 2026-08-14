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
- **`/Connected` now reflects actual reachability.** The original (and this
  fork, until now) only ever set `/Connected = 1` once at startup and never
  updated it again. If the go-e becomes unreachable (e.g. a mobile wallbox
  currently on a different network, or a temporary WiFi/connectivity issue),
  `_getGoeChargerData()` already handled this gracefully (returns `None`,
  logged at DEBUG, no crash) - but every dbus path simply kept showing its
  last known value forever, with no indication to Venus OS/VRM that the
  device was actually gone. `/Connected` is now set to `0` (and `/Status` to
  `0`, "Disconnected", instead of staying frozen on a stale value like
  "Charging") as soon as a poll cycle fails, and back to `1` as soon as the
  go-e responds again - both only written when the value actually changes.

### `/AutoStart` repurposed as a configurable manual phase-switching override (always active, independent of `EnableChargeControl`)

Like `/StartStop` and `/SetCurrent`, this works regardless of the
`EnableChargeControl` setting - it is not part of the "optional features"
list below.

Victron's official `evcharger` dbus spec documents `/AutoStart` as "start
automatically when a vehicle is connected" - a concept the go-e has no direct
equivalent for (see the discussion that led to this decision for the reasoning
why no clean 1:1 mapping exists). Since this path is otherwise permanently
non-functional (neither the original script nor this fork historically wrote
anything to it, leaving the GUI button greyed out with nothing behind it), it
is deliberately **repurposed** here for something genuinely useful instead:
manual override of go-e's phase-switching logic (`psm`).

**What the two toggle positions actually do is configurable** via
`AutoStartMode` in `config.ini` - read once at startup only (a config edit
needs a service restart to take effect, unlike most other settings, since
this defines the meaning of a control path rather than a tuning value):

- `AutoStartMode = 0` (**default**): disabled - the button has no function at
  all, `psm` is never touched. This is the default deliberately: a repurposed
  control path should never be silently active on an installation that didn't
  ask for it, matching how `EnableChargeControl` also defaults to off.
- `AutoStartMode = 1` ("1P-Auto"): `/AutoStart = 0` -> `psm = 1`
  (force single-phase); `/AutoStart = 1` -> `psm = 0` (**Auto** - go-e's own
  live, surplus-based 1-/3-phase switching).
- `AutoStartMode = 2` ("3P-Auto"): `/AutoStart = 0` -> `psm = 2` (force
  three-phase); `/AutoStart = 1` -> `psm = 0` (Auto).
- `AutoStartMode = 3` ("1P-3P"): `/AutoStart = 0` -> `psm = 1` (force
  single-phase); `/AutoStart = 1` -> `psm = 2` (force three-phase) - `psm = 0`
  (Auto) is never used in this mode.

An invalid or unrecognized value (e.g. `true`, or a number outside 0-3) logs a
warning and falls back to `0` rather than preventing the service from
starting.

This was added because a household with little PV surplus most of the time
may prefer to default to forced single-phase and only occasionally check
whether enough surplus exists for 3-phase, rather than constantly running
Auto - modes 1 and 3 both suit that use case, depending on whether the
"probing" toggle position should hand control back to Auto or force 3-phase
outright.

**Important caveat, by direct analogy with the `frc` relay-click findings
above:** phase switching is documented and reported by other users to
involve a real, timed contactor changeover (~10s to complete), not a soft
parameter - so this control goes through the same "only write if the value
actually changed" tracking (`_setPsm()`/`self._lastCommandedPsm`) as `frc`
does, to avoid unnecessary physical switching. **Confirmed live** that the
`/AutoStart` toggle correctly forces single-phase charging via the go-e app.

**External change detection:** if `psm` is changed directly in the go-e app
instead of via the Venus OS `/AutoStart` toggle, this is detected the same
way external `lmo` changes are (see `/Mode` above) and `/AutoStart` is
updated to match - the exact mapping depends on `AutoStartMode` (mode 0
leaves `/AutoStart` untouched entirely, since the toggle has no function to
reflect). This runs independently of `EnableChargeControl`, matching
`/AutoStart`'s own independence from that setting (like
`/StartStop`/`/SetCurrent`).



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
  has its full regulation range available. **Independently confirmed** by
  another fork of the original project,
  [gonzo7734/dbus-goecharger](https://github.com/gonzo7734/dbus-goecharger),
  which does the exact same `amp = MaxCurrent` fix for the exact same reason.
  **PV production reading:** `pPv` sums AC-coupled production
  (`/Ac/PvOnGrid/L1/Power` + `L2` + `L3`, explicitly, `L1` required and
  `L2`/`L3` optional/contributing `0` if not present) and `/Dc/Pv/Power`
  (DC-coupled via solar chargers, inherently system-wide/phase-agnostic
  already). **Corrected after live testing:** an earlier version used
  `/Ac/PvOnGrid/Total/Power` (an aggregate that looked like it should exist,
  published by `dbus-systemcalc-py` on some systems) instead of explicit
  `L1`/`L2`/`L3` summing - this path does **not** exist on this system
  (confirmed live: `dbus -y com.victronenergy.system
  /Ac/PvOnGrid/Total/Power GetValue` failed with an `AttributeError` from the
  dbus CLI itself, i.e. no such object). This silently degraded to `0` via
  this fork's own `_safeGetValue()` defensive handling (see below) - not a
  crash, but a real, unnoticed loss of the *entire* AC-coupled contribution
  to `pPv` for as long as that version ran (confirmed live: ~1700W of real
  AC-coupled production was missing, leaving only the ~650W DC-coupled
  portion, which happened to look plausible enough on its own to not be
  immediately obviously wrong). Reverted to explicit `L1`/`L2`/`L3` summing,
  matching the approach already used for `pGrid` below - this cannot silently
  drop an entire generation source the way relying on an unconfirmed
  "shortcut" aggregate path did.
  **`pGrid` reading:** no equivalent aggregated "Total" path was ever assumed
  for `/Ac/Grid/*` (this mistake was specific to the PV path above), so
  `L1`/`L2`/`L3` are read and summed explicitly (`L1` required, `L2`/`L3`
  optional and contributing `0` if not present - e.g. a genuinely
  single-phase connection). This matters specifically whenever the grid
  **connection point** itself is three-phase, even if only one phase is
  actually managed by a single-phase Multiplus/inverter (this fork's actual
  setup: three-phase grid supply, single-phase Multiplus-II GX on L1 only) -
  load or PV/battery-driven feed-in on the other two phases is otherwise
  invisible to `pGrid` entirely, even though it directly affects true grid
  import/export and therefore the go-e's surplus decision. An earlier version
  of this fork read only `L1` for `pGrid`, matching
  [gonzo7734/dbus-goecharger](https://github.com/gonzo7734/dbus-goecharger)'s
  explicit `L1+L2+L3` summing for PV but missing the same treatment for grid.
  **Critical bug found and fixed shortly after adding the L2/L3/Total reads
  above:** a `dbus.exceptions.DBusException` ("was not provided by any
  .service files") from `com.victronenergy.system` at the moment of an
  actual `.get_value()` call - not just at `VeDbusItemImport` construction
  time, which was already wrapped in `try`/`except` - crashed the entire
  service. This can happen if the underlying Venus system service is
  momentarily unavailable (e.g. during its own restart) or, for a newly added
  path specifically, if that exact path isn't served on a given Venus OS
  version/configuration. Every read of a Venus system item now goes through
  a dedicated `_safeGetValue()` wrapper instead of calling `.get_value()`
  directly, so any single missing/unavailable value degrades gracefully
  (treated the same as if the item were `None`) instead of taking down the
  whole process. Confirmed via the automated test suite by simulating a
  raising `.get_value()` on every affected item simultaneously.
- **Detailed `/Status` reporting:** while a vehicle is connected but not
  actively charging, the official Venus OS `evcharger` status enum
  distinguishes several different reasons (e.g. `4 = waiting for sun`,
  `5 = waiting for RFID`, `6 = waiting for start`) - `car` alone cannot tell
  these apart. This fork additionally reads go-e's `modelStatus` (a detailed
  "reason why we allow charging or not" enum, documented in
  [apikeys-de.md](https://github.com/goecharger/go-eCharger-API-v2/blob/main/apikeys-de.md))
  to pick the correct, specific Venus status. **Important, corrected after
  live testing:** the disambiguation applies when go-e's `car==4`, NOT
  `car==3` as one might assume from the official "car" state names alone - on
  this device/firmware, go-e reports `car==4` ("charging finished, vehicle
  still connected") for both a genuinely completed session *and* for
  paused/force-off states. Confirmed live: `modelStatus 4`
  (`NotChargingBecauseForceStateOff`, this fork's `frc=1` hard-stop states) ->
  Venus `6`, and `modelStatus 17` (`NotChargingBecauseFallbackAwattar`, the
  go-e's own soft pause due to insufficient PV surplus) -> Venus `4`
  ("waiting for sun"); any other `modelStatus` value falls back to `car==4`'s
  plain original meaning, "Charged" (3). See `_update()` for the full mapping
  and which parts are live-confirmed vs. taken directly from go-e's
  documentation without separate live testing.
  **Also added:** `car==5` ("Error", per go-e's own docs) was previously
  unhandled entirely and silently fell through to `/Status=0`
  ("Disconnected"), hiding a real error behind a misleading "not connected"
  display. go-e's separate `err` key is now used to pick the closest matching
  Venus error status where a reasonably confident mapping exists (otherwise a
  generic overheating/error code, still distinct from "Disconnected"). None of
  these error-code mappings have been live-tested (no real error has occurred
  during development) - please verify against the go-e app if this ever
  triggers. The docs also note `car` can be `null` on an internal error, not
  just report `5` - this is now handled defensively (`/Status=0`) instead of
  crashing `int(None)`, which is what an earlier version of this code did in
  the separate charging-time-tracking logic further up in `_update()`.
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
     forward, not to stop an ongoing charge. First fixed by unconditionally
     using `frc=0` (neutral) instead - but this created a *different* problem:
     Basic mode (`lmo=3`) has no PV-surplus gating logic of its own, so
     `frc=0` makes it start charging immediately at the full `amp` ceiling
     the moment a vehicle is connected, regardless of whether it was actually
     charging right before the switch. Switching Auto -> Manual while Auto
     happened to be paused (e.g. insufficient PV surplus) would therefore
     unexpectedly start a manual charge - the opposite of the common intent
     of "just turn Auto off". **Corrected again:** the go-e's actual `car`
     state is now checked right before switching to Manual - only if it shows
     `2` (actively charging) does `frc` stay at `0` (continue seamlessly);
     otherwise `frc=1` is used, taking over whatever state was genuinely
     active at that moment instead of always releasing.
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

### HTTP connection handling (tried Session reuse, reverted)

A shared `requests.Session()` was tried for all go-e API calls, instead of
plain module-level `requests.get(...)` calls (which each internally create a
brand new `Session`, and therefore a brand new TCP connection, every single
time). The intent was connection reuse (HTTP keep-alive), to avoid repeatedly
doing a full TCP handshake against the go-e's small, resource-constrained
ESP32 web server. **Confirmed live, though: the go-e closes the underlying
socket after every single response without declaring `Connection: close` in
its own response headers** - so `urllib3`'s connection pool doesn't know the
connection is already dead, attempts to reuse it anyway, discovers the drop,
and only then opens a fresh connection (logged as `"Resetting dropped
connection"` instead of the plain `"Starting new HTTP connection"` seen
without a shared session). This is strictly *worse* than immediately opening
a fresh connection, since it adds a doomed reuse attempt first.

An explicit `Connection: close` header was then added as a default on the
session, expecting this to stop the pool from attempting reuse at all.
**Confirmed live that this did not work either** - `"Resetting dropped
connection"` still appeared on every single call afterwards. This request
header only asks the *server* to close the connection; it does not reliably
prevent this client's own connection pool from still attempting to reuse an
already-pooled connection from an earlier call.

**Reverted entirely** back to plain module-level `requests.get(...)` calls
everywhere - confirmed to not exhibit the "attempt reuse, discover dead,
reconnect" pattern at all, at the cost of not attempting keep-alive (which
was irrelevant anyway, since the go-e doesn't support it reliably in the
first place).

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

### Findings from a full code review

These were found by systematically reviewing the whole script rather than by
hitting them in practice - three of them are latent bugs that would have been
awkward to diagnose in the field.

- **`/SetCurrent` silently defeated the `amp` ceiling logic.** `/SetCurrent`
  writes the same `amp` key that the Auto-mode ceiling logic manages, but did
  so without updating `self._lastCommandedAmp`. Changing the charge current
  in the Venus OS GUI while in Auto mode therefore lowered the ceiling, while
  the ceiling logic still believed `amp` was at the device maximum and so
  never restored it - re-introducing exactly the capped-Eco-algorithm problem
  described above, but silently and permanently. Verified reproducible, and
  fixed by keeping the tracker in sync.
- **A single config.ini typo could break every cycle.** `_getSetting()`
  called `int()` unguarded, so writing e.g. `true` instead of `1` (easy to do
  - and it happened during development) raised `ValueError` on every
  subsequent cycle, since config.ini is re-read live. Now logs a warning and
  falls back to the default instead.
- **Log rotation was silently disabled.** `RotatingFileHandler` was given
  `maxBytes` but no `backupCount`; with `backupCount` at its default of `0`,
  Python never rotates at all and the file grows unbounded (verified: a
  1000-byte limit produced a 25kB file). Relevant on a space-constrained GX
  device at `Logging=DEBUG` with a 5s poll interval. Fixed by adding
  `backupCount=3`, and raising `maxBytes` from 10kB (small enough that useful
  context scrolled out of the log within a minute or two at DEBUG level) to
  2MB.
- **`/FirmwareVersion` and `/Serial` could be missing for the whole session.**
  Both were only registered if the go-e answered during the first few hundred
  milliseconds of startup. D-Bus paths can only be added before the service
  is registered, so if the charger was unreachable at that moment they were
  absent for the entire lifetime of the process, even after it came back -
  a realistic scenario for a mobile wallbox that isn't always on the GX
  device's network. Now always registered, with placeholder values and a
  warning when unavailable.
- **`AutoStartMode` and `EnableChargeControl` could prevent the service from
  starting.** Both are read with `getint()`/`getboolean()`, which raise on
  anything unparseable - `getboolean()` in particular accepts only
  `1/yes/true/on` and `0/no/false/off`, so writing e.g. `ja` was enough to
  stop the service coming up at all rather than just ignoring one setting.
  Both now log a warning and fall back to their (off) default. `AutoStartMode`
  additionally had its default changed from `1` to `0`, so that a repurposed
  control path is never silently active on an installation that didn't ask for
  it - consistent with `EnableChargeControl` also defaulting to off. All other
  defaults were reviewed at the same time and left as they were: every feature
  flag defaults to "off"/`0`, and the two hysteresis values (`2`) only take
  effect once their respective feature is deliberately enabled.
- **`HardwareVersion` was re-read from disk twice per cycle, unguarded.**
  `_update()` called `int(config['DEFAULT']['HardwareVersion'])` on every
  cycle without a fallback, so an invalid or removed value would raise
  continuously during normal operation, not just at startup. It only affects
  which temperature field is read and requires a restart to change anyway
  (like `Deviceinstance`/`AcPosition`), so it is now read once at startup and
  cached - both safer and cheaper.
- Also cleaned up: `import sys` appeared three times (inherited from the
  original), and a bare `except:` was narrowed to the exceptions it actually
  needs to catch.

### Battery buffer flag could get stuck "active" if disabled mid-session

**Found live:** if `BatterySupportMinSoc` (or `BatterySupportPower`) was
edited to `0` in `config.ini` while the battery buffer feature was already
active (e.g. an accidental typo while adjusting the value), the internal
`_batterySupportActive` flag was never reset - the whole block that would
normally re-evaluate and clear it lived entirely inside the
`if maxSocForSupport > 0 and supportPower > 0...` guard, which is skipped
once the value is `0`. This didn't cause an incorrect `pGrid` adjustment
(that code is also inside the skipped block), but the flag and its "active"
log messages misleadingly kept implying the feature was still engaged for as
long as the service kept running in Auto mode. **Fixed** by explicitly
resetting the flag (and logging it) when the feature is found disabled while
it was previously active.

### Fresh PV data is pushed *before* activating Eco mode, not after

**Found live:** switching Auto (charging on genuine surplus) -> Manual
(continues charging, per the `frc` fix above) -> Auto again later, once it
had actually gone dark, caused the go-e to immediately resume charging as if
the old surplus still applied. The go-e appears to keep its last-received
`pGrid`/`pPv`/`pAkku` values around indefinitely while `lmo != 4` (Eco isn't
evaluating them while inactive, so there is nothing to invalidate the old
reading) and then acts on whatever it has stored the instant Eco
re-activates. If `lmo` is set to `4` first and a fresh push follows moments
later (the previous order), there is a brief window where Eco could start
evaluating using the old, possibly many-minutes-stale surplus value instead
of the current one. **Fixed** by reordering `_applyChargeMode()`'s Auto
branch to push fresh `pGrid`/`pPv`/`pAkku` *before* setting `lmo=4` - by the
time Eco mode actually activates, the most current real reading is already
the "last known" value for it to act on, closing the window entirely.

**Further finding, after the above fix alone turned out to be insufficient:**
a single fresh push at the moment of re-entering Auto did not fully prevent
resuming as if old surplus still applied. Checking `pvopt_averagePGrid`,
`pvopt_averagePPv`, and `pvopt_averagePAkku` (internal rolling averages,
undocumented but readable via `/api/status`) while in Manual revealed why:
these stay frozen at their last computed value indefinitely - confirmed
live, `pgrid`/`ppv`/`pakku` (the raw instantaneous fields) correctly went
`null` once this fork stopped pushing them, but the `pvopt_average*` fields
did not, and appear to be what the Eco algorithm's start/stop and
phase-switch decisions are actually driven by, rather than the raw
instantaneous values. A single fresh sample upon re-entering Auto cannot
immediately override several minutes of accumulated average.

**First attempt (insufficient):** a single `{"pGrid":0,"pPv":0,"pAkku":0}`
reset push when Auto is *left*. Confirmed live to not work - the averages
stayed frozen at their old value (e.g. `-2656W`) even ~40s after switching to
Manual, and re-entering Auto still resumed charging immediately.

**Initial theory (later found incomplete):** the average values always end in
`.0`/`.3`/`.7` and only change every third sample, suggesting the go-e
averages over 3 samples - a single zero would then only be 1 of 3 values in
that window, with no further samples to flush out the other two (the ~6s
watchdog nulls the raw `pgrid`/`ppv`/`pakku` fields, but that does not touch
the averages at all). Zeros were therefore pushed over multiple consecutive
cycles instead of just one, and this was confirmed live to reliably bring the
averages to 0 and keep them there.

**However, an isolated follow-up test** - pushing a known sequence of values
directly via `curl`, completely bypassing this script and its Auto-mode
logic, with the real service stopped so it could not interfere - produced
averages that a plain mean of the pushed values cannot explain (e.g. a
*positive* average after a sequence of only negative and zero pushes, which
is mathematically impossible for a simple average). The 3-sample theory
therefore does not fully hold - something else, possibly a tariff/price
signal this fork doesn't send or control, appears to factor in as well. The
exact mechanism remains a partial black box. What is confirmed, from real
Auto-mode operation rather than the isolated test: pushing zeros for enough
consecutive cycles reliably brings the averages to 0 and keeps them there,
and re-entering Auto afterwards no longer resumes on stale data. The number
of cycles is configurable via `PvAverageResetCycles` (default `5`, `0`
disables it) rather than hardcoded, precisely because the underlying mechanism
isn't fully understood and a different installation or firmware version might
need a different number.

**Also flushed once after a service restart, not just on a live mode
switch.** The go-e keeps its rolling averages regardless of whether this
script is running - a restart while it happens to be sitting on stale
averages from an earlier session (possibly hours old, from before the
restart) would otherwise never get flushed until some unrelated mode switch
happened to occur during the new process's lifetime. `self._pvResetCyclesRemaining`
therefore defaults to `5` (not `0`) at startup. If the very first cycle finds
the go-e is actually already in Auto, this is cancelled immediately (before
any zero could be sent) so real values are never overwritten - verified live
via the same mutual-exclusion structure that protects every other Auto-mode
cycle.

### Phase-switch anti-flapping lock (`mptwt`) can cause a stop/start loop

Not fixed/handled by this fork, but worth understanding: the go-e has a
minimum phase-toggle wait time (community-documented key `mptwt`, reportedly
600s/10 minutes by default) that prevents switching between 1-phase and
3-phase more often than this interval, regardless of current `pGrid`/`pPv`.
If the go-e switches to 3-phase while genuine surplus exists and conditions
then drop below what 3-phase's minimum current requires before this lockout
expires, it cannot fall back to 1-phase - the home battery may then be drawn
on to sustain the current 3-phase session (real load: real PV production, no
override needed here to explain it), and once that stops being sustainable
the go-e may stop, briefly retry (still locked to 3-phase), stop again,
repeating in a stop/start cycle without ever reaching 1-phase until the
lockout window elapses. This is inherent go-e firmware behaviour, not
something this fork's PV surplus push can influence.

### The grid target (`pgt`) acts continuously

`pgt` is a persistent value (configured directly in the go-e app - see below)
and continuously feeds into the Eco mode's current calculation - not just
when it is set. Example: with `pGrid=-1800` (1800W surplus) and `pgt=-200`
(200W reserve), the go-e charges at around 6A instead of the ~7-8A one would
expect at a full 1800W, since 200W is subtracted as a buffer before the
resulting charge current (rounded down to whole amps) is calculated. (This
test predates the `amp`-ceiling discovery above and was incidentally not
affected by it, since 6A happened to be at/below whatever ceiling was in
effect at the time.)

**`pgt` is intentionally never written by this fork - configure it directly
in the go-e app instead** (App: PV surplus -> Grid target / Power
preference). An earlier version of this fork managed `pgt` via `config.ini`,
including a fairly involved mechanism to reconcile `config.ini` edits with
direct changes made in the go-e app (config.ini "winning" on a file change,
an app-side change otherwise being adopted in-memory) - this added real
complexity, and once, a real bug: an app-side change could be silently
reverted again on the very next cycle, because the comparison used to
determine "did config.ini change" couldn't distinguish a genuine file edit
from the in-memory value having previously diverged via an adopted app-side
change. Given `pgt` is just as easy to set once, directly where it actually
takes effect, this fork now simply reads the current `pgt` value (only to
inform the `BatterySupportCompensatePgt` option below) and never writes it.

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

### Visible startup confirmation even at `Logging = WARN`

At `Logging = WARN`, essentially all startup logging (`INFO`/`DEBUG` level)
is filtered out - a restarted service left no confirmation in the log that
it had actually started successfully. One line is now logged at `WARNING`
level specifically so it remains visible regardless of the configured level:
`"dbus-goecharger started successfully..."`, logged once right after
registering on D-Bus and switching to the event loop.

**Note:** a periodic one-line heartbeat at `WARNING` level (logged every
`SignOfLifeLog` minutes) was tried as well, but reverted - `WARNING` is
meant for something actually noteworthy, not routine "still running"
confirmation, so continuously repeating it there would be a misuse of the
log level's meaning purely to force visibility. The existing multi-line
`INFO`-level sign-of-life block (`_signOfLife()`) is unchanged; switch
`Logging` to `INFO` or `DEBUG` temporarily if periodic confirmation is
needed beyond the one-time startup line.

## Configuration reference

`config.ini.example` intentionally only has short comments - full
explanations for every option are here instead, grouped the same way as in
the file.

### Basic (unchanged from the original)

- **`AccessType`**: always `OnPremise`.
- **`SignOfLifeLog`**: minutes between periodic status log entries.
- **`Deviceinstance`**: this device's Venus OS D-Bus instance number.
- **`HardwareVersion`**: go-e hardware generation. Only affects the
  temperature reading (`/MCU/Temperature`) - energy calculation (`eto`) is
  handled uniformly via the API v2 unit (Wh) in this fork, since
  `/api/status` always returns API v2 data.
- **`AcPosition`**: `0` = AC Output (critical loads), `1` = AC Input.
- **`Logging`**: `DEBUG`/`INFO`/`WARN`/etc. At `WARN`, one line is still
  logged on successful startup regardless of this setting - see "Visible
  startup confirmation" above.

### Charge control master switch

- **`EnableChargeControl`**: when `false` or omitted, this script behaves
  exactly like the original - monitoring only, `/Mode` not writable, no
  writes to `lmo`/`fup`/scheduler, no PV surplus push. Every option below
  this line has no effect in that case.

### Battery priority (Auto mode only)

The EV only starts charging once the home battery has reached a configured
SOC.

- **`BatteryPriorityMinSoc`**: `0` or omit = feature disabled.
- **`BatteryPriorityHysteresis`**: in percentage points. Charging pauses
  below `BatteryPriorityMinSoc` and is only released again once SOC reaches
  `BatteryPriorityMinSoc + hysteresis` - prevents flapping right at the
  threshold.

### Battery as a charging buffer (Auto mode only)

Above a second, higher SOC, a configurable amount of home battery power is
allowed to help charge the EV too (virtual surplus added to `pGrid`).

- **`BatterySupportMinSoc`**: `0` or omit = feature disabled.
- **`BatterySupportPower`**: additional power in W reported to the go-e as
  virtual surplus. Should not exceed the system's actual discharge limit,
  otherwise the difference is drawn from the grid instead.
- **`BatterySupportHysteresis`**: in percentage points - the buffer stays
  active down to `BatterySupportMinSoc - hysteresis`.
- **`BatterySupportCompensatePgt`**: `1` (not `true` - this is read as a
  number) compensates for `pgt`'s continuous reserve while the buffer is
  active, by adding `pgt`'s magnitude on top of `BatterySupportPower` -
  without this, `pgt` (configured in the go-e app, see below) reduces the
  amount that actually reaches the charge current calculation below what you
  configured here. The exact relationship between `pgt` and the resulting
  current reduction has only been empirically observed, not confirmed as a
  precise formula - this is a reasonable approximation, not a guaranteed
  exact match. `0` or omit = disabled (default).

### Rolling-average reset after leaving Auto mode

- **`PvAverageResetCycles`**: how many cycles of pushing `pGrid`/`pPv`/`pAkku`=0
  to send after leaving Auto mode (see "Fresh PV data is pushed..." above for
  the full background). `0` disables this entirely - the go-e's rolling
  averages then simply stay frozen at whatever they were until real values
  are pushed again. Default `5`, confirmed reliable in real Auto-mode
  operation; the exact mechanism behind why the go-e's averages behave the
  way they do remains a partial black box (an isolated follow-up test could
  not fully explain it with a plain average of pushed values alone), so this
  is deliberately left tunable rather than hardcoded in case a different
  installation or firmware version needs a different number.

### Grid target (`pgt`)

Not configured here - set it directly in the go-e app instead (App: PV
surplus -> Grid target / Power preference). See "`pgt` is intentionally
never written by this fork" above for the reasoning. This script still reads
the current value (only for `BatterySupportCompensatePgt` above), never
writes it.

### `/AutoStart` button function

- **`AutoStartMode`**: what the repurposed Venus OS "Autostart" toggle does
  (see "`/AutoStart` repurposed..." above for the full reasoning). Read once
  at startup only - **a config.ini edit here needs a service restart**,
  unlike every other option on this page.
  - `0` = disabled, the button has no function at all (`psm` never touched) - **default**
  - `1` = "1P-Auto": Off -> force 1-phase (`psm=1`), On -> Auto (`psm=0`)
  - `2` = "3P-Auto": Off -> force 3-phase (`psm=2`), On -> Auto (`psm=0`)
  - `3` = "1P-3P": Off -> force 1-phase (`psm=1`), On -> force 3-phase (`psm=2`) - Auto/`psm=0` never used

### `[ONPREMISE]`

- **`Host`**: the go-e's IP address.
- **`PauseBetweenRequests`**: poll interval in ms. Must stay at or below
  5000, since the go-e expects `pGrid`/`pPv`/`pAkku` to be updated at least
  every 5 seconds in Auto mode (see "Timing behaviour" above).

### What needs a restart, and what doesn't

Only `AutoStartMode` needs a restart to take effect (see above). Everything
else in `[DEFAULT]` and `[ONPREMISE]` is either read once at genuine startup
regardless (`AccessType`, `Deviceinstance`, `HardwareVersion`, `AcPosition`,
`Logging`, `Host`, `PauseBetweenRequests`, `EnableChargeControl`) - these
still need `restart.sh` after editing, since nothing re-reads them mid-run -
or actively re-read every Auto-mode cycle without needing a restart
(`BatteryPriorityMinSoc`, `BatteryPriorityHysteresis`, `BatterySupportMinSoc`,
`BatterySupportPower`, `BatterySupportHysteresis`,
`BatterySupportCompensatePgt`).

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
See "Configuration reference" above for every option and what does/doesn't
need a restart.

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
