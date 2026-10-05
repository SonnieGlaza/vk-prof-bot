"""SQLite (по умолчанию) или PostgreSQL (Railway DATABASE_URL)."""
from __future__ import annotations

import os
import sqlite3
from typing import Any

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_DEFAULT = os.path.join(_BASE_DIR, "career_bot.db")
DB_PATH = (os.environ.get("SQLITE_PATH") or os.environ.get("DB_PATH") or _DB_DEFAULT).strip() or _DB_DEFAULT

_RAW_DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()


def _pg_url() -> str:
    url = _RAW_DATABASE_URL
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


USE_PG = bool(_RAW_DATABASE_URL)


def sql(sqlite_sql: str) -> str:
    if not USE_PG:
        return sqlite_sql
    s = sqlite_sql
    s = s.replace("INSERT OR IGNORE", "INSERT")
    if "INSERT INTO" in s and "ON CONFLICT" not in s and "dedup_key" in s:
        s = s.rstrip().rstrip(";") + " ON CONFLICT (dedup_key) DO NOTHING"
    return s.replace("?", "%s")


class _Cursor:
    def __init__(self, cur, pg: bool):
        self._cur = cur
        self._pg = pg
        self._last_id: int | None = None

    def execute(self, statement: str, params: tuple | list | None = None):
        self._last_id = None
        self._cur.execute(sql(statement), params or ())

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> int:
        if self._last_id is not None:
            return self._last_id
        return int(getattr(self._cur, "lastrowid", 0) or 0)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _Connection:
    def __init__(self, conn, pg: bool):
        self._conn = conn
        self._pg = pg

    def cursor(self) -> _Cursor:
        return _Cursor(self._conn.cursor(), self._pg)

    def execute(self, statement: str, params: tuple | list | None = None):
        cur = self.cursor()
        cur.execute(statement, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if USE_PG:
            self._conn.close()
        else:
            self._conn.close()


def db_connect() -> _Connection:
    if USE_PG:
        import psycopg2

        return _Connection(psycopg2.connect(_pg_url()), pg=True)
    return _Connection(sqlite3.connect(DB_PATH, check_same_thread=False), pg=False)


def table_column_names(conn: _Connection, table: str) -> set[str]:
    cur = conn.cursor()
    if USE_PG:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        return {row[0] for row in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def ensure_column(conn: _Connection, table: str, column: str, ddl_suffix: str):
    names = table_column_names(conn, table)
    if column in names:
        return
    cur = conn.cursor()
    if USE_PG:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")
    else:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")
    conn.commit()


def insert_returning_id(cur: _Cursor, statement: str, params: tuple) -> int:
    if USE_PG:
        stmt = sql(statement).rstrip().rstrip(";") + " RETURNING id"
        cur.execute(stmt, params)
        row = cur.fetchone()
        if not row:
            raise RuntimeError("INSERT RETURNING id failed")
        return int(row[0])
    cur.execute(statement, params)
    return cur.lastrowid


def pg_ddl_init(cur: _Cursor, lp_dedup_tbl: str):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_progress (
            user_id BIGINT PRIMARY KEY,
            step INTEGER NOT NULL,
            scores_json TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at BIGINT NOT NULL,
            last_activity_at BIGINT NOT NULL,
            reminded_at BIGINT,
            test_id TEXT NOT NULL DEFAULT 'klimov_self',
            reminder_pending INTEGER NOT NULL DEFAULT 0,
            last_session_id BIGINT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS test_results (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            finished_at BIGINT NOT NULL,
            scores_json TEXT NOT NULL,
            top3_json TEXT NOT NULL,
            best_type TEXT NOT NULL DEFAULT '',
            test_id TEXT NOT NULL DEFAULT 'klimov_self'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS test_sessions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            test_id TEXT NOT NULL,
            started_at BIGINT NOT NULL,
            completed_at BIGINT,
            status TEXT NOT NULL DEFAULT 'in_progress',
            final_scores_json TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS answer_log (
            id SERIAL PRIMARY KEY,
            session_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            test_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            answer_key TEXT NOT NULL,
            question_text TEXT NOT NULL,
            answer_label TEXT NOT NULL,
            weights_json TEXT NOT NULL,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_log_session ON answer_log(session_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_log_user ON answer_log(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON test_sessions(user_id)")
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {lp_dedup_tbl} (
            dedup_key TEXT PRIMARY KEY,
            seen_at BIGINT NOT NULL
        )
        """
    )


def backend_label() -> str:
    if USE_PG:
        return "PostgreSQL (DATABASE_URL)"
    return f"SQLite ({DB_PATH})"
