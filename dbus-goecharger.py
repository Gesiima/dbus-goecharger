#!/usr/bin/env python
#
# dbus-goecharger - go-eCharger integration for Victron Venus OS
# https://github.com/Gesiima/dbus-goecharger
#
# Comments in this file are intentionally brief ("why", not "why in detail
# with every measured value") - the full reasoning, live-test evidence, and
# configuration reference for everything referenced below as "README
# Findings" or "Configuration reference" is in README.md in the repo above.

# import normal packages
import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import time
import requests # for http GET
import configparser # for config/ini file
import json
import dbus
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService, VeDbusItemImport


class DbusGoeChargerService:
  def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
    # Plain requests.get() calls throughout, deliberately no shared Session -
    # see README Findings ("HTTP connection handling") for why.
    # State initialized FIRST, since _handlechangedvalue (onchangecallback)
    # accesses it and could in theory already be called during setup.
    self._lastUpdate = 0
    self._chargingTime = 0.0
    self._chargeMode = 0            # 0=Manual, 1=Auto, 2=Scheduled
    self._lastCommandedLmo = None
    self._batteryPriorityPaused = False
    self._batterySupportActive = False
    self._batteryForceStartActive = False
    # 'frc' clicks the relay on every write - only written when it actually
    # changes, globally across all modes. See README Findings ("frc
    # physically clicks..."). Deliberately not reset on mode switches.
    self._lastCommandedFrc = None
    # 'amp' ceiling - only written when the target actually changes, to
    # avoid flash wear. See README Findings ("amp is a CEILING...").
    self._lastCommandedAmp = None
    # 'psm' phase switching - real contactor changeover, tracked the same way
    # as frc/amp to avoid redundant writes.
    self._lastCommandedPsm = None
    # Rolling-average reset cycle counter - see README Findings ("Fresh PV
    # data is pushed..."). Configurable via PvAverageResetCycles, default 0
    # (disabled). Also runs once after a restart, cancelled immediately if
    # the go-e turns out to already be in Auto (see _update()'s lmo check).
    self._pvResetCyclesRemaining = self._getSetting('PvAverageResetCycles', 0)
    # GridCurrentLimit safety feature - protects the house connection fuse
    # (SLS) against overload from the EV charger plus other simultaneous
    # loads, independent of charge mode. See README Findings
    # ("GridCurrentLimit..."). State machine: 'normal' -> 'reduced' (amp
    # forced down to GridCurrentMinAmp) -> 'paused' (frc forced to 1, only
    # reached if even the minimum amp isn't enough headroom). Disabled by
    # default (GridCurrentLimit=0).
    self._gridOverloadState = 'normal'
    self._gridOverloadSustainedCount = 0
    self._gridOverloadReleaseCount = 0
    self._gridOverloadMissingReadingCount = 0
    self._gridOverloadSavedAmp = None
    self._gridOverloadSavedFrc = None
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
    acPosition = int(config['DEFAULT']['AcPosition'])
    pauseBetweenRequests = int(config['ONPREMISE']['PauseBetweenRequests']) # in ms
    # Cached rather than re-read every cycle - see README Findings ("Other
    # robustness findings").
    self._hardwareVersion = hardwareVersion

    if pauseBetweenRequests <= 20:
      raise ValueError("Pause between requests must be greater than 20")
    if pauseBetweenRequests > 5000:
      logging.warning("PauseBetweenRequests > 5000ms: go-e requires an update every 5 seconds for PV surplus (pGrid/pPv/pAkku). In Auto mode, charging may otherwise pause/stop unexpectedly.")

    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
    self._paths = paths

    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

    #get data from go-eCharger
    data = self._getGoeChargerData('sse,fwv')

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)

    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', 0xFFFF) #
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', productname)
    # Registered unconditionally even if the go-e was unreachable at startup -
    # D-Bus paths can't be added later. See README Findings ("Detailed
    # /Status reporting and /Connected").
    fwv = 0
    serial = 'unknown'
    if data:
       fwv = data.get('fwv', 0)
       try:
           fwv = int(str(data['fwv']).replace('.', ''))
       except (KeyError, TypeError, ValueError):
           # Keep the raw value - some firmware versions may not be numeric.
           pass
       serial = data.get('sse', 'unknown')
    else:
       logging.warning("go-eCharger not reachable at startup - /FirmwareVersion and /Serial "
                       "registered with placeholder values (D-Bus paths cannot be added later)")
    self._dbusservice.add_path('/FirmwareVersion', fwv)
    self._dbusservice.add_path('/Serial', serial)
    self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path('/Position', acPosition)

    # /Status: read-only, reflects the car state (set in _update)
    self._dbusservice.add_path('/Status', None)

    # Master switch for all extended control logic. Default off, so existing
    # installations keep the original monitoring-only behaviour until
    # explicitly enabled. getboolean() raises on unparseable values (e.g.
    # 'ja') - guarded so a typo can't prevent the service from starting.
    try:
      self._chargeControlEnabled = config.getboolean('DEFAULT', 'EnableChargeControl', fallback=False)
    except ValueError:
      logging.warning("config.ini: EnableChargeControl is not a valid boolean (use true/false) - "
                      "falling back to false (monitoring only)")
      self._chargeControlEnabled = False
    if self._chargeControlEnabled:
      # only make /Mode actually writable when the control logic is enabled
      self._dbusservice.add_path('/Mode', 0, writeable=True, onchangecallback=self._handlechangedvalue)
    else:
      # otherwise, as in the original: a plain display value, not writable
      self._dbusservice.add_path('/Mode', 0)

    # What the repurposed /AutoStart toggle does - read once at startup only,
    # since this defines the meaning of a control path rather than a tuning
    # value (needs a restart to change). See README ("/AutoStart repurposed
    # as a phase-switching override") for the full reasoning.
    # 0 = disabled (default) - psm never touched
    # 1 = "1P-Auto": AutoStart=0 -> psm=1, AutoStart=1 -> psm=0 (Auto)
    # 2 = "3P-Auto": AutoStart=0 -> psm=2, AutoStart=1 -> psm=0 (Auto)
    # 3 = "1P-3P":   AutoStart=0 -> psm=1, AutoStart=1 -> psm=2 (psm=0 unused)
    try:
      self._autoStartMode = config.getint('DEFAULT', 'AutoStartMode', fallback=0)
    except ValueError:
      logging.warning("config.ini: AutoStartMode is not a valid integer - falling back to 0 (disabled)")
      self._autoStartMode = 0
    if self._autoStartMode not in (0, 1, 2, 3):
      logging.warning("AutoStartMode=%s is not a recognized value (0/1/2/3) - falling back to 0 (disabled)" % self._autoStartMode)
      self._autoStartMode = 0

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # register the service (only after ALL paths have been added)
    self._dbusservice.register()

    if self._chargeControlEnabled:
      logging.info("EnableChargeControl=true: Auto/Manual/Scheduled control is active")
    else:
      logging.info("EnableChargeControl=false (or not set): monitoring only, no mode control")
    logging.info("AutoStartMode=%s: %s" % (self._autoStartMode,
                 {0: "disabled, /AutoStart has no function", 1: "1P-Auto", 2: "3P-Auto", 3: "1P-3P"}[self._autoStartMode]))

    # Separate read-only connection to com.victronenergy.system for PV/grid/
    # battery values (Auto mode / PV surplus push).
    systemBus = dbus.SystemBus()
    try:
      # pGrid/pPv: explicit L1+L2+L3 summing, no aggregate "Total" path - see
      # README Findings ("PV/grid reading") for why.
      self._gridPowerItemL1 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L1/Power')
      self._gridPowerItemL2 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L2/Power')
      self._gridPowerItemL3 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L3/Power')
      self._pvPowerAcItemL1 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/PvOnGrid/L1/Power')
      self._pvPowerAcItemL2 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/PvOnGrid/L2/Power')
      self._pvPowerAcItemL3 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/PvOnGrid/L3/Power')
      self._pvPowerDcItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Dc/Pv/Power')
      self._batteryPowerItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Dc/Battery/Power')
      self._batterySocItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Dc/Battery/Soc')
    except Exception as e:
      logging.critical('Error at %s', 'reading Venus system dbus items for Auto mode', exc_info=e)
      self._gridPowerItemL1 = None
      self._gridPowerItemL2 = None
      self._gridPowerItemL3 = None
      self._pvPowerAcItemL1 = None
      self._pvPowerAcItemL2 = None
      self._pvPowerAcItemL3 = None
      self._pvPowerDcItem = None
      self._batteryPowerItem = None
      self._batterySocItem = None

    # Separate, isolated try/except for the GridCurrentLimit safety feature
    # (see below) - deliberately NOT combined with the block above, so that
    # if these specific paths are unavailable on a given system, the
    # already-working PV/grid/battery items above are not also taken down.
    # Actual CURRENT (A), not power - per-phase, since one phase can be
    # near its rated limit while aggregate power still looks unremarkable
    # (confirmed as a known real-world go-e community concern - see README
    # Findings, "GridCurrentLimit...").
    try:
      self._gridCurrentItemL1 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L1/Current')
      self._gridCurrentItemL2 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L2/Current')
      self._gridCurrentItemL3 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L3/Current')
    except Exception as e:
      logging.warning("Could not connect to per-phase grid current items - GridCurrentLimit will be unavailable: %s" % e)
      self._gridCurrentItemL1 = None
      self._gridCurrentItemL2 = None
      self._gridCurrentItemL3 = None

    # Separate connection to com.victronenergy.settings (a different service
    # than com.victronenergy.system above) for the optional battery-discharge
    # lock during Manual charging - see PreventBatteryDischarge in
    # README ("Configuration reference") for the full reasoning, evcc
    # comparison, and known failure modes.
    try:
      self._essMinSocItem = VeDbusItemImport(systemBus, 'com.victronenergy.settings', '/Settings/CGwacs/BatteryLife/MinimumSocLimit')
    except Exception as e:
      logging.warning("Could not connect to /Settings/CGwacs/BatteryLife/MinimumSocLimit - PreventBatteryDischarge will be unavailable: %s" % e)
      self._essMinSocItem = None
    # None = lock not currently active; otherwise the MinimumSocLimit value
    # to restore once Manual mode is left (or the feature is disabled).
    self._savedEssMinSoc = None
    self._lastCommandedEssMinSoc = None

    # One-time startup sanity check for PreventBatteryDischarge - see
    # README ("Preventing battery discharge...") for the full reasoning.
    # CheckEssMinSocAtStartup follows this fork's normal "0 = disabled"
    # pattern; ExpectedEssMinSoc is only consulted once that switch is on,
    # since 0 is a genuinely valid MinimumSocLimit on some systems and
    # couldn't itself serve as an enable/disable value here.
    if self._getSetting('CheckEssMinSocAtStartup', 0) and self._essMinSocItem is not None:
      expectedEssMinSoc = config['DEFAULT'].get('ExpectedEssMinSoc', None)
      if expectedEssMinSoc is None:
        logging.warning("CheckEssMinSocAtStartup=1 but ExpectedEssMinSoc is not set - skipping startup sanity check")
      else:
        try:
          expectedEssMinSoc = float(expectedEssMinSoc)
          currentEssMinSoc = self._safeGetValue(self._essMinSocItem, '/Settings/CGwacs/BatteryLife/MinimumSocLimit')
          if currentEssMinSoc is None:
            logging.warning("CheckEssMinSocAtStartup=1 but /Settings/CGwacs/BatteryLife/MinimumSocLimit could not be read - "
                            "skipping startup sanity check this time")
          elif abs(currentEssMinSoc - expectedEssMinSoc) > 0.01:
            logging.warning("ExpectedEssMinSoc=%s%% but /Settings/CGwacs/BatteryLife/MinimumSocLimit is currently %s%% - this "
                            "looks like PreventBatteryDischarge was left stuck active after an unclean "
                            "shutdown or crash (see README). Restoring it to %s%% now." %
                            (expectedEssMinSoc, currentEssMinSoc, expectedEssMinSoc))
            try:
              self._essMinSocItem.set_value(expectedEssMinSoc)
            except Exception as e:
              logging.warning("Could not restore MinimumSocLimit to the expected value at startup: %s" % e)
        except (TypeError, ValueError) as e:
          logging.warning("config.ini: ExpectedEssMinSoc is not a valid number - skipping startup sanity check: %s" % e)

    # add _update function 'timer'
    gobject.timeout_add(pauseBetweenRequests, self._update)

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)

  def _safeGetValue(self, item, itemName):
    '''
    Wraps item.get_value() defensively - a Venus system item can raise a raw
    DBusException at the moment of the actual call (not just at
    construction), crashing the service if not caught. See README Findings
    ("PV/grid reading" / DBusException). Every read of a Venus system item
    goes through this instead of calling .get_value() directly.
    '''
    if item is None:
      return None
    try:
      return item.get_value()
    except Exception as e:
      logging.warning("Could not read %s from Venus system dbus (service temporarily unavailable?): %s" % (itemName, e))
      return None

  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config


  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']

    if not value:
        value = 0

    return int(value)


  def _getGoeChargerStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise':
      URL = "http://%s/api/status" % (config['ONPREMISE']['Host'])
    else:
      raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

    return URL


  def _setGoeChargerValueV2(self, parameter, value):
    '''
    Sets a simple, scalar go-e API v2 key (lmo, fup, frc, alw, amp, ama, psm, ...)
    via a direct query parameter on /api/set (e.g. /api/set?lmo=4) - NOT via the
    ids={...} JSON-batch parameter, which is reserved for pGrid/pPv/pAkku and
    the scheduler objects (no single-key setter, genuinely require ids=). See
    README Findings ("API endpoint quirks") for why: sending scalar keys via
    ids={...} was found to be silently reverted by the go-e within under a
    second, while plain query parameters were rock-solid.
    '''
    config = self._getConfig()
    baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
    if isinstance(value, bool):
      paramValue = 'true' if value else 'false'
    else:
      paramValue = str(value)
    try:
      request_data = requests.get(url=baseURL, params={parameter: paramValue}, timeout=2)
    except Exception as e:
      logging.warning("go-eCharger v2 set failed for %s=%s: %s" % (parameter, value, e))
      return False

    if not request_data:
      logging.warning("go-eCharger v2 set: no response for %s=%s" % (parameter, value))
      return False

    try:
      json_data = request_data.json()
    except Exception:
      logging.warning("go-eCharger v2 set: invalid JSON response for %s=%s: %s" %
                      (parameter, value, request_data.text[:200]))
      return False

    return True


  def _setFrc(self, value):
    '''
    Writes 'frc' (force state: 0=Neutral, 1=Off, 2=On) only if it differs
    from the last value we ourselves commanded, tracked globally in
    self._lastCommandedFrc across all modes. 'frc' physically clicks the
    go-e's charge contactor/relay on every write (confirmed live) - all
    frc writes anywhere in this script go through this method so that a
    mode switch (or any other code path) never re-writes an already-correct
    value and never clicks the relay unnecessarily.
    '''
    if self._lastCommandedFrc == value:
      return True
    ok = self._setGoeChargerValueV2('frc', value)
    if ok:
      self._lastCommandedFrc = value
    return ok


  def _setPsm(self, value):
    '''
    Writes 'psm' (phase switch mode: 0=Auto, 1=Force 1-phase, 2=Force
    3-phase) only if it differs from the last value we ourselves commanded.
    Phase switching involves a real, timed contactor changeover (community
    reports ~10s for the physical switch to complete) - not a soft parameter
    like 'amp' - so redundant writes are avoided the same way as for frc.
    '''
    if self._lastCommandedPsm == value:
      return True
    ok = self._setGoeChargerValueV2('psm', value)
    if ok:
      self._lastCommandedPsm = value
    return ok


  def _getGoeChargerData(self, filter):
    URL = "%s?filter=%s" % (self._getGoeChargerStatusUrl(), filter)
    try:
       request_data = requests.get(url = URL, timeout=1)
    except Exception:
       return None

    # check for response
    if not request_data:
        raise ConnectionError("No response from go-eCharger - %s" % (URL))

    json_data = request_data.json()

    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")


    return json_data


  def _getSetting(self, name, default):
    '''
    Reads a tuning value directly and freshly from config.ini every time it is
    called (config.ini is re-parsed on every call via _getConfig()). This
    means an edit to config.ini takes effect on the very next Auto-mode cycle
    or mode switch - no service restart required for these tuning values.
    (Only DEFAULT/ONPREMISE keys read at service startup, such as
    Deviceinstance or Host, still require a restart to take effect.)

    Returns the default (and logs a warning) if the value cannot be parsed as
    an integer, instead of raising. Since config.ini is re-read live on every
    cycle, a single typo while editing it (e.g. writing "true" instead of "1",
    or a stray character) would otherwise raise ValueError on every single
    cycle from then on - this happened during development and is very easy to
    trigger accidentally while tuning values on a running system.
    '''
    config = self._getConfig()
    raw = config['DEFAULT'].get(name, default)
    try:
      return int(raw)
    except (TypeError, ValueError):
      logging.warning("config.ini: '%s = %s' is not a valid integer - using default %s instead" %
                      (name, raw, default))
      return int(default)


  def _setGoeSchedulerEnabled(self, enabled):
    '''
    Enables/disables the go-e's own weekly schedule (sch_week/sch_satur/sch_sund)
    without changing the time windows (ranges) configured there in the app.
    control: Disabled=0, Inside=1, Outside=2 (we only use 0/1)

    IMPORTANT (found live): sending the full object back (reading the
    current one, changing only 'control', writing the whole thing back)
    reliably failed with "ESP_ERR_HTTPD_RESULT_TRUNC" (URL too long for the
    go-e's small ESP32 HTTP server buffer) - confirmed 100% reproducible
    across every mode switch, even with compact JSON encoding (an earlier
    fix attempt that reduced but did not eliminate the problem). Confirmed
    live via direct curl testing that the go-e accepts a MINIMAL payload
    containing only 'control', without 'ranges' at all, AND that the
    existing ranges remain completely unchanged afterwards - no need to
    read/resend the current object at all. This is simpler (no extra GET
    per key) and reliably small enough to stay under the buffer limit.
    '''
    control = 1 if enabled else 0
    config = self._getConfig()
    for key in ('sch_week', 'sch_satur', 'sch_sund'):
      try:
        payload = json.dumps({key: {'control': control}}, separators=(',', ':'))
        baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
        request_data = requests.get(url=baseURL, params={'ids': payload}, timeout=2)
        if not request_data or not getattr(request_data, 'ok', True):
          logging.warning("Scheduler: setting %s failed (HTTP %s): %s" %
                          (key, getattr(request_data, 'status_code', '?'), getattr(request_data, 'text', '')[:150]))
      except Exception as e:
        logging.critical('Error at %s', '_setGoeSchedulerEnabled(%s)' % key, exc_info=e)


  def _resetAutoModeState(self):
    '''
    Resets Auto-mode-specific state when leaving Auto (self._chargeMode is
    already set to the new target mode by the caller before this runs).
    Extracted into its own method so it can be called from BOTH triggering
    paths: an explicit Venus OS /Mode switch (via _applyChargeMode) AND an
    externally-detected go-e app mode change (in _update()) - found live
    (see README Findings) that switching modes directly in the go-e app
    only went through the latter path, which previously skipped this reset
    entirely (including the rolling-average flush), silently leaving stale
    state in place.
    '''
    self._batteryPriorityPaused = False
    self._batterySupportActive = False
    self._batteryForceStartActive = False
    self._lastCommandedAmp = None
    # Flushes the go-e's internal rolling averages, which otherwise stay
    # frozen while not in Auto - see README Findings ("Fresh PV data is
    # pushed..."). Configurable via PvAverageResetCycles, default 0.
    self._pvResetCyclesRemaining = self._getSetting('PvAverageResetCycles', 0)

  def _applyChargeMode(self):
    '''
    Sends the selected charge mode to the go-e:
    Auto (1)      -> lmo=4 (Eco), fup=true, amp raised to device max, fresh
                     pGrid/pPv/pAkku pushed, frc set based on battery priority
    Scheduled (2) -> lmo=5 (Daily Trip), fup=false, frc=0
    Manual (0)    -> lmo=3 (Basic), frc=0 if actively charging (car==2, so an
                     active Auto session continues seamlessly), else frc=1

    'frc' (force state: 0=Neutral, 1=Off, 2=On) is used instead of 'alw' -
    see README Findings ("API endpoint quirks") for why.
    '''
    try:
      # Found live: a mode switch must always freshly re-assert 'frc',
      # even if the desired value happens to match what we last commanded
      # in the PREVIOUS mode. Confirmed live: Auto paused by battery
      # priority (frc=1 already commanded there) -> switched to Manual
      # while not actively charging (frc=1 desired again) -> _setFrc()
      # saw the value as unchanged and skipped the write entirely -> the
      # go-e started charging anyway seconds later. The go-e appears to
      # evaluate 'frc' per mode context, not globally across an 'lmo'
      # change, so an old assertion from a different mode cannot be
      # relied on to still apply. Resetting the tracker here forces the
      # frc write below to always actually happen on every mode switch.
      self._lastCommandedFrc = None

      # The actual 'frc' value for the new mode is set further below via
      # _setFrc(), which only writes (and only clicks the relay) if the
      # value actually needs to change relative to what THIS method itself
      # has already written earlier in the same call.
      if self._chargeMode != 1:
        self._resetAutoModeState()

      if self._chargeMode == 1:
        # Entering Auto - cancel any pending zero-flush from a previous exit,
        # so it cannot keep pushing zeros over the real values we are about to
        # start sending.
        self._pvResetCyclesRemaining = 0
        # 'amp' is a CEILING, not the live-regulated current - see README
        # Findings ("amp is a CEILING..."). Raised to device max on every
        # Auto entry so the Eco algorithm has its full regulation range.
        try:
          maxAmp = int(self._dbusservice['/MaxCurrent'])
        except Exception:
          maxAmp = 0
        if maxAmp > 0:
          ok2 = self._setGoeChargerValueV2('amp', maxAmp)
          if ok2:
            self._lastCommandedAmp = maxAmp
          logging.info("Auto mode: amp ceiling raised to %dA (device max) so the Eco algorithm can regulate freely" % maxAmp)
        else:
          logging.warning("Auto mode: /MaxCurrent not available - could not raise amp ceiling, Eco algorithm may stay capped at its last value")

        # Fresh pGrid/pPv/pAkku pushed BEFORE lmo=4, not after - see README
        # Findings ("Fresh PV data is pushed..."). Also determines/writes the
        # correct initial frc value based on battery priority.
        self._pushPvSurplusValues()

        # Disable the go-e's own weekly schedule while in Auto mode, so it
        # cannot compete with the Eco algorithm's own start/stop decisions.
        self._setGoeSchedulerEnabled(False)
        ok = self._setGoeChargerValueV2('lmo', 4)
        self._setGoeChargerValueV2('fup', True)
        if ok:
          self._lastCommandedLmo = 4
        else:
          logging.warning("Could not set lmo=4 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Auto (PV surplus enabled)")
      elif self._chargeMode == 2:
        # "Scheduled" activates the go-e's own "Daily Trip" mode (lmo=5) - NOT
        # the separate weekly timer (sch_week etc.). See README Findings
        # ("'Scheduled' mode vs. the go-e's weekly timer").
        ok = self._setGoeChargerValueV2('lmo', 5)
        self._setGoeChargerValueV2('fup', False)
        self._setFrc(0)
        self._setGoeSchedulerEnabled(False)
        if ok:
          self._lastCommandedLmo = 5
        else:
          logging.warning("Could not set lmo=5 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Scheduled (go-e Daily Trip mode enabled - fully configured in the go-e app)")
      else:
        # Basic mode (lmo=3) has no PV-surplus gating of its own - frc=0 is
        # only safe here if charging was already active (continues
        # seamlessly); otherwise frc=1 to avoid an unwanted auto-start at the
        # full amp ceiling. See README Findings ("frc physically clicks...").
        currentCarState = None
        try:
          carData = self._getGoeChargerData('car')
          if carData is not None and 'car' in carData and carData['car'] is not None:
            currentCarState = int(carData['car'])
        except Exception as e:
          logging.warning("Could not read current 'car' state before switching to Manual: %s" % e)
        wasActivelyCharging = (currentCarState == 2)
        ok = self._setGoeChargerValueV2('lmo', 3)
        self._setGoeChargerValueV2('fup', False)
        self._setFrc(0 if wasActivelyCharging else 1)
        self._setGoeSchedulerEnabled(False)
        if ok:
          self._lastCommandedLmo = 3
        else:
          logging.warning("Could not set lmo=3 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Manual (%s)" %
                     ("continuing active charging session" if wasActivelyCharging else "not currently charging, staying off"))
    except Exception as e:
      logging.critical('Error at %s', '_applyChargeMode', exc_info=e)

  def _updateBatteryDischargeLock(self, carState):
    '''
    Optional feature (PreventBatteryDischarge): prevents the home
    battery from discharging while the car is actually charging outside of
    Auto mode (Manual or Scheduled), similar to evcc's equivalent option.
    Implemented the same way evcc does it for Victron systems: temporarily
    raises /Settings/CGwacs/BatteryLife/MinimumSocLimit to the battery's
    current SOC while charging, restoring the original value once charging
    stops (or the feature is disabled).

    IMPORTANT (found live): tied to carState==2 (go-e reports "actively
    charging"), NOT merely to being in Manual/Scheduled mode - switching to
    Manual is also used simply to disable Eco mode, without necessarily
    charging at all (e.g. to override /AutoStart's phase-switching without
    also triggering the discharge lock) - the lock must not engage in that
    case. Applies in BOTH Manual (0) and Scheduled (2) mode, since Scheduled
    charging (the go-e's own Daily Trip) is just as capable of drawing on
    the battery outside of genuine PV surplus as Manual charging is. Auto
    mode (1) is deliberately excluded - that mode already has its own,
    separate PV-aware battery logic (BatterySupportMinSoc/BatteryForceStartSoc).

    Called every cycle regardless of HOW self._chargeMode most recently
    changed - both an explicit Venus OS /Mode switch (via _applyChargeMode)
    and an externally-detected go-e app mode change (in _update()) update
    self._chargeMode, and this method reacts to the resulting value either
    way, rather than being tied to one specific trigger path.

    IMPORTANT LIMITATION, deliberately accepted rather than building a more
    complex safeguard: if the restore write below fails (e.g. a transient
    dbus error) or this service crashes/is killed while the lock is active,
    the raised MinimumSocLimit can be left stuck in place, requiring manual
    correction - this is a REAL, repeatedly reported failure mode in evcc's
    own equivalent feature (see README Findings) and applies here too. No
    watchdog/timeout is implemented for this - see README for the reasoning
    and the explicit warning to check for this after any crash/unclean
    restart while this feature is enabled.

    Chosen deliberately over other candidate mechanisms (see README
    Findings): raising MinimumSocLimit, unlike forcing the VE.Bus switch to
    "Charger Only", does not disable AC-Out - loads/backup power remain
    available for as long as the grid is present, and ESS's own normal
    inverter/backup behaviour during an actual grid failure is unaffected.

    A manual change to MinimumSocLimit WHILE the lock is already active
    (e.g. directly in the Venus OS GUI) is detected and adopted as the new
    value to restore to later, rather than being silently overwritten by
    the stale value recorded when the lock was first applied.
    '''
    if self._essMinSocItem is None:
      return
    try:
      preventDischarge = self._getSetting('PreventBatteryDischarge', 0)
      lockShouldBeActive = bool(preventDischarge) and self._chargeMode in (0, 2) and carState == 2

      if lockShouldBeActive and self._savedEssMinSoc is None:
        currentSoc = self._safeGetValue(self._batterySocItem, '/Dc/Battery/Soc')
        currentMinSoc = self._safeGetValue(self._essMinSocItem, '/Settings/CGwacs/BatteryLife/MinimumSocLimit')
        if currentSoc is None or currentMinSoc is None:
          logging.warning("PreventBatteryDischarge: battery SOC or current MinimumSocLimit "
                          "not available - battery discharge lock skipped this cycle")
          return
        self._savedEssMinSoc = currentMinSoc
        try:
          self._essMinSocItem.set_value(currentSoc)
          self._lastCommandedEssMinSoc = currentSoc
          logging.info("PreventBatteryDischarge: entered Manual mode - raised ESS MinimumSocLimit "
                       "from %s%% to current SOC %s%% (will restore %s%% on leaving Manual)" %
                       (self._savedEssMinSoc, currentSoc, self._savedEssMinSoc))
        except Exception as e:
          logging.warning("PreventBatteryDischarge: could not raise MinimumSocLimit - will retry next cycle: %s" % e)
          self._savedEssMinSoc = None

      elif not lockShouldBeActive and self._savedEssMinSoc is not None:
        try:
          self._essMinSocItem.set_value(self._savedEssMinSoc)
          logging.info("PreventBatteryDischarge: restored ESS MinimumSocLimit to %s%%" % self._savedEssMinSoc)
          self._savedEssMinSoc = None
          self._lastCommandedEssMinSoc = None
        except Exception as e:
          logging.warning("PreventBatteryDischarge: could not restore MinimumSocLimit (still trying to "
                          "restore %s%%) - will retry next cycle: %s" % (self._savedEssMinSoc, e))

      elif lockShouldBeActive and self._savedEssMinSoc is not None:
        # Lock already active, ongoing - check whether MinimumSocLimit was
        # changed EXTERNALLY (e.g. directly in the Venus OS GUI) since we
        # last set it. If so, the user's new value is adopted as the new
        # value to restore to later (rather than silently overwriting their
        # change with our old, stale record once Manual is eventually
        # left) - matching the same "respect an external change" principle
        # used elsewhere in this fork (lmo, psm). The lock itself is then
        # re-applied on top of this new baseline, using the current SOC
        # again, since Manual charging is still ongoing.
        currentMinSoc = self._safeGetValue(self._essMinSocItem, '/Settings/CGwacs/BatteryLife/MinimumSocLimit')
        if (currentMinSoc is not None and self._lastCommandedEssMinSoc is not None and
            abs(currentMinSoc - self._lastCommandedEssMinSoc) > 0.01):
          logging.info("PreventBatteryDischarge: MinimumSocLimit changed externally while the lock was "
                       "active (now %s%%, we had set %s%%) - adopting %s%% as the new value to restore later" %
                       (currentMinSoc, self._lastCommandedEssMinSoc, currentMinSoc))
          self._savedEssMinSoc = currentMinSoc
          currentSoc = self._safeGetValue(self._batterySocItem, '/Dc/Battery/Soc')
          if currentSoc is not None:
            try:
              self._essMinSocItem.set_value(currentSoc)
              self._lastCommandedEssMinSoc = currentSoc
            except Exception as e:
              logging.warning("PreventBatteryDischarge: could not re-apply lock after external "
                              "MinimumSocLimit change: %s" % e)
    except Exception as e:
      logging.critical('Error at %s', '_updateBatteryDischargeLock', exc_info=e)

  def _checkGridCurrentLimit(self, data):
    '''
    Optional feature (GridCurrentLimit): protects the house connection fuse
    (SLS) against overload from the EV charger plus other simultaneous
    loads, independent of charge mode - unlike go-e's own built-in "static"
    load balancing (which only coordinates between go-e chargers and does
    NOT react to other house loads like ovens or heat pumps), or "dynamic"
    load balancing (which requires the separate go-e Controller hardware
    accessory with its own current-transformer measurement). This fork
    achieves an equivalent result using the current measurement Venus OS
    already has from its own grid meter, with no extra hardware.

    Monitors actual per-phase CURRENT (A), not power - deliberately, since
    with typically single-phase charging, one phase can be near its rated
    limit while aggregate power still looks unremarkable (a concern
    explicitly raised in real go-e community discussions about exactly
    this scenario). GridCurrentLimit is per-phase (e.g. a "35A SLS" means
    35A on EACH phase, not a 35A total).

    State machine, escalating one step at a time, each step requiring
    GridCurrentSustainedCycles consecutive over-threshold cycles:
      normal -> reduced (amp forced down to GridCurrentMinAmp)
      reduced -> paused (frc forced to 1) - only reached if the phase
        current is STILL over threshold even at the reduced amp, i.e.
        other house loads alone already exceed the safety margin
    If amp is already at or below GridCurrentMinAmp at the moment normal
    charging is found to be over threshold, escalates directly to paused -
    forcing amp down further would not have helped.

    Recovery is symmetric and fully automatic: GridCurrentReleaseCycles
    consecutive UNDER-threshold cycles restore the exact amp/frc values
    that were in effect immediately before this fork's own intervention
    (read live from the go-e at the moment of first intervening, not
    inferred from mode logic) - this correctly hands control back to
    whatever amp the user had set in Manual, or lets Auto's own per-cycle
    ceiling logic naturally reassert itself right after.

    Disabled entirely by default (GridCurrentLimit=0 or unset). Applies in
    ALL charge modes as a safety overlay on top of whatever that mode's own
    logic is doing - this is the one feature in this fork that writes
    amp/frc in Manual mode.

    GridCurrentLimitMode controls how this feature behaves once triggered:
    0=off (default, skipped entirely), 1=log only (the full state machine
    below runs exactly as normal, including all WARNING-level log lines,
    but the actual amp/frc write calls are skipped - lets you observe what
    this feature WOULD do, with your real house's load patterns, before
    trusting it to actually intervene), 2=active (writes amp/frc for real).
    Every log line below is identical in wording between log-only and
    active mode except for an explicit "[LOG ONLY, not applied]" prefix
    this fork adds - so grepping the log tells you unambiguously which
    mode produced it.
    '''
    if self._gridCurrentItemL1 is None and self._gridCurrentItemL2 is None and self._gridCurrentItemL3 is None:
      return
    try:
      limitMode = self._getSetting('GridCurrentLimitMode', 0)
      if limitMode not in (1, 2):
        if self._gridOverloadState != 'normal':
          # Feature was live-disabled (or switched to an invalid value)
          # mid-override via config.ini - release immediately rather than
          # leaving amp/frc stuck.
          logging.info("GridCurrentLimit disabled while an override was active - releasing immediately")
          self._releaseGridOverload(logOnly=False)
        return
      logOnly = (limitMode == 1)
      logPrefix = "[LOG ONLY, not applied] " if logOnly else ""

      gridLimit = self._getSetting('GridCurrentLimit', 0)
      if gridLimit <= 0:
        if self._gridOverloadState != 'normal':
          logging.info("GridCurrentLimit disabled while an override was active - releasing immediately")
          self._releaseGridOverload(logOnly=False)
        return

      margin = self._getSetting('GridCurrentSafetyMargin', 3)
      # Deliberately separate from the trigger margin - safety-first
      # principle explicitly requested: releasing back to full current
      # should be MORE conservative than triggering the reduction was, to
      # avoid flapping back and forth if the load happens to hover right
      # around a single shared threshold. Defaults to requiring an extra
      # 2A of headroom (on top of GridCurrentSafetyMargin) before release.
      releaseMargin = self._getSetting('GridCurrentReleaseMargin', 2)
      minAmp = self._getSetting('GridCurrentMinAmp', 6)
      sustainedCycles = max(1, self._getSetting('GridCurrentSustainedCycles', 3))
      releaseCycles = max(1, self._getSetting('GridCurrentReleaseCycles', 3))
      # Same safety-first principle: if the grid current can't be verified
      # at all for several consecutive cycles (sensor/communication
      # problem), that is treated as a reason to pause rather than to
      # silently do nothing - a missing reading must never mean "assume
      # it's fine". Reuses the same GridCurrentSustainedCycles count.
      missingReadingCycles = max(1, self._getSetting('GridCurrentSustainedCycles', 3))
      triggerThreshold = gridLimit - margin
      releaseThreshold = gridLimit - margin - releaseMargin

      l1 = self._safeGetValue(self._gridCurrentItemL1, '/Ac/Grid/L1/Current')
      l2 = self._safeGetValue(self._gridCurrentItemL2, '/Ac/Grid/L2/Current')
      l3 = self._safeGetValue(self._gridCurrentItemL3, '/Ac/Grid/L3/Current')
      readings = [abs(v) for v in (l1, l2, l3) if v is not None]
      if not readings:
        self._gridOverloadMissingReadingCount += 1
        logging.warning("GridCurrentLimit enabled but no per-phase grid current reading is "
                        "available this cycle (%d consecutive cycle(s)) - cannot verify safety" %
                        self._gridOverloadMissingReadingCount)
        if (self._gridOverloadMissingReadingCount >= missingReadingCycles and
            self._gridOverloadState != 'paused'):
          logOnly = self._getSetting('GridCurrentLimitMode', 0) == 1
          logPrefix = "[LOG ONLY, not applied] " if logOnly else ""
          if self._gridOverloadState == 'normal':
            self._gridOverloadSavedAmp = int(data['amp']) if 'amp' in data and data['amp'] is not None else None
          self._gridOverloadSavedFrc = self._lastCommandedFrc
          if not logOnly:
            self._setFrc(1)
          self._gridOverloadState = 'paused'
          logging.warning("%sGridCurrentLimit: grid current unavailable for %d consecutive cycles - "
                          "pausing charging as a fail-safe (cannot verify the house connection is "
                          "within its rated limit)" % (logPrefix, self._gridOverloadMissingReadingCount))
        return
      self._gridOverloadMissingReadingCount = 0
      maxPhaseCurrent = max(readings)

      if maxPhaseCurrent > triggerThreshold:
        self._gridOverloadReleaseCount = 0
        self._gridOverloadSustainedCount += 1
        if self._gridOverloadSustainedCount >= sustainedCycles:
          self._gridOverloadSustainedCount = 0
          liveAmp = int(data['amp']) if 'amp' in data and data['amp'] is not None else None
          if self._gridOverloadState == 'normal':
            if liveAmp is not None and liveAmp <= minAmp:
              # Already at/below the configured minimum - reducing further
              # would not help, so escalate straight to pausing.
              self._gridOverloadSavedFrc = self._lastCommandedFrc
              if not logOnly:
                self._setFrc(1)
              self._gridOverloadState = 'paused'
              logging.warning("%sGridCurrentLimit: phase current %.1fA exceeds %.1fA (limit %.1fA - margin "
                              "%.1fA) and amp is already at/below the configured minimum (%dA) - "
                              "pausing charging" % (logPrefix, maxPhaseCurrent, triggerThreshold, gridLimit, margin, minAmp))
            else:
              self._gridOverloadSavedAmp = liveAmp
              if not logOnly:
                ok = self._setGoeChargerValueV2('amp', minAmp)
                if ok:
                  self._lastCommandedAmp = minAmp
              logging.warning("%sGridCurrentLimit: phase current %.1fA exceeds %.1fA (limit %.1fA - margin "
                              "%.1fA) for %d consecutive cycles - reducing amp from %s to %dA" %
                              (logPrefix, maxPhaseCurrent, triggerThreshold, gridLimit, margin, sustainedCycles, liveAmp, minAmp))
              self._gridOverloadState = 'reduced'
          elif self._gridOverloadState == 'reduced':
            # Still over threshold even at the reduced minimum - other
            # house loads alone already exceed the safety margin.
            self._gridOverloadSavedFrc = self._lastCommandedFrc
            if not logOnly:
              self._setFrc(1)
            self._gridOverloadState = 'paused'
            logging.warning("%sGridCurrentLimit: phase current %.1fA still exceeds %.1fA even at the "
                            "reduced amp - pausing charging entirely" % (logPrefix, maxPhaseCurrent, triggerThreshold))
          # else already 'paused' - nothing further to escalate to.
      elif maxPhaseCurrent <= releaseThreshold:
        self._gridOverloadSustainedCount = 0
        if self._gridOverloadState != 'normal':
          self._gridOverloadReleaseCount += 1
          if self._gridOverloadReleaseCount >= releaseCycles:
            self._releaseGridOverload(logOnly)
      else:
        # In the hysteresis gap between releaseThreshold and
        # triggerThreshold - neither escalating nor counting toward
        # release. Holds whatever state is currently in effect steady,
        # resetting both progress counters so only genuinely consecutive
        # readings on either side count towards the next transition.
        self._gridOverloadSustainedCount = 0
        self._gridOverloadReleaseCount = 0
    except Exception as e:
      logging.critical('Error at %s', '_checkGridCurrentLimit', exc_info=e)

  def _releaseGridOverload(self, logOnly):
    '''
    Restores whatever amp/frc value this fork itself saved immediately
    before intervening in _checkGridCurrentLimit(), and resets the state
    machine back to 'normal'. Separated into its own method since it's
    called both from the normal release path (sustained safe cycles) and
    from the config-disabled-mid-override path (always logOnly=False there,
    since disabling the feature outright should always actually release
    any real override in effect, regardless of which mode set it).
    '''
    logPrefix = "[LOG ONLY, not applied] " if logOnly else ""
    if self._gridOverloadSavedFrc is not None:
      if not logOnly:
        self._setFrc(self._gridOverloadSavedFrc)
      self._gridOverloadSavedFrc = None
    if self._gridOverloadSavedAmp is not None:
      if not logOnly:
        ok = self._setGoeChargerValueV2('amp', self._gridOverloadSavedAmp)
        if ok:
          self._lastCommandedAmp = self._gridOverloadSavedAmp
      self._gridOverloadSavedAmp = None
    logging.info("%sGridCurrentLimit: phase current back within safe margin - restoring normal operation" % logPrefix)
    self._gridOverloadState = 'normal'
    self._gridOverloadSustainedCount = 0
    self._gridOverloadReleaseCount = 0
    self._gridOverloadMissingReadingCount = 0

  def _pushPvSurplusValues(self):
    '''
    Reads PV/grid/battery power from Venus OS and forwards it in the go-e's
    own format. Must be called at least every 5s, otherwise the last value
    is kept (watchdog - see README Findings, "Timing behaviour").

    Sign convention (confirmed via official go-e docs and live energy-balance
    checks - see README Findings): pGrid >0 = import, <0 = feed-in; pPv >0 =
    production; pAkku <0 = battery charging, >0 = discharging (Venus reports
    /Dc/Battery/Power the opposite way, hence inverted below).
    '''
    if self._gridPowerItemL1 is None:
      return

    try:
      config = self._getConfig()

      # Safety net for the 'amp' ceiling - _applyChargeMode() only raises it
      # on an explicit mode switch; this re-checks every cycle so a service
      # restart while already in Auto also gets it raised. See README
      # Findings ("amp is a CEILING...").
      try:
        maxAmp = int(self._dbusservice['/MaxCurrent'])
      except Exception:
        maxAmp = 0
      if maxAmp > 0 and self._lastCommandedAmp != maxAmp:
        ok = self._setGoeChargerValueV2('amp', maxAmp)
        if ok:
          logging.info("Auto mode: amp ceiling (re)confirmed at %dA (device max)" % maxAmp)
          self._lastCommandedAmp = maxAmp
        else:
          logging.warning("Could not confirm amp ceiling at %dA - will retry next cycle" % maxAmp)

      # pgt is intentionally never written - configure it in the go-e app
      # instead. See README ("Grid target (pgt)") for why. Only read below,
      # for BatterySupportCompensatePgt.

      # Battery priority: pauses charging below a configured SOC, released
      # again at minSoc+hysteresis. Only updates the flag here; the frc write
      # happens in one combined place below (see comment there for why).
      minBatterySoc = self._getSetting('BatteryPriorityMinSoc', 0)
      if minBatterySoc > 0 and self._batterySocItem is not None:
        hysteresis = self._getSetting('BatteryPriorityHysteresis', 2)
        batterySoc = self._safeGetValue(self._batterySocItem, '/Dc/Battery/Soc')

        if batterySoc is None:
          logging.warning("Auto mode: /Dc/Battery/Soc not available - battery priority ignored")
        else:
          if self._batteryPriorityPaused:
            if batterySoc >= minBatterySoc + hysteresis:
              self._batteryPriorityPaused = False
          else:
            if batterySoc < minBatterySoc:
              self._batteryPriorityPaused = True
          if self._batteryPriorityPaused:
            logging.debug("Auto mode: battery SOC %s%% < %s%% - EV charging should pause (battery priority)" %
                          (batterySoc, minBatterySoc))

      # Explicit L1+L2+L3 summing - see README Findings ("PV/grid reading").
      gridPowerL1 = self._safeGetValue(self._gridPowerItemL1, '/Ac/Grid/L1/Power')
      gridPowerL2 = self._safeGetValue(self._gridPowerItemL2, '/Ac/Grid/L2/Power')
      gridPowerL3 = self._safeGetValue(self._gridPowerItemL3, '/Ac/Grid/L3/Power')
      gridPower = (gridPowerL1 or 0) + (gridPowerL2 or 0) + (gridPowerL3 or 0)
      pvPowerAcL1 = self._safeGetValue(self._pvPowerAcItemL1, '/Ac/PvOnGrid/L1/Power')
      pvPowerAcL2 = self._safeGetValue(self._pvPowerAcItemL2, '/Ac/PvOnGrid/L2/Power')
      pvPowerAcL3 = self._safeGetValue(self._pvPowerAcItemL3, '/Ac/PvOnGrid/L3/Power')
      pvPowerAc = (pvPowerAcL1 or 0) + (pvPowerAcL2 or 0) + (pvPowerAcL3 or 0)
      pvPowerDc = self._safeGetValue(self._pvPowerDcItem, '/Dc/Pv/Power')
      batteryPower = self._safeGetValue(self._batteryPowerItem, '/Dc/Battery/Power')

      if gridPowerL1 is None or batteryPower is None:
        logging.warning("Auto mode: /Ac/Grid/L1/Power or /Dc/Battery/Power not available - PV push skipped this cycle")
        return

      pvPower = pvPowerAc
      if pvPowerDc is not None:
        pvPower += pvPowerDc

      pGrid = gridPower
      pPv = pvPower
      pAkku = -1 * batteryPower  # see docstring - adjust sign if needed

      # Battery as a charging buffer: above an SOC threshold, additional
      # battery power is reported as virtual surplus (evcc's "battery as
      # buffer above X%"). See README Findings ("Battery buffer...") and
      # "Configuration reference" for the full three-tier design.
      maxSocForSupport = self._getSetting('BatterySupportMinSoc', 0)
      supportPower = self._getSetting('BatterySupportPower', 0)
      minPvForSupport = self._getSetting('BatterySupportMinPv', 0)
      forceStartSoc = self._getSetting('BatteryForceStartSoc', 0)
      if maxSocForSupport > 0 and supportPower > 0 and self._batterySocItem is not None:
        supportHysteresis = self._getSetting('BatterySupportHysteresis', 2)
        batterySocForSupport = self._safeGetValue(self._batterySocItem, '/Dc/Battery/Soc')

        if batterySocForSupport is None:
          logging.warning("Auto mode: /Dc/Battery/Soc not available - battery buffer ignored")
        else:
          if self._batterySupportActive:
            if batterySocForSupport < maxSocForSupport - supportHysteresis:
              self._batterySupportActive = False
          else:
            if batterySocForSupport >= maxSocForSupport:
              self._batterySupportActive = True

          # Force-start tier: separate hysteresis state, reuses the same
          # hysteresis value as the tier above.
          if forceStartSoc > 0:
            if self._batteryForceStartActive:
              if batterySocForSupport < forceStartSoc - supportHysteresis:
                self._batteryForceStartActive = False
            else:
              if batterySocForSupport >= forceStartSoc:
                self._batteryForceStartActive = True
          else:
            self._batteryForceStartActive = False

          # BatterySupportMinPv gates the boost on real PV also being present
          # (unless the force-start tier bypasses it) - see README Findings
          # ("Battery buffer could discharge at night..."). The explicit
          # "minPvForSupport > 0" check matters: see README Findings
          # ("BatterySupportMinPv=0 did not reliably mean disabled").
          if self._batterySupportActive and minPvForSupport > 0 and pPv < minPvForSupport and not self._batteryForceStartActive:
            logging.debug("Battery buffer eligible (SOC=%s%%) but pPv=%sW is below BatterySupportMinPv=%sW - no virtual surplus applied this cycle" %
                          (batterySocForSupport, pPv, minPvForSupport))
          elif self._batterySupportActive:
            # Added to pGrid, not pPv - see README Findings ("Battery
            # buffer...") for why pGrid is what actually drives the go-e.
            adjustment = supportPower
            # Optional pgt compensation - see README ("Grid target (pgt)").
            if self._getSetting('BatterySupportCompensatePgt', 0):
              try:
                pgtData = self._getGoeChargerData('pgt')
                currentPgt = pgtData.get('pgt') if pgtData is not None else None
              except Exception:
                currentPgt = None
              adjustment += abs(currentPgt) if currentPgt is not None else 0
            pGrid -= adjustment
            logging.debug("Battery buffer active: SOC=%s%% (threshold %s%%, hysteresis %s%%) -> pGrid adjusted by -%sW virtual surplus%s%s" %
                          (batterySocForSupport, maxSocForSupport, supportHysteresis, adjustment,
                           " (incl. pgt compensation)" if adjustment != supportPower else "",
                           " (force-start tier, PV gate bypassed)" if self._batteryForceStartActive else ""))
      elif self._batterySupportActive:
        # Flag could otherwise get stuck "active" if disabled mid-session -
        # see README Findings ("Battery buffer: two bugs...").
        self._batterySupportActive = False
        logging.info("Battery buffer disabled via config.ini (BatterySupportMinSoc/Power set to 0) - deactivating")

      # Pause/release decision for battery priority. The go-e's own Eco
      # algorithm DOES regulate the actual charge current live from
      # pGrid/pPv/pAkku (confirmed by live testing - see _applyChargeMode for
      # the root-cause finding about the 'amp' ceiling); the actual current
      # itself is intentionally not computed or set here.
      if self._batteryPriorityPaused:
        if not self._setFrc(1):
          logging.warning("Could not set frc=1 - will retry next cycle")
        return
      else:
        if not self._setFrc(0):
          logging.warning("Could not release frc - will retry next cycle")

      payload = json.dumps({"pGrid": int(round(pGrid)), "pPv": int(round(pPv)), "pAkku": int(round(pAkku))})
      baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
      requests.get(url=baseURL, params={'ids': payload}, timeout=1)

      logging.debug("Auto mode PV push: pGrid=%s pPv=%s pAkku=%s" % (pGrid, pPv, pAkku))
    except Exception as e:
      logging.critical('Error at %s', '_pushPvSurplusValues', exc_info=e)


  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("Charge mode: %s" % ("Auto" if self._chargeMode == 1 else "Manual"))
    logging.info("--- End: sign of life ---")
    return True

  def _update(self):
    try:
       #get data from go-eCharger (incl. 'lmo' to detect external mode changes,
       #'modelStatus' to disambiguate WHY charging is paused, and 'err' to
       #disambiguate WHICH error occurred if car==5 - see /Status below)
       baseFilter = 'nrg,eto,wh,alw,amp,ama,car,tmp,tma,modelStatus,err,psm,pgt,pvopt_averagePGrid,pvopt_averagePPv,pvopt_averagePAkku'
       filter = baseFilter + ',lmo' if self._chargeControlEnabled else baseFilter
       data = self._getGoeChargerData(filter)

       if data is not None:
          # go-e reachable again (or still) - reflect this in /Connected.
          # Only written when it actually changes, avoiding a redundant
          # write every single successful cycle.
          if self._dbusservice['/Connected'] != 1:
            logging.info("go-eCharger reachable again - /Connected set to 1")
            self._dbusservice['/Connected'] = 1

          '''
          data['nrg']
          0 = U L1
          1 = U L2
          2 = U L3
          3 = U N
          4 = I L1
          5 = I L2
          6 = I L3
          7 = P L1
          8 = P L2
          9 = P L3
          10 = P N
          11 = P Total
          12 = PF L1
          13 = PF L2
          14 = PF L3
          15 = PF N
          '''
          hardwareVersion = self._hardwareVersion

          #send data to DBus
          self._dbusservice['/Ac/Voltage'] = int(data['nrg'][0])
          self._dbusservice['/Ac/L1/Power'] = int(data['nrg'][7])
          self._dbusservice['/Ac/L2/Power'] = int(data['nrg'][8])
          self._dbusservice['/Ac/L3/Power'] = int(data['nrg'][9])
          self._dbusservice['/Ac/Power'] = int(data['nrg'][11])
          self._dbusservice['/Current'] = max(data['nrg'][4], data['nrg'][5], data['nrg'][6])

          # /Ac/Energy/Forward = lifetime total energy (kWh); eto (API v2) is in Wh
          self._dbusservice['/Ac/Energy/Forward'] = round(float(data['eto']) / 1000.0, 2)
          # /Session/Energy = energy of the current charging session (kWh); wh is in Wh
          # 'wh' is only available on newer hardware/API - fall back to 0 if missing
          if 'wh' in data and data['wh'] is not None:
            self._dbusservice['/Session/Energy'] = round(data['wh'] / 1000, 2)
          else:
            self._dbusservice['/Session/Energy'] = 0

          self._dbusservice['/StartStop'] = int(data['alw'])
          self._dbusservice['/SetCurrent'] = int(data['amp'])
          self._dbusservice['/MaxCurrent'] = int(data['ama'])

          # increment charge time only on active charging (2), reset when no
          # car connected (1). Local stopwatch, not go-e's 'cdi'/'rbt-lcctc'
          # fields - see README Findings for why those didn't work.
          timeDelta = time.time() - self._lastUpdate
          carForTiming = int(data['car']) if data['car'] is not None else None
          if carForTiming == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif carForTiming == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)
          # /Session/Time - read by the Venus OS GUI/VRM; /ChargingTime is
          # deprecated in the official docs.
          self._dbusservice['/Session/Time'] = int(self._chargingTime)

          # External psm change detection (e.g. changed directly in the go-e
          # app) - mirrors /AutoStart to match, depending on AutoStartMode.
          # Independent of EnableChargeControl, like /AutoStart itself.
          if self._autoStartMode != 0:
            currentPsm = int(data['psm']) if 'psm' in data and data['psm'] is not None else None
            if currentPsm is not None and currentPsm != self._lastCommandedPsm:
              if self._autoStartMode == 1:
                newAutoStart = 0 if currentPsm == 1 else 1
              elif self._autoStartMode == 2:
                newAutoStart = 0 if currentPsm == 2 else 1
              else:  # mode 3: "1P-3P"
                newAutoStart = 0 if currentPsm == 1 else 1
              if newAutoStart != self._dbusservice['/AutoStart']:
                logging.info("External change detected: go-e psm=%s -> AutoStart set to %s" %
                              (currentPsm, newAutoStart))
                self._dbusservice['/AutoStart'] = newAutoStart
              self._lastCommandedPsm = currentPsm

          # The entire following block (mode sync, PV surplus push) only runs if
          # the new control logic has been explicitly enabled via config.ini.
          if self._chargeControlEnabled:
            # External lmo change detection - lmo: 4=Eco (Auto), 5=Daily
            # Trip (Scheduled), anything else -> Manual.
            currentLmo = int(data['lmo']) if 'lmo' in data and data['lmo'] is not None else None
            if currentLmo is not None and currentLmo != self._lastCommandedLmo:
              if currentLmo == 4:
                newChargeMode = 1
              elif currentLmo == 5:
                newChargeMode = 2
              else:
                newChargeMode = 0
              if newChargeMode != self._chargeMode:
                logging.info("External change detected: go-e lmo=%s -> charge mode set to %s" %
                              (currentLmo, {1: "Auto", 2: "Scheduled"}.get(newChargeMode, "Manual")))
                self._chargeMode = newChargeMode
                self._dbusservice['/Mode'] = self._chargeMode
                if self._chargeMode != 1:
                  self._resetAutoModeState()
              self._lastCommandedLmo = currentLmo

            # In Auto mode, forward the PV surplus values to the go-e
            if self._chargeMode == 1:
              # Cancels any pending zero-flush (relevant after a restart if
              # the go-e turns out to already be in Auto).
              self._pvResetCyclesRemaining = 0
              self._pushPvSurplusValues()
            elif self._pvResetCyclesRemaining > 0:
              # Flushes the go-e's rolling averages after leaving Auto - see
              # README Findings ("Fresh PV data is pushed...").
              try:
                payload = json.dumps({"pGrid": 0, "pPv": 0, "pAkku": 0}, separators=(',', ':'))
                baseURL = "http://%s/api/set" % self._getConfig()['ONPREMISE']['Host']
                requests.get(url=baseURL, params={'ids': payload}, timeout=1)
                self._pvResetCyclesRemaining -= 1
                logging.info("Flushing go-e rolling averages after leaving Auto: pushed pGrid/pPv/pAkku=0 "
                             "(%d cycle(s) remaining)" % self._pvResetCyclesRemaining)
              except Exception as e:
                logging.warning("Could not send PV reset push after leaving Auto mode: %s" % e)
                self._pvResetCyclesRemaining -= 1

            # Checked every cycle, regardless of whether self._chargeMode was
            # just updated by the explicit Venus /Mode switch above or by the
            # external lmo-change detection just above - see the method's
            # own docstring for the full reasoning.
            self._updateBatteryDischargeLock(carForTiming)

            # Also checked every cycle, independent of charge mode - see the
            # method's own docstring for the full reasoning.
            self._checkGridCurrentLimit(data)
          else:
            currentLmo = None

          hardwareVersion = self._hardwareVersion
          if '/MCU/Temperature' in self._dbusservice: # check if path exists, at some point it was removed
             if hardwareVersion >= 3:
                self._dbusservice['/MCU/Temperature'] = int(data['tma'][0] if data['tma'][0] else 0)
             else:
                self._dbusservice['/MCU/Temperature'] = int(data['tmp'])

          # car (go-e): 0=Unknown/Error, 1=Idle, 2=Charging, 3=WaitCar,
          # 4=Complete, 5=Error (can also be null on an internal error).
          # Venus /Status (confirmed against the official Venus OS dbus-api
          # wiki, evcharger section): 0=Disconnected, 1=Connected,
          # 2=Charging, 3=Charged, 4=Waiting for sun, 5=Waiting for RFID,
          # 6=Waiting for start, 7=Low SOC, 8=Ground test error,
          # 9=Welded contacts error, 10=CP input test error (shorted),
          # 11=Residual current detected, 12=Undervoltage detected,
          # 13=Overvoltage detected, 14=Overheating detected, 15-19=reserved,
          # 20=Charging limit. Status=1 (Connected) is not currently
          # emitted - WaitCar(3) is mapped to 6 (Waiting for start)
          # instead, since go-e gives no further signal to distinguish a
          # merely-connected-and-idle car from one gating an imminent
          # start. Status=7 (Low SOC) refers to the HOME/system battery
          # SOC, not the EV's - confirmed via real Victron community
          # threads about Victron's own EVCS product, where this is
          # communicated from the GX device to the charger. This fork now
          # sets it directly whenever BatteryPriorityMinSoc is pausing
          # charging (see override below), since that is exactly this
          # condition. Status=12 (Undervoltage) remains genuinely
          # unreachable - go-e's err enum has no undervoltage code, only
          # Overvolt.
          # car==4 needs go-e's modelStatus to disambiguate WHY (paused vs.
          # genuinely finished) - see README Findings ("Detailed /Status
          # reporting"). Only modelStatus 4/17 are live-confirmed; the err
          # mappings below are confirmed against the official API v2 field
          # reference but not live-tested (this fork has not encountered a
          # real fault condition to verify against).
          carValue = data['car']
          if carValue is None:
            status = 0
          elif int(carValue) == 1:
            status = 0
          elif int(carValue) == 2:
            status = 2
          elif int(carValue) == 3:
            # Found on reconsideration: "WaitCar" (per go-e's own IFTTT
            # integration docs, "Wait for Car") means the go-e itself is
            # waiting for the VEHICLE to request charging (CP handshake) -
            # a normal, unremarkable transient state right after plugging
            # in, not something being held back externally. This is a
            # better fit for Venus Status=1 ("Connected") than 6 ("Waiting
            # for start"), which better describes something actively
            # gating an otherwise-ready session. Distinguished here using
            # this fork's own last-commanded frc value (not car/modelStatus
            # alone, which can't tell the two apart): if we ourselves are
            # currently forcing off (frc=1, e.g. Manual entered before any
            # charging ever started), that IS an active hold - Status=6.
            # Otherwise, genuinely just Status=1.
            status = 6 if self._lastCommandedFrc == 1 else 1
          elif int(carValue) == 4:
            modelStatus = int(data['modelStatus']) if 'modelStatus' in data and data['modelStatus'] is not None else None
            if modelStatus == 4:
              # Confirmed live: this is exactly the "frc=1, hard stop" state
              # (e.g. battery priority pause) that requires acknowledgement
              # in the go-e app ("Eco mode fortsetzen").
              status = 6
            elif modelStatus == 17:
              # Confirmed live: this is the go-e's own soft pause due to
              # insufficient PV surplus (frc=0, no external force needed) -
              # exactly what "waiting for sun" is meant to represent.
              status = 4
            elif modelStatus == 2:
              # go-e's own modelStatus enum names this
              # NotChargingBecauseAccessControlWait - this fork's user
              # actively uses RFID, so this path is genuinely reachable
              # (not just documented) - not separately live-verified here,
              # but expected to trigger correctly when a card tap is
              # pending.
              status = 5
            elif modelStatus == 6:
              # go-e's own modelStatus enum names this
              # NotChargingBecauseEnergyLimit - this is the go-e's own
              # per-session energy limit (the 'dwo' key, e.g. "stop after
              # 5 kWh this session") having been reached. Confirmed against
              # the official modelStatus enum, not live-tested (this fork's
              # own installation does not use this go-e feature).
              status = 20
            else:
              # No known pause reason matched - genuinely finished/charged.
              status = 3
          elif int(carValue) == 5:
            errValue = int(data['err']) if 'err' in data and data['err'] is not None else None
            # err enum confirmed against the official go-e API v2 field
            # reference (apikeys-en.md): None=0, FiAc=1, FiDc=2, Phase=3,
            # Overvolt=4, Overamp=5, Diode=6, PpInvalid=7, GndInvalid=8,
            # ContactorStuck=9, ContactorMiss=10, FiUnknown=11, Unknown=12,
            # Overtemp=13, NoComm=14, StatusLockStuckOpen=15,
            # StatusLockStuckLocked=16, Reserved20-24.
            if errValue == 8:
              status = 8   # GndInvalid -> Ground fault
            elif errValue == 9:
              status = 9   # ContactorStuck -> Welded contacts
            elif errValue == 7:
              status = 10  # PpInvalid -> CP input test error (shorted)
            elif errValue == 1 or errValue == 2:
              status = 11  # FiAc/FiDc (AC/DC residual current) -> Residual current detected
            elif errValue == 4:
              status = 13  # Overvolt -> Overvoltage
            elif errValue == 13:
              status = 14  # Overtemp -> Overheating
            else:
              # Remaining go-e err values (Phase, Overamp, Diode,
              # ContactorMiss, FiUnknown, Unknown, NoComm,
              # StatusLockStuckOpen/Locked) have no confident, specific
              # Venus equivalent - Venus also has no dedicated "unspecified
              # error" code (15-19 are reserved, not to be used). Falls
              # back to Overheating as the closest generic "something is
              # wrong" signal - still correctly shows SOME error rather
              # than "Disconnected", just not the precise cause.
              status = 14
          else:
            status = 0

          # Override: our own BatteryPriorityMinSoc pause takes priority
          # over whatever the go-e's own car/modelStatus would otherwise
          # map to. Confirmed via real Victron community threads about
          # Victron's own EVCS product that Status=7 ("Low SOC") refers to
          # the home/system battery SOC being below a charger-specific
          # threshold - communicated from the GX device to the charger -
          # not the vehicle's own SOC as initially assumed. This is exactly
          # what BatteryPriorityMinSoc represents, so this fork can
          # confidently report it accurately instead of leaving it to
          # whatever generic pause reason the go-e itself reports.
          if self._batteryPriorityPaused:
            status = 7
          # Override: this fork's own GridCurrentLimit safety intervention
          # takes priority even over BatteryPriorityMinSoc above - protecting
          # the house connection fuse is a more urgent concern than the
          # battery-priority pause. Uses Status=20 ("Charging limit"),
          # already used elsewhere in this fork for go-e's own per-session
          # energy limit (modelStatus=6) - both genuinely share the same
          # meaning ("charging is being deliberately capped/stopped by an
          # external limit, not a fault"). Status=11 ("Residual current
          # detected") was considered and rejected: that specifically means
          # an RCD/GFCI trip (a real ground-fault/leakage condition), a
          # different electrical phenomenon from a phase simply being near
          # its rated current - showing that here would be actively
          # misleading. Only applied in GridCurrentLimitMode=2 (active) -
          # in log-only mode (1), nothing was actually done to the charger,
          # so showing this here would misrepresent what's really happening.
          if self._gridOverloadState != 'normal' and self._getSetting('GridCurrentLimitMode', 0) == 2:
            status = 20
          self._dbusservice['/Status'] = status

          #logging
          logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
          logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
          logging.debug("Wallbox Session Energy (/Session/Energy): %s" % (self._dbusservice['/Session/Energy']))
          logging.debug("Wallbox Session Time (/Session/Time): %s" % (self._dbusservice['/Session/Time']))
          logging.debug("Charge mode: %s (lmo=%s)" % ("Auto" if self._chargeMode == 1 else "Manual", currentLmo))
          # Logged regardless of mode, to observe the reset-push decaying
          # these (see README Findings, "Fresh PV data is pushed...").
          logging.debug("go-e rolling averages: pvopt_averagePGrid=%s pvopt_averagePPv=%s pvopt_averagePAkku=%s" %
                        (data.get('pvopt_averagePGrid'), data.get('pvopt_averagePPv'), data.get('pvopt_averagePAkku')))
          # Single, compact per-cycle state summary for detailed debugging -
          # everything above is logged only once per value or only when it
          # changes, so reconstructing the FULL picture at one point in time
          # (car state, actual SOC, currently-commanded amp/frc, the live
          # ESS MinimumSocLimit, and which internal feature flags are
          # currently active) previously required cross-referencing several
          # separate log lines, or falling back to pvcheck.sh. This line
          # exists purely for troubleshooting and has no effect on behaviour.
          activeFlags = []
          if self._batteryPriorityPaused:
            activeFlags.append('priority_paused')
          if self._batterySupportActive:
            activeFlags.append('support_active')
          if self._batteryForceStartActive:
            activeFlags.append('force_start_active')
          if self._savedEssMinSoc is not None:
            activeFlags.append('discharge_lock_active')
          if self._gridOverloadState != 'normal':
            activeFlags.append('grid_overload_%s' % self._gridOverloadState)
          currentSocForLog = self._safeGetValue(self._batterySocItem, '/Dc/Battery/Soc')
          currentEssMinSocForLog = self._safeGetValue(self._essMinSocItem, '/Settings/CGwacs/BatteryLife/MinimumSocLimit')
          logging.debug("State: car=%s SOC=%s%% amp_ceiling=%s frc=%s ESS_MinSoc=%s%% flags=%s" %
                        (data.get('car'), currentSocForLog, self._lastCommandedAmp, self._lastCommandedFrc,
                         currentEssMinSocForLog, activeFlags if activeFlags else 'none'))
          logging.debug("---")

          # increment UpdateIndex - to show that new data is available
          index = self._dbusservice['/UpdateIndex'] + 1  # increment index
          if index > 255:   # maximum value of the index
            index = 0       # overflow from 255 to 0
          self._dbusservice['/UpdateIndex'] = index

          #update lastupdate vars
          self._lastUpdate = time.time()
       else:
          # go-e unreachable this cycle - reflect in /Connected/Status so
          # Venus OS/VRM shows it as offline instead of freezing on stale
          # values. See README Findings ("Detailed /Status reporting").
          if self._dbusservice['/Connected'] != 0:
            logging.info("go-eCharger unreachable - /Connected set to 0")
            self._dbusservice['/Connected'] = 0
            self._dbusservice['/Status'] = 0
          logging.debug("Wallbox is not available")

    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)

    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True

  def _handlechangedvalue(self, path, value):
    logging.info("someone else updated %s to %s" % (path, value))

    if path == '/SetCurrent':
      # Also updates _lastCommandedAmp, since this writes the same 'amp' key
      # the Auto-mode ceiling logic manages - see README Findings ("Other
      # robustness findings").
      ok = self._setGoeChargerValueV2('amp', int(value))
      if ok:
        self._lastCommandedAmp = int(value)
      return ok
    elif path == '/StartStop':
      # 'alw' is not written - 'frc' alone is sufficient. See README Findings
      # ("API endpoint quirks").
      enable = bool(int(value))
      return self._setFrc(0 if enable else 1)
    elif path == '/AutoStart':
      # Repurposed - see README ("/AutoStart repurposed..."). Behaviour
      # depends on AutoStartMode (read once at startup, see __init__).
      if self._autoStartMode == 0:
        return True
      enable = bool(int(value))
      if self._autoStartMode == 1:
        return self._setPsm(0 if enable else 1)
      elif self._autoStartMode == 2:
        return self._setPsm(0 if enable else 2)
      elif self._autoStartMode == 3:
        return self._setPsm(2 if enable else 1)
    elif path == '/MaxCurrent':
      return self._setGoeChargerValueV2('ama', int(value))
    elif path == '/Mode':
      if not self._chargeControlEnabled:
        logging.warning("EnableChargeControl is disabled - /Mode change ignored")
        return False
      if int(value) not in (0, 1, 2):
        logging.warning("Charge mode %s not supported (0=Manual, 1=Auto, 2=Scheduled)" % value)
        return False
      self._chargeMode = int(value)
      self._applyChargeMode()
      return True
    else:
      logging.info("mapping for evcharger path %s does not exist" % (path))
      return False


