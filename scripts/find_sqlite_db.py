#!/usr/bin/env python3
"""Показать путь к SQLite и число строк (запускать в railway shell / run на сервисе бота)."""
import os
import sqlite3

candidates = []
for key in ("SQLITE_PATH", "DB_PATH"):
    v = (os.environ.get(key) or "").strip()
    if v:
        candidates.append(v)
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
candidates.append(os.path.join(base, "career_bot.db"))
candidates.append("/data/career_bot.db")

seen = set()
for path in candidates:
    if path in seen:
        continue
    seen.add(path)
    if not os.path.isfile(path):
        print(f"нет файла: {path}")
        continue
    print(f"найден: {path} ({os.path.getsize(path)} байт)")
    conn = sqlite3.connect(path)
    try:
        for table in (
            "test_results",
            "test_sessions",
            "answer_log",
            "user_progress",
        ):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table}: {n}")
            except sqlite3.OperationalError:
                print(f"  {table}: (таблица отсутствует)")
    finally:
        conn.close()
