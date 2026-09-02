#!/usr/bin/env python3
"""acurite_test.py — one-off check of the My AcuRite station-health pull.

Reads ACURITE_EMAIL / ACURITE_PASSWORD from the environment (never hard-coded,
never printed). Prints what the poster would see: each sensor's battery, signal
and last check-in, plus how the poster interprets those values.

    PowerShell:  $env:ACURITE_EMAIL='you@example.com'
                 $env:ACURITE_PASSWORD='...'
                 python acurite_test.py
    Git Bash:    ACURITE_EMAIL='you@example.com' ACURITE_PASSWORD='...' python acurite_test.py

Pass --raw to dump the full JSON payload. Use that if the summary comes back
empty: AcuRite has no public API, so the payload shape is not guaranteed, and
the dump shows what to teach acurite.py to look for. The dump is redacted, but
read it before pasting it anywhere.
"""
import json
import os
import sys

import acurite

RAW = "--raw" in sys.argv
EMAIL = os.environ.get("ACURITE_EMAIL")
PASSWORD = os.environ.get("ACURITE_PASSWORD")

if not (EMAIL and PASSWORD):
    sys.exit("ACURITE_EMAIL and ACURITE_PASSWORD are not both set in this "
             "terminal. See the comment at the top of this file.")


def redact(text: str) -> str:
    """Strip credentials and session tokens out of the raw dump."""
    for secret in (PASSWORD, EMAIL):
        if secret:
            text = text.replace(secret, "****")
    return text


if RAW:
    # Re-run the request with the walker bypassed so the payload can be seen.
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": acurite.USER_AGENT, "Accept": "application/json"})
    r = s.post(f"{acurite.API_BASE}/users/login", timeout=acurite.TIMEOUT,
               json={"remember": True, "email": EMAIL, "password": PASSWORD})
    print(f"login -> HTTP {r.status_code}")
    if r.status_code != 200:
        sys.exit("login failed — check the credentials")
    token, account = acurite._token_and_account(r.json())
    print("token found :", bool(token))
    print("account id  :", account or "(not found)")
    s.headers["x-one-vue-token"] = token or ""
    for path in ([f"/accounts/{account}/dashboard/hubs"] if account else []) + \
                ["/dashboard/hubs", "/hubs"]:
        rr = s.get(acurite.API_BASE + path, timeout=acurite.TIMEOUT)
        print(f"\nGET {path} -> HTTP {rr.status_code}")
        if rr.status_code == 200:
            print(redact(json.dumps(rr.json(), indent=2))[:20000])
            break
    sys.exit(0)

health = acurite.fetch_health(EMAIL, PASSWORD)
if not health:
    sys.exit("\nNo health data. Re-run with --raw to see what the API returned.")

print("\n===== STATION HEALTH =====")
if not health["sensors"]:
    print("No battery/signal fields found.")
    print("Top-level keys in the response:", health["raw_keys"])
    print("\nRe-run with --raw and adjust the *_KEYS lists in acurite.py.")
    sys.exit(1)

for s in health["sensors"]:
    low = acurite.is_low_battery(s["battery"])
    weak = acurite.is_weak_signal(s["signal"])
    def verdict(flag, bad, good):
        return "?" if flag is None else (bad if flag else good)
    print(f"\n  {s['name']}")
    print(f"    battery      : {s['battery']}   -> {verdict(low, 'LOW', 'ok')}")
    print(f"    signal       : {s['signal']}   -> {verdict(weak, 'WEAK', 'ok')}")
    print(f"    last check-in: {s['last_check_in']}")

print("\nA '?' means acurite.py could not interpret that value — send it over "
      "and it can be added to is_low_battery()/is_weak_signal().")
