# Timetable Generator

An automatic timetable generation system for engineering colleges, built with Flask.

Teachers submit subject preferences, HODs review them department by department, and the system assigns subjects to teachers and generates conflict-free weekly timetables for every semester and division.

---

## What it does

The system runs in three stages:

**1. Preference collection.** An admin opens a preference window for the active semester group (odd or even). Each teacher picks three subjects, one from each of three different semesters, ranked by preference. HODs can view and edit preferences on behalf of teachers in their own department.

**2. Subject assignment.** `subject_assignment_engine.py` allocates subjects to teachers, honouring preferences where possible and enforcing workload rules:

- Maximum 2 core theory subjects per teacher (3 for BSH teachers)
- Maximum 1 lab **or** project **or** seminar per teacher — these share a single slot
- Weekly period caps by designation: HOD 16, senior staff 17 (Principal, Deputy Dean, Associate Professor), regular staff 19
- The same subject taught across multiple divisions counts as one load unit

Where no real teacher is available, placeholder teachers (`X1`, `X2`, ...) are created so every subject-division pair is covered and the gaps are visible.

**3. Timetable generation.** `timetable_generator_engine.py` places the assignments into weekly slots, handling lab blocks, lunch breaks, year-specific period structures, and teacher clash avoidance.

## Roles

| Role | Can do |
|---|---|
| **Admin** | Manage departments, teachers, subjects; open/close preference windows; set the active semester; generate and publish timetables for all departments |
| **HOD** | Everything above, scoped to their own department; review and edit their teachers' preferences; submit their own |
| **Teacher** | Submit and update subject preferences; view their personal timetable and notifications |

---

## Requirements

- Python 3.9 or later
- The packages below

```bash
pip install flask flask-sqlalchemy flask-login werkzeug pandas openpyxl
```

`openpyxl` is required because the source data files are `.xlsx`, not CSV, despite living in a folder called `csv/`.

## Setup

```bash
git clone https://github.com/shweta-nair/timetable-generator.git
cd timetable-generator
```

Populate the database from the spreadsheets in `csv/`:

```bash
python import_csv.py
```

This creates the tables, imports departments, teachers, courses and any existing preferences, and creates an admin account.

> **Default admin login is `admin` / `admin`.** Change it immediately after first sign-in. It is a hardcoded fallback in `import_csv.py` and is not safe to leave in place.

Then start the app:

```bash
python app.py
```

It runs on **http://localhost:5001** with debug mode on.

## Data files

`import_csv.py` reads `.xlsx` files from `csv/`, one set per department, following the naming pattern:

| Pattern | Contents |
|---|---|
| `<DEPT> - course_done.xlsx` | Subject list: code, name, semester, type, LTPR |
| `<DEPT> - tdetail.xlsx` | Teacher roster: name, code, designation |
| `<DEPT> - tpref.xlsx` | Pre-existing teacher preferences |

Department prefixes vary a little between files (`AIDS`, `CS`, `EC`, `EEE`, `CIVIL`, `MECH`, `AUTOMOBILE`, `BSH`) and are normalised on import.

## Typical workflow

1. Sign in as admin and set the active semester type (odd or even) in Settings
2. Open a preference window
3. Teachers submit their three ranked preferences
4. HODs review and correct their department's submissions
5. Close the window
6. Run subject assignment, then generate timetables
7. Review the preview, then publish

---

## Project layout

```
app.py                              Flask routes, auth, all UI endpoints
database.py                         SQLAlchemy models
import_csv.py                       Populate the DB from csv/*.xlsx
subject_assignment_engine.py        Assign subjects to teachers (workload rules)
timetable_generator_engine.py       Place assignments into weekly slots
templates/                          Jinja2 templates (admin / hod / teacher)
static/                             CSS, JS, uploaded profile photos
csv/                                Source .xlsx data files
exports/                            Generated comparison and audit reports
```

### Maintenance scripts

Run these from the project root only when you need them:

| Script | Purpose |
|---|---|
| `reset_and_reimport.py` | Clear teachers, courses, preferences, assignments and timetables, then reimport from `csv/`. Keeps notifications, settings and the admin account. |
| `migrate_teacher_preference_semester.py` | One-time cleanup converting legacy `'odd'`/`'even'` preference rows to numeric semester strings |
| `migrate_lab_room_capacity.py` | Add lab room capacity columns |
| `migrate_session_token.py` | Add session token columns |
| `validate_course_data.py` | Check course data for inconsistencies before import |
| `ltpr_audit_and_fix.py` | Audit and correct LTPR (lecture/tutorial/practical) values |

Files prefixed `tmp_`, `test_`, `diagnose_` and `verify.py` are ad-hoc debugging scripts from development. They are not part of the application.

---

## Notes for contributors

**Databases are per-developer.** `timetable.db` holds generated local data and should not be shared through git — two people generating timetables independently will produce conflicting binary files that git cannot merge. Regenerate yours with `import_csv.py` or `reset_and_reimport.py` rather than copying someone else's.

**Preferences are saved through one function.** Every route that writes teacher preferences goes through `save_teacher_preferences()` in `app.py`. If you add another preference-submission path, call that helper rather than writing your own delete-and-insert logic. Five separate near-duplicate implementations previously caused legacy rows to survive resubmission, which is why HOD dashboards showed counts like `6/3` instead of `3/3`.

**Semester is always stored numerically.** `TeacherPreference.semester` holds `"1"` through `"8"` as strings, never the literal `'odd'` or `'even'`.