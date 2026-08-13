# dbus-goecharger (Fork mit erweiterter Ladesteuerung)

Integrate go-eCharger into Victron Energies Venus OS.

Dies ist ein Fork von [vikt0rm/dbus-goecharger](https://github.com/vikt0rm/dbus-goecharger),
der zusaetzlich eine optionale, aktive Ladesteuerung (Auto/Manual/Scheduled-Modus mit
PV-Ueberschuss) direkt aus der Venus-OS-GUI ermoeglicht.

## Purpose

With the scripts in this repo it should be easy possible to install, uninstall, restart
a service that connects the go-eCharger to the VenusOS and GX devices from Victron.
Idea is inspired on @fabian-lauer and @trixing project linked below, many thanks for
sharing the knowledge:

- https://github.com/fabian-lauer/dbus-shelly-3em-smartmeter
- https://github.com/trixing/venus.dbus-twc3

## Was dieser Fork zusaetzlich bietet

### Fixes gegenueber dem Original

- **Lifetime-Energie korrigiert:** `/Ac/Energy/Forward` nutzte im Original bei
  `HardwareVersion >= 4` faelschlich `wh` (Session-Energie) statt `eto` (Lifetime-Energie).
  Beide Werte wurden dadurch vertauscht/falsch angezeigt.
- **Session-Energie/-Zeit ergaenzt:** `/Session/Energy` und `/Session/Time` fehlten
  komplett. Die Venus-OS-GUI/VRM liest fuer die "Session"-Anzeige diese Pfade -
  `/ChargingTime` allein (das einzige, was das Original setzt) ist laut offizieller
  Victron-Doku als deprecated markiert und wird von der GUI nicht mehr fuer die
  Session-Anzeige verwendet.

### Neue, optionale Funktionen (per `EnableChargeControl = true` in `config.ini`)

Alles Folgende ist **standardmaessig deaktiviert**. Ohne diese Einstellung verhaelt
sich das Skript exakt wie das Original - reines Monitoring, `/Mode` bleibt read-only.

- **Echter Auto/Manual/Scheduled-Lademodus** direkt aus der Venus-OS-"Charge mode"-Seite
  steuerbar (im Original ist die Auswahl wirkungslos, siehe Restrictions unten)
- **PV-Ueberschuss-Push:** liest PV-/Netz-/Batterieleistung aus Venus OS
  (`com.victronenergy.system`) und uebergibt sie an den go-e-eigenen Eco-Modus
  (`pGrid`/`pPv`/`pAkku`), inkl. automatischer Phasenumschaltung durch die
  go-e-Firmware selbst
- **Scheduled-Modus:** aktiviert/deaktiviert nur den go-e-eigenen Wochenzeitplan
  (`sch_week`/`sch_satur`/`sch_sund`) - die Zeitfenster selbst werden weiterhin in
  der go-e-App verwaltet, nicht von diesem Skript
- **Netz-Zielwert (`pgt`):** fuer eine Batterie-Reserve beim PV-Ueberschussladen
- **Akku-Prioritaet:** EV laedt erst, wenn der Hausspeicher einen konfigurierbaren
  Mindest-SOC erreicht hat (mit Hysterese gegen Flackern)
- **Akku als Ladepuffer:** oberhalb eines konfigurierbaren SOC wird zusaetzliche
  Batterie-Leistung als virtueller Ueberschuss gemeldet (mit Hysterese)
- **Sync bei externer Aenderung:** wird der Modus direkt in der go-e-App geaendert,
  zieht Venus OS `/Mode` automatisch nach

Siehe `config.ini.example` fuer alle Optionen mit Erklaerung.

## Empirisch getestetes go-e-Regelverhalten (nicht offiziell dokumentiert)

Die folgenden Erkenntnisse stammen aus systematischen Tests gegen einen realen
go-e Charger (Firmware 60.5, HW V4) und sind in der offiziellen go-e-API-Doku
**nicht** oder nur unvollstaendig beschrieben. Das Verhalten kann sich mit
anderen Firmware-Versionen unterscheiden.

### API-Endpunkt-Besonderheiten

- `lmo`, `fup`, `pgt` lassen sich per **direktem Query-Parameter** setzen
  (`GET /api/set?lmo=4`) - funktioniert zuverlaessig.
- `pGrid`, `pPv`, `pAkku` und die Scheduler-Objekte (`sch_week` etc.) muessen dagegen
  ueber `ids={"key":value}` gesetzt werden. Wichtig: Der `ids`-Parameter muss korrekt
  URL-encodiert werden (z.B. via `requests`' `params=`-Mechanismus oder `curl
  --data-urlencode`) - ein manuell zusammengebauter, nicht codierter Query-String
  fuehrt zu `"value must be null or JsonObject"`-Fehlern.
- Der aeltere `/mqtt?payload=key=value`-Endpunkt (aus dem Original-Skript fuer
  `amp`/`alw`/`ama` genutzt) kennt neuere API-v2-Schluessel wie `lmo` nicht
  (`"unknown payload key"`).

### Timing-Verhalten (PV-Ueberschuss-Push)

Gemessen mit kontinuierlichem Senden von `pGrid`/`pPv`/`pAkku` alle 5 Sekunden:

| Ereignis | Gemessene Reaktionszeit |
|---|---|
| Ladestart bei gemeldetem Ueberschuss (aus stabilem Ruhezustand) | ~30-35 Sekunden |
| Kurze Unterbrechung des Ueberschusses (bis mind. 30s) | keine Reaktion - wird toleriert |
| Ladestopp bei dauerhaft fehlendem Ueberschuss | ~2 Minuten (120-125s) |
| **Watchdog:** Ausbleiben neuer Werte fuehrt zum Stopp nach | ~6 Sekunden, danach fallen `pgrid`/`ppv`/`pakku` auf `null` zurueck |

**Wichtige Randbeobachtung:** Nach einer Watchdog-Luecke (>6s ohne neue Werte) fuehrt
der *naechste* eintreffende Wert - unabhaengig von dessen Vorzeichen - zu einem
sofortigen Session-Neustart (`car` wechselt binnen 1-2s). Der eigentliche
Ueberschuss-Check (Start nach ~30s / Stopp nach ~2min) greift erst in den Zyklen
danach. Bei laufendem, luecklosem Senden (wie es dieses Skript alle 5s tut) tritt
dieser Effekt im Normalbetrieb nicht auf - nur nach einem Neustart des Skripts/Diensts.

**Konsequenz fuer `PauseBetweenRequests`:** Das Intervall sollte spuerbar unter der
6-Sekunden-Watchdog-Grenze liegen (empfohlen: 5000ms, wie im Original), damit ein
einzelner verzoegerter HTTP-Request nicht bereits zum ungewollten Pausieren fuehrt.

### Netz-Zielwert (`pgt`) wirkt kontinuierlich

`pgt` ist ein persistenter Config-Wert und fliesst laufend in die
Ampere-Berechnung des Eco-Modus ein - nicht nur beim Setzen. Beispiel: Bei
`pGrid=-1800` (1800W Ueberschuss) und `pgt=-200` (200W Reserve) laedt der go-e mit
ca. 6A statt der bei vollen 1800W erwarteten ~7-8A, da 200W als Puffer abgezogen
werden, bevor der resultierende Ladestrom (abgerundet auf ganze Ampere) berechnet wird.

## Restrictions

Die Steuerung von `/SetCurrent`, `/StartStop` und `/MaxCurrent` funktioniert wie im
Original. Der native Venus-OS-Modus "Auto" fuer Drittanbieter-Ladestationen (nicht
die offizielle Victron-EVCS-Hardware) ist architektonisch nicht vorgesehen - Venus OS
reicht dafuer keine berechneten Werte durch. **Dieser Fork loest das**, indem er
eigenstaendig die PV-Ueberschuss-Werte aus Venus OS ausliest und direkt an den
go-e-eigenen Eco-Modus uebergibt (siehe oben) - vollstaendig unabhaengig von der
(fuer Drittanbieter wirkungslosen) nativen Venus-OS-Automatik.

Phasenumschaltung (1/3-phasig) bleibt bewusst der go-e-eigenen Firmware-Logik
(`psm`, `spl3`) ueberlassen und wird von diesem Skript nicht manipuliert.

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
Fuer die erweiterte Ladesteuerung siehe `config.ini.example` fuer alle Optionen.

⚠️ Nach jeder Aenderung an `config.ini` oder der `.py`-Datei: `restart.sh` ausfuehren.

## Used documentation

- https://github.com/goecharger/go-eCharger-API-v2
- https://github.com/victronenergy/venus/wiki/dbus
- https://github.com/victronenergy/venus/wiki/dbus-api

## Credits

Basiert auf der Arbeit von [vikt0rm](https://github.com/vikt0rm/dbus-goecharger),
inspiriert von [fabian-lauer](https://github.com/fabian-lauer/dbus-shelly-3em-smartmeter)
und [trixing](https://github.com/trixing/venus.dbus-twc3).
