"""
import_csv.py – Populate the SQLite database from updated XLSX files.
Run from the project root:  python import_csv.py
"""

import os
import re
import pandas as pd
from app import app
from database import db, Department, Teacher, Course, TeacherPreference, TeacherAuth

CSV_DIR = os.path.join(os.path.dirname(__file__), 'csv')


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def clean(val):
    """Return stripped string or empty string for NaN."""
    if pd.isna(val):
        return ''
    return str(val).strip()


def clean_int(val, default=0):
    """Return int or default for NaN / non-numeric values."""
    if pd.isna(val):
        return default
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default


def first_name_from(full_name: str) -> str:
    """
    Extract usable first name for login credentials.
    'Dr. Sindhya K. Nambiar'  →  'sindhya'
    'Ms. Binu John'           →  'binu'
    'Mr. Anvar Sadath A K'   →  'anvar'
    """
    name = re.sub(r'^(Dr\.?|Mr\.?|Ms\.?|Prof\.?)\s*', '', full_name, flags=re.IGNORECASE).strip()
    first = name.split()[0].lower()
    first = re.sub(r'[^a-z]', '', first)
    return first


def normalise_cols(df):
    """Strip whitespace and content in parentheses from column names."""
    df.columns = [re.sub(r'\s*\(.*?\)', '', c).strip() for c in df.columns]
    return df


# ──────────────────────────────────────────────────────────────
# Department code normalisation
# ──────────────────────────────────────────────────────────────

DEPT_CODE_MAP = {
    'CIVIL':      'CE',
    'AUTO':       'AU',
    'AUTOMOBILE': 'AU',
    'MECH':       'ME',
    'EEEE':       'EEE',
}

VALID_TEACHER_DEPTS = {'AU', 'CE', 'EEE', 'ME', 'BSH', 'CS', 'AIDS', 'EC'}


def normalise_dept_code(code: str) -> str:
    c = code.strip()
    return DEPT_CODE_MAP.get(c, c)


def normalise_teacher_dept(raw: str) -> str:
    parts = [normalise_dept_code(p) for p in raw.split(',')]
    return ', '.join(p for p in parts if p)


# ──────────────────────────────────────────────────────────────
# 1. Departments  (seeded from a hard-coded canonical list)
# ──────────────────────────────────────────────────────────────

DEPARTMENTS = [
    ('AIDS', 'Artificial Intelligence and Data Science'),
    ('AU',   'Automobile Engineering'),
    ('BSH',  'Basic Sciences and Humanities'),
    ('CE',   'Civil Engineering'),
    ('CS',   'Computer Science and Engineering'),
    ('EC',   'Electronics and Communication Engineering'),
    ('EEE',  'Electrical and Electronics Engineering'),
    ('ME',   'Mechanical Engineering'),
]


def import_departments():
    count = 0
    for dept_id, dept_name in DEPARTMENTS:
        existing = Department.query.get(dept_id)
        if not existing:
            db.session.add(Department(id=dept_id, name=dept_name))
            count += 1
    db.session.commit()
    print(f'  ✓ Departments: {count} new rows (total {Department.query.count()})')


# ──────────────────────────────────────────────────────────────
# 2. Teachers   (* - tdetail.xlsx files)
# ──────────────────────────────────────────────────────────────

# Map dept_id → xlsx filename (in csv/ folder)
TEACHER_FILES = {
    'AIDS': 'AIDS.xlsx - tdetails.xlsx',
    'AU':   'AUTOMOBILE - tdetail.xlsx',
    'BSH':  'BSH - tdetail.xlsx',
    'CE':   'CIVIL - tdetail.xlsx',
    'CS':   'CS - tdetail.xlsx',
    'EC':   'EC - tdetail.xlsx',
    'EEE':  'EEE - tdetail.xlsx',
    'ME':   'MECH - tdetail.xlsx',
}


