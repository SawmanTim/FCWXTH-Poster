# FCWXTH Poster

Self-hosted replacement for the IFTTT "RSS → Facebook Page" applets that feed
**Franklin County Weather with Tim Haithcock (FCWXTH)** and **Paden Fire & Rescue (PVFD)**.

**RSS.app is still used** as the source for the agency-page feeds — this project only
replaces IFTTT (the part that posts to Facebook).

## What it does (replaced all 54 applets)

- **A.** Reposts 10 RSS.app agency feeds (NWS offices, SPC, NASA, Ready.gov, etc.)
- **B.** NHC Atlantic tropical cyclones (official feed) + USGS significant earthquakes
  with ShakeMap images
- **C.** IEM office feeds — *disabled* (duplicated Group D; rename the key in
  `feeds.yaml` to re-enable)
- **D.** 27 per-county NWS watch/warning zones — **consolidated**: one alert that covers
  many counties becomes a **single post** listing all of them (fixes the 27-message flood)
- **E.** Weather-condition and sunset posts (NWS observations + astral sun times)
- **G.** Community Service Alerts — state-wide non-weather emergencies (AMBER, Blue
  Alert, HazMat, 911 outages, …) for AL/TN/MS
- **Station** — personal WU station KALPHILC8: scheduled conditions graphic
  (`wx_card.py`), rain onset, and tiered heat/cold posts with hysteresis
- **Extras** — NASA APOD, SWPC space weather, weekly Drought Monitor, SPC
  mesoscale discussions

Two things IFTTT couldn't do, now fixed:
- **One post per event** instead of one per county.
- **Photos/maps attached** to posts (IFTTT's status-message action was text-only).

## Files
| File | Purpose |
|---|---|
| `feeds.yaml` | All feeds, templates, target pages (edit this to change behavior) |
| `post.py` | The poster |
| `state.json` | Auto-created; remembers what was already posted |
| `.github/workflows/run.yml` | GitHub Actions: ~60-second polling loop, restarted by cron (free) |

---

## Setup

### 1. Get a long-lived Facebook Page token (one per page)

You're an admin of both pages, so you can do this **without Facebook App Review**:

1. Go to **developers.facebook.com** → *My Apps* → *Create App* → type **"Other" / "Business"**.
2. In the app, add the **Facebook Login** product (or just use the Graph API Explorer).
3. Open the **Graph API Explorer**, select your app, and grant these permissions:
   `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`.
4. Generate a **User token**, then exchange it for a **long-lived** token, then call
   `GET /me/accounts` — the response lists each Page with its own **`access_token`**.
   Those Page tokens (derived from a long-lived user token) are effectively long-lived.
   - Page **FCWXTH** id = `103994271809938`
   - Page **PVFD** id = `254298421099827`
   > ⚠️ Tokens are sensitive — handle them yourself, never paste them in chat.
   > Re-check them every ~2 months in case Facebook expires them.

### 2. Put the tokens where the script can read them

- **Local test:** `cp .env.example .env` and paste the tokens, then
  `set -a; . ./.env; set +a` (Git Bash) before running.
- **GitHub (production):** repo → **Settings → Secrets and variables → Actions → New secret**:
  - `FB_TOKEN_FCWXTH`
  - `FB_TOKEN_PVFD`

### 3. First run — SEED so you don't blast the backlog

```bash
pip install -r requirements.txt
python post.py --seed      # records all current items as "seen", posts NOTHING
```

Then a real dry-run to preview:
```bash
python post.py --dry-run   # shows what it WOULD post
```

### 4. Go live on GitHub Actions

1. Create a new GitHub repo and push this folder.
2. Add the secrets (step 2) — plus `NASA_API_KEY` and `WU_API_KEY`.
3. The workflow polls every ~60 seconds (one long job, restarted by the cron); it
   commits `state.json` back whenever it changes so it remembers across runs.
   Pushing a code/config change to `main` restarts the loop immediately.
   Trigger a manual run anytime from the **Actions** tab.

---

## Changing things later
- Add/remove a feed or change wording → edit `feeds.yaml` (goes live on push).
- Change post frequency → edit the `sleep` in `.github/workflows/run.yml`.
- Turn off image attachment → set `defaults.attach_image: false` in `feeds.yaml`.

## Notes / honest caveats
- GitHub's cron is best-effort (can lag to ~hourly); the long polling job rides
  through the gaps, so effective polling stays ~60s.
- `state.json` is committed back whenever it changes; that's normal and keeps it free.
- County consolidation reposts an alert if NWS issues an **updated** version (new alert id).
- If any cycle fails to post (e.g. an expired Facebook token), two alarms fire:
  **instantly**, `alert.py` opens a `poster-alert` issue that @mentions you (GitHub
  Mobile pushes that to your phone within ~a minute); and at the end of the window
  the job ends **failed** so GitHub also emails you. Fix the token, close the issue.
  Test the phone alarm anytime: Actions → FCWXTH Poster → Run workflow → check
  "Send a TEST phone alert".
