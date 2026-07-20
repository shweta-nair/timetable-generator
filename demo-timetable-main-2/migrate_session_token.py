"""
migrate_session_token.py — adds `teacher_auth.active_session_token`, used for
single-session enforcement (a new login elsewhere terminates any older
session for the same account).

Safe to re-run: no-ops if the column already exists.

Run:
    python migrate_session_token.py [path-to-db]
"""
import sqlite3
import sys


def main(db_path: str = 'timetable.db') -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(teacher_auth)")
    cols = {row[1] for row in cur.fetchall()}
    if 'active_session_token' in cols:
        print('active_session_token already exists — nothing to do.')
        return
    cur.execute(
        "ALTER TABLE teacher_auth ADD COLUMN active_session_token VARCHAR(64)"
    )
    conn.commit()
    print('Migration complete: teacher_auth.active_session_token added.')


if __name__ == '__main__':
    db = sys.argv[1] if len(sys.argv) > 1 else 'timetable.db'
    main(db)
