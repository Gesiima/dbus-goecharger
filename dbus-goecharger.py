#!/usr/bin/env python

# import normal packages
import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file
import json
import dbus

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService, VeDbusItemImport


class DbusGoeChargerService:
  def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
    # NOTE on HTTP connections: a shared requests.Session() (for connection
    # reuse/keep-alive) was tried here, then an explicit 'Connection: close'
    # header was added on top after live testing showed the go-e closes its
    # socket after every response without declaring this itself - causing
    # urllib3's pool to attempt a doomed reuse first ("Resetting dropped
    # connection") before opening a fresh connection, which is strictly worse
    # than never pooling at all. CONFIRMED LIVE that this header did not
    # reliably prevent the pool from still attempting reuse (the header only
    # asks the *server* to close; it does not reliably stop this client's own
    # pool from trying to reuse a still-pooled connection from an earlier
    # call). Reverted back to plain module-level requests.get(...) calls
    # everywhere below instead - confirmed to not exhibit this "attempt
    # reuse, discover dead, reconnect" pattern, at the cost of not attempting
    # keep-alive at all (irrelevant anyway, since the go-e doesn't support it
    # reliably in the first place).
    # Initialize state variables FIRST, since _handlechangedvalue (registered as
    # onchangecallback) accesses them and could in theory already be called
    # during setup.
    self._lastUpdate = 0
    self._chargingTime = 0.0
    self._chargeMode = 0            # 0=Manual, 1=Auto, 2=Scheduled
    self._lastCommandedLmo = None
    self._batteryPriorityPaused = False
    self._batterySupportActive = False
    # Last 'frc' value we ourselves commanded, GLOBALLY across all modes (None
    # = not yet known, e.g. right after service start). 'frc' physically
    # clicks the go-e's charge contactor/relay on every write (confirmed
    # live) - this tracker, together with _setFrc() below, ensures 'frc' is
    # only ever written when the desired value actually differs from what we
    # last commanded, regardless of which mode or code path is asking for it.
    # Deliberately NOT reset on mode switches, since the goal is precisely to
    # avoid redundant re-writes of an already-correct value across mode
    # transitions (e.g. Auto -> Manual should not click the relay if frc was
    # already 0 in both).
    self._lastCommandedFrc = None
    # last amp value we ourselves commanded in Auto mode (None = never set).
    # IMPORTANT: 'amp' is written to flash on every set on API v2 hardware
    # (unlike the API v1-only 'amx' key, which does not exist on this API v2
    # charger and was confirmed absent via live testing). To avoid excessive
    # flash wear (~100,000 write cycles) from writing every 5s, 'amp' is only
    # written when the computed target current actually changes - matching
    # how evcc's own go-e driver behaves (confirmed via evcc source code).
    self._lastCommandedAmp = None
    # Last 'psm' value we ourselves commanded (None = not yet known). Phase
    # switching (psm: 0=Auto, 1=Force 1-phase, 2=Force 3-phase) involves a
    # real, timed contactor changeover on the hardware (community reports
    # ~10s for the physical switch to complete) - not a soft parameter like
    # 'amp'. Tracked the same way as frc/amp to avoid redundant writes.
    self._lastCommandedPsm = None
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
    acPosition = int(config['DEFAULT']['AcPosition'])
    pauseBetweenRequests = int(config['ONPREMISE']['PauseBetweenRequests']) # in ms

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
    if data:
       fwv = data['fwv']
       try:
           fwv = int(data['fwv'].replace('.', ''))
       except:
           pass
       self._dbusservice.add_path('/FirmwareVersion', fwv)
       self._dbusservice.add_path('/Serial', data['sse'])
    self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path('/Position', acPosition)

    # /Status: read-only, reflects the car state (set in _update)
    self._dbusservice.add_path('/Status', None)

    # Feature flag: the entire new control logic (mode switching, PV push,
    # scheduler control, grid target, battery priority, battery buffer) can be
    # fully enabled/disabled via config.ini. Default: off, so existing
    # installations keep their previous, monitoring-only behaviour after
    # updating this script, until explicitly enabled.
    self._chargeControlEnabled = config.getboolean('DEFAULT', 'EnableChargeControl', fallback=False)
    if self._chargeControlEnabled:
      # only make /Mode actually writable when the control logic is enabled
      self._dbusservice.add_path('/Mode', 0, writeable=True, onchangecallback=self._handlechangedvalue)
    else:
      # otherwise, as in the original: a plain display value, not writable
      self._dbusservice.add_path('/Mode', 0)

    # What the repurposed /AutoStart toggle actually does - read ONCE at
    # startup only (unlike most other tuning values, which are re-read from
    # config.ini on every cycle via _getSetting()), since this defines the
    # very meaning of a control path and isn't something that should change
    # its behaviour mid-session without a restart.
    # 0 = disabled (button has no function at all - psm is never touched)
    # 1 = "1P-Auto": AutoStart=0 -> force 1-phase (psm=1), AutoStart=1 -> Auto (psm=0) [default, matches this fork's original behaviour]
    # 2 = "3P-Auto": AutoStart=0 -> force 3-phase (psm=2), AutoStart=1 -> Auto (psm=0)
    # 3 = "1P-3P":   AutoStart=0 -> force 1-phase (psm=1), AutoStart=1 -> force 3-phase (psm=2) - Auto (psm=0) never used
    self._autoStartMode = config.getint('DEFAULT', 'AutoStartMode', fallback=1)
    if self._autoStartMode not in (0, 1, 2, 3):
      logging.warning("AutoStartMode=%s is not a recognized value (0/1/2/3) - falling back to 1 (1P-Auto)" % self._autoStartMode)
      self._autoStartMode = 1

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

    # Private, separate connection to the system D-Bus, to read PV/grid/battery
    # values from com.victronenergy.system (for Auto mode / PV surplus push).
    # Kept separate from self._dbusservice, since this connection only reads
    # and does not register a service.
    systemBus = dbus.SystemBus()
    try:
      # pGrid: no equivalent aggregated "Total" path could be confirmed for
      # /Ac/Grid/* (unlike /Ac/PvOnGrid/Total/Power below) - L1/L2/L3 are
      # therefore read and summed explicitly. This matters whenever the grid
      # CONNECTION POINT itself is three-phase (a proper multi-phase grid
      # meter measuring all three phases), even if only one phase is actually
      # managed by a single-phase Multiplus/inverter - load or generation on
      # the other two phases would otherwise be invisible to pGrid entirely,
      # even though it directly affects real grid import/export.
      self._gridPowerItemL1 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L1/Power')
      self._gridPowerItemL2 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L2/Power')
      self._gridPowerItemL3 = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L3/Power')
      # AC-coupled PV: /Ac/PvOnGrid/Total/Power (a dbus-systemcalc-py
      # aggregate) looked like a clean shortcut, but was CONFIRMED LIVE to
      # not exist on this system - `dbus -y com.victronenergy.system
      # /Ac/PvOnGrid/Total/Power GetValue` raised an AttributeError from the
      # dbus CLI itself (no such object). This silently degraded to 0 via
      # this fork's own _safeGetValue() defensive handling - not a crash, but
      # a real, unnoticed loss of the entire AC-coupled contribution to pPv
      # (confirmed live: ~1700W of real AC PV production was missing,
      # leaving only the ~650W DC-coupled portion). Reverted to explicit
      # L1/L2/L3 summing, matching the approach already used for pGrid above.
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

    # add _update function 'timer'
    gobject.timeout_add(pauseBetweenRequests, self._update)

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)

  def _safeGetValue(self, item, itemName):
    '''
    Wraps item.get_value() defensively. Live testing showed that a Venus OS
    system dbus item (e.g. a newly added path like /Ac/Grid/L2/Power,
    /Ac/Grid/L3/Power, or /Ac/PvOnGrid/Total/Power that may not exist or be
    served on every Venus OS version/configuration) can raise a raw
    dbus.exceptions.DBusException ("was not provided by any .service files")
    at the moment of the FIRST actual .get_value() call, even though
    constructing the VeDbusItemImport object itself succeeded without error -
    this is different from (and not caught by) the try/except around item
    construction in __init__. An uncaught exception here previously crashed
    the entire service. Every read of a Venus system item now goes through
    this method instead of calling .get_value() directly, so a single
    missing/unavailable value degrades gracefully (treated the same as an
    item that was None from the start) instead of taking down the whole
    process.
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
    ids={...} JSON-batch parameter.

    ROOT CAUSE FOUND (after extensive live testing): earlier versions of this
    method wrapped every key in ids={"key":value}, matching the mechanism
    required for pGrid/pPv/pAkku and the scheduler objects (which have no
    single-key setter and genuinely require ids=). For simple scalar keys like
    lmo/frc, however, this was wrong: manual tests using the plain query
    parameter form (?lmo=4, ?frc=1) were rock-solid stable, while the exact
    same values sent via ids={"lmo":4} were silently reverted by the go-e
    within well under a second. The two write paths are evidently handled
    differently internally by the firmware. Plain query parameters are
    therefore used here; ids={...} is reserved for pGrid/pPv/pAkku and the
    scheduler objects, which are set directly in their respective methods.
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
    '''
    config = self._getConfig()
    return int(config['DEFAULT'].get(name, default))


  def _setGoeSchedulerEnabled(self, enabled):
    '''
    Enables/disables the go-e's own weekly schedule (sch_week/sch_satur/sch_sund)
    without changing the time windows (ranges) configured there in the app. To do
    this, the current object is read, only 'control' is changed, and the whole
    object is written back - the times remain exactly as set in the go-e app.
    control: Disabled=0, Inside=1, Outside=2 (we only use 0/1)

    IMPORTANT: uses compact JSON encoding (no whitespace after ':'/',') - the
    default json.dumps() output previously caused this call to fail with
    "ESP_ERR_HTTPD_RESULT_TRUNC" (URL too long for the go-e's small ESP32 HTTP
    server buffer). Compact encoding saves ~30 characters per call, which may
    be enough to stay under that limit - if it still fails on devices with
    more configured time ranges, the URL may simply be too long regardless.
    '''
    control = 1 if enabled else 0
    config = self._getConfig()
    for key in ('sch_week', 'sch_satur', 'sch_sund'):
      try:
        current = self._getGoeChargerData(key)
        if current is None or key not in current or current[key] is None:
          logging.warning("Scheduler: %s not available, skipping" % key)
          continue
        schedObj = current[key]
        schedObj['control'] = control
        payload = json.dumps({key: schedObj}, separators=(',', ':'))
        baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
        request_data = requests.get(url=baseURL, params={'ids': payload}, timeout=2)
        if not request_data or not getattr(request_data, 'ok', True):
          logging.warning("Scheduler: setting %s failed (HTTP %s): %s" %
                          (key, getattr(request_data, 'status_code', '?'), getattr(request_data, 'text', '')[:150]))
      except Exception as e:
        logging.critical('Error at %s', '_setGoeSchedulerEnabled(%s)' % key, exc_info=e)


  def _applyChargeMode(self):
    '''
    Sends the selected charge mode to the go-e:
    Auto (1)      -> lmo=4 (Eco mode), fup=true, frc=0 (neutral - let the Eco
                     algorithm decide on/off itself), grid target (pgt)
                     (go-e takes over PV surplus control, including phase
                     switching, by itself)
    Scheduled (2) -> lmo=3, frc=0 (neutral - let the go-e's own schedule decide on/off)
    Manual (0)    -> lmo=3, frc=1 (force off) as a safe default when entering
                     Manual, direct control afterwards via SetCurrent/StartStop

    IMPORTANT finding from live testing: plain alw=false is NOT reliably
    respected by the go-e while its own Eco algorithm considers charging
    active/justified (e.g. right after a PV surplus push) - it gets silently
    reverted back to alw=true within seconds. The dedicated 'frc' (force state:
    0=Neutral, 1=Off, 2=On) key reliably overrides this and was confirmed
    stable over 15+ seconds in testing, unlike alw alone.
    '''
    try:
      # Reset Auto-mode-specific state when leaving Auto - the actual 'frc'
      # value for the new mode is set further below via _setFrc(), which
      # only writes (and only clicks the relay) if the value actually needs
      # to change.
      if self._chargeMode != 1:
        self._batteryPriorityPaused = False
        self._batterySupportActive = False
        self._lastCommandedAmp = None
        # IMPORTANT (found live): the raw pGrid/pPv/pAkku fields correctly go
        # null once this fork stops pushing them (on leaving Auto), but the
        # go-e's own INTERNAL ROLLING AVERAGES of these values
        # (pvopt_averagePGrid/pvopt_averagePPv/pvopt_averagePAkku) do NOT -
        # confirmed live, they stay frozen at their last computed value
        # indefinitely while in Manual/Scheduled, seemingly for as long as
        # desired, with no visible decay. Since the Eco algorithm's
        # start/stop and phase-switch decisions appear to be driven by these
        # averages rather than the instantaneous raw values (this would also
        # explain why simply pushing one fresh value immediately upon
        # re-entering Auto did not fully prevent it from resuming as if the
        # old surplus still applied - a single fresh sample does not
        # immediately override several minutes of accumulated average), a
        # one-time reset push of zeros is sent here, when Auto is left, to
        # start nudging the stored averages back down towards "no known
        # surplus" instead of leaving them frozen on a stale, possibly much
        # higher value for however long Manual/Scheduled happens to last.
        try:
          payload = json.dumps({"pGrid": 0, "pPv": 0, "pAkku": 0}, separators=(',', ':'))
          baseURL = "http://%s/api/set" % self._getConfig()['ONPREMISE']['Host']
          requests.get(url=baseURL, params={'ids': payload}, timeout=1)
          logging.info("Left Auto mode - sent one-time pGrid/pPv/pAkku=0 reset so the go-e's internal rolling averages start decaying instead of staying frozen on stale surplus data")
        except Exception as e:
          logging.warning("Could not send PV reset push on leaving Auto mode: %s" % e)

      if self._chargeMode == 1:
        # ROOT CAUSE FOUND (after extensive live testing): 'amp' is NOT the
        # live-regulated charge current - it is a CEILING that the go-e's Eco
        # algorithm will not exceed. The actual, live-regulated current is
        # reflected in nrg[4] (A) / nrg[11] (W), never in 'amp' itself. If
        # 'amp' was left at a low value (e.g. 6, from Manual mode or an
        # earlier test), the Eco algorithm is silently capped there and
        # cannot regulate upward at all - this looked exactly like "the Eco
        # algorithm doesn't respond to pGrid" during many hours of testing,
        # when in fact it was working correctly the whole time, just capped
        # by our own leftover ceiling. Confirmed live: with amp raised to 16,
        # a simulated ~2070W surplus (9A) made the real current climb from
        # ~5.6A to ~8.2A and rising within 40s - clear, genuine regulation.
        # The ceiling is therefore raised to the device's configured maximum
        # (ama/'/MaxCurrent') every time Auto mode is (re-)entered, so the Eco
        # algorithm always has its full intended regulation range available -
        # this is NOT computing the charge current ourselves, it only removes
        # an artificial constraint so the go-e's own algorithm can do its job.
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

        # IMPORTANT (found live): fresh pGrid/pPv/pAkku values are pushed
        # BEFORE lmo is switched to 4 (Eco), not after. Confirmed live: Auto
        # (charging on real surplus) -> Manual (continues charging, per the
        # frc fix above) -> Auto again later once it had actually gone dark
        # caused the go-e to immediately resume charging as if the old
        # surplus still applied. The go-e appears to keep its last-received
        # pGrid/pPv/pAkku values around indefinitely while lmo!=4 (Eco isn't
        # evaluating them, so there is nothing to invalidate the old
        # reading), and then acts on whatever it has stored the instant Eco
        # re-activates - if that happens to be a fresh push, fine; if lmo=4
        # is set first and our fresh push follows moments later, there is a
        # brief window where Eco could start evaluating using the old,
        # possibly many-minutes-stale surplus value instead. Sending the
        # fresh values first closes that window entirely: by the time lmo
        # actually flips to 4, the most current real reading is already the
        # "last known" value for Eco to act on. This call also determines and
        # writes the correct initial frc value based on battery priority
        # (frc itself is independent of lmo and safe to set regardless of
        # the current mode) - see the frc-click note in _setFrc() itself for
        # why this ordering also matters for avoiding redundant relay clicks.
        self._pushPvSurplusValues()

        # Disable the go-e's own weekly schedule while in Auto mode, so it
        # cannot compete with the Eco algorithm's own start/stop decisions.
        # (Grid target 'pgt' is no longer set here separately - the
        # _pushPvSurplusValues() call above already sets/tracks it on every
        # cycle now, including this first one right after entering Auto.)
        self._setGoeSchedulerEnabled(False)
        ok = self._setGoeChargerValueV2('lmo', 4)
        self._setGoeChargerValueV2('fup', True)
        if ok:
          self._lastCommandedLmo = 4
        else:
          logging.warning("Could not set lmo=4 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Auto (PV surplus enabled)")
      elif self._chargeMode == 2:
        # "Scheduled" in Venus OS activates the go-e's own "Daily Trip" mode
        # (lmo=5) - NOT the separate weekly on/off timer (sch_week etc., which
        # is a different, independent feature available under Basic mode).
        # Daily Trip lets the go-e charge a target energy amount by a target
        # time, optimizing for the cheapest tariff hours if configured -
        # everything (target kWh, target time, tariff settings) remains fully
        # defined in the go-e app; this script only switches the top-level
        # mode, exactly like Auto/Manual.
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
        # IMPORTANT: Basic mode (lmo=3) has NO PV-surplus gating logic of its
        # own - unlike Eco mode, it does not wait for anything and simply
        # charges immediately once frc allows it and a vehicle is connected.
        # This means frc=0 (neutral) is only safe to use here if charging was
        # ALREADY actively happening at the moment of the switch (e.g. active
        # PV-surplus charging in Auto) - continuing that seamlessly is
        # desired and matches the earlier frc-click fix below. But if
        # charging was NOT actively happening right then (e.g. paused in
        # Auto due to insufficient PV surplus), frc=0 would make Basic mode
        # immediately start charging at the full amp ceiling on its own -
        # which is generally not wanted: switching to Manual is usually meant
        # to simply turn Auto off, not to start a manual charge. The go-e's
        # actual current 'car' state is therefore checked right before
        # deciding: only car==2 (actively charging) keeps frc=0; anything
        # else forces frc=1, taking over "whatever is currently active"
        # instead of blindly always releasing.
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


  def _pushPvSurplusValues(self):
    '''
    Reads PV/grid/battery power from Venus OS (com.victronenergy.system) and
    forwards it in the go-e's own format. According to the go-e API this must
    be called every 5 seconds, otherwise the last value is kept (see warning
    in __init__).

    IMPORTANT - sign convention not yet verified live:
    - pGrid: >0 = grid import, <0 = feed-in (Venus convention matches the go-e convention directly)
    - pPv:   >0 = production
    - pAkku: go-e wants <0 = battery charging. Venus typically reports >0 = charging,
             hence inverted here. Please verify against reality after the first
             test run via "dbus -y com.victronenergy.system / GetValue" (does the
             battery actually charge when /Dc/Battery/Power is positive?) and
             adjust the sign below if it does not match.
    '''
    if self._gridPowerItemL1 is None:
      return

    try:
      config = self._getConfig()

      # Safety net for the 'amp' ceiling (see _applyChargeMode for the full
      # root-cause explanation): _applyChargeMode() only raises the ceiling
      # at the moment of an explicit mode switch via the GUI/D-Bus callback.
      # If the service restarts while the go-e is ALREADY in Auto mode (e.g.
      # after a service or Venus OS restart), that code path is never hit -
      # _update()'s external-change-detection only adopts the existing mode
      # without calling _applyChargeMode(). This check runs on every Auto-mode
      # cycle instead, so the ceiling is guaranteed to be raised regardless of
      # how Auto mode was entered - not just when switched via the GUI.
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

      # Grid target (pgt) is intentionally NEVER written by this fork -
      # configure it directly in the go-e app instead (PV surplus -> Grid
      # target). An earlier version of this script managed pgt via
      # config.ini, including a fairly involved "master value" mechanism to
      # reconcile config.ini edits with direct changes in the go-e app - this
      # added real complexity and a real bug (an app-side change could be
      # silently reverted again on the very next cycle) for a value that is
      # just as easy to set once, directly where it actually takes effect.
      # The current value is still read (see the BatterySupportCompensatePgt
      # handling below) to compensate for it when the battery buffer feature
      # is active, but this script has no writable opinion on what it should be.

      # Battery priority: only release EV charging once the battery SOC has
      # reached a configured minimum threshold (similar to the evcc setting).
      # Hysteresis (default 2%) prevents frequent on/off flapping right at the
      # threshold: charging pauses below minSoc, and is only released again once
      # SOC >= minSoc + hysteresis. This only updates self._batteryPriorityPaused;
      # the actual frc write happens in one combined place below, together with
      # the insufficient-surplus check, to avoid two independent code paths
      # writing frc back-to-back in the same cycle.
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

      # gridPower: summed explicitly across L1/L2/L3 (see __init__ comment) -
      # important for a three-phase grid CONNECTION POINT even when only one
      # phase is managed by a single-phase Multiplus/inverter, so load or
      # generation on the other phases is not silently missed. L1 is treated
      # as required (checked above); L2/L3 are optional and simply contribute
      # 0 if not present/available (e.g. a genuinely single-phase connection).
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

      # Battery as a charging buffer: above an SOC threshold, a fixed additional
      # amount of "virtual surplus" from the battery is allowed to flow into the
      # car (similar to evcc's "battery as buffer above X%" feature). The battery
      # supplies this energy automatically once the go-e requests more power than
      # pure PV production covers - Venus OS/ESS balances this physically on its own.
      maxSocForSupport = self._getSetting('BatterySupportMinSoc', 0)
      supportPower = self._getSetting('BatterySupportPower', 0)
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

          if self._batterySupportActive:
            # IMPORTANT finding from live testing: pGrid, not pPv, appears to
            # be the value that actually drives the go-e's charging decision -
            # every successful test so far worked by making pGrid sufficiently
            # negative; adding only to pPv while pGrid stayed near zero
            # produced no reaction at all, even though pAkku correctly showed
            # the battery discharging. The virtual surplus is therefore added
            # to pGrid (making it more negative = more exportable surplus),
            # not to pPv.
            adjustment = supportPower
            # Optional compensation for pgt's continuous reserve (see "The
            # grid target (pgt) acts continuously" in the README): pgt keeps
            # subtracting its own reserve from whatever pGrid reports,
            # including this virtual battery-support surplus - so without
            # compensating for it, less than the configured BatterySupportPower
            # actually reaches the charge current calculation. Enabled via
            # BatterySupportCompensatePgt in config.ini (default: off, to
            # preserve existing behaviour for anyone already relying on it).
            # pgt itself is only ever configured directly in the go-e app (see
            # note above) - it is read here fresh, purely to inform this
            # compensation, never written.
            # NOTE: the exact relationship between pgt and the resulting
            # current reduction has only been empirically observed, not
            # precisely confirmed as a formula - this compensation is a
            # reasonable approximation (adding pgt's magnitude), not a
            # guaranteed exact match.
            if self._getSetting('BatterySupportCompensatePgt', 0):
              try:
                pgtData = self._getGoeChargerData('pgt')
                currentPgt = pgtData.get('pgt') if pgtData is not None else None
              except Exception:
                currentPgt = None
              adjustment += abs(currentPgt) if currentPgt is not None else 0
            pGrid -= adjustment
            logging.debug("Battery buffer active: SOC=%s%% (threshold %s%%, hysteresis %s%%) -> pGrid adjusted by -%sW virtual surplus%s" %
                          (batterySocForSupport, maxSocForSupport, supportHysteresis, adjustment,
                           " (incl. pgt compensation)" if adjustment != supportPower else ""))
      elif self._batterySupportActive:
        # IMPORTANT (found live): if the feature is disabled via config.ini
        # (BatterySupportMinSoc or BatterySupportPower set to 0) WHILE it was
        # previously active, the block above is skipped entirely and this
        # flag was never being reset - it stayed stuck at True indefinitely
        # (until leaving Auto mode entirely), even though the code that
        # would normally re-evaluate and clear it never ran. This didn't
        # cause a wrong pGrid adjustment (that code is also inside the
        # skipped block), but the flag and its "active" log messages
        # misleadingly kept implying the feature was still engaged. Reset
        # explicitly here instead.
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
          config = self._getConfig()
          hardwareVersion = int(config['DEFAULT']['HardwareVersion'])

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

          # update chargingTime, increment charge time only on active charging (2), reset when no car connected (1)
          # Alternatives tested and discarded: go-e's 'cdi' field does not provide a
          # real-time millisecond value while charging (type=0) - confirmed by test:
          # unchanged after 60s of active charging - and 'rbt - lcctc' resets on every
          # PV-triggered charge restart. This local stopwatch is therefore the only
          # reliable source for the active charging time; it correctly keeps counting
          # across PV-surplus pauses (no reset on car==4), but is lost on a restart of
          # this service (a rare edge case).
          timeDelta = time.time() - self._lastUpdate
          carForTiming = int(data['car']) if data['car'] is not None else None
          if carForTiming == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif carForTiming == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)
          # /Session/Time - this path is read by the Venus OS GUI/VRM for the
          # "Session" display; /ChargingTime is marked deprecated in the official docs.
          self._dbusservice['/Session/Time'] = int(self._chargingTime)

          # Detect an external change of phase-switch mode (e.g. the user
          # switched psm directly in the go-e app, not via the repurposed
          # /AutoStart toggle in Venus OS). Only react if psm changed
          # WITHOUT us having set it ourselves last - same pattern as lmo
          # below. The psm -> /AutoStart mapping depends on AutoStartMode
          # (see __init__): mode 0 leaves /AutoStart untouched entirely
          # (the toggle has no function, so there is nothing meaningful to
          # reflect); modes 1-3 each treat one psm value as "off" (0) and
          # everything else as "on" (1), matching whichever two states that
          # mode's toggle actually switches between.
          # NOTE: deliberately OUTSIDE the EnableChargeControl block below -
          # /AutoStart, like /StartStop and /SetCurrent, works independently
          # of that setting.
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
            # Detect an external change of charge mode (e.g. the user switched
            # Eco/Daily Trip/Normal directly in the go-e app, not via Venus
            # OS). Only react if lmo has changed WITHOUT us having set it
            # ourselves last. lmo: 4=Eco (Auto), 5=Daily Trip (Scheduled),
            # anything else (3=default/Basic) -> Manual.
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
              self._lastCommandedLmo = currentLmo

            # In Auto mode, forward the PV surplus values to the go-e
            if self._chargeMode == 1:
              self._pushPvSurplusValues()
          else:
            currentLmo = None

          config = self._getConfig()
          hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
          if '/MCU/Temperature' in self._dbusservice: # check if path exists, at some point it was removed
             if hardwareVersion >= 3:
                self._dbusservice['/MCU/Temperature'] = int(data['tma'][0] if data['tma'][0] else 0)
             else:
                self._dbusservice['/MCU/Temperature'] = int(data['tmp'])

          # carState, per official go-e API v2 docs (apikeys-de.md):
          # "null wenn interner Fehler" (Unknown/Error=0, Idle=1, Charging=2,
          # WaitCar=3, Complete=4, Error=5). IMPORTANT: car can apparently be
          # None/null itself on an internal error, not just report value 5 -
          # handled defensively below instead of letting int(None) crash.
          #
          # Venus OS /Status (official evcharger dbus spec): 0=Disconnected;
          # 1=Connected; 2=Charging; 3=Charged; 4=Waiting for sun;
          # 5=Waiting for RFID; 6=Waiting for start; 7=Low SOC;
          # 8=Ground fault; 9=Welded contacts; 10=CP Input shorted;
          # 11=Residual current detected; 12=Under voltage; 13=Overvoltage;
          # 14=Overheating; 20=Charging limit.
          #
          # go-e's 'car' alone cannot distinguish WHY charging is paused while
          # a vehicle is connected - it could be insufficient PV surplus, an
          # explicit force-off, RFID required, or a genuinely finished
          # session, all of which look identical from 'car' alone. go-e's
          # 'modelStatus' (a detailed "reason why we allow charging or not"
          # enum) is used here to pick a more specific Venus status, matching
          # the official go-e API v2 documentation:
          # https://github.com/goecharger/go-eCharger-API-v2/blob/main/apikeys-de.md
          #
          # IMPORTANT (corrected after live testing): the disambiguation must
          # be applied when car==4, NOT car==3. On this device/firmware, go-e
          # reports car==4 ("charging finished, vehicle still connected") for
          # BOTH a genuinely completed session AND for paused/force-off states
          # - confirmed live: both "gestoppt" (frc=1, modelStatus=4) and "ECO
          # pausiert" (frc=0, modelStatus=17) showed car==4, not car==3. An
          # earlier version of this mapping applied the disambiguation to
          # car==3 instead, which never matched these real-world pause states
          # in practice - car==4 always fell through to a plain "Charged",
          # which is what was seen live even while genuinely waiting for PV.
          #
          # Only modelStatus 4 (NotChargingBecauseForceStateOff) and 17
          # (NotChargingBecauseFallbackAwattar) have been directly confirmed
          # live against this fork's own frc-driven pause states; the RFID
          # mapping (2) is taken directly from go-e's own documentation but
          # not separately live-tested here since this fork does not use RFID.
          # If modelStatus indicates none of these known pause reasons, car==4
          # falls back to its plain, original meaning: Charged (3).
          #
          # car==5 (Error) was previously not handled at all - it silently
          # fell through to the default status=0 ("Disconnected"), hiding a
          # real error condition behind a misleading "not connected" display.
          # go-e's separate 'err' key (documented error reasons: FiAc=1,
          # FiDc=2, Phase=3, Overvolt=4, Overamp=5, Diode=6, PpInvalid=7,
          # GndInvalid=8, ContactorStuck=9, ContactorMiss=10, FiUnknown=11,
          # Unknown=12, Overtemp=13, NoComm=14) is used to pick the closest
          # matching Venus error status where a reasonably confident mapping
          # exists. NONE of these error mappings have been live-tested (no
          # real error condition has occurred during development) - if this
          # ever triggers, please verify in the go-e app that the displayed
          # error genuinely matches, and report back if not.
          carValue = data['car']
          if carValue is None:
            status = 0
          elif int(carValue) == 1:
            status = 0
          elif int(carValue) == 2:
            status = 2
          elif int(carValue) == 3:
            status = 6
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
              # Documented, not live-tested (this fork does not use RFID).
              status = 5
            else:
              # No known pause reason matched - genuinely finished/charged.
              status = 3
          elif int(carValue) == 5:
            errValue = int(data['err']) if 'err' in data and data['err'] is not None else None
            if errValue == 8:
              status = 8   # GndInvalid -> Ground fault
            elif errValue == 9:
              status = 9   # ContactorStuck -> Welded contacts
            elif errValue == 1:
              status = 11  # FiAc (RCD) -> Residual current detected
            elif errValue == 4:
              status = 13  # Overvolt -> Overvoltage
            elif errValue == 13:
              status = 14  # Overtemp -> Overheating
            else:
              # No confident specific mapping for this err value - still
              # correctly signals SOME error rather than "Disconnected".
              # Closest generic fit in the Venus enum without a dedicated
              # "unspecified error" code.
              status = 14
          else:
            status = 0
          self._dbusservice['/Status'] = status

          #logging
          logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
          logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
          logging.debug("Wallbox Session Energy (/Session/Energy): %s" % (self._dbusservice['/Session/Energy']))
          logging.debug("Wallbox Session Time (/Session/Time): %s" % (self._dbusservice['/Session/Time']))
          logging.debug("Charge mode: %s (lmo=%s)" % ("Auto" if self._chargeMode == 1 else "Manual", currentLmo))
          # go-e's internal rolling averages of pGrid/pPv/pAkku - logged
          # regardless of mode, so the reset-push behaviour on leaving Auto
          # (see _applyChargeMode) can be observed decaying these over time
          # instead of staying frozen, and so the Eco algorithm's likely
          # actual decision basis is visible for diagnosis.
          logging.debug("go-e rolling averages: pvopt_averagePGrid=%s pvopt_averagePPv=%s pvopt_averagePAkku=%s" %
                        (data.get('pvopt_averagePGrid'), data.get('pvopt_averagePPv'), data.get('pvopt_averagePAkku')))
          logging.debug("---")

          # increment UpdateIndex - to show that new data is available
          index = self._dbusservice['/UpdateIndex'] + 1  # increment index
          if index > 255:   # maximum value of the index
            index = 0       # overflow from 255 to 0
          self._dbusservice['/UpdateIndex'] = index

          #update lastupdate vars
          self._lastUpdate = time.time()
       else:
          # go-e unreachable this cycle (e.g. a mobile wallbox currently not
          # on a network Venus OS can reach). Reflect this in /Connected so
          # Venus OS/VRM can show the device as offline instead of silently
          # freezing on its last known values forever. Only written when it
          # actually changes, to avoid a redundant write every single failed
          # cycle while unreachable for an extended period. /Status is also
          # reset to 0 (Disconnected) for the same reason - leaving it frozen
          # on e.g. "Charging" while the box is actually gone would be
          # actively misleading.
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
      return self._setGoeChargerValueV2('amp', int(value))
    elif path == '/StartStop':
      # NOTE: writing 'alw' directly via a plain query parameter returns
      # HTTP 500 ("tried to set api key without setter") - the same error
      # pattern seen with pGrid/pPv/pAkku early on. 'alw' therefore appears to
      # need the ids={} form, but doing so gets silently reverted by the go-e's
      # own Eco algorithm within moments (the very problem 'frc' was
      # introduced to solve). Live testing showed that 'frc' alone - without
      # ever successfully writing 'alw' - was sufficient for charging to
      # actually start (with an observed ~30s delay, matching the go-e's
      # general PV-surplus-mode startup timing measured elsewhere - this may
      # be an inherent ramp-up/handshake delay in the charger itself, not
      # something dependent on 'alw'). 'alw' is therefore no longer written
      # here at all.
      enable = bool(int(value))
      return self._setFrc(0 if enable else 1)
    elif path == '/AutoStart':
      # DELIBERATELY REPURPOSED (see paths dict comment in main() and
      # README): this is NOT Victron's official "start automatically when a
      # vehicle is connected" semantic. What it actually does depends on
      # AutoStartMode from config.ini (read once at startup, see __init__):
      # 0 = disabled, no function at all; 1 = "1P-Auto"; 2 = "3P-Auto";
      # 3 = "1P-3P". See __init__ for the full mapping.
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
                                RotatingFileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__))), maxBytes=10000),
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