def import_teachers():
    count = 0
    for dept_id, filename in TEACHER_FILES.items():
        filepath = os.path.join(CSV_DIR, filename)
        if not os.path.exists(filepath):
            print(f'  ⚠ Missing: {filename}')
            continue

        df = pd.read_excel(filepath, engine='openpyxl')
        df = normalise_cols(df)  # strips "(PK)", "(FK)" etc.

        for _, row in df.iterrows():
            name = clean(row.get('NAME', ''))
            if not name:
                continue

            code        = clean(row.get('CODE', ''))
            teacher_id  = clean(row.get('ID', ''))

            # Fallback: use CODE, then auto-generate
            if not teacher_id:
                teacher_id = code if code else f'{dept_id}_{name[:6].replace(" ", "")}'

            existing = Teacher.query.get(teacher_id)
            if not existing:
                teacher = Teacher(
                    id=teacher_id,
                    name=name,
                    code=code,
                    designation=clean(row.get('DESIGNATION', '')),
                    gender=clean(row.get('GENDER', '')),
                    experience=clean(row.get('EXPERIENCE', '')),
                    date_of_joining=clean(row.get('DATE_OF_JOINING', '')),
                    seniority_level=clean(row.get('SENIORITY_LEVEL', '')),
                    area_of_specialization=clean(row.get('AREA_OF_SPECIALIZATION', '')),
                    qualification=clean(row.get('QUALIFICATION', '')),
                    dept_id=dept_id,
                )
                db.session.add(teacher)
                count += 1

    db.session.commit()
    print(f'  ✓ Teachers: {count} new rows (total {Teacher.query.count()})')


# ──────────────────────────────────────────────────────────────
# 3. Teacher Auth  (auto-generate)
# ──────────────────────────────────────────────────────────────

def create_teacher_auth():
    teachers = Teacher.query.all()
    count = 0
    for t in teachers:
        if t.auth:
            continue
        username = first_name_from(t.name)
        base = username
        suffix = 1
        while TeacherAuth.query.filter_by(username=username).first():
            username = f'{base}{suffix}'
            suffix += 1

        auth = TeacherAuth(teacher_id=t.id, username=username, role='teacher')
        auth.set_password(base)
        db.session.add(auth)
        count += 1

    db.session.commit()
    print(f'  ✓ Teacher Auth: {count} new accounts created')


# ──────────────────────────────────────────────────────────────
# 4. Courses   (* - course_done.xlsx files)
# ──────────────────────────────────────────────────────────────

COURSE_FILES = {
    'AIDS': 'AIDS - course_done.xlsx',
    'AU':   'AU - course_done.xlsx',
    'BSH':  'BSH - course_done.xlsx',
    'CE':   'CE - course_done.xlsx',
    'CS':   'CS - course_done_.xlsx',
    'EC':   'EC - course_done.xlsx',
    'EEE':  'EEE - course_done.xlsx',
    'ME':   'ME - course_done.xlsx',
}


def import_courses():
    count = 0
    updated = 0
    for student_dept, filename in COURSE_FILES.items():
        filepath = os.path.join(CSV_DIR, filename)
        if not os.path.exists(filepath):
            print(f'  ⚠ Missing: {filename}')
            continue

        df = pd.read_excel(filepath, engine='openpyxl')
        df = normalise_cols(df)  # strips "(PK)", "(FK)", "(L-T-P-R)" etc.

        for _, row in df.iterrows():
            code = clean(row.get('CODE', ''))
            name = clean(row.get('NAME', ''))
            if not code or not name:
                continue

            raw_teacher_dept = clean(row.get('DEPT_ID', ''))
            teacher_dept = normalise_teacher_dept(raw_teacher_dept) if raw_teacher_dept else ''

            # Check for duplicate by code+department (the unique constraint)
            existing = Course.query.filter_by(code=code, department=student_dept).first()
            if not existing:
                db.session.add(Course(
                    code=code,
                    name=name,
                    department=student_dept,
                    dept_id=teacher_dept,
                    type=clean(row.get('TYPE', '')),
                    credit=clean_int(row.get('CREDIT', 0)),
                    hours_per_week=clean(row.get('HOURS_PER_WEEK', '')),
                    semester=clean(row.get('SEMESTER', '')),
                    division=clean(row.get('DIVISION', '')),
                    revision=clean(row.get('REVISION', '')),
                ))
                count += 1
            else:
                # Update credit if it was missing
                existing.credit = clean_int(row.get('CREDIT', existing.credit))
                existing.dept_id = teacher_dept if teacher_dept else existing.dept_id
                updated += 1

    db.session.commit()
    print(f'  ✓ Courses: {count} new rows, {updated} updated (total {Course.query.count()})')


