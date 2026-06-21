# FCWXTH Poster

Self-hosted replacement for the IFTTT "RSS → Facebook Page" applets that feed
**Franklin County Weather with Tim Haithcock (FCWXTH)** and **Paden Fire & Rescue (PVFD)**.

**RSS.app is still used** as the source for the agency-page feeds — this project only
replaces IFTTT (the part that posts to Facebook).

## What it does (Phase 1 — 42 of the 54 applets)

- **A.** Reposts 12 RSS.app agency feeds (NWS offices, NASA, USGS, SPC, Ready.gov, etc.)
- **B.** NHC Atlantic tropical cyclones (official feed)
- **C.** 4 IEM office feeds, keyword-filtered by your county names
- **D.** 25 per-county NWS watch/warning feeds — **consolidated**: one alert that covers
  many counties becomes a **single post** listing all of them (fixes the 27-message flood)

Two things IFTTT couldn't do, now fixed:
- **One post per event** instead of one per county.
- **Photos/maps attached** to posts (IFTTT's status-message action was text-only).

Phase 2 (later): the weather-condition/sunrise posts, the MagicLight bulbs, and the push
notification. See `feeds.yaml` Groups E & F.

## Files
| File | Purpose |
|---|---|
| `feeds.yaml` | All feeds, templates, target pages (edit this to change behavior) |
| `post.py` | The poster |
| `state.json` | Auto-created; remembers what was already posted |
| `.github/workflows/run.yml` | Runs every ~10 min on GitHub Actions (free) |

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
2. Add the two secrets (step 2).
3. The workflow runs automatically every ~10 min; it commits `state.json` back so it
   remembers across runs. Trigger a manual run anytime from the **Actions** tab.

---

## Changing things later
- Add/remove a feed or change wording → edit `feeds.yaml`.
- Change post frequency → edit the `cron` in `.github/workflows/run.yml`.
- Turn off image attachment → set `defaults.attach_image: false` in `feeds.yaml`.

## Notes / honest caveats
- GitHub's scheduler fires ~every 5 min at best and can lag a few minutes under load.
- `state.json` is committed to the repo each run; that's normal and keeps it free.
- County consolidation reposts an alert if NWS issues an **updated** version (new alert id).
