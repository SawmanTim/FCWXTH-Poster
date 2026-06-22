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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from astral import LocationInfo
from astral.sun import sun

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


def clean_nws_product(text: str) -> str:
    """Strip raw NWS transmission noise (WMO header, AWIPS id, 000, $$) from a
    product so it reads cleanly on Facebook."""
    out = []
    for i, ln in enumerate(text.split("\n")):
        s = ln.strip()
        if s == "000" or s == "$$":
            continue
        if re.match(r"^[A-Z]{4}\d{2} [A-Z]{4} \d{6}$", s):   # e.g. ABNT20 KNHC 212309
            continue
        if i < 4 and re.match(r"^[A-Z]{3,6}$", s):           # AWIPS id near top, e.g. TWOAT
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def entry_image(entry) -> str | None:
    """Best-effort extraction of an image URL from a feed entry."""
    url = None
    for enc in getattr(entry, "enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            url = enc["href"]
            break
    if not url:
        for mc in getattr(entry, "media_content", []) or []:
            if mc.get("url") and (mc.get("medium") == "image"
                                  or str(mc.get("type", "")).startswith("image")):
                url = mc["url"]
                break
    if not url:
        for mt in getattr(entry, "media_thumbnail", []) or []:
            if mt.get("url"):
                url = mt["url"]
                break
    if not url:
        body = ""
        if getattr(entry, "content", None):
            body = entry.content[0].get("value", "")
        body = body or getattr(entry, "summary", "")
        m = re.search(r'<img[^>]+src="([^"]+)"', body or "", flags=re.I)
        url = m.group(1) if m else None
    # feeds HTML-encode the URL (&amp;); decode so the link actually works
    return html.unescape(url) if url else None


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

    def selftest(self, page_key: str) -> bool:
        """Post an UNPUBLISHED (draft) post to verify the token + permissions.
        Unpublished posts are visible only in Meta Business Suite, never public."""
        page_id = self.page_ids[page_key]
        token = self._token(page_key)
        if not token:
            log(f"  [{page_key}] NO TOKEN set — add secret FB_TOKEN_{page_key}")
            return False
        msg = ("✅ FCWXTH Poster self-test — UNPUBLISHED draft confirming PHOTO "
               "posting works (safe to delete).")
        test_img = ("https://mesonet.agron.iastate.edu/plotting/auto/plot/208/"
                    "network:WFO::wfo:MOB::year:2026::phenomenav:FL::"
                    "significancev:W::etn:0037::opt:single::_r:t::dpi:100.png")
        try:
            im = requests.get(test_img, timeout=30, headers={"User-Agent": USER_AGENT})
            r = requests.post(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos",
                data={"caption": msg, "published": "false", "access_token": token},
                files={"source": ("test.png", im.content, "image/png")},
                timeout=60)
            if r.status_code >= 400:
                log(f"  [{page_key}] PHOTO FAILED: {r.status_code} {r.text[:300]}")
                return False
            log(f"  [{page_key}] OK — unpublished PHOTO draft created "
                f"(id={r.json().get('id','?')}). Check Meta Business Suite drafts.")
            return True
        except requests.RequestException as exc:
            log(f"  [{page_key}] ERROR: {exc}")
            return False

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
                # Download the image and upload the actual BYTES (multipart "source").
                # Far more reliable than passing url= and hoping Facebook's scraper
                # fetches it — dynamic images like the IEM maps often fail via url=.
                im = None
                try:
                    im = requests.get(image_url, timeout=30,
                                      headers={"User-Agent": USER_AGENT})
                except requests.RequestException:
                    im = None
                if im is not None and im.status_code == 200 and \
                        im.headers.get("content-type", "").startswith("image"):
                    r = requests.post(
                        endpoint,
                        data={"caption": message, "access_token": token},
                        files={"source": ("map.png", im.content,
                                          im.headers.get("content-type", "image/png"))},
                        timeout=60)
                else:
                    r = requests.post(
                        endpoint,
                        data={"url": image_url, "caption": message, "access_token": token},
                        timeout=30)
            else:
                endpoint = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/feed"
                r = requests.post(
                    endpoint,
                    data={"message": message, "access_token": token}, timeout=30)
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
        if item.get("clean") == "nws":
            content = clean_nws_product(content)
        link = getattr(entry, "link", "")

        # optional skip (e.g. NHC's redundant "no tropical cyclones" status line)
        skip = item.get("skip_title_contains")
        if skip and skip.lower() in title.lower():
            continue

        # Group C keyword filter: only post if a watched county is mentioned
        if match_any:
            haystack = f"{title}\n{content}".lower()
            if not any(c.lower() in haystack for c in match_any):
                continue

        if seed:
            continue  # record as seen, don't post

        base = render(item["template"], title=title, content=content, url=link)
        # a feed may pin a fixed image (e.g. the NHC outlook graphic); else use the entry's
        img = item.get("image") or (entry_image(entry) if attach_image else None)
        log(f"[{item['name']}] new: {title[:70]!r}")
        for pk in item["pages"]:
            tags = PAGE_HASHTAGS.get(pk, "")
            msg = f"{base}\n\n{tags}" if tags else base
            fb.post(pk, msg, img)

    if new_ids:
        state[key] = list(seen.union(new_ids))


def zone_from_url(u: str) -> str:
    # affectedZones look like ".../zones/county/ALC059" or ".../zones/forecast/ALZ001"
    m = re.search(r"([A-Z]{2}[CZ]\d{3})\s*$", u or "")
    return m.group(1) if m else ""


# Page-specific sign-off + hashtags (appended to alert posts per target page).
SIGN_OFFS = {
    "FCWXTH": 'From your local "Franklin County Weather Team".\n#THW #FCWXTH',
    "PVFD":   'From your local "Paden Fire & Rescue Department Weather Team".\n#THW #PVFRD',
}

# Per-page hashtags appended to agency reposts (alerts use the fuller SIGN_OFFS).
PAGE_HASHTAGS = {
    "FCWXTH": "#THW #FCWXTH",
    "PVFD":   "#THW #PVFRD",
}


def _param(props: dict, key: str) -> str | None:
    v = (props.get("parameters") or {}).get(key)
    if isinstance(v, list) and v:
        return str(v[0]).strip()
    return None


def ibw_tags(props: dict) -> list[str]:
    """Impact-Based Warning tags — the pro details NWS attaches to warnings."""
    tags: list[str] = []
    tdt = (_param(props, "tornadoDamageThreat") or "").upper()
    if tdt == "CATASTROPHIC":
        tags.append("*** TORNADO EMERGENCY ***")
    elif tdt == "CONSIDERABLE":
        tags.append("*** PARTICULARLY DANGEROUS SITUATION ***")
    fft = (_param(props, "flashFloodDamageThreat") or "").upper()
    if fft == "CATASTROPHIC":
        tags.append("*** FLASH FLOOD EMERGENCY ***")
    elif fft == "CONSIDERABLE":
        tags.append("*** CONSIDERABLE flash flood threat ***")
    det = _param(props, "tornadoDetection")
    if det:
        tags.append(f"Tornado: {det.title()}")
    dmg = _param(props, "thunderstormDamageThreat")
    if dmg:
        tags.append(f"Wind damage threat: {dmg.title()}")
    hail = _param(props, "maxHailSize")
    if hail and hail not in ("0.00", "0"):
        tags.append(f'Max hail: {hail}"')
    wind = _param(props, "maxWindGust")
    if wind and wind not in ("0", "0 MPH"):
        tags.append(f"Max wind gust: {wind}")
    return tags


def iem_map_url(props: dict) -> str | None:
    """Build the IEM Autoplot #208 map image URL from the alert's VTEC code."""
    for v in (props.get("parameters") or {}).get("VTEC", []) or []:
        m = re.search(r"/[A-Z]\.[A-Z]{3}\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.(\d{2})\d{4}T", v)
        if m:
            office, ph, sig, etn, yy = m.groups()
            wfo = office[1:] if len(office) == 4 else office   # KHUN -> HUN
            year = 2000 + int(yy)
            return ("https://mesonet.agron.iastate.edu/plotting/auto/plot/208/"
                    f"network:WFO::wfo:{wfo}::year:{year}::phenomenav:{ph}::"
                    f"significancev:{sig}::etn:{etn}::opt:single::_r:t::dpi:100.png")
    return None


_COMPASS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def storm_motion(props: dict) -> str | None:
    """Human-readable storm motion from NWS eventMotionDescription.
    The degrees are the direction the storm comes FROM, so movement is +180."""
    emd = _param(props, "eventMotionDescription")
    if not emd:
        return None
    m = re.search(r"(\d{1,3})DEG\.\.\.(\d{1,3})KT", emd)
    if not m:
        return None
    frm, kt = int(m.group(1)), int(m.group(2))
    toward = _COMPASS16[int(((frm + 180) % 360) / 22.5 + 0.5) % 16]
    return f"Storm motion: moving {toward} at {round(kt * 1.151)} mph"


def build_alert_message(props: dict, county_names: list[str], page_key: str) -> str:
    headline = (props.get("headline") or "").strip()
    desc = strip_html(props.get("description", ""))
    instruction = strip_html(props.get("instruction") or "")
    parts = ["***Affected Counties: " + "; ".join(county_names) + "***",
             "***NWS ALERT for Your LOCATION***"]
    if headline:
        parts.append(headline)
    tags = ibw_tags(props)
    if tags:
        parts.append("\n".join(tags))
    motion = storm_motion(props)
    if motion:
        parts.append(motion)
    if desc:
        parts.append(desc)
    if instruction:
        parts.append("PRECAUTIONARY/PREPAREDNESS ACTIONS:\n" + instruction)
    parts.append(SIGN_OFFS.get(page_key, SIGN_OFFS["FCWXTH"]))
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(p for p in parts if p)).strip()


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
        img = iem_map_url(props)   # IEM Autoplot map of the warning polygon
        log(f"[county_alerts] {props.get('event','alert')} -> "
            f"{len(county_names)} counties, pages={pages}, map={'yes' if img else 'no'}")
        for pk in pages:
            fb.post(pk, build_alert_message(props, county_names, pk), img)

    if new_ids:
        state[key] = list(seen.union(new_ids))


