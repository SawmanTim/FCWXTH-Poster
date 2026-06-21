#!/usr/bin/env python3
"""
FCWXTH Poster — self-hosted replacement for the IFTTT "RSS -> Facebook Page" applets.

Phase 1 covers Groups A-D from feeds.yaml:
  A. RSS.app agency reposts
  B. Official direct feeds (NHC)
  C. IEM office feeds (keyword-filtered by county)
  D. Per-county NWS watch/warning alerts  -> CONSOLIDATED into one post per event

Improvements over IFTTT:
  * One post per alert event listing every affected county (no more 27-message floods)
  * Real photo/map attachment (IFTTT's "create status message" was text-only)

Run modes:
  python post.py            # normal: post new items
  python post.py --seed     # mark everything currently present as "seen" WITHOUT posting
                            #   (use this ONCE on first run so you don't blast the backlog)
  python post.py --dry-run  # show what WOULD be posted, but don't call Facebook

Facebook tokens come from env vars, one long-lived Page token per page:
  FB_TOKEN_FCWXTH , FB_TOKEN_PVFD   (named FB_TOKEN_<PAGEKEY> from feeds.yaml `pages`)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests
import yaml

# Windows consoles default to cp1252 and crash on emoji/special chars in posts.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "feeds.yaml"
STATE_PATH = ROOT / "state.json"
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v21.0")
ALERTS_API = "https://api.weather.gov/alerts/active"
USER_AGENT = "FCWXTH-Poster (contact: sawmantim@hotmail.com)"

# How many seen-IDs to keep per source before trimming (keeps state.json small).
MAX_SEEN_PER_SOURCE = 400


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_state(state: dict) -> None:
    # trim each source's seen list to the most recent N
    for key, val in state.items():
        if isinstance(val, list) and len(val) > MAX_SEEN_PER_SOURCE:
            state[key] = val[-MAX_SEEN_PER_SOURCE:]
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    tmp.replace(STATE_PATH)


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def entry_image(entry) -> str | None:
    """Best-effort extraction of an image URL from a feed entry."""
    for enc in getattr(entry, "enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image"):
            return enc.get("href")
    for mc in getattr(entry, "media_content", []) or []:
        if mc.get("url") and str(mc.get("type", "")).startswith("image"):
            return mc["url"]
    for mt in getattr(entry, "media_thumbnail", []) or []:
        if mt.get("url"):
            return mt["url"]
    # fall back to first <img> in the content/summary
    body = ""
    if getattr(entry, "content", None):
        body = entry.content[0].get("value", "")
    body = body or getattr(entry, "summary", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', body or "", flags=re.I)
    return m.group(1) if m else None


def render(template: str, *, title="", content="", url="", image="") -> str:
    # {image} renders empty — the image is ATTACHED, not printed as a URL
    out = (template
           .replace("{title}", title or "")
           .replace("{content}", content or "")
           .replace("{url}", url or "")
           .replace("{image}", ""))
    # collapse the runs of blank lines the old templates left behind
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def entry_uid(entry) -> str:
    return (getattr(entry, "id", "") or getattr(entry, "link", "")
            or getattr(entry, "title", "")).strip()


# --------------------------------------------------------------------------- #
# Facebook Graph API
# --------------------------------------------------------------------------- #
class Facebook:
    def __init__(self, page_ids: dict, dry_run: bool = False):
        self.page_ids = page_ids                 # {"FCWXTH": "4237...", ...}
        self.dry_run = dry_run
        self.tokens = {k: os.environ.get(f"FB_TOKEN_{k}") for k in page_ids}

    def _token(self, page_key: str) -> str | None:
        return self.tokens.get(page_key)

    def post(self, page_key: str, message: str, image_url: str | None) -> bool:
        page_id = self.page_ids[page_key]
        token = self._token(page_key)
        if self.dry_run or not token:
            tag = "DRY-RUN" if self.dry_run else "NO-TOKEN(skipped)"
            log(f"  [{tag}] -> {page_key}: {message[:90]!r} "
                f"{'(+image)' if image_url else ''}")
            return self.dry_run  # in dry-run we count it as 'handled'
        try:
            if image_url:
                endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos"
                data = {"url": image_url, "caption": message, "access_token": token}
            else:
                endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/feed"
                data = {"message": message, "access_token": token}
            r = requests.post(endpoint, data=data, timeout=30)
            if r.status_code >= 400:
                # if a photo post fails (e.g. bad image), retry as plain text
                if image_url:
                    log(f"  photo post failed ({r.status_code}); retrying as text")
                    return self.post(page_key, message, None)
                log(f"  ERROR posting to {page_key}: {r.status_code} {r.text[:200]}")
                return False
            log(f"  posted to {page_key} (id={r.json().get('id', '?')})")
            return True
        except requests.RequestException as exc:
            log(f"  ERROR posting to {page_key}: {exc}")
            return False


# --------------------------------------------------------------------------- #
# Feed fetching
# --------------------------------------------------------------------------- #
def fetch_feed(url: str):
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except requests.RequestException as exc:
        log(f"  fetch failed {url}: {exc}")
        return None


def process_simple_feed(item, fb, state, *, seed, attach_image,
                        match_any=None):
    """Groups A, B, C — one post per new entry (C also keyword-filters)."""
    key = item["url"]
    seen = set(state.get(key, []))
    parsed = fetch_feed(item["url"])
    if not parsed:
        return
    new_ids = []
    for entry in reversed(parsed.entries):       # oldest first
        uid = entry_uid(entry)
        if not uid or uid in seen:
            continue
        new_ids.append(uid)

        title = strip_html(getattr(entry, "title", ""))
        body = ""
        if getattr(entry, "content", None):
            body = entry.content[0].get("value", "")
        content = strip_html(body or getattr(entry, "summary", ""))
        link = getattr(entry, "link", "")

        # Group C keyword filter: only post if a watched county is mentioned
        if match_any:
            haystack = f"{title}\n{content}".lower()
            if not any(c.lower() in haystack for c in match_any):
                continue

        if seed:
            continue  # record as seen, don't post

        msg = render(item["template"], title=title, content=content, url=link)
        img = entry_image(entry) if attach_image else None
        log(f"[{item['name']}] new: {title[:70]!r}")
        for pk in item["pages"]:
            fb.post(pk, msg, img)

    if new_ids:
        state[key] = list(seen.union(new_ids))


def zone_from_url(u: str) -> str:
    # affectedZones look like ".../zones/county/ALC059" or ".../zones/forecast/ALZ001"
    m = re.search(r"([A-Z]{2}[CZ]\d{3})\s*$", u or "")
    return m.group(1) if m else ""


def process_county_alerts(cfg, fb, state, *, seed):
    """Group D — pull all zones in ONE api.weather.gov call, consolidate by event."""
    counties = cfg["counties"]
    by_zone = {c["zone"]: c for c in counties}
    zones = ",".join(by_zone)
    key = "county_alerts"
    seen = set(state.get(key, []))

    try:
        r = requests.get(ALERTS_API, params={"zone": zones}, timeout=40,
                         headers={"User-Agent": USER_AGENT,
                                  "Accept": "application/geo+json"})
        r.raise_for_status()
        features = r.json().get("features", [])
    except (requests.RequestException, ValueError) as exc:
        log(f"[county_alerts] fetch failed: {exc}")
        return

    new_ids = []
    for feat in features:
        alert_id = feat.get("id", "")
        if not alert_id or alert_id in seen:
            continue
        new_ids.append(alert_id)
        if seed:
            continue

        props = feat.get("properties", {})
        # which of OUR counties does this alert touch?
        hit_zones = {zone_from_url(z) for z in props.get("affectedZones", [])}
        matched = [by_zone[z] for z in hit_zones if z in by_zone]
        if not matched:
            continue

        county_names = sorted({f"{c['county']}, {c['state']}" for c in matched})
        pages = sorted({p for c in matched for p in c["pages"]})
        headline = props.get("headline") or props.get("event", "")
        desc = strip_html(props.get("description", ""))
        link = (props.get("@id") or alert_id)

        msg = (
            "***NWS ALERT for your LOCATION***\n"
            f"Counties affected: {'; '.join(county_names)}\n\n"
            f"{headline}\n\n{desc}\n{link}"
        )
        msg = re.sub(r"\n{3,}", "\n\n", msg).strip()
        log(f"[county_alerts] {props.get('event','alert')} -> "
            f"{len(county_names)} counties, pages={pages}")
        for pk in pages:
            fb.post(pk, msg, None)

    if new_ids:
        state[key] = list(seen.union(new_ids))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="mark current items as seen without posting (first run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be posted; do not call Facebook")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()
    attach_image = bool(cfg.get("defaults", {}).get("attach_image", True))
    fb = Facebook(cfg["pages"], dry_run=args.dry_run)

    if args.seed:
        log("SEED MODE — recording current items as seen, NOT posting.")

    # Group A
    for item in cfg.get("rss_app_feeds", []):
        process_simple_feed(item, fb, state, seed=args.seed,
                            attach_image=attach_image)
    # Group B
    for item in cfg.get("official_feeds", []):
        process_simple_feed(item, fb, state, seed=args.seed,
                            attach_image=attach_image)
    # Group C
    for item in cfg.get("iem_office_feeds", []):
        process_simple_feed(item, fb, state, seed=args.seed,
                            attach_image=attach_image,
                            match_any=item.get("match_any"))
    # Group D
    county_cfg = cfg.get("county_alert_feeds")
    if county_cfg:
        process_county_alerts(county_cfg, fb, state, seed=args.seed)

    if args.dry_run:
        log("Done (dry-run — state NOT saved).")
    else:
        save_state(state)
        log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
