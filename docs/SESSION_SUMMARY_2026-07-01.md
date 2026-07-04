# FCWXTH Poster — Session Summary (2026-06-30 → 2026-07-01)

Handoff doc for the next session. Everything below is **LIVE** on
`github.com/SawmanTim/FCWXTH-Poster` (main) unless noted. Local working copy:
`C:\Users\Sawma\Desktop\Claude Code\fcwxth-poster`.

---

## What we shipped this session (in order)

| # | Change | Commit | Notes |
|---|--------|--------|-------|
| 1 | **Drought Monitor → AL + MS + TN** | `2714736` | Was AL-only. `usdm_drought` config is now a `states` list; one post + state map each, Thursdays. |
| 2 | **Extreme-temp thresholds tuned** | `0417123` | Heat 99→100°F; cold 32→20°F w/ drip-faucets tip. |
| 3 | **Restored 32° as a 2nd cold tier** | `c7952d8` | Two tiers: Freezing (32) + (then) Dangerous Cold (20). |
| 4 | **Personal weather-station integration** | `6becd66` | The big one — see below. |
| 5 | **Extreme Heat rename + liability disclaimer** | `d106e72` | "Excessive"→"Extreme" Heat Warning; observation framing + disclaimer. |
| 6 | **Hashtag order: #THWX always LAST** | `8cad177` | `#FCWXTH #THWX` / `#PVFRD #THWX`. Single source = `FOOTERS` in post.py. |
| 7 | **Hourly card redesign (GOLD STANDARD)** | `afd1886` | Rebuilt `wx_card.py` to the design spec. User-approved. |
| 8 | **Design spec added to repo** | `c766ed4` | `docs/FCWXTH_Current_Conditions_Graphic_Design_Specification.md`. |

---

## The personal weather station (biggest feature)

**Source:** Tim's own Weather Underground PWS **`KALPHILC8`** (AcuRite, at his house
in Phil Campbell, Franklin Co. AL — 34.37, -87.81, elev 910 ft). It is now the source
for **all Franklin County posts**, each attributed to the station.

- **Secret:** `WU_API_KEY` (free WU PWS key) — in GitHub Actions secrets + passed in
  `run.yml` env. `run.yml` also `apt-get install`s `fonts-dejavu-core` for the card.
- **API:** `api.weather.com/v2/pws/observations/current?stationId=KALPHILC8&units=e&numericPrecision=decimal&apiKey=…`
  Fields: `imperial.{temp,heatIndex,windChill,dewpt,windSpeed,windGust,pressure,precipRate,precipTotal}`
  + top-level `humidity`, `winddir` (deg), `uv` (null — no sensor).
- **Diagnostic:** `wu_test.py` (standalone; reads key from env).

**What it posts (all → FCWXTH):**
1. **Hourly conditions card** — on the hour (`now.minute<2` + `state['station']['card_hour']`
   dedupe). Shows temp, feels-like, wind/gust, dewpoint, humidity, pressure, precip
   rate/accum, and **today's high/low** (tracked from the 60s polling — resets local
   midnight, no extra API calls).
