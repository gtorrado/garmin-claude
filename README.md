# Garmin → Claude

A small server that syncs your Garmin Connect data into a private history database
and lets Claude (web + phone app) read it as a **custom connector**.

Ask things like *"How recovered am I today?"*, *"Was yesterday's run too hard given my HRV?"*,
*"How has my sleep trended this month?"*.

## What Claude can see
| Tool | What it returns |
|---|---|
| `get_coach_briefing` | Today + last night, 7d vs 28d baselines, last 5 workouts |
| `get_daily_metrics` | Steps, resting HR, stress, Body Battery, sleep stages/score, HRV, training readiness, training status/load, VO2 max, weight |
| `get_trends` | Any metric day-by-day over up to 2 years, with avg/min/max |
| `get_recent_activities` | Workouts: distance, pace, HR, zones, training effect, load |
| `get_activity_details` | Per-lap splits and HR-zone time for one workout |
| `sync_now` / `sync_day` | Pull fresh data from Garmin on demand |
| `get_raw_garmin_data` | Raw Garmin JSON for anything not in the summaries |

It syncs automatically every hour and backfills 90 days of history on first start.

## Setup (≈15 minutes)

### 1. Get your Garmin login token (on your computer, once)
Garmin uses 2FA, so you log in once locally and give the server only the resulting token.
```bash
pip install garminconnect==0.3.20
python login.py
```
Copy the long `{"di_token": ...}` line it prints.

### 2. Deploy to Railway (or any Docker host)
1. Push this folder to a **private** GitHub repo.
2. On [railway.com](https://railway.com): **New Project → Deploy from GitHub repo** → pick it.
3. On the service: **Settings → Volumes → Add volume**, mount path `/data`.
4. **Variables**:
   | Name | Value |
   |---|---|
   | `GARMIN_TOKENS` | the line from step 1 |
   | `MCP_SECRET` | a long random string, e.g. output of `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
   | `TZ_NAME` | your timezone, e.g. `Europe/Madrid` |
5. **Settings → Networking → Generate Domain**.
6. Check `https://<your-domain>/health` returns `{"ok": true, ...}`.

### 3. Add it to Claude
claude.ai → **Settings → Connectors → Add custom connector**
- Name: `Garmin`
- URL: `https://<your-domain>/mcp/<MCP_SECRET>`

It then shows up in the browser and the mobile app. Tip: create a Claude Project
("Training coach") with instructions like *"Always check my Garmin data before answering."*

## Optional settings
| Variable | Default | |
|---|---|---|
| `SYNC_INTERVAL_MIN` | `60` | How often to refresh |
| `BACKFILL_DAYS` | `90` | History pulled on first start (more = slower first sync) |
| `GARMIN_CALL_DELAY` | `0.4` | Pause between Garmin requests |

## Security notes
- The connector URL contains your secret — treat it like a password. Anyone with it can read your data.
- The token in `/data` gives access to your Garmin account. Changing your Garmin password revokes it.
- Uses the unofficial `garminconnect` library; if Garmin changes their API, bump its version in `requirements.txt`.

## Run locally
```bash
pip install -r requirements.txt
GARMIN_TOKENS='...' MCP_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))") TZ_NAME=Europe/Madrid python server.py
```
