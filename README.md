# Crystal Sports court watcher

Watches every tennis court at **Crystal Sports** and **Crystal Sports G** for the whole
bookable window (today + 14 days, 06:00–23:00, all courts) and sends an alert the moment a
booked slot is cancelled — or a new day opens up. Only the plain court booking
(non-coaching) flow is watched.

Alert example:

```
🎾 Tennis court slot AVAILABLE!

📅 Sat 12 Sep 2026
• Crystal Sports — North-1: 19:00-20:00 (฿500/hr)
• Crystal Sports G — G South-2: 21:00-23:00 (฿500/hr)

Book now: https://crystalsports-booking.kegroup.co.th/booking.php
```

## How it works

The booking page talks to `api_helper.php?action=getAvailableStadiums`. One POST with
`{"date": "...", "stadiumId": null, "locId": null}` returns all 17 courts × 18 hourly slots for
that date (306 rows, ~2–4 s) and needs **no login**. A full sweep of the 15-day window is
therefore 15 requests (~45–60 s). The watcher loops sweeps back to back, remembers the
booked/free status of every slot in `state.json`, and alerts only on a **booked → free**
transition (or a slot it has never seen, i.e. a newly opened day). A slot that stays free is
not repeated; if it gets booked and cancelled again you get a fresh alert.

Files:

| file | purpose |
|---|---|
| `watcher.py` | main loop, diffing, alert formatting |
| `crystal_api.py` | the reverse-engineered API client (endpoint notes at the top) |
| `notifiers.py` | LINE Messaging API, Discord webhook, ntfy.sh, console |
| `test_watcher.py` | offline tests with a fake API (`python -m pytest -q`) |
| `.env.example` | every setting, documented |
| `.github/workflows/watch.yml` | run it on GitHub Actions |
| `Dockerfile`, `docker-compose.yml` | run it anywhere Docker runs |
| `crystal-watch.service` | systemd unit (VPS / Raspberry Pi) |
| `com.crystalwatch.plist` | macOS launchd agent |

## Quick start (any machine with Python 3.11+)

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in DISCORD_WEBHOOK_URL and/or LINE_* 
python watcher.py --test-alert   # confirms the channel works
python watcher.py --once         # one sweep, prints what it sees
python watcher.py                # runs forever
```

## Hosting options

**GitHub Actions (free, no server).** Push this folder to a repo, add
`DISCORD_WEBHOOK_URL` (and/or the LINE secrets) under *Settings → Secrets → Actions*, set
*Settings → Actions → General → Workflow permissions* to **Read and write**, then run the
workflow once manually from the *Actions* tab. It self-schedules every 5 minutes and each
run polls for ~4 minutes, so coverage is near-continuous with gaps of a few minutes when
GitHub is slow to start runs. Caveats: GitHub's scheduler can delay runs 5–15 min at busy
times; on a **private** repo you would burn the 2,000 free minutes/month in about two days,
so use a **public** repo (the webhook URL lives in Secrets, never in the code); and heavy
24/7 polling is at the edge of GitHub's Actions fair-use terms, so treat this as the
"good enough, zero cost" option rather than the forever solution.

**Always-on VM (best).** Oracle Cloud's Always Free tier or any ~$5/month VPS gives truly
continuous ~1-minute detection:

```bash
git clone <repo> /opt/crystal-watch && cd /opt/crystal-watch
cp .env.example .env && nano .env
docker compose up -d          # or: sudo cp crystal-watch.service /etc/systemd/system/ && sudo systemctl enable --now crystal-watch
docker compose logs -f
```

**Your Mac.** `cp com.crystalwatch.plist ~/Library/LaunchAgents/`, edit the two paths,
`launchctl load ~/Library/LaunchAgents/com.crystalwatch.plist`. Pauses while the Mac sleeps.

## Alert channels

* **LINE Official Account** (preferred): create a Messaging API channel at
  <https://developers.line.biz/console>, issue a long-lived channel access token, copy your
  user ID from *Basic settings*, add the OA as a friend, set `LINE_CHANNEL_ACCESS_TOKEN`
  and `LINE_TO`. Push messages to a single user are free (well within the 200/month free
  quota for a hobby OA — cancellations are rare events).
* **Discord**: server → *Integrations → Webhooks → New Webhook → Copy URL* → `DISCORD_WEBHOOK_URL`.
* **ntfy**: install the ntfy app, subscribe to a topic, set `NTFY_TOPIC`.

Multiple channels can be active at once.

## Tuning

`REQUEST_GAP_SEC` / `SWEEP_GAP_SEC` control how hard the site is hit (defaults ≈ 15 requests
a minute, gentle). `HEARTBEAT_HOURS=24` sends a daily "still alive" ping. The `FILTER_*`
variables narrow alerts to a venue, court, weekday or time range if you ever want that.

## If the site changes

Everything site-specific is in `crystal_api.py`. A sweep that returns 0 rows logs a
warning, and after `FAIL_ALERT_AFTER_MIN` minutes of failed requests you get one
"site unreachable" alert (and one "back" alert when it recovers).