2. **Rain onset** — `precipRate>0` edge. (Snow stays on NWS — a gauge can't type precip.)
3. **NWS-Huntsville reinforcing alerts** (see thresholds below) with community-service
   safety messages, worded as observations + a liability disclaimer.
4. **Local milestones:** Freezing (32°) + Dangerous Cold (20°).

**HUN alert thresholds (in `feeds.yaml` → `station.messages`):**
- Heat Advisory: heat index 105–109 **or** temp 100–104
- **Extreme Heat Warning**: heat index ≥110 **or** temp ≥105 (renamed from "Excessive
  Heat Warning" per NWS national rename, Mar 2025 — this is what HUN issues now)
- Cold Weather Advisory: temp/feels ≤ +5°F
- Extreme Cold Warning: temp/feels ≤ −6°F
- (Franklin 100° milestone was RETIRED — Heat Advisory supersedes it. 20° "Extreme
  Cold" was RENAMED to "Dangerous Cold" so it doesn't clash with the −6° official.)

**Liability (important):** every alert is worded as an OBSERVATION — *"our station is
reading X, which meets the NWS ___ level"* — and carries a `disclaimer`: *"NOT AN
OFFICIAL ALERT. Official watches, warnings, and advisories are issued ONLY by the
National Weather Service (weather.gov)…"* Tim is **reporting his station readings,
not issuing warnings.** Disclaimer is on alerts only, not the card.

**Deploy safety:** `process_station` soft-seeds on first deploy (no prior
`state['station']['heat']`) so an in-progress heat/cold event isn't back-posted — but
the hourly card still posts.

---

## The hourly card — GOLD STANDARD (approved 2026-07-01)

`wx_card.py` (Pillow) renders **exactly** to
`docs/FCWXTH_Current_Conditions_Graphic_Design_Specification.md`.
**Follow that spec file LITERALLY — do NOT "improve"/override its hex values.**

- 1920×1080 (16:9); bg `#111417`→`#101214` subtle gradient
- **GOLD `#F4B321`** = title, temperature, degree symbol, separator bullet
- **BLUE `#2F8CFF`** = "CURRENT CONDITIONS", "Feels Like" label
- **WHITE** = weather values, wind speed, ALL footer text + station name
- **LIGHT-GRAY `#C8CDD3`** = card labels + city
- **`#34475A`** dividers (78% opacity, 2px) + card borders (1px)
- **`#1D2835`** card backgrounds, rounded, 24–32px padding
- soft ~15% gold glow on the temperature

⚠️ History note: user said "make it black not blue" once; I overrode to charcoal;
he then reverted and said follow the MD EXACTLY. **The navy `#1D2835`/`#34475A` ARE
intended** — ship the spec's literal colors.

---

## Current post-type inventory (~28 total)

10 RSS.app agency feeds · NHC tropical · USGS quakes · county NWS warnings (27
counties consolidated) · community/emergency alerts · NASA APOD · space weather ·
drought (AL/MS/TN) · SPC mesoscale · Paden conditions (NWS) · Franklin snow+sunset
(NWS) · **station (8): hourly card, rain, Heat Advisory, Extreme Heat Warning, Cold
Weather Advisory, Extreme Cold Warning, Freezing, Dangerous Cold.**

---

## Hosting / how it runs (unchanged)

`run.yml` = one long GitHub Actions job that loops `post.py` every 60s for ~5.9h;
`*/30` cron restarts it (concurrency cancel-in-progress). `state.json` committed back
each cycle. **After any push, the runner picks up new code within ~30 min** (the
`*/30` restart), and the hourly card posts at the top of the next hour.

---

## Open items / things to watch

- Confirm the new-design card + #THWX-last hashtags look right on the live page over
  the next hour or two (nothing else pending).
- Volume: hourly card = 24/day. If overnight (2–5 AM) engagement is dead in Insights,
  a **daytime-only (6a–10p)** cap is a one-line change (was offered, user chose 24/day).
- Earlier heat posts (before commit `d106e72`) may say "Excessive Heat Warning" without
  the disclaimer — user may want to delete/edit those. (Not yet verified on-page.)

---

## Tooling / access notes (lessons for next time)

- **Monitor mapping:** "monitor N" = Windows **Display settings** numbering.
  **Monitor 1 = display "PLL2410W"** (confirmed). Go by the Windows display NAME.
- **Chrome via computer-use is READ-ONLY** (can screenshot, can't scroll/click). For
  browser interaction use the Claude-in-Chrome extension — but in this environment the
  extension was very flaky (tabs vanished between calls, logged-out tabs, login walls).
  **Don't rabbit-hole on browser access** — state the limit and move on (see the
  `avoid-rabbit-holes-state-limits` memory). Pasting a screenshot into chat is the
  reliable fallback.
- **WU_API_KEY** lives ONLY in GitHub Secrets. For local testing the user drops it in a
  `*.env` file (gitignored via `*.env`); delete it after and never commit it.
- **Windows/LibreOffice:** no LibreOffice here; verify formulas/renders manually.
- **This machine:** i5-6400, 12 GB RAM (16 GB max) is the bottleneck; Freedom Fiber
  gigabit — internet is a STRENGTH, never flag it.
