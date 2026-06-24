# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted server monitor: ICMP/TCP checks on configured hosts, Gmail SMTP email alerts on DOWN/UP/repeating reminders, dark observability dashboard. Flask + Postgres + APScheduler + nginx, all in Docker Compose. Single-admin login. Targets Ubuntu 24 deployments via `install.sh`.

## Common commands

### Bring it up / down
```bash
docker compose up -d --build       # build and start all 4 containers
docker compose down                # stop (keep DB volume)
docker compose down -v             # nuke everything including DB
```

### Iterating on code
```bash
# Frontend changes (CSS, JS, HTML) are LIVE — app/static and app/templates
# are bind-mounted into the web container. Just hard-refresh the browser.

# Python changes — REBUILD the affected container. The .py code is baked
# into the image (only static/templates are bind-mounted), so a bare
# `docker compose restart` runs the OLD code.
docker compose up -d --build web scheduler
```

### Logs (most useful debugging tool)
```bash
docker compose logs -f scheduler   # one log line per check tick — verifies
                                   # state-machine transitions and email sends
docker compose logs -f web         # API requests, bootstrap password
docker compose logs -f             # everything
```

### DB shell + introspection
```bash
docker compose exec postgres psql -U monitor -d monitor
# Useful queries:
#   SELECT name, current_status, consecutive_failures, last_checked_at FROM servers;
#   SELECT * FROM outage_events WHERE ended_at IS NULL;       -- ongoing outages
#   SELECT * FROM check_results ORDER BY checked_at DESC LIMIT 20;
```

### Bootstrap admin password (first boot only)
```bash
docker logs server-monitor-web 2>&1 | grep -A1 "BOOTSTRAP ADMIN"
```
Reset it: `DELETE FROM admins;` then `docker compose restart web` — a fresh password is generated and logged.

### install.sh actions (Linux deploy)
```bash
sudo /opt/server-monitor/install.sh         # interactive menu
sudo /opt/server-monitor/install.sh update  # pg_dump backup → git pull → rebuild → image prune
sudo /opt/server-monitor/install.sh status  # ps + logs + /health
sudo /opt/server-monitor/install.sh remove  # requires typing DELETE
```
`update` preserves `pg_data` volume and `.env`; `.env` is gitignored so `git reset --hard origin/main` cannot clobber it. Last 10 backups kept in `/opt/server-monitor/backups/`.

### Tests / linting
None configured. `bash -n install.sh` and `python3 -m compileall app` for sanity. Don't add a test framework without a clear ask.

## Architecture: things that aren't obvious from a glance