# --------------------------------------------------------------------------- #
# Group E — weather-condition & sunrise/sunset posts (event-triggered)
# --------------------------------------------------------------------------- #
def fetch_current_obs(lat, lon) -> dict | None:
    """Latest temperature (F) + condition text from the nearest NWS station."""
    ua = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    try:
        pts = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                           headers=ua, timeout=30).json()
        st_url = pts["properties"]["observationStations"]
        sid = requests.get(st_url, headers=ua, timeout=30).json() \
            ["features"][0]["properties"]["stationIdentifier"]
        o = requests.get(f"https://api.weather.gov/stations/{sid}/observations/latest",
                         headers=ua, timeout=30).json()["properties"]
        c = (o.get("temperature") or {}).get("value")
        return {"tempF": round(c * 9 / 5 + 32) if c is not None else None,
                "cond": o.get("textDescription") or "",
                "time": o.get("timestamp") or ""}
    except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
        log(f"  obs fetch failed ({lat},{lon}): {exc}")
        return None


def _fmt_dt(iso_utc: str, tzname: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(tzname))
        return f"{dt.strftime('%b')} {dt.day} at {dt.strftime('%I:%M %p').lstrip('0')} {dt.strftime('%Z')}"
    except (ValueError, AttributeError):
        return iso_utc or ""


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def process_weather_conditions(cfg, fb, state, *, seed):
    locs = cfg.get("weather_locations", {})
    posts = cfg.get("weather_condition_posts", [])
    if not posts:
        return
    wx = state.setdefault("wx", {})
    obs = {n: fetch_current_obs(l["lat"], l["lon"]) for n, l in locs.items()}

    for p in posts:
        loc = locs[p["loc"]]
        tz = loc.get("tz", "America/Chicago")
        page = loc["page"]
        st = wx.setdefault(p["loc"], {})
        o = obs.get(p["loc"])
        trig = p["trigger"]
        fire, fields = False, {}

        if trig.startswith("condition:"):
            want = trig.split(":", 1)[1].lower()
            if o and o["cond"]:
                if want in o["cond"].lower() and want not in st.get("cond", ""):
                    fire = True
                fields = {"time": _fmt_dt(o["time"], tz), "condition": o["cond"]}
        elif trig.startswith("temp_above:"):
            t = float(trig.split(":", 1)[1])
            if o and o["tempF"] is not None:
                prev = st.get("temp")
                if o["tempF"] > t and (prev is None or prev <= t):
                    fire = True
                fields = {"time": _fmt_dt(o["time"], tz), "tempF": o["tempF"]}
        elif trig.startswith("temp_below:"):
            t = float(trig.split(":", 1)[1])
            if o and o["tempF"] is not None:
                prev = st.get("temp")
                if o["tempF"] < t and (prev is None or prev >= t):
                    fire = True
                fields = {"time": _fmt_dt(o["time"], tz), "tempF": o["tempF"]}
        elif trig == "sunset":
            now = datetime.now(ZoneInfo(tz))
            today = now.date()
            s = sun(LocationInfo(latitude=loc["lat"], longitude=loc["lon"]).observer,
                    date=today, tzinfo=ZoneInfo(tz))
            if now >= s["sunset"] and st.get("sun_date") != today.isoformat():
                fire = True
                st["sun_date"] = today.isoformat()
                s2 = sun(LocationInfo(latitude=loc["lat"], longitude=loc["lon"]).observer,
                         date=today + timedelta(days=1), tzinfo=ZoneInfo(tz))
                fields = {"sunset": _clock(s["sunset"]), "sunrise": _clock(s2["sunrise"])}

        if fire and not seed:
            msg = p["template"]
            for k, v in fields.items():
                msg = msg.replace("{" + k + "}", str(v))
            log(f"[wx] {p['loc']} {trig} -> posting to {page}")
            fb.post(page, msg, None)

    # update baselines AFTER all triggers are evaluated (so edges compare to prior)
    for name, o in obs.items():
        if o:
            st = wx.setdefault(name, {})
            if o["cond"]:
                st["cond"] = o["cond"].lower()
            if o["tempF"] is not None:
                st["temp"] = o["tempF"]
    if seed:   # don't let the first live run dump a backlog sunset
        for name, loc in locs.items():
            tz = loc.get("tz", "America/Chicago")
            wx.setdefault(name, {})["sun_date"] = datetime.now(ZoneInfo(tz)).date().isoformat()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="mark current items as seen without posting (first run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be posted; do not call Facebook")
    ap.add_argument("--selftest", action="store_true",
                    help="post one UNPUBLISHED draft to each page to verify tokens")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()
    attach_image = bool(cfg.get("defaults", {}).get("attach_image", True))
    fb = Facebook(cfg["pages"], dry_run=args.dry_run)

    if args.selftest:
        log("SELF-TEST — creating one unpublished draft post per page.")
        all_ok = True
        for pk in cfg["pages"]:
            if not fb.selftest(pk):
                all_ok = False
        log("Self-test PASSED." if all_ok else "Self-test had FAILURES (see above).")
        return 0 if all_ok else 1

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

    # Group E — weather-condition & sunrise/sunset posts
    process_weather_conditions(cfg, fb, state, seed=args.seed)

    if args.dry_run:
        log("Done (dry-run — state NOT saved).")
    else:
        save_state(state)
        log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
