#!/bin/bash
# pvcheck.sh - Shows the most important go-e PV surplus and diagnostic
# values in a readable, formatted way, without changing the main script's
# logging level.
#
# Usage:
#   ./pvcheck.sh              one-time snapshot
#   ./pvcheck.sh -w           continuously refreshing, every 5s (Ctrl+C to stop)
#   ./pvcheck.sh -w 10        continuously refreshing, every 10s
#   ./pvcheck.sh -h|--help    show this help text
#
# Host is read from config.ini if present in the same directory,
# otherwise falls back to 192.168.2.36.
#
# Also shows the actual home battery SOC (read directly from Venus OS via
# the 'dbus' CLI tool) alongside the configured BatteryPriorityMinSoc /
# BatterySupportMinSoc thresholds from config.ini - this fork's own
# battery-priority/battery-support state is internal to the Python script
# and not visible to the go-e itself, so it can't be read via curl; this
# is the closest equivalent available from a plain shell script.

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
CONFIG_FILE="$SCRIPT_DIR/config.ini"
PYHELPER="$SCRIPT_DIR/.pvcheck_format.py"

print_help() {
  cat << 'HELPEOF'
pvcheck.sh - go-e PV surplus / diagnostic monitor

Usage:
  ./pvcheck.sh              one-time snapshot
  ./pvcheck.sh -w           continuously refreshing, every 5 seconds (Ctrl+C to stop)
  ./pvcheck.sh -w SECONDS   continuously refreshing, every SECONDS seconds
  ./pvcheck.sh -h|--help    show this help text

Shows: charging status, reason (modelStatus), phase-switch mode (psm),
live charging power/current, pGrid/pPv/pAkku, the go-e's internal rolling
averages of those three, and the home battery SOC alongside the
configured BatteryPriorityMinSoc/BatterySupportMinSoc thresholds - all
decoded to readable labels.

Host is read from config.ini (section [ONPREMISE], key Host) if the file
is present in the same directory as this script, otherwise falls back to
192.168.2.36.
HELPEOF
}

if [ "$1" == "-h" ] || [ "$1" == "--help" ]; then
  print_help
  exit 0
fi

get_ini_value() {
  # get_ini_value KEY DEFAULT - reads a key from the [DEFAULT] section of
  # config.ini (i.e. before any '[' section header), case-sensitive match
  # on the key name as it appears in config.ini.
  local key="$1"
  local default="$2"
  local val=""
  if [ -f "$CONFIG_FILE" ]; then
    val=$(sed -n '/^\[ONPREMISE\]/q;p' "$CONFIG_FILE" | grep "^$key" | head -n 1 | sed 's/.*=\s*//' | tr -d '[:space:]')
  fi
  echo "${val:-$default}"
}

if [ -f "$CONFIG_FILE" ]; then
  # head -n 1 instead of head -1: BusyBox (Venus OS) doesn't know the -1 shorthand
  HOST=$(grep -A5 '^\[ONPREMISE\]' "$CONFIG_FILE" | grep '^Host' | head -n 1 | sed 's/.*=\s*//' | tr -d '[:space:]')
fi
HOST=${HOST:-192.168.2.36}

# NOTE: the BatteryPriority*/BatterySupport* values are deliberately NOT read
# here - they are read fresh inside fetch_and_print() on every refresh, so
# that editing config.ini while ./pvcheck.sh -w is running is reflected in the
# very next output line. The main script re-reads these values live too (no
# restart needed), so reading them once at startup here would show stale
# thresholds that no longer match what the service is actually applying.

FILTER="car,modelStatus,psm,err,pgrid,ppv,pakku,pvopt_averagePGrid,pvopt_averagePPv,pvopt_averagePAkku,nrg"

# The Python formatting logic is written to its own file instead of being
# embedded inline via "python3 -c '...'" - this avoids quoting conflicts
# between bash and Python quoting (this already caused a real bug once).
cat > "$PYHELPER" << 'PYEOF'
import sys
import json
import datetime

CAR = {
    1: "Idle (no vehicle)",
    2: "Charging",
    3: "WaitCar (waiting for vehicle)",
    4: "Complete (finished/paused)",
    5: "Error",
}
MODELSTATUS = {
    0: "NoChargeCtrlData", 1: "Overtemperature", 2: "AccessControlWait",
    3: "ForceStateOn", 4: "ForceStateOff (hard stop)",
    5: "Scheduler", 6: "EnergyLimit", 7: "AwattarPriceLow",
    8: "AutomaticStopTestLadung", 9: "AutomaticStopNotEnoughTime",
    10: "AutomaticStop", 11: "AutomaticStopNoClock",
    12: "PvSurplus (charging due to surplus)",
    13: "FallbackGoEDefault", 14: "FallbackGoEScheduler",
    15: "FallbackDefault (normal, Basic mode)",
    16: "FallbackGoEAwattar", 17: "FallbackAwattar (soft pause, no surplus)",
    18: "FallbackAutomaticStop", 19: "CarCompatibilityKeepAlive",
    20: "ChargePauseNotAllowed", 22: "SimulateUnplugging",
    23: "PhaseSwitch (phase switch in progress)", 24: "MinPauseDuration",
    25: "ChargeDelay", 26: "Error", 27: "LoadManagementDoesntWant",
}
PSM = {0: "Auto", 1: "forced 1-phase", 2: "forced 3-phase"}


def fmt_w(v):
    if v is None:
        return "   n/aW"
    return "{:>6.0f}W".format(v)


