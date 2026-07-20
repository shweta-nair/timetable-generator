"""
reset_and_reimport.py
─────────────────────
Clears all data tables and re-imports everything from the updated XLSX files.

WHAT IS CLEARED:
  • timetables
  • subject_assignments
  • teacher_preferences
  • courses
  • teachers  (+ their teacher_auth rows, so auth is rebuilt fresh too)

WHAT IS KEPT:
  • notifications
  • system_settings
  • preference_windows
  • password_reset_tokens
  • The 'admin' TeacherAuth account (re-created if missing)

Run from the project root:
  python3 reset_and_reimport.py
"""

from app import app
from database import (
    db,
    Timetable, SubjectAssignment, TeacherPreference,
    Course, Teacher, TeacherAuth,
)
from import_csv import (
    import_departments,
    import_teachers,
    create_teacher_auth,
    import_courses,
    import_preferences,
    create_admin,
)


def reset_data_tables():
    """Delete all rows from the data tables (preserves auth/settings/notifications)."""

    print('Clearing data tables...')

    # Drop in FK-safe order
    Timetable.query.delete()
    db.session.flush()
    print('  ✓ timetables cleared')

    SubjectAssignment.query.delete()
    db.session.flush()
    print('  ✓ subject_assignments cleared')

    TeacherPreference.query.delete()
    db.session.flush()
    print('  ✓ teacher_preferences cleared')

    Course.query.delete()
    db.session.flush()
    print('  ✓ courses cleared')

    # Delete teacher auth rows that belong to actual teachers (not admin/hod)
    TeacherAuth.query.filter(TeacherAuth.teacher_id.isnot(None)).delete()
    db.session.flush()
    print('  ✓ teacher_auth (teacher roles) cleared')

    Teacher.query.delete()
    db.session.flush()
    print('  ✓ teachers cleared')

    db.session.commit()
    print()


if __name__ == '__main__':
    with app.app_context():
        # Ensure all tables exist (adds new columns / new tables)
        db.create_all()
        print('Schema verified / updated.\n')

        reset_data_tables()

        print('Re-importing from updated XLSX files...\n')
        import_departments()
        import_teachers()
        create_teacher_auth()
        import_courses()
        import_preferences()
        create_admin()

        from database import Department, TeacherPreference, Course, Teacher
        print(f'\n✅ Reset & re-import complete!')
        print(f'   Departments : {Department.query.count()}')
        print(f'   Teachers    : {Teacher.query.count()}')
        print(f'   Courses     : {Course.query.count()}')
        print(f'   Preferences : {TeacherPreference.query.count()}')
        print(f'   SubjectAssignments : {SubjectAssignment.query.count()} (empty — ready for new run)')
        print(f'   Timetables         : {Timetable.query.count()} (empty — ready for new run)')