def main():
  #configure logging
  config = configparser.ConfigParser()
  config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
  logging_level = config["DEFAULT"]["Logging"].upper()

  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging_level,
                            handlers=[
                                # backupCount required, else rotation never
                                # happens - see README Findings ("Other
                                # robustness findings").
                                RotatingFileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__))), maxBytes=2000000, backupCount=3),
                                logging.StreamHandler()
                            ])

  try:
      logging.info("Start")

      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)

      #formatting
      _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')
      _degC = lambda p, v: (str(v) + '°C')
      _s = lambda p, v: (str(v) + 's')

      #start our main-service
      pvac_output = DbusGoeChargerService(
        servicename='com.victronenergy.evcharger',
        paths={
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          '/ChargingTime': {'initial': 0, 'textformat': _s},
          '/Session/Energy': {'initial': 0, 'textformat': _kwh},
          '/Session/Time': {'initial': 0, 'textformat': _s},

          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Current': {'initial': 0, 'textformat': _a},
          '/SetCurrent': {'initial': 0, 'textformat': _a},
          '/MaxCurrent': {'initial': 0, 'textformat': _a},
          '/MCU/Temperature': {'initial': 0, 'textformat': _degC},
          '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))},
          # /AutoStart is deliberately repurposed here - NOT its official
          # Victron meaning ("start automatically when a vehicle is
          # connected"). Instead: 1 = Auto (go-e's own live surplus-based
          # 1-/3-phase switching, psm=0), 0 = force single-phase (psm=1).
          # This is the only standard evcharger GUI element available for a
          # user-facing toggle without building a separate custom mechanism -
          # see README for the full reasoning and the physical-relay-wear
          # caveat (phase switching is a real, timed contactor changeover,
          # not a soft parameter).
          '/AutoStart': {'initial': 1, 'textformat': lambda p, v: (str(v))}
        }
        )

      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      # Visible even at Logging=WARN (INFO/DEBUG lines above are filtered out
      # at that level, leaving nothing in the log to confirm the service
      # actually started successfully) - a single line at WARNING level so a
      # restart is always confirmable regardless of the configured log level.
      logging.warning('dbus-goecharger started successfully - service is running (this line is shown regardless of Logging level in config.ini)')
      mainloop = gobject.MainLoop()
      mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
