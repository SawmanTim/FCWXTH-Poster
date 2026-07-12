#!/usr/bin/env python3
"""Phone alarm for posting failures — opens a GitHub issue that @mentions the
repo owner. GitHub Mobile pushes @mentions to the phone immediately, so this
acts like an instant text message the moment a cycle fails (no waiting for the
6-hour job window to end).

Skips creating a duplicate if an alert issue is already open, so a long outage
raises ONE alarm, not one per minute. Close the issue after fixing; a fresh one
opens automatically if posting ever fails again.

Called from run.yml. Needs env GH_TOKEN (the workflow's github.token) and the
workflow permission `issues: write`. Pass --test for a harmless test alert.
"""
import json
import os
import sys
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
API = f"https://api.github.com/repos/{REPO}/issues"
HDRS = {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "fcwxth-poster-alert"}
LABEL = "poster-alert"
TEST = "--test" in sys.argv

# One alarm at a time: if an alert issue is already open, don't pile on.
req = urllib.request.Request(f"{API}?state=open&labels={LABEL}", headers=HDRS)
if json.load(urllib.request.urlopen(req, timeout=30)):
    print("alert issue already open — not creating another")
    sys.exit(0)

owner = REPO.split("/")[0]
run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
           f"{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")

if TEST:
    title = "TEST — FCWXTH Poster phone alert"
    body = (f"@{owner} — this is a TEST of the phone alarm. If your phone "
            "buzzed, it works! You can close this issue.\n\n"
            f"Run: {run_url}")
else:
    title = "🚨 FCWXTH Poster: posting FAILURE"
    body = (f"@{owner} — the FCWXTH Poster just FAILED to post to Facebook.\n\n"
            "Most likely cause: an expired Facebook Page token (they last ~2 "
            "months). Could also be a Facebook/API outage — if posting "
            "recovers on its own, it was an outage.\n\n"
            f"Check the run log: {run_url}\n\n"
            "Close this issue after fixing — a new one opens automatically if "
            "posting fails again.")

data = json.dumps({"title": title, "body": body, "labels": [LABEL]}).encode()
urllib.request.urlopen(urllib.request.Request(API, data=data, headers=HDRS),
                       timeout=30)
print("alert issue created — phone notification on its way")
