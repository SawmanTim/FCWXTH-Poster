#!/usr/bin/env python3
"""My AcuRite station-health client — battery + signal for the 5-in-1.

WHY THIS EXISTS
    Weather Underground's PWS API carries meteorological fields only: no
    battery, no signal, no last-check-in. So the poster can tell that KALPHILC8
    has gone SILENT (see fetch_wu_station's staleness guard) but not WHY. My
    AcuRite knows, because the battery and RF signal are reported by the sensor
    itself to the AcuRite Access/smartHUB and on to myacurite.com.

    A 5-in-1 setup has two independent failure points, and they need different
    fixes, so telling them apart matters:
      * the 5-in-1 sensor  — battery powered, talks RF to the hub
      * the Access/smartHUB — mains + WiFi, uploads to My AcuRite AND to WU
    Dead sensor batteries show up here as a low battery / poor signal while the
    hub stays online. A dead hub shows up as the hub itself not checking in.

UNOFFICIAL — READ BEFORE CHANGING
    AcuRite publishes no public API. This talks to the same endpoints the
    myacurite.com dashboard uses, so it can change or start refusing us without
    warning. Two consequences shape the code below:

    1. Everything is best-effort. Any failure returns None and the caller
       carries on — station health is a nice-to-have, and it must never be able
       to break weather posting.
    2. The JSON shape is NOT contractual. Rather than hard-coding a path like
       hubs[0].devices[0].sensors[0].battery_level, `_walk` searches the whole
       response for objects that carry battery/signal fields. That keeps this
       working when AcuRite reshuffles the payload, which is the most likely
       way it breaks.

    AcuRite's own System Alerts (low battery, loss of signal, communication
    loss) are the officially supported path and are worth enabling regardless
    of this file. This exists to surface the same facts in the poll log and in
    the poster's existing phone alarm.

CREDENTIALS
    There is no scoped API token — the dashboard authenticates with the account
    login, so this needs env ACURITE_EMAIL and ACURITE_PASSWORD (GitHub
    secrets). Treat them as account credentials, because that is what they are.
    Nothing here logs them; post.py's scrub_secrets also redacts the literal
    password value from any exception text that reaches the log.

    Run `python acurite_test.py` locally to verify the login and see exactly
    what your account returns.
"""
from __future__ import annotations

import os

import requests

API_BASE = os.environ.get("ACURITE_API_BASE", "https://marapi.myacurite.com")
USER_AGENT = "FCWXTH-Poster/1.0 (+https://github.com/SawmanTim/FCWXTH-Poster)"
TIMEOUT = 30

# Keys seen carrying each fact. Ordered by preference; the first present wins.
_BATTERY_KEYS = ("battery_level", "battery_status", "battery")
_SIGNAL_KEYS = ("signal_strength", "signal_level", "signal", "rssi")
_CHECKIN_KEYS = ("last_check_in_at", "last_checkin", "last_reading_at",
                 "last_report_at", "updated_at")
_NAME_KEYS = ("name", "sensor_name", "device_name", "model_code", "model", "id")


def _first(obj: dict, keys) -> object:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None


def _walk(node, found: list, depth: int = 0) -> list:
    """Collect every object in the response that reports a battery or a signal.

    Deliberately shape-agnostic: AcuRite nests sensors under hubs under devices
    and has reshuffled that before. Searching beats hard-coding a path that
    silently yields nothing after a redesign."""
    if depth > 12:
        return found
    if isinstance(node, dict):
        battery = _first(node, _BATTERY_KEYS)
        signal = _first(node, _SIGNAL_KEYS)
        if battery is not None or signal is not None:
            found.append({
                "name": str(_first(node, _NAME_KEYS) or "unknown"),
                "battery": battery,
                "signal": signal,
                "last_check_in": _first(node, _CHECKIN_KEYS),
            })
        for v in node.values():
            _walk(v, found, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found, depth + 1)
    return found