# ──────────────────────────────────────────────────────────────
# 5. Teacher Preferences   (* - tpref.xlsx files)
# ──────────────────────────────────────────────────────────────

PREF_FILES = {
    'AIDS': 'AIDS.xlsx - tpref.xlsx',
    'AU':   'AUTOMOBILE - tpref.xlsx',
    'BSH':  'BSH - tpref.xlsx',
    'CE':   'CIVIL - tpref.xlsx',
    'CS':   'CS - Tpref.xlsx',
    'EC':   'EC - Tpref.xlsx',
    'EEE':  'EE - tpref.xlsx',
    'ME':   'MECH - Tpref.xlsx',
}


def import_preferences():
    count = 0
    for dept_id, filename in PREF_FILES.items():
        filepath = os.path.join(CSV_DIR, filename)
        if not os.path.exists(filepath):
            print(f'  ⚠ Missing: {filename}')
            continue

        df = pd.read_excel(filepath, engine='openpyxl')
        df = normalise_cols(df)  # strips "(FK)" etc.

        for _, row in df.iterrows():
            teacher_code = clean(row.get('TEACHER_ID', ''))
            course_code  = clean(row.get('COURSE_CODE', ''))
            rank_raw     = clean(row.get('RANK', '1'))
            semester     = clean(row.get('SEMESTER', ''))

            if not teacher_code or not course_code:
                continue

            try:
                rank = int(float(rank_raw)) if rank_raw else 1
            except ValueError:
                rank = 1

            # Resolve teacher_id from code
            teacher = Teacher.query.filter_by(code=teacher_code).first()
            teacher_id = teacher.id if teacher else None

            # Validate course exists
            course = Course.query.filter_by(code=course_code).first()
            if not course:
                code_stripped = course_code.replace(' ', '')
                course = Course.query.filter(Course.code.contains(code_stripped)).first()
                course_code = course.code if course else course_code

            db.session.add(TeacherPreference(
                teacher_id=teacher_id,
                teacher_code=teacher_code,
                course_code=course_code,
                rank=rank,
                semester=semester,
            ))
            count += 1

    db.session.commit()
    print(f'  ✓ Teacher Preferences: {count} rows (total {TeacherPreference.query.count()})')


# ──────────────────────────────────────────────────────────────
# 6. Admin account
# ──────────────────────────────────────────────────────────────

def create_admin():
    existing = TeacherAuth.query.filter_by(username='admin').first()
    if not existing:
        admin = TeacherAuth(teacher_id=None, username='admin', role='admin')
        admin.set_password('admin')
        db.session.add(admin)
        db.session.commit()
        print('  ✓ Admin account created  (username: admin / password: admin)')
    else:
        print('  ✓ Admin account already exists')


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print('Database tables verified/created.\nImporting CSV data...\n')

        import_departments()
        import_teachers()
        create_teacher_auth()
        import_courses()
        import_preferences()
        create_admin()

        total_dept    = Department.query.count()
        total_teacher = Teacher.query.count()
        total_course  = Course.query.count()
        total_pref    = TeacherPreference.query.count()

        print(f'\n✅ Import complete!')
        print(f'   Departments : {total_dept}')
        print(f'   Teachers    : {total_teacher}')
        print(f'   Courses     : {total_course}')
        print(f'   Preferences : {total_pref}')
