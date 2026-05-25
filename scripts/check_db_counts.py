#!/usr/bin/env python3
"""Показать число строк в таблицах бота (DATABASE_URL или --sqlite)."""
import argparse
import os
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="")
    args = ap.parse_args()
    tables = ("answer_log", "test_results", "test_sessions", "user_progress")
    if args.sqlite.strip():
        conn = sqlite3.connect(args.sqlite.strip())
        cur = conn.cursor()
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                print(t, cur.fetchone()[0])
            except sqlite3.OperationalError as e:
                print(t, f"— {e}")
        conn.close()
        return
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("Задайте DATABASE_URL или --sqlite", file=sys.stderr)
        sys.exit(1)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    import psycopg2

    conn = psycopg2.connect(url)
    cur = conn.cursor()
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            print(t, cur.fetchone()[0])
        except Exception as e:
            print(t, f"— {e}")
    conn.close()


if __name__ == "__main__":
    main()
