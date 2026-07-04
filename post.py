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

    def post_bytes(self, page_key: str, message: str, image_bytes: bytes,
                   filename: str = "card.png") -> bool:
        """Post a locally-generated image (raw PNG bytes) with a caption."""
        page_id = self.page_ids[page_key]
        token = self._token(page_key)
        if self.dry_run or not token:
            tag = "DRY-RUN" if self.dry_run else "NO-TOKEN(skipped)"
            log(f"  [{tag}] -> {page_key}: {message[:80]!r} (+generated image)")
            return self.dry_run
        try:
            r = requests.post(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos",
                data={"caption": message, "access_token": token},
                files={"source": (filename, image_bytes, "image/png")},
                timeout=60)
            if r.status_code >= 400:
                log(f"  ERROR posting image to {page_key}: {r.status_code} {r.text[:200]}")
                return False
            log(f"  posted image to {page_key} (id={r.json().get('id', '?')})")
            return True
        except requests.RequestException as exc:
            log(f"  ERROR posting image to {page_key}: {exc}")
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
            footer = FOOTERS.get(pk, "")
            msg = f"{base}\n\n{footer}" if footer else base
            fb.post(pk, msg, img)

    if new_ids:
        state[key] = list(seen.union(new_ids))


def zone_from_url(u: str) -> str:
    # affectedZones look like ".../zones/county/ALC059" or ".../zones/forecast/ALZ001"
    m = re.search(r"([A-Z]{2}[CZ]\d{3})\s*$", u or "")
    return m.group(1) if m else ""


# GOLD-STANDARD footer appended to EVERY post, per target page:
#     <our sign-off>
#     (blank line)
#     <our hashtags>
# Any hashtags from an outside source stay in the post body, ABOVE this footer.
FOOTERS = {
    "FCWXTH": 'Provided by your local "Franklin County Weather Staff"\n#FCWXTH #THWX',
    "PVFD":   'Provided by your local "Paden Fire & Rescue Department Staff"\n#PVFRD #THWX',
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
    parts.append(FOOTERS.get(page_key, FOOTERS["FCWXTH"]))
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
# Group G — Community Service Alerts (state-wide non-weather emergencies)
# --------------------------------------------------------------------------- #
EVENT_LABELS = {
    "child abduction emergency": "AMBER ALERT",
}


def build_community_message(props: dict, page_key: str) -> str:
    event = props.get("event", "Emergency Alert")
    label = EVENT_LABELS.get(event.lower(), event.upper())
    headline = (props.get("headline") or "").strip()
    desc = strip_html(props.get("description", ""))
    instruction = strip_html(props.get("instruction") or "")
    area = (props.get("areaDesc") or "").strip()
    parts = ["***COMMUNITY SERVICE ALERT***", f"*** {label} ***"]
    if headline:
        parts.append(headline)
    if area:
        parts.append("Area: " + area)
    if desc:
        parts.append(desc)
    if instruction:
        parts.append(instruction)
    parts.append(FOOTERS.get(page_key, FOOTERS["FCWXTH"]))
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(p for p in parts if p)).strip()


def process_community_alerts(cfg, fb, state, *, seed):
    csa = cfg.get("community_service_alerts")
    if not csa:
        return
    events = {e.lower() for e in csa.get("events", [])}
    pages = csa.get("pages", [])
    key = "community_alerts"
    seen = set(state.get(key, []))
    ua = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}

    features = []
    for stt in csa.get("states", []):
        try:
            r = requests.get(ALERTS_API, params={"area": stt}, headers=ua, timeout=40)
            r.raise_for_status()
            features += r.json().get("features", [])
        except (requests.RequestException, ValueError) as exc:
            log(f"[community_alerts] {stt} fetch failed: {exc}")

    new_ids, handled = [], set()
    for feat in features:
        aid = feat.get("id", "")
        props = feat.get("properties", {})
        if (props.get("event") or "").lower() not in events:
            continue                      # weather / non-emergency -> skip
        if not aid or aid in seen or aid in handled:
            continue                      # dedupe (alerts can span states)
        new_ids.append(aid)
        handled.add(aid)
        if seed:
            continue
        log(f"[community_alerts] {props.get('event','alert')} -> pages={pages}")
        for pk in pages:
            fb.post(pk, build_community_message(props, pk), None)

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


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _deg_to_compass(deg) -> str:
    try:
        return _COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]
    except (TypeError, ValueError):
        return ""