def _token_and_account(payload: dict) -> tuple[str | None, str | None]:
    """Pull the session token and account id out of the login response without
    assuming where they sit — the login payload has moved between versions."""
    token = account = None

    def visit(node, depth=0):
        nonlocal token, account
        if depth > 8 or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            for v in node:
                visit(v, depth + 1)
            return
        for k, v in node.items():
            kl = k.lower()
            if token is None and isinstance(v, str) and "token" in kl:
                token = v
            if account is None and "account" in kl:
                if isinstance(v, (str, int)):
                    account = str(v)
                elif isinstance(v, dict) and "id" in v:
                    account = str(v["id"])
                elif isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
                    account = str(v[0]["id"])
            visit(v, depth + 1)

    visit(payload)
    return token, account


def fetch_health(email: str, password: str, *, base: str = API_BASE,
                 log=print) -> dict | None:
    """Log in and return station health, or None if anything at all goes wrong.

    Returns {"sensors": [{name, battery, signal, last_check_in}, ...],
             "raw_keys": [...]}  — raw_keys helps diagnose an empty result."""
    if not (email and password):
        log("  [acurite] ACURITE_EMAIL/ACURITE_PASSWORD not set — skipping health check")
        return None
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        r = s.post(f"{base}/users/login", timeout=TIMEOUT,
                   json={"remember": True, "email": email, "password": password})
        if r.status_code != 200:
            # 401 here means the credentials are wrong. The caller backs off
            # rather than retrying every cycle — repeated bad logins are how an
            # account gets locked.
            log(f"  [acurite] login HTTP {r.status_code}")
            return None
        token, account = _token_and_account(r.json())
        if not token:
            log("  [acurite] login succeeded but no token in the response "
                "— the API shape changed; run acurite_test.py")
            return None
        # Header name used by the dashboard's own XHRs.
        s.headers["x-one-vue-token"] = token

        data = None
        # Try the account-scoped dashboard first, then the unscoped variants.
        paths = ([f"/accounts/{account}/dashboard/hubs"] if account else []) + [
            "/dashboard/hubs", "/hubs"]
        for path in paths:
            rr = s.get(base + path, timeout=TIMEOUT)
            if rr.status_code == 200:
                try:
                    data = rr.json()
                except ValueError:
                    continue
                break
        if data is None:
            log(f"  [acurite] no dashboard endpoint answered (tried {', '.join(paths)})")
            return None

        sensors = _walk(data, [])
        if not sensors:
            log("  [acurite] logged in but found no battery/signal fields "
                "— run acurite_test.py to see the payload")
        return {"sensors": sensors,
                "raw_keys": sorted(data)[:12] if isinstance(data, dict) else []}
    except (requests.RequestException, ValueError) as exc:
        log(f"  [acurite] health check failed: {exc}")
        return None
    finally:
        s.close()


def is_low_battery(value) -> bool | None:
    """True/False, or None when the value can't be interpreted.

    AcuRite reports words ('Normal'/'Low', 'Good'/'Low') on the dashboard and a
    percentage in some payloads, so handle both. Unknown values return None so
    the caller stays quiet rather than crying wolf on a string it doesn't know."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) < 25          # AcuRite's own "low" threshold
    text = str(value).strip().lower()
    if text in ("low", "bad", "critical", "empty", "poor", "false", "0"):
        return True
    if text in ("normal", "good", "ok", "full", "excellent", "true", "1"):
        return False
    if text.rstrip("%").replace(".", "", 1).isdigit():
        return float(text.rstrip("%")) < 25
    return None


def is_weak_signal(value) -> bool | None:
    """True/False, or None when uninterpretable. AcuRite shows signal as bars
    (0-4) on the dashboard; 0 means the sensor is not being heard at all."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        # dBm FIRST: a negative is always dBm, and every negative is also <= 4,
        # so testing bars first would read a healthy -60 dBm as zero bars.
        if v < 0:
            return v <= -85
        if v <= 4:                        # bars (AcuRite shows 0-4)
            return v <= 1
        return False
    text = str(value).strip().lower()
    if text in ("poor", "weak", "bad", "none", "lost", "offline", "0"):
        return True
    if text in ("excellent", "good", "strong", "fair", "normal", "ok"):
        return False
    if text.isdigit():
        return int(text) <= 1
    return None
