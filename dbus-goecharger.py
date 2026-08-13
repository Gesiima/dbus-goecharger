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
    # Zustandsvariablen ZUERST initialisieren, da _handlechangedvalue (als
    # onchangecallback registriert) darauf zugreift und theoretisch schon waehrend
    # des Setups aufgerufen werden koennte.
    self._lastUpdate = 0
    self._chargingTime = 0.0
    self._chargeMode = 0            # 0=Manual, 1=Auto, 2=Scheduled
    self._lastCommandedLmo = None
    self._batteryPriorityPaused = False
    self._batterySupportActive = False
    # zuletzt durch die Akku-Prioritaet gesetzter alw-Wert (None = noch nie gesetzt).
    # Verhindert, dass alw bei jedem Zyklus (alle 5s) unnoetig neu geschrieben wird.
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
      logging.warning("PauseBetweenRequests > 5000ms: go-e verlangt fuer PV-Ueberschuss (pGrid/pPv/pAkku) ein Update alle 5 Sekunden. Im Auto-Modus kann das Laden sonst unerwuenscht pausieren/anhalten.")

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

    # Feature-Flag: gesamte neue Regellogik (Mode-Umschaltung, PV-Push, Scheduler-Steuerung,
    # Netz-Zielwert, Akku-Prioritaet, Akku-Ladepuffer) komplett ein/ausschaltbar per
    # config.ini. Default: aus, damit bestehende Installationen nach einem Update dieses
    # Skripts ihr bisheriges, reines Monitoring-Verhalten unveraendert beibehalten.
    self._chargeControlEnabled = config.getboolean('DEFAULT', 'EnableChargeControl', fallback=False)
    if self._chargeControlEnabled:
      # /Mode nur bei aktivierter Regellogik wirklich schreibbar machen
      self._dbusservice.add_path('/Mode', 0, writeable=True, onchangecallback=self._handlechangedvalue)
    else:
      # sonst wie im Original: reiner Anzeigewert, nicht schreibbar
      self._dbusservice.add_path('/Mode', 0)

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # Einstellwerte zusaetzlich als schreibbare D-Bus-Pfade veroeffentlichen, damit sie
    # ueber das VRM-Portal (Device List -> Gerät -> Advanced) angepasst werden koennen,
    # ohne die config.ini zu editieren. Initialwerte kommen aus der config.ini.
    # Aenderungen ueber VRM wirken zur Laufzeit, werden aber NICHT in die config.ini
    # zurueckgeschrieben - nach einem Neustart gelten wieder die config.ini-Werte.
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

    # register the service (erst nachdem ALLE Pfade angelegt wurden)
    self._dbusservice.register()

    if self._chargeControlEnabled:
      logging.info("EnableChargeControl=true: Auto/Manual/Scheduled-Steuerung aktiv")
      logging.info("Einstellwerte via VRM aenderbar unter /Settings/* : %s" % list(self._settingsPaths.keys()))
    else:
      logging.info("EnableChargeControl=false (oder nicht gesetzt): nur Monitoring, keine Modi-Steuerung")

    # Private, separate Verbindung zum System-D-Bus, um PV/Netz/Batterie-Werte aus
    # com.victronenergy.system zu lesen (fuer den Auto-Modus / PV-Ueberschuss-Push).
    # Getrennt von self._dbusservice, da diese Verbindung nur liest, nicht registriert.
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

  def _getGoeChargerMqttPayloadUrl(self, parameter, value):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise':
        URL = "http://%s/mqtt?payload=%s=%s" % (config['ONPREMISE']['Host'], parameter, value)
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

    return URL

  def _setGoeChargerValue(self, parameter, value):
    URL = self._getGoeChargerMqttPayloadUrl(parameter, str(value))
    request_data = requests.get(url = URL)

    # check for response
    if not request_data:
      raise ConnectionError("No response from go-eCharger - %s" % (URL))

    json_data = request_data.json()

    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")

    if json_data[parameter] == str(value):
      return True
    else:
      logging.warning("go-eCharger parameter %s not set to %s" % (parameter, str(value)))
      return False


  def _setGoeChargerValueV2(self, parameter, value):
    '''
    Setzt einen API-v2-Schluessel (z.B. lmo, fup, pgt, psm, sch_week) ueber den
    /api/set?ids=-Endpunkt. WICHTIG: Der aeltere /mqtt?payload=-Endpunkt (siehe
    _setGoeChargerValue) akzeptiert nur die alten API-v1-Schluessel (amp/alw/ama) -
    bei neueren Schluesseln liefert er "unknown payload key" statt einer Fehlermeldung
    oder gueltigem JSON, was zu einem unbehandelten Absturz fuehren wuerde.
    '''
    config = self._getConfig()
    payload = json.dumps({parameter: value})
    baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
    try:
      request_data = requests.get(url=baseURL, params={'ids': payload}, timeout=2)
    except Exception as e:
      logging.warning("go-eCharger v2 set fehlgeschlagen fuer %s=%s: %s" % (parameter, value, e))
      return False

    if not request_data:
      logging.warning("go-eCharger v2 set: keine Antwort fuer %s=%s" % (parameter, value))
      return False

    try:
      json_data = request_data.json()
    except Exception:
      logging.warning("go-eCharger v2 set: ungueltige JSON-Antwort fuer %s=%s: %s" %
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
    Liest einen Einstellwert. Bevorzugt wird der (ggf. ueber VRM geaenderte) D-Bus-Wert
    unter /Settings/<name>; existiert der Pfad nicht (z.B. EnableChargeControl=false),
    wird auf die config.ini zurueckgegriffen.
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
    Aktiviert/deaktiviert den go-e eigenen Wochenzeitplan (sch_week/sch_satur/sch_sund),
    ohne die dort in der App konfigurierten Zeitfenster (ranges) zu veraendern.
    Dazu wird das aktuelle Objekt gelesen, nur 'control' geaendert und komplett
    zurueckgeschrieben - die Zeiten bleiben exakt so, wie sie in der go-e-App
    hinterlegt wurden.
    control: Disabled=0, Inside=1, Outside=2 (wir nutzen nur 0/1)
    '''
    control = 1 if enabled else 0
    config = self._getConfig()
    for key in ('sch_week', 'sch_satur', 'sch_sund'):
      try:
        current = self._getGoeChargerData(key)
        if current is None or key not in current or current[key] is None:
          logging.warning("Scheduler: %s nicht verfuegbar, wird ausgelassen" % key)
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
    Sendet den gewaehlten Lademodus an den go-e:
    Auto (1)      -> lmo=4 (Eco-Modus), fup=true, Scheduler aus, Netz-Zielwert (pgt)
                     (go-e uebernimmt PV-Ueberschussregelung inkl. Phasenumschaltung selbst)
    Scheduled (2) -> lmo=3, Scheduler an (control=1, Zeiten bleiben wie in der go-e-App definiert)
    Manual (0)    -> lmo=3, Scheduler aus (direkte Steuerung ueber SetCurrent/StartStop)
    '''
    try:
      # Beim Verlassen des Auto-Modus die Akku-Prioritaets-Zustaende zuruecksetzen und,
      # falls das Laden dadurch gesperrt war, wieder freigeben - sonst bliebe alw=false
      # stehen und der Nutzer koennte im Manual-Modus nicht laden.
      if self._chargeMode != 1:
        if self._lastAlwCommanded == False:
          self._setGoeChargerValue('alw', 'true')
          logging.info("Akku-Prioritaet aufgehoben (Moduswechsel) - Laden wieder freigegeben")
        self._batteryPriorityPaused = False
        self._batterySupportActive = False
        self._lastAlwCommanded = None

      if self._chargeMode == 1:
        self._setGoeChargerValueV2('lmo', 4)
        self._setGoeChargerValueV2('fup', True)
        self._setGoeSchedulerEnabled(False)
        gridTarget = self._getSetting('PvGridTarget', 0)
        self._setGoeChargerValueV2('pgt', gridTarget)
        logging.info("Netz-Zielwert (pgt) gesetzt: %s W" % gridTarget)
        self._lastCommandedLmo = 4
        logging.info("Charge mode: Auto (PV-Ueberschuss aktiviert)")
      elif self._chargeMode == 2:
        self._setGoeChargerValueV2('lmo', 3)
        self._setGoeChargerValueV2('fup', False)
        self._setGoeSchedulerEnabled(True)
        self._lastCommandedLmo = 3
        logging.info("Charge mode: Scheduled (go-e eigener Zeitplan aktiviert)")
      else:
        self._setGoeChargerValueV2('lmo', 3)
        self._setGoeChargerValueV2('fup', False)
        self._setGoeSchedulerEnabled(False)
        self._lastCommandedLmo = 3
        logging.info("Charge mode: Manual")
    except Exception as e:
      logging.critical('Error at %s', '_applyChargeMode', exc_info=e)


  def _pushPvSurplusValues(self):
    '''
    Liest PV-/Netz-/Batterieleistung aus Venus OS (com.victronenergy.system) und
    schickt sie im go-e-eigenen Format weiter. Muss laut go-e-API alle 5 Sekunden
    aufgerufen werden, sonst bleibt der letzte Wert stehen (siehe Warnung in __init__).

    WICHTIG - Vorzeichenkonvention noch nicht live verifiziert:
    - pGrid: >0 = Netzbezug, <0 = Einspeisung (Venus-Konvention entspricht direkt der go-e-Konvention)
    - pPv:   >0 = Produktion
    - pAkku: go-e will <0 = Batterie laedt. Venus meldet ueblicherweise >0 = laedt,
             daher hier invertiert. Bitte nach dem ersten Testlauf per
             "dbus -y com.victronenergy.system / GetValue" gegen die Realitaet
             pruefen (laedt die Batterie tatsaechlich, wenn /Dc/Battery/Power positiv ist?)
             und die Vorzeichen unten anpassen, falls es nicht passt.
    '''
    if self._gridPowerItem is None:
      return

    try:
      config = self._getConfig()

      # Akku-Prioritaet: EV-Laden erst freigeben, wenn Batterie-SOC eine
      # konfigurierte Mindestschwelle erreicht hat (analog zur evcc-Einstellung).
      # Hysterese (Default 2%) verhindert haeufiges An/Aus-Flackern genau an der Schwelle:
      # Laden pausiert bei SOC < minSoc, wird erst bei SOC >= minSoc + Hysterese wieder freigegeben.
      minBatterySoc = self._getSetting('BatteryPriorityMinSoc', 0)
      if minBatterySoc > 0 and self._batterySocItem is not None:
        hysteresis = self._getSetting('BatteryPriorityHysteresis', 2)
        batterySoc = self._batterySocItem.get_value()

        if batterySoc is None:
          logging.warning("Auto mode: /Dc/Battery/Soc nicht verfuegbar - Akku-Prioritaet wird ignoriert")
        else:
          if self._batteryPriorityPaused:
            if batterySoc >= minBatterySoc + hysteresis:
              self._batteryPriorityPaused = False
          else:
            if batterySoc < minBatterySoc:
              self._batteryPriorityPaused = True

          if self._batteryPriorityPaused:
            if not self._lastAlwCommanded == False:
              self._setGoeChargerValue('alw', 'false')
              self._lastAlwCommanded = False
            logging.debug("Auto mode: Batterie-SOC %s%% < %s%% - EV-Laden pausiert (Akku-Prioritaet)" %
                          (batterySoc, minBatterySoc))
            return
          else:
            if not self._lastAlwCommanded == True:
              self._setGoeChargerValue('alw', 'true')
              self._lastAlwCommanded = True

      gridPower = self._gridPowerItem.get_value()
      pvPowerAc = self._pvPowerAcItem.get_value()
      pvPowerDc = self._pvPowerDcItem.get_value()
      batteryPower = self._batteryPowerItem.get_value()

      if gridPower is None or batteryPower is None:
        logging.warning("Auto mode: /Ac/Grid/L1/Power oder /Dc/Battery/Power nicht verfuegbar - PV-Push in diesem Zyklus ausgelassen")
        return

      pvPower = 0
      if pvPowerAc is not None:
        pvPower += pvPowerAc
      if pvPowerDc is not None:
        pvPower += pvPowerDc

      pGrid = gridPower
      pPv = pvPower
      pAkku = -1 * batteryPower  # siehe Docstring - Vorzeichen ggf. anpassen

      # Batterie als Ladepuffer: oberhalb einer SOC-Schwelle darf ein fester
      # zusaetzlicher Betrag "virtueller Ueberschuss" aus der Batterie ins Auto
      # fliessen (analog zur evcc-Funktion "Batterie als Ladepuffer" oberhalb X%).
      # Die Batterie liefert diese Energie automatisch, sobald der go-e dadurch mehr
      # Strom anfordert als die reine PV-Produktion deckt - Venus OS/ESS gleicht das
      # physikalisch selbststaendig aus.
      maxSocForSupport = self._getSetting('BatterySupportMinSoc', 0)
      supportPower = self._getSetting('BatterySupportPower', 0)
      if maxSocForSupport > 0 and supportPower > 0 and self._batterySocItem is not None:
        supportHysteresis = self._getSetting('BatterySupportHysteresis', 2)
        batterySocForSupport = self._batterySocItem.get_value()

        if batterySocForSupport is None:
          logging.warning("Auto mode: /Dc/Battery/Soc nicht verfuegbar - Batterie-Ladepuffer wird ignoriert")
        else:
          if self._batterySupportActive:
            if batterySocForSupport < maxSocForSupport - supportHysteresis:
              self._batterySupportActive = False
          else:
            if batterySocForSupport >= maxSocForSupport:
              self._batterySupportActive = True

          if self._batterySupportActive:
            pPv += supportPower
            logging.debug("Batterie-Ladepuffer aktiv: SOC=%s%% (Schwelle %s%%, Hysterese %s%%) -> +%sW virtueller Ueberschuss" %
                          (batterySocForSupport, maxSocForSupport, supportHysteresis, supportPower))

      payload = json.dumps({"pGrid": pGrid, "pPv": pPv, "pAkku": pAkku})
      baseURL = "http://%s/api/set" % config['ONPREMISE']['Host']
      requests.get(url=baseURL, params={'ids': payload}, timeout=1)

      logging.debug("Auto mode PV-Push: pGrid=%s pPv=%s pAkku=%s" % (pGrid, pPv, pAkku))
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
       #get data from go-eCharger (inkl. 'lmo' zum Erkennen externer Modus-Aenderungen)
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

          # /Ac/Energy/Forward = Lifetime-Gesamtenergie (kWh); eto (API v2) ist in Wh
          self._dbusservice['/Ac/Energy/Forward'] = round(float(data['eto']) / 1000.0, 2)
          # /Session/Energy = Energie der aktuellen Ladesession (kWh); wh ist in Wh
          if 'wh' in data and data['wh'] is not None:
            self._dbusservice['/Session/Energy'] = round(data['wh'] / 1000, 2)
          else:
            self._dbusservice['/Session/Energy'] = 0

          self._dbusservice['/StartStop'] = int(data['alw'])
          self._dbusservice['/SetCurrent'] = int(data['amp'])
          self._dbusservice['/MaxCurrent'] = int(data['ama'])

          # update chargingTime, increment charge time only on active charging (2), reset when no car connected (1)
          timeDelta = time.time() - self._lastUpdate
          if int(data['car']) == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif int(data['car']) == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)
          self._dbusservice['/Session/Time'] = int(self._chargingTime)

          # Der gesamte folgende Block (Mode-Sync, PV-Ueberschuss-Push) laeuft nur,
          # wenn die neue Regellogik per config.ini explizit aktiviert wurde.
          if self._chargeControlEnabled:
            # Externe Aenderung des Lademodus erkennen (z.B. Nutzer hat direkt in der
            # go-e-App auf Eco/Normal umgeschaltet, nicht ueber Venus OS). Nur reagieren,
            # wenn sich lmo geaendert hat, OHNE dass wir es selbst zuletzt gesetzt haben.
            currentLmo = int(data['lmo']) if 'lmo' in data and data['lmo'] is not None else None
            if currentLmo is not None and currentLmo != self._lastCommandedLmo:
              newChargeMode = 1 if currentLmo == 4 else 0
              if newChargeMode != self._chargeMode:
                logging.info("Externe Aenderung erkannt: go-e lmo=%s -> Charge mode wird auf %s gesetzt" %
                              (currentLmo, "Auto" if newChargeMode == 1 else "Manual"))
                self._chargeMode = newChargeMode
                self._dbusservice['/Mode'] = self._chargeMode
              self._lastCommandedLmo = currentLmo

            # Im Auto-Modus die PV-Ueberschusswerte an den go-e weiterreichen
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
      return self._setGoeChargerValue('amp', value)
    elif path == '/StartStop':
      return self._setGoeChargerValue('alw', value)
    elif path == '/MaxCurrent':
      return self._setGoeChargerValue('ama', value)
    elif path == '/Mode':
      if not self._chargeControlEnabled:
        logging.warning("EnableChargeControl ist deaktiviert - /Mode-Aenderung wird ignoriert")
        return False
      if int(value) not in (0, 1, 2):
        logging.warning("Charge mode %s nicht unterstuetzt (0=Manual, 1=Auto, 2=Scheduled)" % value)
        return False
      self._chargeMode = int(value)
      self._applyChargeMode()
      return True
    elif path in self._settingsPaths:
      # Einstellwert via VRM geaendert - wird ab dem naechsten Zyklus automatisch
      # ueber _getSetting() beruecksichtigt. Kein Zurueckschreiben in die config.ini,
      # daher gilt nach einem Neustart des Dienstes wieder der config.ini-Wert.
      logging.info("Einstellwert %s via VRM geaendert auf %s (nicht persistent, config.ini bleibt unveraendert)" % (path, value))
      if path == '/Settings/PvGridTarget' and self._chargeMode == 1:
        # pgt wirkt nur, wenn es aktiv an den go-e gesendet wird - daher sofort neu setzen
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
