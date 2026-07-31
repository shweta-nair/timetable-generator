"""
One-time migration: normalize TeacherPreference.semester to numeric strings.

Background
----------
Multiple past versions of the preference-saving code stored
TeacherPreference.semester as:
  - the literal string "odd" / "even"
  - a numeric string ("1".."8")
  - the course's own semester value (usually already numeric)
  - or left it unset entirely (empty string / NULL)

Because several of the old save routes only deleted "previous
preferences" that matched the NEW numeric format, legacy "odd"/"even"
rows were never cleaned up on resubmission — so a teacher could end up
with both a legacy row and a numeric row for the very same course
selection. That's what produced HOD-dashboard counts like "6/3"
instead of "3/3" (see app.py's save_teacher_preferences()).

This script is idempotent and safe to re-run: after it completes,
every remaining TeacherPreference.semester value is a numeric string
("1".."8"), and each teacher has at most 3 rows per semester-parity
group (odd / even), matching the standard the application now enforces
going forward via save_teacher_preferences() in app.py.

What it does, per teacher (grouped by teacher_code):
  1. Resolve every row's semester to a numeric string, using the
     row's own value if already numeric, otherwise looking up the
     course via course_code and using Course.semester.
  2. Rows that cannot be resolved (course_code no longer exists) are
     removed — there's no way to recover a valid numeric semester for
     them.
  3. Rows that resolve to the exact same course_code as another row
     for the same teacher are duplicates from the old buggy delete
     logic; only one copy is kept (preferring a row that already had
     a numeric semester, then the most recently created row).
  4. If a teacher still ends up with more than 3 resolved rows in one
     parity group (a genuine conflict between different course
     choices, not just a format duplicate), only the 3 most recent
     are kept and the rest are removed, since exactly 3 preferences
     per semester group is the application's invariant.
  5. Kept rows have their semester column rewritten to the resolved
     numeric string.

A timestamped backup of the database file is taken before any writes.
Run with --dry-run first to see what would change without touching
the database.
"""
import argparse
import shutil
import sys
from datetime import datetime

from app import app
from database import db, TeacherPreference, Course


def resolve_numeric_semester(pref, course_by_code):
    """Return a numeric-string semester for this row, or None if it
    can't be determined."""
    raw = (pref.semester or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 8:
        return raw
    course = course_by_code.get(pref.course_code)
    if course and course.semester and str(course.semester).strip().isdigit():
        return str(course.semester).strip()
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would change without writing anything.")
    args = parser.parse_args()

    with app.app_context():
        db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")

        if not args.dry_run:
            backup_path = f"{db_path}.backup_pre_semester_migration_{datetime.now():%Y%m%d_%H%M%S}"
            shutil.copyfile(db_path, backup_path)
            print(f"Backed up database to: {backup_path}")

        all_prefs = TeacherPreference.query.all()
        all_courses = {c.code: c for c in Course.query.all()}

        by_teacher = {}
        for p in all_prefs:
            by_teacher.setdefault(p.teacher_code, []).append(p)

        removed_unresolvable = []
        removed_duplicates = []
        removed_overflow = []
        updated_count = 0
        unchanged_count = 0

        for teacher_code, prefs in by_teacher.items():
            resolved = []  # list of (pref, numeric_semester)
            for p in prefs:
                sem = resolve_numeric_semester(p, all_courses)
                if sem is None:
                    removed_unresolvable.append(p)
                else:
                    resolved.append((p, sem))

            # De-dupe by course_code: same teacher + same course_code
            # chosen more than once = leftover from the old buggy
            # delete logic. Prefer a row whose semester was already
            # numeric, then the highest id (most recently inserted).
            by_course = {}
            for p, sem in resolved:
                key = p.course_code
                was_numeric = (p.semester or "").strip().isdigit()
                candidate = (p, sem, was_numeric)
                if key not in by_course:
                    by_course[key] = candidate
                else:
                    existing = by_course[key]
                    existing_p, _, existing_numeric = existing
                    # Prefer already-numeric row; tie-break on higher id.
                    if (was_numeric, p.id) > (existing_numeric, existing_p.id):
                        removed_duplicates.append(existing_p)
                        by_course[key] = candidate
                    else:
                        removed_duplicates.append(p)

            deduped = [(p, sem) for p, sem, _ in by_course.values()]

            # Enforce at most 3 rows per parity group; keep the most
            # recent (highest id) 3 if there's a genuine conflict.
            odd_group = sorted([t for t in deduped if int(t[1]) % 2 == 1],
                                key=lambda t: t[0].id, reverse=True)
            even_group = sorted([t for t in deduped if int(t[1]) % 2 == 0],
                                 key=lambda t: t[0].id, reverse=True)

            for group in (odd_group, even_group):
                if len(group) > 3:
                    overflow = group[3:]
                    for p, _ in overflow:
                        removed_overflow.append(p)
                    del group[3:]

            keep = {p.id: sem for p, sem in odd_group + even_group}

            for p in prefs:
                if p.id in keep:
                    new_sem = keep[p.id]
                    if p.semester != new_sem:
                        p.semester = new_sem
                        updated_count += 1
                    else:
                        unchanged_count += 1

        to_delete = removed_unresolvable + removed_duplicates + removed_overflow

        print(f"Teachers processed:        {len(by_teacher)}")
        print(f"Rows updated to numeric:   {updated_count}")
        print(f"Rows already correct:      {unchanged_count}")
        print(f"Rows removed (unresolvable course): {len(removed_unresolvable)}")
        print(f"Rows removed (duplicate course):     {len(removed_duplicates)}")
        print(f"Rows removed (>3 per group overflow): {len(removed_overflow)}")
        print(f"Total rows removed:        {len(to_delete)}")

        if args.dry_run:
            print("\nDry run — no changes written.")
            db.session.rollback()
            return

        for p in to_delete:
            db.session.delete(p)

        db.session.commit()

        remaining_legacy = TeacherPreference.query.filter(
            db.or_(
                TeacherPreference.semester.in_(["odd", "even", ""]),
                TeacherPreference.semester.is_(None),
            )
        ).count()
        print(f"\nDone. Remaining non-numeric semester rows: {remaining_legacy}")
        if remaining_legacy:
            print("WARNING: some rows could not be resolved automatically — inspect manually.")


if __name__ == "__main__":
    main()
