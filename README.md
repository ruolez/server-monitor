# Server Monitor

Self-hosted, Docker-Compose server monitoring with email alerts.

- **ICMP ping** + **TCP port** checks on per-server intervals
- **Gmail SMTP** (or any STARTTLS host) for alert delivery
- **DOWN** / **UP recovery** / **periodic reminder** emails, with per-server recipient overrides
- 90-day rolling history, sparkline uptime visualization
- Single-admin login, dark observability dashboard

## Stack

| Layer       | Tech                                              |
|-------------|---------------------------------------------------|
| Frontend    | HTML5 / vanilla JS / CSS (Inter + JetBrains Mono) |
| Backend     | Python 3.12 · Flask · gunicorn                    |
| Scheduler   | APScheduler (separate container)                  |
| Database    | PostgreSQL 16                                     |
| Reverse pxy | nginx                                             |
| Container   | Docker Compose                                    |

## Quick start

```bash
cp .env.example .env

# Generate APP_SECRET_KEY (Fernet key for SMTP password encryption)
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'

# Generate POSTGRES_PASSWORD
openssl rand -base64 24

# Edit .env and paste the two values, then:
docker compose up -d --build

# Grab the bootstrap admin password (first boot only):
docker logs server-monitor-web 2>&1 | grep -A1 "BOOTSTRAP ADMIN"
```

Open <http://localhost:8765> and sign in as `admin`.

## First-time setup checklist

