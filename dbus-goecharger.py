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
    # Initialize state variables FIRST, since _handlechangedvalue (registered as
    # onchangecallback) accesses them and could in theory already be called
    # during setup.
    self._lastUpdate = 0
    self._chargingTime = 0.0
    self._chargeMode = 0            # 0=Manual, 1=Auto, 2=Scheduled
    self._lastCommandedLmo = None
    self._batteryPriorityPaused = False
    self._batterySupportActive = False
    # last alw value we ourselves commanded via battery priority (None = never set).
    # Prevents alw from being rewritten unnecessarily on every cycle (every 5s).
    self._lastAlwCommanded = None
    self._settingsPaths = {}

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

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # Additionally publish the tuning values as writable D-Bus paths, so they
    # can be adjusted via the VRM portal (Device List -> device -> Advanced)
    # without editing config.ini. Initial values come from config.ini. Changes
    # made via VRM take effect immediately but are NOT written back to
    # config.ini - after a restart, the config.ini values apply again.
    if self._chargeControlEnabled:
      settingsDefaults = {
        '/Settings/PvGridTarget': int(config['DEFAULT'].get('PvGridTarget', 0)),
        '/Settings/BatteryPriorityMinSoc': int(config['DEFAULT'].get('BatteryPriorityMinSoc', 0)),
        '/Settings/BatteryPriorityHysteresis': int(config['DEFAULT'].get('BatteryPriorityHysteresis', 2)),
        '/Settings/BatterySupportMinSoc': int(config['DEFAULT'].get('BatterySupportMinSoc', 0)),
        '/Settings/BatterySupportPower': int(config['DEFAULT'].get('BatterySupportPower', 0)),
        '/Settings/BatterySupportHysteresis': int(config['DEFAULT'].get('BatterySupportHysteresis', 2)),
      }
      for path, initial in settingsDefaults.items():
        self._dbusservice.add_path(path, initial, writeable=True, onchangecallback=self._handlechangedvalue)
        self._settingsPaths[path] = initial

    # register the service (only after ALL paths have been added)
    self._dbusservice.register()

    if self._chargeControlEnabled:
      logging.info("EnableChargeControl=true: Auto/Manual/Scheduled control is active")
      logging.info("Tuning values adjustable via VRM under /Settings/*: %s" % list(self._settingsPaths.keys()))
    else:
      logging.info("EnableChargeControl=false (or not set): monitoring only, no mode control")

    # Private, separate connection to the system D-Bus, to read PV/grid/battery
    # values from com.victronenergy.system (for Auto mode / PV surplus push).
    # Kept separate from self._dbusservice, since this connection only reads
    # and does not register a service.
    systemBus = dbus.SystemBus()
    try:
      self._gridPowerItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/Grid/L1/Power')
      self._pvPowerAcItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Ac/PvOnGrid/L1/Power')
      self._pvPowerDcItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Dc/Pv/Power')
      self._batteryPowerItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Dc/Battery/Power')
      self._batterySocItem = VeDbusItemImport(systemBus, 'com.victronenergy.system', '/Dc/Battery/Soc')
    except Exception as e:
      logging.critical('Error at %s', 'reading Venus system dbus items for Auto mode', exc_info=e)
      self._gridPowerItem = None
      self._pvPowerAcItem = None
      self._pvPowerDcItem = None
      self._batteryPowerItem = None
      self._batterySocItem = None

    # add _update function 'timer'
    gobject.timeout_add(pauseBetweenRequests, self._update)

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)

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
    Sets a go-e API v2 key (lmo, fup, pgt, alw, amp, ama, psm, sch_week, ...) via
    the /api/set?ids= endpoint. This is the single, unified way to write any
    value - the older /mqtt?payload= endpoint was removed: in practice, 'alw'
    repeatedly and persistently failed over it (error status/no response),
    while 'amp' worked fine over the very same endpoint. The exact cause was
    never conclusively determined. /api/set has worked reliably for every key
    tested so far and is therefore used consistently throughout.
    '''
    config = self._getConfig()
    payload = json.dumps({parameter: value})
    baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
    try:
      request_data = requests.get(url=baseURL, params={'ids': payload}, timeout=2)
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
    Reads a tuning value. The (possibly VRM-modified) D-Bus value under
    /Settings/<name> is preferred; if the path does not exist (e.g. when
    EnableChargeControl=false), it falls back to config.ini.
    '''
    path = '/Settings/%s' % name
    if path in self._settingsPaths:
      try:
        value = self._dbusservice[path]
        if value is not None:
          return int(value)
      except Exception:
        pass
    config = self._getConfig()
    return int(config['DEFAULT'].get(name, default))


  def _setGoeSchedulerEnabled(self, enabled):
    '''
    Enables/disables the go-e's own weekly schedule (sch_week/sch_satur/sch_sund)
    without changing the time windows (ranges) configured there in the app. To do
    this, the current object is read, only 'control' is changed, and the whole
    object is written back - the times remain exactly as set in the go-e app.
    control: Disabled=0, Inside=1, Outside=2 (we only use 0/1)
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
        payload = json.dumps({key: schedObj})
        baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
        requests.get(url=baseURL, params={'ids': payload}, timeout=1)
      except Exception as e:
        logging.critical('Error at %s', '_setGoeSchedulerEnabled(%s)' % key, exc_info=e)


  def _applyChargeMode(self):
    '''
    Sends the selected charge mode to the go-e:
    Auto (1)      -> lmo=4 (Eco mode), fup=true, scheduler off, grid target (pgt)
                     (go-e takes over PV surplus control, including phase
                     switching, by itself)
    Scheduled (2) -> lmo=3, scheduler on (control=1, times remain as defined in the go-e app)
    Manual (0)    -> lmo=3, scheduler off (direct control via SetCurrent/StartStop)
    '''
    try:
      # When leaving Auto mode, reset the battery-priority state and, if
      # charging had been locked because of it, release it again - otherwise
      # alw=false would remain in effect and the user would not be able to
      # charge in Manual mode.
      if self._chargeMode != 1:
        if self._lastAlwCommanded == False:
          try:
            self._setGoeChargerValueV2('alw', True)
            logging.info("Battery priority lifted (mode switch) - charging released again")
          except Exception as e:
            logging.warning("Could not set alw=true on mode switch: %s" % e)
        self._batteryPriorityPaused = False
        self._batterySupportActive = False
        self._lastAlwCommanded = None

      if self._chargeMode == 1:
        ok = self._setGoeChargerValueV2('lmo', 4)
        self._setGoeChargerValueV2('fup', True)
        self._setGoeSchedulerEnabled(False)
        gridTarget = self._getSetting('PvGridTarget', 0)
        self._setGoeChargerValueV2('pgt', gridTarget)
        logging.info("Grid target (pgt) set: %s W" % gridTarget)
        if ok:
          self._lastCommandedLmo = 4
        else:
          logging.warning("Could not set lmo=4 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Auto (PV surplus enabled)")
      elif self._chargeMode == 2:
        ok = self._setGoeChargerValueV2('lmo', 3)
        self._setGoeChargerValueV2('fup', False)
        self._setGoeSchedulerEnabled(True)
        if ok:
          self._lastCommandedLmo = 3
        else:
          logging.warning("Could not set lmo=3 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Scheduled (go-e's own schedule enabled)")
      else:
        ok = self._setGoeChargerValueV2('lmo', 3)
        self._setGoeChargerValueV2('fup', False)
        self._setGoeSchedulerEnabled(False)
        if ok:
          self._lastCommandedLmo = 3
        else:
          logging.warning("Could not set lmo=3 - _lastCommandedLmo left unchanged, will retry next cycle")
        logging.info("Charge mode: Manual")
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
    if self._gridPowerItem is None:
      return

    try:
      config = self._getConfig()

      # Battery priority: only release EV charging once the battery SOC has
      # reached a configured minimum threshold (similar to the evcc setting).
      # Hysteresis (default 2%) prevents frequent on/off flapping right at the
      # threshold: charging pauses below minSoc, and is only released again once
      # SOC >= minSoc + hysteresis.
      minBatterySoc = self._getSetting('BatteryPriorityMinSoc', 0)
      if minBatterySoc > 0 and self._batterySocItem is not None:
        hysteresis = self._getSetting('BatteryPriorityHysteresis', 2)
        batterySoc = self._batterySocItem.get_value()

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
            if not self._lastAlwCommanded == False:
              ok = self._setGoeChargerValueV2('alw', False)
              if ok:
                self._lastAlwCommanded = False
              else:
                logging.warning("Could not set alw=false (battery priority) - will retry next cycle")
            logging.debug("Auto mode: battery SOC %s%% < %s%% - EV charging paused (battery priority)" %
                          (batterySoc, minBatterySoc))
            return
          else:
            if not self._lastAlwCommanded == True:
              ok = self._setGoeChargerValueV2('alw', True)
              if ok:
                self._lastAlwCommanded = True
              else:
                logging.warning("Could not set alw=true - will retry next cycle")

      gridPower = self._gridPowerItem.get_value()
      pvPowerAc = self._pvPowerAcItem.get_value()
      pvPowerDc = self._pvPowerDcItem.get_value()
      batteryPower = self._batteryPowerItem.get_value()

      if gridPower is None or batteryPower is None:
        logging.warning("Auto mode: /Ac/Grid/L1/Power or /Dc/Battery/Power not available - PV push skipped this cycle")
        return

      pvPower = 0
      if pvPowerAc is not None:
        pvPower += pvPowerAc
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
        batterySocForSupport = self._batterySocItem.get_value()

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
            pPv += supportPower
            logging.debug("Battery buffer active: SOC=%s%% (threshold %s%%, hysteresis %s%%) -> +%sW virtual surplus" %
                          (batterySocForSupport, maxSocForSupport, supportHysteresis, supportPower))

      payload = json.dumps({"pGrid": pGrid, "pPv": pPv, "pAkku": pAkku})
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
       #get data from go-eCharger (incl. 'lmo' to detect external mode changes)
       baseFilter = 'nrg,eto,wh,alw,amp,ama,car,tmp,tma'
       filter = baseFilter + ',lmo' if self._chargeControlEnabled else baseFilter
       data = self._getGoeChargerData(filter)

       if data is not None:

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
          if int(data['car']) == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif int(data['car']) == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)
          # /Session/Time - this path is read by the Venus OS GUI/VRM for the
          # "Session" display; /ChargingTime is marked deprecated in the official docs.
          self._dbusservice['/Session/Time'] = int(self._chargingTime)

          # The entire following block (mode sync, PV surplus push) only runs if
          # the new control logic has been explicitly enabled via config.ini.
          if self._chargeControlEnabled:
            # Detect an external change of charge mode (e.g. the user switched
            # to Eco/Normal directly in the go-e app, not via Venus OS). Only
            # react if lmo has changed WITHOUT us having set it ourselves last.
            currentLmo = int(data['lmo']) if 'lmo' in data and data['lmo'] is not None else None
            if currentLmo is not None and currentLmo != self._lastCommandedLmo:
              newChargeMode = 1 if currentLmo == 4 else 0
              if newChargeMode != self._chargeMode:
                logging.info("External change detected: go-e lmo=%s -> charge mode set to %s" %
                              (currentLmo, "Auto" if newChargeMode == 1 else "Manual"))
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

          # carState, null if internal error (Unknown/Error=0, Idle=1, Charging=2, WaitCar=3, Complete=4, Error=5)
          # status 0=Disconnected; 1=Connected; 2=Charging; 3=Charged; 4=Waiting for sun; 5=Waiting for RFID; 6=Waiting for start; 7=Low SOC; 8=Ground fault; 9=Welded contacts; 10=CP Input shorted; 11=Residual current detected; 12=Under voltage detected; 13=Overvoltage detected; 14=Overheating detected
          status = 0
          if int(data['car']) == 1:
            status = 0
          elif int(data['car']) == 2:
            status = 2
          elif int(data['car']) == 3:
            status = 6
          elif int(data['car']) == 4:
            status = 3
          self._dbusservice['/Status'] = status

          #logging
          logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
          logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
          logging.debug("Wallbox Session Energy (/Session/Energy): %s" % (self._dbusservice['/Session/Energy']))
          logging.debug("Wallbox Session Time (/Session/Time): %s" % (self._dbusservice['/Session/Time']))
          logging.debug("Charge mode: %s (lmo=%s)" % ("Auto" if self._chargeMode == 1 else "Manual", currentLmo))
          logging.debug("---")

          # increment UpdateIndex - to show that new data is available
          index = self._dbusservice['/UpdateIndex'] + 1  # increment index
          if index > 255:   # maximum value of the index
            index = 0       # overflow from 255 to 0
          self._dbusservice['/UpdateIndex'] = index

          #update lastupdate vars
          self._lastUpdate = time.time()
       else:
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
      return self._setGoeChargerValueV2('alw', bool(int(value)))
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
    elif path in self._settingsPaths:
      # Tuning value changed via VRM - will automatically be picked up by
      # _getSetting() from the next cycle onward. Not written back to
      # config.ini, so after a restart of the service the config.ini value
      # applies again.
      logging.info("Setting %s changed via VRM to %s (not persistent, config.ini remains unchanged)" % (path, value))
      if path == '/Settings/PvGridTarget' and self._chargeMode == 1:
        # pgt only takes effect once actively sent to the go-e - so set it immediately
        self._setGoeChargerValueV2('pgt', int(value))
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
          '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))}
        }
        )

      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
