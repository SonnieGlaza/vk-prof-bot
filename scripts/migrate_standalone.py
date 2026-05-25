#!/usr/bin/env python3
"""SQLite career_bot.db → Railway Postgres. Один файл, без git и без db_backend."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_TABLES = (
    "user_progress",
    "test_results",
    "test_sessions",
    "answer_log",
    "longpoll_incoming_dedup",
)
_SERIAL_TABLES = ("test_results", "test_sessions", "answer_log")


def _pg_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("Ошибка: задайте DATABASE_URL (Postgres → Connect в Railway)", file=sys.stderr)
        sys.exit(1)
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _init_pg(cur):
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
        """
        CREATE TABLE IF NOT EXISTS longpoll_incoming_dedup (
            dedup_key TEXT PRIMARY KEY,
            seen_at BIGINT NOT NULL
        )
        """
    )


def _sqlite_has(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _pg_empty(pg, table: str) -> bool:
    cur = pg.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0]) == 0


def _copy(sq: sqlite3.Connection, pg, table: str) -> int:
    cur_sq = sq.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur_sq.description]
    rows = cur_sq.fetchall()
    if not rows:
        return 0
    ph = ", ".join(["%s"] * len(cols))
    cur_pg = pg.cursor()
    cur_pg.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})",
        rows,
    )
    return len(rows)


def _reset_serial(pg, table: str):
    cur = pg.cursor()
    cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
    max_id = int(cur.fetchone()[0])
    cur.execute(
        "SELECT setval(pg_get_serial_sequence(%s, 'id'), %s, true)",
        (table, max_id),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sqlite", default=r"C:\Users\User\career_bot.db")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    if not os.path.isfile(args.sqlite):
        print(f"Нет файла: {args.sqlite}", file=sys.stderr)
        sys.exit(1)
    try:
        import psycopg2
    except ImportError:
        print("Сначала: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)
    sq = sqlite3.connect(args.sqlite)
    pg = psycopg2.connect(_pg_url())
    try:
        cur = pg.cursor()
        _init_pg(cur)
        pg.commit()
        for table in _TABLES:
            if not _sqlite_has(sq, table):
                print(f"  пропуск {table}: нет в SQLite")
                continue
            if args.force:
                cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                pg.commit()
            elif not _pg_empty(pg, table):
                print(f"  пропуск {table}: в Postgres уже есть данные (--force чтобы перезаписать)")
                continue
            n = _copy(sq, pg, table)
            pg.commit()
            print(f"  {table}: {n} строк")
        for table in _SERIAL_TABLES:
            if _sqlite_has(sq, table):
                _reset_serial(pg, table)
        pg.commit()
        print("Готово.")
    finally:
        sq.close()
        pg.close()


if __name__ == "__main__":
    main()
