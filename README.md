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
| `get_activity_details` | Everything for one workout: laps, HR & power zones, running dynamics, stamina, RPE/feel, training-effect messages, strength sets (exercise/reps/kg), weather, PRs |
| `sync_now` / `sync_day` | Pull fresh data from Garmin on demand |
| `get_raw_garmin_data` | Raw Garmin JSON for anything not in the summaries |

- **History:** backfills **2 years** on first start (takes a few hours in the background; recent data is available within minutes). Resumes where it left off after restarts.
- **After training:** checks for a new workout every 10 minutes and pulls it with full details as soon as your watch syncs.
- **Health metrics:** refreshed every hour.

## Setup (≈15 minutes)

### 1. Get your Garmin login token (on your computer, once)
Garmin uses 2FA, so you log in once locally and give the server only the resulting token.
```bash
pip install garminconnect==0.3.20
python login.py
```
Copy the long `{"di_token": ...}` line it prints.

### 2. Host it — pick one

| Option | Cost | Effort |
|---|---|---|
| **A. Oracle Cloud Always Free VM** | **€0** | ~30 min, needs a card at signup (not charged) |
| B. Hetzner CX22 VPS (EU) | ~€4/mo | ~15 min |
| C. Railway | ~$5/mo | ~10 min, all clicks |

#### A / B — VPS (free or ~€4)
1. Create an **Ubuntu** VM (Oracle: *Compute → Instances → Create*, shape `VM.Standard.A1.Flex` 1 OCPU / 6 GB, "Always Free"; then in the VCN's security list add ingress TCP 80 and 443 from `0.0.0.0/0`).
2. SSH in, then:
   ```bash
   git clone <your-private-repo-url> garmin-claude && cd garmin-claude
   bash deploy/setup-vps.sh
   ```
3. It asks for your Garmin email, password and 2FA code (one time, nothing is stored but the login token), then prints your connector URL. Done.

(Step 1 above is only needed for Railway.)

#### C — Railway
1. Push this folder to a **private** GitHub repo.
2. On [railway.com](https://railway.com): **New Project → Deploy from GitHub repo** → pick it.
3. On the service: **Settings → Volumes → Add volume**, mount path `/data`.
4. **Variables**:
   | Name | Value |
   |---|---|
   | `GARMIN_TOKENS` | the line from step 1 |
   | `MCP_SECRET` | a long random string, e.g. output of `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
   | `TZ_NAME` | `Europe/Lisbon` (default; same clock as the UK) |
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
| `BACKFILL_DAYS` | `730` | History pulled on first start |
| `WORKOUT_POLL_MIN` | `10` | How often to check for a new workout |
| `GARMIN_CALL_DELAY` | `0.4` | Pause between Garmin requests |

## Security notes
- The connector URL contains your secret — treat it like a password. Anyone with it can read your data.
- The token in `/data` gives access to your Garmin account. Changing your Garmin password revokes it.
- Uses the unofficial `garminconnect` library; if Garmin changes their API, bump its version in `requirements.txt`.

## Run locally
```bash
pip install -r requirements.txt
GARMIN_TOKENS='...' MCP_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))") python server.py
```