1. **Recipients** → add at least one and mark it **Default**.
2. **Settings → SMTP** → for Gmail:
   - Host: `smtp.gmail.com` Port: `587` STARTTLS: ✓
   - Username: your full Gmail address
   - Password: a 16-char [App Password](https://myaccount.google.com/apppasswords) (requires 2-Step Verification)
   - Click **Send test email…** to verify before saving servers.
3. **Servers** → add hostnames/IPs. Pick **ICMP** for raw reachability or **TCP** if you'd rather verify a service port (e.g. `443`, `22`).

## Configuration

All runtime configuration lives in `.env`:

| Variable             | Purpose                                                |
|----------------------|--------------------------------------------------------|
| `APP_SECRET_KEY`     | Fernet key — encrypts the SMTP password at rest        |
| `POSTGRES_PASSWORD`  | DB superuser password (only used inside the network)   |
| `HOST_PORT`          | Host port published by nginx (default `8765`)          |

Per-server settings (interval, timeout, failure threshold, recipient overrides) and global policy (reminder interval, retention) live in the database and are edited through the UI.

## Architecture

```
nginx (host:8765 → :80)
   │
   ▼
flask web (gunicorn :8000)  ◀──┐
   │                            │
   ▼                            │
postgres:16 (volume pg_data)   │
   ▲                            │
scheduler  (APScheduler) ───────┘
```

The **scheduler** is a separate container so APScheduler isn't duplicated across gunicorn workers. It runs three jobs:

| Job             | Cadence            | Purpose                                      |
|-----------------|--------------------|----------------------------------------------|
| Due checks      | every 2 s          | Picks servers whose `interval_seconds` elapsed and runs ICMP/TCP |
| Reminders       | every 1 min        | Re-sends "still down" email after `reminder_interval_minutes` |
| Retention prune | daily 03:00 UTC    | Deletes `check_results` and `outage_events` older than `retention_days` (default 90) |

ICMP runs unprivileged via `icmplib` — `docker-compose.yml` sets `net.ipv4.ping_group_range=0 2147483647` on the scheduler container so a non-root process can use ICMP datagram sockets. No `NET_RAW` capability needed.

## Alerting state machine

```
unknown ─── 1st success ──► up
   │
   │  failure_threshold consecutive failures
   ▼
  down ─── 1st success ──► up   (sends RECOVERY email)
   │
   │  reminder_interval_minutes elapsed
   └──► STILL DOWN reminder (loops)
```

When a server goes down, an `outage_events` row opens. It closes the moment a successful check arrives, with `duration_seconds` filled. Each transition emails the resolved recipients (per-server overrides if set, otherwise default recipients).

## API reference

All endpoints require an authenticated session (cookie). 401 redirects HTML pages to `/login`; API endpoints return JSON.

| Method | Path                                  | Purpose |
|--------|---------------------------------------|---------|
| GET    | `/api/status`                         | Dashboard snapshot (servers, summary, sparklines) |
| GET    | `/api/servers`                        | List servers |
| POST   | `/api/servers`                        | Create server |
| GET    | `/api/servers/<id>`                   | Read server (with override recipient ids) |
| PUT    | `/api/servers/<id>`                   | Update server |
| DELETE | `/api/servers/<id>`                   | Delete server |
| POST   | `/api/servers/<id>/check-now`         | Run a one-off check |
| GET    | `/api/recipients`                     | List recipients |
| POST   | `/api/recipients`                     | Create recipient |
| PUT    | `/api/recipients/<id>`                | Update recipient |
| DELETE | `/api/recipients/<id>`                | Delete recipient |
| GET    | `/api/settings`                       | Read SMTP + policy (password never returned) |
| PUT    | `/api/settings`                       | Update SMTP + policy |
| POST   | `/api/settings/test-smtp`             | Send a test email |
| GET    | `/api/history/outages`                | Outage timeline |
| GET    | `/api/history/checks`                 | Recent check results |
| GET    | `/health`                             | Liveness (checks DB) |

## Operations

```bash
# Watch all containers
docker compose logs -f

# Just the scheduler (per-check log line)
docker compose logs -f scheduler

# Reset bootstrap password (delete the admin row, restart web)
docker compose exec postgres psql -U monitor -d monitor -c "DELETE FROM admins;"
docker compose restart web

# Backup
docker compose exec postgres pg_dump -U monitor monitor > backup.sql

# Restore
cat backup.sql | docker compose exec -T postgres psql -U monitor monitor
```

## ICMP and Docker Desktop (Mac / Windows) — important caveat

If you are running this stack on **Docker Desktop for Mac or Windows**, ICMP checks (`ping`) will **only work for public-internet targets** like `8.8.8.8`. Pings to LAN hosts (`192.168.x.x`, `10.x.x.x`) will fail with "no reply".

**Why:** Docker Desktop runs containers inside a LinuxKit VM. The default bridge network NATs outbound TCP/UDP to the host's LAN, but the bridge does not forward ICMP echo-replies back from your local network. Even Docker Desktop's [host networking feature](https://docs.docker.com/desktop/features/networking/) (Settings → Resources → Network → "Enable host networking") operates only at layer 4 (TCP/UDP) and explicitly does not pass ICMP through.

**Fix:** Use a **TCP port check** instead of ICMP for LAN servers. Pick a port the service actually listens on:

| Service              | Port  |
|----------------------|-------|
| HTTP                 | 80    |
| HTTPS                | 443   |
| SSH                  | 22    |
| RDP                  | 3389  |
| SQL Server           | 1433  |
| Postgres             | 5432  |
| MySQL                | 3306  |
| SMB / file share     | 445   |

A TCP check is also a stronger health signal — it confirms the service is listening, not just that the IP responds. ICMP works fine on **Linux Docker hosts** for both LAN and internet targets.

## Security notes

- The SMTP password is **encrypted at rest** with Fernet (`APP_SECRET_KEY`). Losing the key means re-entering the password.
- `SESSION_COOKIE_SECURE=False` by default — set to `True` and put nginx behind HTTPS for production.
- The default published port `8765` should be firewalled if reachable from the internet. Put a real reverse proxy (Caddy, Traefik, nginx with certs) in front for TLS.
- No CSRF token on the API — sessions are `SameSite=Lax`, but if you expose this on the open web, consider adding a CSRF check or wrapping it in an authenticating proxy.

## Out of scope (v1)

- HTTPS / TLS (use a fronting proxy)
- HTTP(S) endpoint checks (schema can be extended — `check_type` is a CHECK constraint)
- Multi-user / RBAC (the `admins` table allows future expansion without migration)
- Slack / webhook notifications
- Maintenance windows / alert suppression
- Public read-only status page