### Memory budget is fixed by design — and "100% RAM" on the host is usually page cache
Each container has a hard `mem_limit` in `docker-compose.yml` (postgres 768m, web 512m, scheduler 384m, nginx 96m ≈ 1.7 GiB total), sized for a dedicated 4 GB VM and leaving ~2 GiB for OS page cache. Postgres is tuned with explicit `-c` flags in its `command:` (shared_buffers 256MB, work_mem 8MB, max_connections 30, etc.) so its footprint is predictable rather than auto-scaled. The web `gunicorn` runs `--max-requests 1000 --max-requests-jitter 200` so workers recycle and no slow leak accumulates over long uptimes — keep the flag set in both `docker-compose.yml` and `app/Dockerfile` CMD in sync. The DB pool is `max_size=4` (`app/db.py`), comfortably under `max_connections=30`. If you raise any of these, re-check the totals against the VM size. Note: a Proxmox/host panel showing the VM near or over 100% RAM is normally just Linux filling free memory with **reclaimable** page cache (`total − free` accounting) — install `qemu-guest-agent` and a small swap file (the installer's `ensure_swap` does this) so the host reports actual usage and can evict cleanly.

### Two containers, one image (`web` + `scheduler`)
Both are built from `app/Dockerfile`. `web` runs `gunicorn -w 2 ... app.main:create_app()`. `scheduler` runs `python -m app.scheduler`. They share code but **never share process**. Why: gunicorn forks workers, so APScheduler running inside Flask would fire every job N times. The scheduler container is the single owner of all check execution and alert dispatch.

### All containers are on the bridge network — scheduler included, deliberately
The scheduler originally used `network_mode: host` so probes could reach the LAN directly. That was reverted: a host-network container inherits the **host's** sysctls (`net.ipv4.ping_group_range`) and firewall, which made every unprivileged ICMP socket fail in production while the bridge-networked web container's "check now" worked fine. On the bridge, Docker enables unprivileged ICMP inside the container namespace itself and probes reach LAN hosts (`192.168.x.x`, `10.x.x.x`) through NAT on Linux. Don't reintroduce host networking without re-reading this history.

Both `web` and `scheduler` use `DATABASE_URL` with the service name `postgres`; the Postgres port is not published to the host at all.

**Docker Desktop Mac caveat:** containers in the LinuxKit VM cannot exchange ICMP with the Mac's LAN (and host networking there is L4-only anyway). LAN monitoring from a Mac dev box is not possible regardless of config — deploy on Linux for real testing. Internet ICMP (e.g. `8.8.8.8`) does work from the Mac bridge, which is handy for local smoke tests.

### State machine lives in `app/checker.py`
`process_server(server)` is the single entry point that:
1. Runs the check (`app/checks/icmp.py` or `app/checks/tcp.py`)
2. Inserts a row into `check_results`, updates `consecutive_failures` and `last_checked_at`
3. Detects transitions: `up → down` (after `failure_threshold` consecutive failures) opens an `outage_events` row and fires DOWN email; `down → up` closes the outage and fires RECOVERY email
4. The reminder loop is a separate `reminder_pass()` function called once per minute by the scheduler

Both paths (the periodic scheduler tick and the UI's "Check now" button via `app/api/servers.py`) call `process_server` — never run a check by any other route.

### Schema is bootstrapped on every web boot
`app/db.py:init_schema()` runs `CREATE TABLE IF NOT EXISTS` for every table on every web container start (gated by `RUN_MIGRATIONS=1`). There is **no migration framework**. Adding a new column to an existing table requires an explicit `ALTER TABLE IF NOT EXISTS` block in `SCHEMA_SQL` — `IF NOT EXISTS` patterns differ for ADD COLUMN, so use the `DO $$ ... EXCEPTION WHEN duplicate_column THEN NULL; END $$;` pattern, or just add a separate migration file when this gets nontrivial.

### SMTP password is encrypted at rest
`APP_SECRET_KEY` (Fernet key, 32 random bytes urlsafe-base64) encrypts the SMTP password before insert and decrypts on send (`app/crypto.py`, used by `app/api/settings.py` and `app/mailer.py`). Losing the key means re-entering the password from the UI. The key is generated by `install.sh` on first install and stored in `.env`.

### Frontend is vanilla JS, no build step
ES module-style files in `app/static/js/` (`util.js` is shared, one file per page). The dashboard polls `GET /api/status` every 5 seconds — that endpoint is the aggregated payload for cards + sparklines + summary chip; tune it there if dashboard performance ever matters. Templates extend `base.html`. Dark observability theme in `app/static/css/app.css` — `[hidden] { display: none !important; }` is load-bearing because `.modal` / `.summary-chip` use `display: flex` and would otherwise override the HTML `hidden` attribute.

### Recipient resolution
`app/alerting.py:recipients_for(server_id)` returns per-server overrides if any rows exist in `server_recipients` for that server, otherwise the default recipients (`is_default = TRUE` on `recipients`). This rule is the only thing that decides who gets emailed for a given alert.

## Code conventions in use

- Flask app factory pattern (`app/main.py:create_app`), blueprints registered per resource (`app/api/*.py`)
- `psycopg` 3 connection pool (`app/db.py:get_pool`) with `dict_row` row factory — queries return dicts everywhere
- API endpoints return JSON; UI pages render Jinja templates and let the JS modules do the rest via `fetch`
- All datetime values stored as `TIMESTAMPTZ` in Postgres, transmitted as ISO strings, formatted client-side
- Auth: cookie session via `Flask`'s built-in signed cookies; `@auth.login_required` decorator on UI pages and APIs (`app/auth.py`). API auth failures return 401 JSON; UI failures redirect to `/login`.
- No ORM. Direct SQL with parameterized queries.

## Known limits / explicit non-goals

- HTTP(S) endpoint checks not implemented (the `check_type` CHECK constraint can be widened later; the schema and dispatch in `checker.py:run_check` are designed to extend)
- No multi-user / RBAC (the `admins` table allows future expansion without migration)
- No CSRF token on the API — sessions are SameSite=Lax. Behind a real reverse proxy / authenticating proxy on the open web, this is fine. For untrusted networks, add a CSRF check before exposing.
- Database has no migration tooling — see "Schema is bootstrapped" above.
- TLS termination is the operator's responsibility (front this with Caddy/Traefik/nginx-with-certs for HTTPS).
