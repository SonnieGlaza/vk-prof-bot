#!/usr/bin/env python3
"""
Однократная загрузка данных из SQLite в PostgreSQL (Railway DATABASE_URL).

Пример (локально, с файлом career_bot.db):
  export DATABASE_URL='postgresql://...'   # из Railway → Postgres → Connect
  python scripts/migrate_sqlite_to_postgres.py --sqlite career_bot.db

На Railway (в сервисе бота, если есть старый SQLite на томе):
  railway run python scripts/migrate_sqlite_to_postgres.py --sqlite /data/career_bot.db

После миграции в Variables сервиса бота добавьте DATABASE_URL (Reference → Postgres)
и уберите зависимость от эфемерного SQLite без тома, либо оставьте SQLITE_PATH как резерв.
"""
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
        print("Ошибка: не задан DATABASE_URL", file=sys.stderr)
        sys.exit(1)
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _sqlite_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _pg_table_empty(pg, table: str) -> bool:
    cur = pg.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0]) == 0


def _copy_table(sq: sqlite3.Connection, pg, table: str) -> int:
    cur_sq = sq.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur_sq.description]
    rows = cur_sq.fetchall()
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    cur_pg = pg.cursor()
    cur_pg.executemany(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
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
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL для vk-prof-bot")
    parser.add_argument(
        "--sqlite",
        default=os.environ.get("SQLITE_PATH") or os.environ.get("DB_PATH") or "career_bot.db",
        help="Путь к файлу SQLite",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Очистить таблицы Postgres перед копированием (TRUNCATE)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.sqlite):
        print(f"Файл SQLite не найден: {args.sqlite}", file=sys.stderr)
        sys.exit(1)

    import psycopg2

    from db_backend import pg_ddl_init

    sq = sqlite3.connect(args.sqlite)
    pg = psycopg2.connect(_pg_url())

    try:
        cur = pg.cursor()
        pg_ddl_init(cur, "longpoll_incoming_dedup")
        pg.commit()

        for table in _TABLES:
            if not _sqlite_table_exists(sq, table):
                print(f"  пропуск {table}: нет в SQLite")
                continue
            if args.force:
                cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                pg.commit()
            elif not _pg_table_empty(pg, table):
                print(
                    f"  пропуск {table}: в Postgres уже есть строки (используйте --force)",
                    file=sys.stderr,
                )
                continue
            n = _copy_table(sq, pg, table)
            pg.commit()
            print(f"  {table}: {n} строк")

        for table in _SERIAL_TABLES:
            if _sqlite_table_exists(sq, table):
                _reset_serial(pg, table)
        pg.commit()
        print("Готово. В сервисе бота задайте DATABASE_URL (Reference на Postgres) и redeploy.")
    finally:
        sq.close()
        pg.close()


if __name__ == "__main__":
    main()