def fetch_wu_station(station_id: str) -> dict | None:
    """Current observation from a Weather Underground PWS. Needs env WU_API_KEY.
    Returns a flat dict (imperial units) or None on any failure."""
    key = os.environ.get("WU_API_KEY")
    if not key:
        log("  [wu] WU_API_KEY not set — skipping station pull")
        return None
    url = "https://api.weather.com/v2/pws/observations/current"
    params = {"stationId": station_id, "format": "json", "units": "e",
              "numericPrecision": "decimal", "apiKey": key}
    try:
        r = requests.get(url, params=params, timeout=30,
                         headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            log(f"  [wu] {station_id} HTTP {r.status_code}: {r.text[:120]}")
            return None
        obs = (r.json().get("observations") or [None])[0]
        if not obs:
            log(f"  [wu] {station_id} returned no observations")
            return None
        imp = obs.get("imperial", {})
        return {
            "tempF": imp.get("temp"),
            "heatIndexF": imp.get("heatIndex"),
            "windChillF": imp.get("windChill"),
            "dewF": imp.get("dewpt"),
            "humidity": obs.get("humidity"),
            "wind_dir": _deg_to_compass(obs.get("winddir")),
            "wind_mph": imp.get("windSpeed"),
            "gust_mph": imp.get("windGust"),
            "pressure_in": imp.get("pressure"),
            "precip_rate": imp.get("precipRate"),
            "precip_accum": imp.get("precipTotal"),
            "uv": obs.get("uv"),
            "obs_local": obs.get("obsTimeLocal") or "",
        }
    except (requests.RequestException, ValueError, KeyError) as exc:
        log(f"  [wu] {station_id} fetch failed: {exc}")
        return None


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
            footer = FOOTERS.get(page, "")
            if footer:
                msg = f"{msg}\n\n{footer}"
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
# Personal weather station (Weather Underground PWS) — hourly card, rain,
# and NWS-Huntsville heat/cold reinforcing alerts. Source: feeds.yaml `station`.
# --------------------------------------------------------------------------- #
def _num(v) -> str:
    if v is None:
        return "--"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _station_post(fb, page, body, attribution, image_bytes=None, disclaimer="") -> None:
    footer = FOOTERS.get(page, FOOTERS["FCWXTH"])
    msg = "\n\n".join(p for p in (body, attribution, disclaimer, footer) if p)
    if image_bytes is not None:
        fb.post_bytes(page, msg, image_bytes)
    else:
        fb.post(page, msg, None)


def _heat_level(temp, hi) -> int:
    """0 none, 1 Heat Advisory, 2 Excessive Heat Warning (NWS Huntsville)."""
    if temp is None:
        return 0
    h = hi if hi is not None else temp
    if temp >= 105 or h >= 110:
        return 2
    if temp >= 100 or h >= 105:
        return 1
    return 0


def _cold_level(temp, wc) -> int:
    """0 none, 1 Freezing(32), 2 Dangerous Cold(20), 3 Cold Weather Advisory(+5),
    4 Extreme Cold Warning(-6). +5/-6 use apparent temp; 32/20 use air temp."""
    if temp is None:
        return 0
    feels = min(temp, wc) if wc is not None else temp
    if feels <= -6:
        return 4
    if feels <= 5:
        return 3
    if temp < 20:
        return 2
    if temp < 32:
        return 1
    return 0


def _heat_level_hyst(temp, hi, prev, buf):
    """Hysteresis (Schmitt trigger): a tier is ENTERED at the NWS threshold, but
    only DE-ARMS after the reading falls `buf`°F below that threshold. Brief dips
    and threshold flapping therefore never re-post the same category — only a real
    cooldown (out of the tier by more than buf) re-arms it for a genuine new event."""
    if temp is None:
        return 0
    raw = _heat_level(temp, hi)
    if raw >= prev:
        return raw
    # Falling below entry: hold the current tier until we clear entry-minus-buffer.
    h = hi if hi is not None else temp
    lvl = 0
    if temp >= 100 - buf or h >= 105 - buf:
        lvl = 1
    if temp >= 105 - buf or h >= 110 - buf:
        lvl = 2
    return min(prev, lvl)


def _cold_level_hyst(temp, wc, prev, buf):
    """Cold-side hysteresis: a tier de-arms only after the reading rises `buf`°F
    above its entry threshold, so a brief warm-up doesn't re-post the same tier."""
    if temp is None:
        return 0
    raw = _cold_level(temp, wc)
    if raw >= prev:
        return raw
    feels = min(temp, wc) if wc is not None else temp
    lvl = 0
    if temp < 32 + buf:
        lvl = 1
    if temp < 20 + buf:
        lvl = 2
    if feels <= 5 + buf:
        lvl = 3
    if feels <= -6 + buf:
        lvl = 4
    return min(prev, lvl)


_HEAT_MSG = {1: "heat_advisory", 2: "heat_warning"}
_COLD_MSG = {1: "freezing", 2: "dangerous_cold", 3: "cold_advisory", 4: "extreme_cold"}


def process_station(cfg, fb, state, *, seed):
    sc = cfg.get("station")
    if not sc:
        return
    data = fetch_wu_station(sc["station_id"])
    if not data:
        return
    tz = ZoneInfo(sc.get("tz", "America/Chicago"))
    now = datetime.now(tz)
    page = sc.get("page", "FCWXTH")
    loc = sc.get("location", "")
    attribution = sc.get("attribution", "")
    disc = sc.get("disclaimer", "")
    msgs = sc.get("messages", {})
    stt = state.setdefault("station", {})

    temp = data["tempF"]
    feels_hot, feels_cold = data["heatIndexF"], data["windChillF"]
    feels_label = "Feels Like"
    if temp is not None and feels_hot is not None and feels_hot - temp >= 1:
        feelsF = feels_hot
    elif temp is not None and feels_cold is not None and temp - feels_cold >= 1:
        feelsF, feels_label = feels_cold, "Wind Chill"
    else:
        feelsF = temp

    def fmt(key):
        return (msgs.get(key, "")
                .replace("{tempF}", _num(temp))
                .replace("{feelsF}", _num(feelsF))
                .replace("{location}", loc)
                .replace("{time}", _clock(now)))

    heat = _heat_level(temp, feels_hot)
    cold = _cold_level(temp, feels_cold)
    raining = (data["precip_rate"] or 0) > 0

    # Track today's high/low from the 60s polling — no extra API calls, and it
    # survives runner restarts because state.json is committed. Resets at local midnight.
    today = now.strftime("%Y-%m-%d")
    if temp is not None:
        if stt.get("hilo_date") != today:
            stt.update({"hilo_date": today, "hi": temp, "lo": temp})
        else:
            stt["hi"] = max(stt.get("hi", temp), temp)
            stt["lo"] = min(stt.get("lo", temp), temp)

    if seed:
        stt.update({"heat": heat, "cold": cold, "raining": raining,
                    "card_hour": now.strftime("%Y-%m-%dT%H")})
        return

    # First run after deploy (no prior baselines): establish them WITHOUT firing,
    # so an in-progress heat/cold event isn't back-posted. The card still posts.
    first = "heat" not in stt

    # Hourly conditions card — on the hour (first loop cycle in the first 2 min).
    if sc.get("hourly_card") and now.minute < 2:
        hour_key = now.strftime("%Y-%m-%dT%H")
        if stt.get("card_hour") != hour_key:
            stt["card_hour"] = hour_key
            try:
                import wx_card
                cd = dict(data)
                cd["feelsF"], cd["feels_label"] = feelsF, feels_label
                cd["as_of"] = (f"As of {_clock(now)} {now.strftime('%Z')} · "
                               f"{now.strftime('%b')} {now.day}, {now.year}")
                if cd.get("humidity") is not None:
                    cd["humidity"] = int(round(float(cd["humidity"])))
                cd["high_today"] = round(stt["hi"]) if stt.get("hi") is not None else None
                cd["low_today"] = round(stt["lo"]) if stt.get("lo") is not None else None
                for k in ("precip_rate", "precip_accum"):
                    if isinstance(cd.get(k), (int, float)):
                        cd[k] = f"{cd[k]:.2f}"
                png = wx_card.render_conditions_card(cd)
                cap = (f"Current conditions from our weather station in {loc} — "
                       f"{_clock(now)} {now.strftime('%Z')}.")
                _station_post(fb, page, cap, attribution, image_bytes=png)
                log(f"[station] hourly card posted ({hour_key})")
            except Exception as exc:  # noqa: BLE001 — never let the card break the loop
                log(f"  [station] card render/post failed: {exc}")

    # Rain onset (edge: not raining -> raining)
    if sc.get("rain"):
        if not first and raining and not stt.get("raining"):
            _station_post(fb, page, fmt("rain"), attribution, disclaimer=disc)
            log("[station] rain onset posted")
        stt["raining"] = raining

    # Buffer (°F) a reading must clear past a threshold before that tier re-arms.
    buf = float(sc.get("hysteresis_f", 2.0))

    # Heat tiers — fire only on entering a higher level; hysteresis on de-arm so a
    # brief dip (pop-up storm, threshold flapping) can't re-post the same category.
    new_heat = _heat_level_hyst(temp, feels_hot, stt.get("heat", 0), buf)
    if not first and new_heat > stt.get("heat", 0):
        key = _HEAT_MSG.get(new_heat)
        if key and msgs.get(key):
            _station_post(fb, page, fmt(key), attribution, disclaimer=disc)
            log(f"[station] heat level {new_heat} ({key}) posted")
    stt["heat"] = new_heat

    # Cold tiers — same rule on the cold side.
    new_cold = _cold_level_hyst(temp, feels_cold, stt.get("cold", 0), buf)
    if not first and new_cold > stt.get("cold", 0):
        key = _COLD_MSG.get(new_cold)
        if key and msgs.get(key):
            _station_post(fb, page, fmt(key), attribution, disclaimer=disc)
            log(f"[station] cold level {new_cold} ({key}) posted")
    stt["cold"] = new_cold


# --------------------------------------------------------------------------- #
# USGS earthquakes — official feed + ShakeMap map image
# --------------------------------------------------------------------------- #
def _fmt_epoch_ms(ms, tzname: str) -> str:
    try:
        dt = datetime.fromtimestamp(ms / 1000, ZoneInfo(tzname))
        return f"{dt.strftime('%b')} {dt.day}, {dt.year} at {dt.strftime('%I:%M %p').lstrip('0')} {dt.strftime('%Z')}"
    except (TypeError, ValueError, OSError):
        return ""


def usgs_shakemap_image(detail_url: str) -> str | None:
    """The USGS ShakeMap intensity map (a real map of the quake), if available."""
    if not detail_url:
        return None
    try:
        det = requests.get(detail_url, headers={"User-Agent": USER_AGENT}, timeout=30).json()
        sm = det.get("properties", {}).get("products", {}).get("shakemap")
        if sm:
            c = sm[0].get("contents", {}).get("download/intensity.jpg")
            if c and c.get("url"):
                return c["url"]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    return None


def usgs_map_image(props: dict, coords: list) -> str | None:
    """Prefer the USGS ShakeMap; if it isn't generated yet (fresh quakes), fall
    back to a location map of the epicenter so the post always has a map."""
    img = usgs_shakemap_image(props.get("detail"))
    if img:
        return img
    if len(coords) >= 2:
        lon, lat = coords[0], coords[1]
        return f"https://maps.wikimedia.org/img/osm-intl,5,{lat},{lon},600x400.png"
    return None


def process_usgs_quakes(cfg, fb, state, *, seed):
    uq = cfg.get("usgs_quakes")
    if not uq:
        return
    pages = uq.get("pages", [])
    prefix = uq.get("prefix", "")
    key = "usgs_quakes"
    first_time = key not in state   # auto-seed first run so we don't dump the week's backlog
    seen = set(state.get(key, []))
    try:
        feats = requests.get(uq["feed"], headers={"User-Agent": USER_AGENT},
                             timeout=40).json().get("features", [])
    except (requests.RequestException, ValueError) as exc:
        log(f"[usgs] fetch failed: {exc}")
        return

    new_ids = []
    for f in feats:
        eid = f.get("id", "")
        if not eid or eid in seen:
            continue
        new_ids.append(eid)
        if seed or first_time:
            continue
        p = f.get("properties", {})
        coords = (f.get("geometry") or {}).get("coordinates") or []
        depth = coords[2] if len(coords) > 2 else None
        info = ["Notable earthquake — preliminary info:",
                f"M {p.get('mag')} — {p.get('place', '')}"]
        if depth is not None:
            info.append(f"Depth: {round(depth)} km")
        t = _fmt_epoch_ms(p.get("time"), "America/Chicago")
        if t:
            info.append(f"Time: {t}")
        if p.get("tsunami"):
            info.append("Tsunami: possible — see tsunami.gov")
        if p.get("url"):
            info.append(f"Details: {p['url']}")
        body = (f"{prefix}\n\n" + "\n".join(info)) if prefix else "\n".join(info)
        img = usgs_map_image(p, coords)
        log(f"[usgs] M{p.get('mag')} {p.get('place', '')[:40]} -> map={'yes' if img else 'no'}")
        for pk in pages:
            fb.post(pk, f"{body}\n\n{FOOTERS.get(pk, FOOTERS['FCWXTH'])}", img)

    if new_ids or first_time:
        state[key] = list(seen.union(new_ids))


# --------------------------------------------------------------------------- #
# Extras — NASA APOD, space weather, drought monitor, SPC mesoscale discussions
# --------------------------------------------------------------------------- #
def process_nasa_apod(cfg, fb, state, *, seed):
    """NASA Astronomy Picture of the Day — posts once per day."""
    c = cfg.get("nasa_apod")
    if not c:
        return
    if time.time() - state.get("apod_fetch_ts", 0) < 3000:   # throttle ~50 min (DEMO_KEY safe)
        return
    state["apod_fetch_ts"] = time.time()
    try:
        d = requests.get("https://api.nasa.gov/planetary/apod",
                         params={"api_key": os.environ.get("NASA_API_KEY", "DEMO_KEY"),
                                 "thumbs": "true"},
                         headers={"User-Agent": USER_AGENT}, timeout=30).json()
    except (requests.RequestException, ValueError) as exc:
        log(f"[apod] fetch failed: {exc}")
        return
    date = d.get("date", "")
    if not date or state.get("apod_date") == date:
        return
    if seed:
        state["apod_date"] = date
        return
    body = f"{c.get('prefix', 'NASA Astronomy Picture of the Day')}\n\n{d.get('title', '')}\n\n{d.get('explanation', '')}"
    if d.get("copyright"):
        body += f"\n\nImage credit: {d['copyright'].strip()}"
    if d.get("media_type") == "video" and d.get("url"):
        body += f"\n\nWatch: {d['url']}"
        img = d.get("thumbnail_url")
    else:
        img = d.get("url")   # standard size (hdurl can be too large for FB)
    state["apod_date"] = date
    log(f"[apod] {date}: {d.get('title', '')[:40]}")
    for pk in c["pages"]:
        fb.post(pk, f"{body}\n\n{FOOTERS.get(pk, FOOTERS['FCWXTH'])}", img)


def _clean_swpc(msg: str) -> str:
    lines = [ln.rstrip() for ln in msg.replace("\r", "").split("\n")]
    keep = [ln for ln in lines
            if not re.match(r"^(Space Weather Message Code|Serial Number|Issue Time):", ln.strip())]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def process_swpc(cfg, fb, state, *, seed):
    """NOAA SWPC space-weather alerts: geomagnetic storms (aurora) + strong blackouts."""
    c = cfg.get("swpc_space_weather")
    if not c:
        return
    key = "swpc"
    first_time = key not in state
    seen = set(state.get(key, []))
    try:
        alerts = requests.get("https://services.swpc.noaa.gov/products/alerts.json",
                              headers={"User-Agent": USER_AGENT}, timeout=30).json()
    except (requests.RequestException, ValueError) as exc:
        log(f"[swpc] fetch failed: {exc}")
        return
    new_ids = []
    for a in alerts:
        sid = f"{a.get('issue_datetime', '')}|{a.get('product_id', '')}"
        if sid in seen:
            continue
        low = a.get("message", "").lower()
        geo = ("geomagnetic storm category g" in low                       # predicted (watch/warning)
               or bool(re.search(r"geomagnetic k-index of [789]", low)))   # sudden strong storm (G3+)
        radio = bool(re.search(r"radio blackout.*?r[345]", low, re.S))
        solar = bool(re.search(r"solar radiation storm.*?s[345]", low, re.S))
        if not (geo or radio or solar):
            continue
        new_ids.append(sid)
        if seed or first_time:
            continue
        body = "***SPACE WEATHER ALERT***\n\n" + _clean_swpc(a.get("message", ""))
        img = c.get("aurora_image") if geo else None
        log(f"[swpc] {a.get('product_id')} -> posting (aurora={'y' if geo else 'n'})")
        for pk in c["pages"]:
            fb.post(pk, f"{body}\n\n{FOOTERS.get(pk, FOOTERS['FCWXTH'])}", img)
    if new_ids or first_time:
        state[key] = list(seen.union(new_ids))


def process_usdm(cfg, fb, state, *, seed):
    """U.S. Drought Monitor — once a week (released Thursdays)."""
    c = cfg.get("usdm_drought")
    if not c:
        return
    now = datetime.now(ZoneInfo("America/Chicago"))
    yw = now.strftime("%G-W%V")
    if state.get("usdm_week") == yw:
        return
    if seed:
        state["usdm_week"] = yw
        return
    if now.weekday() < 3:        # USDM releases Thursday (Mon=0..Sun=6); wait for the fresh map
        return
    state["usdm_week"] = yw
    # Each state gets its own post + map. Falls back to the old single-image form.
    states = c.get("states") or [{"name": "Alabama", "image": c.get("image")}]
    text_tmpl = c.get("text", "U.S. Drought Monitor — this week's update for {state}.")
    log(f"[usdm] weekly drought update {yw} ({len(states)} states)")
    for st in states:
        body = text_tmpl.format(state=st["name"])
        for pk in c["pages"]:
            fb.post(pk, f"{body}\n\n{FOOTERS.get(pk, FOOTERS['FCWXTH'])}", st.get("image"))


def _mentions_area(text: str, terms: list) -> bool:
    low = text.lower()
    for t in terms:
        t = t.lower()
        if len(t) <= 3:
            if re.search(r"\b" + re.escape(t) + r"\b", low):
                return True
        elif t in low:
            return True
    return False


def process_spc_md(cfg, fb, state, *, seed):
    """SPC Mesoscale Discussions that mention our area (pre-watch heads-up)."""
    c = cfg.get("spc_mesoscale")
    if not c:
        return
    key = "spc_md"
    first_time = key not in state
    seen = set(state.get(key, []))
    parsed = fetch_feed(c["feed"])
    if not parsed:
        return
    terms = c.get("match", [])
    new_ids = []
    for e in reversed(parsed.entries):
        uid = entry_uid(e)
        if not uid or uid in seen:
            continue
        new_ids.append(uid)
        if seed or first_time:
            continue
        title = strip_html(getattr(e, "title", ""))
        m = re.search(r"(\d{3,4})", title + " " + getattr(e, "link", ""))
        num = m.group(1) if m else None
        disc, page_html = strip_html(getattr(e, "summary", "")), ""
        if num:
            try:
                page_html = requests.get(f"https://www.spc.noaa.gov/products/md/md{num}.html",
                                         headers={"User-Agent": USER_AGENT}, timeout=20).text
                pm = re.search(r"<pre[^>]*>(.*?)</pre>", page_html, re.S | re.I)
                if pm:
                    disc = strip_html(pm.group(1))
            except requests.RequestException:
                pass
        if terms and not _mentions_area(disc, terms):
            continue        # not our area
        img = f"https://www.spc.noaa.gov/products/md/mcd{num}.png" if num else None
        body = f"{c.get('prefix', '***SPC MESOSCALE DISCUSSION***')}\n\n{disc[:1800]}"
        log(f"[spc_md] {title} -> posting (our area)")
        for pk in c["pages"]:
            fb.post(pk, f"{body}\n\n{FOOTERS.get(pk, FOOTERS['FCWXTH'])}", img)
    if new_ids or first_time:
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

    # USGS earthquakes (official feed + ShakeMap image)
    process_usgs_quakes(cfg, fb, state, seed=args.seed)

    # Group G — Community Service Alerts (state-wide non-weather emergencies)
    process_community_alerts(cfg, fb, state, seed=args.seed)

    # Group E — weather-condition & sunrise/sunset posts
    process_weather_conditions(cfg, fb, state, seed=args.seed)

    # Personal weather station (KALPHILC8) — hourly card + rain + heat/cold alerts
    process_station(cfg, fb, state, seed=args.seed)

    # Extras — NASA APOD, space weather, drought monitor, SPC mesoscale discussions
    process_nasa_apod(cfg, fb, state, seed=args.seed)
    process_swpc(cfg, fb, state, seed=args.seed)
    process_usdm(cfg, fb, state, seed=args.seed)
    process_spc_md(cfg, fb, state, seed=args.seed)

    if args.dry_run:
        log("Done (dry-run — state NOT saved).")
    else:
        save_state(state)
        log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
