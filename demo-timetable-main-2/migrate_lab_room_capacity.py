"""
migrate_lab_room_capacity.py — one-off migration for gap #3 (classroom/lab-
room capacity model).

Adds `departments.lab_room_capacity` (default 2, matching the parallel-lab
capacity that was implicitly assumed everywhere before this field existed)
to an existing timetable.db without touching any other data.

Safe to re-run: no-ops if the column already exists.

Run:
    python migrate_lab_room_capacity.py [path-to-db]
"""
import sqlite3
import sys


def main(db_path: str = 'timetable.db') -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(departments)")
    cols = {row[1] for row in cur.fetchall()}
    if 'lab_room_capacity' in cols:
        print('lab_room_capacity already exists — nothing to do.')
        return
    cur.execute(
        "ALTER TABLE departments ADD COLUMN lab_room_capacity INTEGER DEFAULT 2 NOT NULL"
    )
    conn.commit()
    cur.execute("SELECT id, lab_room_capacity FROM departments")
    for row in cur.fetchall():
        print(f'  {row[0]:6s} lab_room_capacity={row[1]}')
    print('Migration complete.')


if __name__ == '__main__':
    db = sys.argv[1] if len(sys.argv) > 1 else 'timetable.db'
    main(db)
