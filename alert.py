#!/usr/bin/env python3
"""Phone alarm for posting failures — opens a GitHub issue that @mentions the
repo owner. GitHub Mobile pushes @mentions to the phone immediately, so this
acts like an instant text message the moment a cycle fails (no waiting for the
6-hour job window to end).

Skips creating a duplicate if an alert issue is already open, so a long outage
raises ONE alarm, not one per minute. Close the issue after fixing; a fresh one
opens automatically if posting ever fails again.

Called from run.yml. Needs env GH_TOKEN (the workflow's github.token) and the
workflow permission `issues: write`. Pass --test for a harmless test alert;
pass --log <path> to quote the failing cycle's error lines in the issue body
so the alarm itself says WHAT failed (no log-digging needed).
"""
import json
import os
import re
import sys
import urllib.request


def error_excerpt(path: str, limit: int = 2500) -> str:
    """Pull the interesting lines out of a cycle log: errors, tracebacks,
    warnings. Falls back to the tail if nothing matches. API keys are masked —
    urllib3 exception text can include the full request URL, query and all."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    keep = [ln for ln in lines
            if re.search(r"ERROR|FATAL|Traceback|UNEXPECTED|WARNING|Error\b|failed",
                         ln)]
    if not keep:
        keep = lines[-15:]
    text = "\n".join(keep[-40:])[-limit:]
    return re.sub(r"(apiKey|api_key|access_token)=[^&\s\"']+", r"\1=****",
                  text, flags=re.I)

REPO = os.environ["GITHUB_REPOSITORY"]
API = f"https://api.github.com/repos/{REPO}/issues"
HDRS = {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "fcwxth-poster-alert"}
LABEL = "poster-alert"
TEST = "--test" in sys.argv
LOG_PATH = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else ""

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
            "Common causes: an expired Facebook Page token (they last ~2 "
            "months), an API/Facebook outage, or a crash — the log excerpt "
            "below usually says which. If posting recovers on its own, it "
            "was an outage.\n\n"
            f"Full run log: {run_url}\n\n"
            "Close this issue after fixing — a new one opens automatically if "
            "posting fails again.")
    excerpt = error_excerpt(LOG_PATH) if LOG_PATH else ""
    if excerpt:
        body += f"\n\n**What the failing cycle said:**\n\n```text\n{excerpt}\n```"

data = json.dumps({"title": title, "body": body, "labels": [LABEL]}).encode()
urllib.request.urlopen(urllib.request.Request(API, data=data, headers=HDRS),
                       timeout=30)
print("alert issue created — phone notification on its way")
