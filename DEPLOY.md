# Deploying Janus Desk

## Free vs paid

| | Free (`render.yaml`) | Paid (`render.paid.yaml`) |
|---|---|---|
| Plan | free | starter |
| Persistent disk | **none** | 1 GB at `/var/data` |
| Position book across restarts | **lost** | kept |
| Spin-down on idle | yes | no |
| Boot guard | requires `EPHEMERAL_STORAGE_ACK=true` | mounts `/var/data`, no ack needed |
| Health check | `/healthz` (always 200) | `/healthz` |

**Use free only for demos.** Paper PnL and open positions live in SQLite. On
free tier that file is on an ephemeral filesystem: every deploy, every
spin-down, every machine move starts you from a blank book. The process
refuses to boot without `EPHEMERAL_STORAGE_ACK=true` so that cannot happen
silently.

For anything you care about, deploy `render.paid.yaml`. The disk is the
whole point.

## Health endpoints

| Path | When 200 | When 503 | Use for |
|---|---|---|---|
| `/healthz` | web process is up | never (if process is up) | Render health check, cron-job.org, keep-alive |
| `/health` | engine heartbeat &lt; 120s | engine not beating | real monitoring / paging |

Do not point a keep-alive pinger at `/health`. A cold engine after a free-tier
wake would 503 and the pinger would think the service is down.

## Keep-alive (free tier)

Free Render services sleep after idle. Two options:

### Option A — cron-job.org (recommended)

1. Create an account at [cron-job.org](https://cron-job.org).
2. New cron job:
   - **URL:** `https://YOUR-SERVICE.onrender.com/healthz`
   - **Schedule:** every 5 minutes
   - **Method:** GET
   - **Notifications:** optional on failure
3. Save. Confirm the job runs and returns 200 while the service is up.

No app config required. The pinger hits the public URL from outside.

### Option B — outbound self-ping

Set on the service:

```
KEEPALIVE_ENABLED=true
KEEPALIVE_INTERVAL_S=300
```

The engine process GETs `{DASHBOARD_URL}/healthz` on that interval. It binds
no port (the old bug was a keep-alive that tried to own `$PORT`). This only
helps if something else has already woken the service at least once after
deploy; pair it with Option A for reliability.

## Environment checklist

Required for a useful deploy:

- `GEMINI_API_KEY` and/or `DEEPSEEK_API_KEY` — commentary + PREP news bias
- `DISCORD_WEBHOOK` (or `DISCORD_WEBHOOK_URL`) — lifecycle alerts
- `STARTING_CAPITAL` — paper bankroll

Free tier only:

- `EPHEMERAL_STORAGE_ACK=true`
- keep-alive as above

Paid only:

- `DB_PATH=/var/data/janus.db`
- `LOG_DIR=/var/data/logs`

## Deploy steps (Render)

1. Push this repo to GitHub.
2. New Blueprint → select `render.yaml` (free) or `render.paid.yaml` (paid).
3. Fill secret env vars when prompted.
4. After first deploy, open `/healthz` then `/` and confirm the dashboard
   loads. On paid, confirm `DB_PATH` is under `/var/data` via the System card
   or `scripts/doctor.py` over SSH/shell.

## Local parity

```bash
cp .env.example .env
# for free-tier-like local testing:
# ENV=production EPHEMERAL_STORAGE_ACK=true
python scripts/doctor.py
PYTHONPATH=. python -m pytest
python run.py
```
