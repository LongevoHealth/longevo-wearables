#!/usr/bin/env python3
"""Bootstrap the app/migrator database roles and schema-level grants on Aurora.

Runs once per environment stand-up (and again on every password rotation),
authenticated as the RDS-managed master user — the only credential in the
account allowed to create roles at all. Connects directly to the target
database (not the default `postgres` admin database), since `GRANT ... ON
SCHEMA public` and `ALTER DEFAULT PRIVILEGES` are scoped to whichever database
the session is connected to, not to a database named on the statement.
PostgreSQL 15+ also revokes CREATE on `public` from PUBLIC by default, so the
migrator needs it granted back explicitly.

Passwords are never interpolated into SQL text: psycopg.sql.Literal escapes
and quotes them, since PostgreSQL's CREATE/ALTER ROLE grammar requires a
string-constant token in the PASSWORD clause and cannot bind a query
parameter there.
"""

import json
import logging
import os

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)


def _connect_as_master(db_name: str) -> psycopg.Connection:
    master = json.loads(os.environ["MASTER_DSN_JSON"])
    dsn = (
        f"host={master['host']} "
        f"port={master.get('port', 5432)} "
        f"dbname={db_name} "
        f"user={master['username']} "
        f"password={master['password']} "
        "sslmode=require"
    )
    return psycopg.connect(dsn, autocommit=True)


def _upsert_role(cur: psycopg.Cursor, role: str, password: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if cur.fetchone():
        cur.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        logger.info("Updated password for role %r.", role)
    else:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        logger.info("Created role %r.", role)


def bootstrap() -> None:
    db_name = os.environ["DB_NAME"]
    app_password = os.environ["APP_PASSWORD"]
    migrator_password = os.environ["MIGRATOR_PASSWORD"]

    with _connect_as_master(db_name) as conn, conn.cursor() as cur:
        _upsert_role(cur, "app", app_password)
        _upsert_role(cur, "migrator", migrator_password)

        cur.execute(sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO migrator").format(sql.Identifier(db_name)))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO app").format(sql.Identifier(db_name)))
        cur.execute("GRANT CREATE, USAGE ON SCHEMA public TO migrator")
        cur.execute("GRANT USAGE ON SCHEMA public TO app")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO app"
        )

    logger.info("Database roles and grants are up to date.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s - %(name)s] (%(levelname)s) %(message)s")
    bootstrap()
