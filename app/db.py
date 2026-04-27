import os
import threading
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=_database_url(),
                    min_size=1,
                    max_size=8,
                    kwargs={"row_factory": psycopg.rows.dict_row},
                )
    return _pool


@contextmanager
def conn():
    pool = get_pool()
    with pool.connection() as c:
        yield c


@contextmanager
def cursor(commit: bool = False):
    with conn() as c:
        with c.cursor() as cur:
            yield cur
        if commit:
            c.commit()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admins (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS servers (
    id                       SERIAL PRIMARY KEY,
    name                     VARCHAR(120) NOT NULL,
    hostname                 VARCHAR(255) NOT NULL,
    check_type               VARCHAR(8)  NOT NULL CHECK (check_type IN ('icmp','tcp')),
    tcp_port                 INTEGER,
    interval_seconds         INTEGER NOT NULL DEFAULT 60 CHECK (interval_seconds >= 5),
    timeout_seconds          INTEGER NOT NULL DEFAULT 5  CHECK (timeout_seconds  >= 1),
    failure_threshold        INTEGER NOT NULL DEFAULT 3  CHECK (failure_threshold >= 1),
    enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
    current_status           VARCHAR(8)  NOT NULL DEFAULT 'unknown' CHECK (current_status IN ('up','down','unknown')),
    consecutive_failures     INTEGER NOT NULL DEFAULT 0,
    last_checked_at          TIMESTAMPTZ,
    last_latency_ms          INTEGER,
    last_status_change_at    TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS servers_enabled_idx ON servers (enabled, last_checked_at);

CREATE TABLE IF NOT EXISTS recipients (
    id          SERIAL PRIMARY KEY,
    email       VARCHAR(255) UNIQUE NOT NULL,
    name        VARCHAR(120),
    is_default  BOOLEAN NOT NULL DEFAULT FALSE,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS server_recipients (
    server_id    INTEGER NOT NULL REFERENCES servers(id)    ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
    PRIMARY KEY (server_id, recipient_id)
);

CREATE TABLE IF NOT EXISTS check_results (
    id            BIGSERIAL PRIMARY KEY,
    server_id     INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        VARCHAR(4) NOT NULL CHECK (status IN ('up','down')),
    latency_ms    INTEGER,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS check_results_server_time_idx
    ON check_results (server_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS check_results_time_idx
    ON check_results (checked_at);

CREATE TABLE IF NOT EXISTS outage_events (
    id                       BIGSERIAL PRIMARY KEY,
    server_id                INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at                 TIMESTAMPTZ,
    duration_seconds         INTEGER,
    down_alert_sent_at       TIMESTAMPTZ,
    recovery_alert_sent_at   TIMESTAMPTZ,
    last_reminder_sent_at    TIMESTAMPTZ,
    reminder_count           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outage_events_server_idx ON outage_events (server_id, started_at DESC);
CREATE INDEX IF NOT EXISTS outage_events_open_idx   ON outage_events (server_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS app_settings (
    id                              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    smtp_host                       VARCHAR(255),
    smtp_port                       INTEGER DEFAULT 587,
    smtp_username                   VARCHAR(255),
    smtp_password_encrypted         TEXT,
    smtp_from_address               VARCHAR(255),
    smtp_from_name                  VARCHAR(120) DEFAULT 'Server Monitor',
    smtp_use_starttls               BOOLEAN NOT NULL DEFAULT TRUE,
    reminder_interval_minutes       INTEGER NOT NULL DEFAULT 60 CHECK (reminder_interval_minutes >= 5),
    retention_days                  INTEGER NOT NULL DEFAULT 90 CHECK (retention_days >= 7),
    default_check_interval_seconds  INTEGER NOT NULL DEFAULT 60,
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    action      VARCHAR(64) NOT NULL,
    details     JSONB,
    actor       VARCHAR(120),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_schema() -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        c.commit()
