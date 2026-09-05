# FCWXTH Poster — working notes for Claude

Self-hosted replacement for 54 IFTTT "RSS → Facebook Page" applets, feeding two
Facebook pages: **FCWXTH** (Franklin County Weather with Tim Haithcock) and
**PVFD** (Paden Fire & Rescue). RSS.app is still the source for agency feeds;
this project replaced only the posting half.

Read `README.md` first — it covers setup, tokens and the honest caveats. This
file is the orientation a fresh session needs before touching anything.

## Shape of the thing

| File | Purpose |
|---|---|
| `feeds.yaml` | **All** feeds, templates, thresholds, target pages. Behaviour changes belong here, not in code. |
| `post.py` | The poster. One `step()` per source, each isolated so one broken source can't abort the rest. |
| `wx_card.py` | Renders the conditions graphic (PIL). |
| `acurite.py` | My AcuRite client — station battery + RF signal. **Unofficial API.** |
| `alert.py` | Opens an @mention GitHub issue = instant phone alarm via GitHub Mobile. |
| `state.json` | What's already been posted. Committed back every cycle; never hand-edit. |
| `.github/workflows/run.yml` | One ~5.9h job polling every 60s, which queues its own successor. |

Diagnostics: `wu_test.py` (Weather Underground pull), `acurite_test.py`
(AcuRite login + health; `--raw` dumps the payload).

## Things that will bite you

- **The cron is a backstop, not the driver.** Each job dispatches its successor
  up front. GitHub's scheduler gets throttled and left real multi-hour holes —
  that history is in the `run.yml` comments. Don't "simplify" it back.
- **`state.json` is committed every cycle**, so `git log` on it is a minute-by-minute
  record of what the poster saw. Genuinely useful for forensics. It also means
  the branch is almost always behind `main` by a few commits.
- **Never post an unverified reading.** Everything downstream stamps posts with
  the wall clock, not the observation time, so stale data becomes a *lie* on a
  public weather-safety page. `fetch_wu_station` enforces a staleness limit.
- **Alarms dedupe by label**, one per label: `poster-alert` (posting failure),
  `station-offline`, `station-battery`, `station-signal`. Never share a label
  between unrelated failures — an open issue silences the others.
- **"No data" is not "all clear."** `_heat_level_hyst` won't let a blank sample
  de-arm a heat/cold tier, and `_aggregate` won't let an unreadable sensor value
  count as a recovery. Preserve that three-state logic in anything you add.
- **Health checks must never break posting.** `process_station_health` swallows
  its own exceptions so `step()` can't count them as posting failures and fire
  the Facebook alarm for an unrelated cause.
- **Secrets:** `scrub_secrets()` masks query-string keys, JSON password/token
  fields, and the literal value of every var in `_SECRET_ENV`. Route anything
  loggable through it. Never print a credential, never commit one.

## The station (KALPHILC8)

An **AcuRite 5-in-1** in Phil Campbell, AL, with two independent failure points:

- **the sensor** — 4× AA, RF link to the hub. Dies as: low battery → weak signal → silence.
- **the AcuRite Access/smartHUB** — mains + WiFi, uploads to both My AcuRite *and* Weather Underground.

WU's PWS API carries weather fields only — no battery, no signal, no
last-check-in — so WU can only ever show *silence*. `acurite.py` is what tells
the two apart. It uses the endpoints the myacurite.com dashboard calls, because
**AcuRite publishes no public API**; assume it will break, keep every failure
non-fatal, and keep parsing the JSON by *searching* for battery/signal fields
rather than by a fixed path.

AcuRite's own System Alerts (low battery, loss of signal, communication loss)
are the officially supported path and should be on as well — delivered by
**email or app push, never text**. The carriers shut down the email-to-SMS
gateways those texts ride on (Sprint 2022, T-Mobile late 2024, AT&T June 2025,
Verizon phasing out through March 2027), so SMS alerts are silently dropped.

## Conventions

- Comments explain **why**, especially where the code looks odd — most oddities
  here are scar tissue from a real incident, and the comment names it. Match that.
- Validate before pushing: `python -m py_compile post.py acurite.py`,
  `python post.py --dry-run` (exit 0, `state.json` unmodified), plus a targeted
  test of the logic you touched against mocked payloads.
- Config over code: a new threshold goes in `feeds.yaml` with a comment saying
  why that number.

## Session continuity

Claude Code sessions do **not** share memory — not across devices, and not with
the regular Claude chat app, which cannot see Claude Code sessions at all. To
resume specific work, reopen that session from claude.ai/code (or `/artifacts`
→ sessions list), rather than starting a new one and re-explaining.

This file plus the PR description is what a cold session should read to catch up.
