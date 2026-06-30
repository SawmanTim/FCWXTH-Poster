"""
wu_test.py — one-off check of the Weather Underground PWS pull for KALPHILC8.
Reads the key from the WU_API_KEY environment variable (never hard-coded).
Prints the raw JSON + a parsed summary. Output is weather data only — safe to share.
Run:
    PowerShell:  $env:WU_API_KEY='<your key>'; python wu_test.py
    Git Bash:    WU_API_KEY='<your key>' python wu_test.py
"""
import os, json, sys, urllib.parse, urllib.request

KEY = os.environ.get("WU_API_KEY")
if not KEY:
    sys.exit("WU_API_KEY is not set in this terminal. See the comment at the top of this file.")

STATION = "KALPHILC8"
params = {
    "stationId": STATION,
    "format": "json",
    "units": "e",                 # English / imperial
    "numericPrecision": "decimal",  # 93.9 instead of 94
    "apiKey": KEY,
}
url = "https://api.weather.com/v2/pws/observations/current?" + urllib.parse.urlencode(params)

# Show the URL with the key masked, so a pasted screenshot/log never leaks it.
print("GET", url.replace(KEY, "****"), "\n")

try:
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
except Exception as exc:  # noqa: BLE001
    sys.exit(f"Request failed: {exc}")

print("===== RAW JSON =====")
print(json.dumps(data, indent=2))

obs = (data.get("observations") or [{}])[0]
imp = obs.get("imperial", {})
print("\n===== PARSED SUMMARY =====")
print("station   :", obs.get("stationID"))
print("obs time  :", obs.get("obsTimeLocal"))
print("temp      :", imp.get("temp"))
print("heatIndex :", imp.get("heatIndex"))
print("windChill :", imp.get("windChill"))
print("dewpt     :", imp.get("dewpt"))
print("humidity  :", obs.get("humidity"))
print("winddir   :", obs.get("winddir"))
print("windSpeed :", imp.get("windSpeed"))
print("windGust  :", imp.get("windGust"))
print("pressure  :", imp.get("pressure"))
print("precipRate:", imp.get("precipRate"))
print("precipTotl:", imp.get("precipTotal"))
print("uv        :", obs.get("uv"))
print("solarRad  :", obs.get("solarRadiation"))