def fmt_avg(v):
    if v is None:
        return "     n/a"
    return "{:>8.1f}".format(v)


def fmt_soc(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def main():
    now = datetime.datetime.now().strftime("%H:%M:%S")

    # Battery/config info comes from bash argv, not from the go-e's JSON:
    #   argv[1] = actual battery SOC (from dbus), or empty if unavailable
    #   argv[2] = BatteryPriorityMinSoc, argv[3] = its hysteresis
    #   argv[4] = BatterySupportMinSoc,  argv[5] = its hysteresis
    soc = fmt_soc(sys.argv[1]) if len(sys.argv) > 1 else None
    prioMinSoc = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    prioHyst = float(sys.argv[3]) if len(sys.argv) > 3 else 0
    supportMinSoc = float(sys.argv[4]) if len(sys.argv) > 4 else 0
    supportHyst = float(sys.argv[5]) if len(sys.argv) > 5 else 0

    try:
        d = json.load(sys.stdin)
    except Exception as e:
        print("{}  ERROR: invalid response ({})".format(now, e))
        return

    car = d.get("car")
    ms = d.get("modelStatus")
    psm = d.get("psm")
    pgrid = d.get("pgrid")
    ppv = d.get("ppv")
    pakku = d.get("pakku")
    avgGrid = d.get("pvopt_averagePGrid")
    avgPv = d.get("pvopt_averagePPv")
    avgAkku = d.get("pvopt_averagePAkku")
    nrg = d.get("nrg") or []
    liveWatt = nrg[11] if len(nrg) > 11 else None
    liveAmp = nrg[4] if len(nrg) > 4 else None

    carTxt = CAR.get(car, "unknown ({})".format(car))
    msTxt = MODELSTATUS.get(ms, "unknown ({})".format(ms))
    psmTxt = PSM.get(psm, "unknown ({})".format(psm))
    if pakku is not None and pakku < 0:
        akkuTxt = "charging"
    elif pakku:
        akkuTxt = "discharging"
    else:
        akkuTxt = "-"

    liveWattTxt = "{:.0f}".format(liveWatt) if liveWatt is not None else "?"
    liveAmpTxt = "{:.1f}".format(liveAmp) if liveAmp is not None else "?"

    print("[{}] Status: {:<30} | Reason: {}".format(now, carTxt, msTxt))
    print("          Phase: {:<20} | Live charging: {}W ({}A)".format(psmTxt, liveWattTxt, liveAmpTxt))
    print("          pGrid={}  pPv={}  pAkku={} ({})".format(fmt_w(pgrid), fmt_w(ppv), fmt_w(pakku), akkuTxt))
    print("          Averages: Grid={}  Pv={}  Akku={}".format(fmt_avg(avgGrid), fmt_avg(avgPv), fmt_avg(avgAkku)))

    if soc is not None:
        socTxt = "{:.1f}%".format(soc)
        # NOTE: this is a simple threshold check, NOT a stateful hysteresis
        # replication - this fork's actual _batteryPriorityPaused /
        # _batterySupportActive flags also remember whether they were
        # already active/paused, so near the hysteresis band this line may
        # show "below/above threshold" while the running script is still
        # in its previous state for a little longer. Good enough to see
        # WHY something is probably happening, not a guaranteed live match.
        if prioMinSoc > 0:
            prioState = "below" if soc < prioMinSoc else "at/above"
            print("          Battery: {} SOC | Priority>={:.0f}%(+{:.0f}) -> {} threshold (approx., no hysteresis state)".format(
                socTxt, prioMinSoc, prioHyst, prioState))
        if supportMinSoc > 0:
            supportState = "at/above" if soc >= supportMinSoc else "below"
            print("          Battery: {} SOC | Support>={:.0f}%(-{:.0f}) -> {} threshold (approx., no hysteresis state)".format(
                socTxt, supportMinSoc, supportHyst, supportState))
    else:
        print("          Battery: SOC not available (dbus query failed or not run on Venus OS)")

    print("-" * 70)


main()
PYEOF

fetch_and_print() {
  local json
  json=$(curl -s --max-time 3 "http://$HOST/api/status?filter=$FILTER")
  if [ -z "$json" ]; then
    echo "$(date '+%H:%M:%S')  ERROR: no response from $HOST"
    return
  fi
  # Battery SOC read directly from Venus OS, if the 'dbus' CLI tool is
  # available (it is on Venus OS itself, not necessarily elsewhere).
  local soc=""
  if command -v dbus >/dev/null 2>&1; then
    soc=$(dbus -y com.victronenergy.system /Dc/Battery/Soc GetValue 2>/dev/null)
  fi
  # Thresholds are read fresh on every refresh (not once at startup), so that
  # editing config.ini while -w is running shows up in the next output line -
  # matching the main script, which also applies these live without a restart.
  local prioMinSoc prioHyst supportMinSoc supportHyst
  prioMinSoc=$(get_ini_value "BatteryPriorityMinSoc" "0")
  prioHyst=$(get_ini_value "BatteryPriorityHysteresis" "2")
  supportMinSoc=$(get_ini_value "BatterySupportMinSoc" "0")
  supportHyst=$(get_ini_value "BatterySupportHysteresis" "2")
  echo "$json" | python3 "$PYHELPER" "$soc" "$prioMinSoc" "$prioHyst" "$supportMinSoc" "$supportHyst"
}

if [ "$1" == "-w" ]; then
  INTERVAL=${2:-5}
  while true; do
    fetch_and_print
    sleep "$INTERVAL"
  done
else
  fetch_and_print
fi
