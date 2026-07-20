"""
validate_course_data.py — Diagnose bad HOURS_PER_WEEK / LTPR / semester data
in the `courses` table before it cascades into the timetable generator.

Why this exists
────────────────
The 19-period/week teacher cap and the class-timetable "no vacant periods"
rule are both hard constraints that are already implemented correctly in
subject_assignment_engine.py / timetable_generator_engine.py. But several
divisions were landing at wildly wrong weekly totals (25, 40, 60...) instead
of the expected 34 periods/week (Years 1-3) or 29 periods/week (Year 4).

That is NOT a scheduler bug — it's bad source data. Two concrete examples
found in this database:

  CS dept, semester=1 also contains "Mathematics for Information Science-2"
  (a semester-2 subject, mistagged as semester 1).

  CS dept, semester=2 also contains "Mathematics for Information Sciences-3"
  and "...-4" (semester-3 and semester-4 subjects, both mistagged as
  semester 2).

Fixing the correct semester for each of these requires the actual KTU
curriculum scheme — not something safe to guess from code. This script
instead makes the problem visible and actionable:

  1. Totals every division+semester's weekly periods and flags any that
     don't match the expected 34 (Y1-3) / 29 (Y4).
  2. Flags rows with unparseable HOURS_PER_WEEK values.
  3. Heuristically flags subjects whose name ends in a number suffix
     ("...-2", "...-3", " II", " III"...) that looks inconsistent with
     other same-numbered subjects' semester placement — a strong signal
     of the mistagging pattern above.

Run:
    python validate_course_data.py [path-to-db]

Exit code is non-zero if any HIGH severity issues are found (useful for
wiring into a pre-generation admin check).
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict

# Only these `courses.department` values are ever queried by
# subject_assignment_engine.load_from_db() (see app.py STUDENT_DEPTS). Each
# department's own course file already embeds the BSH-taught subjects its
# students take (via DEPT_ID/teacher_dept_id='BSH'), so the standalone
# "BSH - course_done.xlsx" file's rows (department='BSH') are never actually
# queried during generation — they're legacy/unused data and must be excluded
# from totals, or every division's total gets polluted by irrelevant rows.
STUDENT_DEPTS = {'AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC'}

EXPECTED_TOTAL = {
    1: 34, 2: 34,          # Year 1
    3: 34, 4: 34,          # Year 2
    5: 34, 6: 34,          # Year 3
    7: 29, 8: 29,          # Year 4
}
TOLERANCE = 3  # +/- periods considered "close enough" (electives, seminars vary)

_LTPR_RE = re.compile(r'^(\d+)-(\d+)-(\d+)(?:-(\d+))?')
_PROJECT_NAME_RE = re.compile(
    r'\b(project(\s*phase\s*[\divx]+)?|seminar|mini[\s\-]?project|miniproject)\b',
    re.IGNORECASE,
)
_TRAILING_NUM_RE = re.compile(r'[-\s](\d+|[IVX]+)\s*$')

_ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8}


def parse_ltpr(hours_str: str):
    m = _LTPR_RE.match((hours_str or '').strip())
    if not m:
        return None
    L, T, P, R = [int(x) if x else 0 for x in m.groups(default='0')]
    return L, T, P, R


def _is_project(name: str) -> bool:
    return bool(_PROJECT_NAME_RE.search(name or ''))


def periods_for(hours_str: str, ctype: str, name: str):
    parsed = parse_ltpr(hours_str)
    if parsed is None:
        return None
    L, T, P, R = parsed
    ctype_lower = (ctype or '').lower()
    if 'project' in ctype_lower or 'seminar' in ctype_lower or _is_project(name):
        return L + T + P + R
    is_lab = 'lab' in ctype_lower
    return P if is_lab else (L + T + P + R)


def trailing_number(name: str):
    m = _TRAILING_NUM_RE.search(name or '')
    if not m:
        return None
    tok = m.group(1)
    if tok.isdigit():
        return int(tok)
    return _ROMAN.get(tok.upper())


def main(db_path: str = 'timetable.db') -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, code, name, department, type, hours_per_week, semester, division
        FROM courses
    """)
    rows = cur.fetchall()

    # Totals are keyed by the STUDENT division alone (not department), because a
    # division's real weekly load is the sum of courses taught by its own
    # department PLUS service courses from BSH / other departments (workshops,
    # intro-to-X modules, etc). Summing per-department subsets against the 34/29
    # target produces false positives.
    totals = defaultdict(int)          # (division, semester) -> periods
    contributors = defaultdict(list)   # (division, semester) -> [(dept, periods, name)]
    bad_hours = []                     # rows with unparseable HOURS_PER_WEEK
    name_semester_clues = defaultdict(list)  # (dept, base_name) -> [(trailing_num, semester)]

    for cid, code, name, dept, ctype, hours, sem, division in rows:
        if dept not in STUDENT_DEPTS:
            continue  # e.g. the unused standalone BSH course file — not queried by the engine
        p = periods_for(hours, ctype, name)
        if p is None:
            bad_hours.append((cid, code, name, dept, hours))
            continue
        try:
            sem_int = int(float(sem)) if sem not in (None, '') else None
        except ValueError:
            sem_int = None
        divs = [d.strip() for d in re.split(r'[,&]', division or '') if d.strip()]
        for d in divs:
            totals[(d, sem_int)] += p
            contributors[(d, sem_int)].append((dept, p, name))

        tn = trailing_number(name)
        if dept in STUDENT_DEPTS and tn is not None and sem_int is not None:
            base = _TRAILING_NUM_RE.sub('', name or '').strip().lower()
            name_semester_clues[(dept, base)].append((tn, sem_int, code, name))

    print("=" * 78)
    print("1) DIVISION/SEMESTER WEEKLY-PERIOD TOTALS vs EXPECTED")
    print("=" * 78)
    high_severity = 0
    for (div, sem), total in sorted(totals.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        expected = EXPECTED_TOTAL.get(sem)
        if expected is None:
            flag = "?  (unknown semester)"
        elif abs(total - expected) <= TOLERANCE:
            continue  # fine, don't print
        else:
            flag = f"expected ~{expected}"
            high_severity += 1
        print(f"  div={div:8s} sem={sem}  total={total:3d}   {flag}")
        for dept, p, name in sorted(contributors[(div, sem)], key=lambda c: -c[1])[:6]:
            print(f"      +{p:2d}  [{dept:5s}] {name}")

    print()
    print("=" * 78)
    print("2) ROWS WITH UNPARSEABLE HOURS_PER_WEEK")
    print("=" * 78)
    for cid, code, name, dept, hours in bad_hours:
        print(f"  id={cid} dept={dept} code={code!r} name={name!r} hours_per_week={hours!r}")
        high_severity += 1

    print()
    print("=" * 78)
    print("3) LIKELY SEMESTER-MISTAGGING (same subject family, inconsistent semester)")
    print("=" * 78)
    print("   Heuristic: subjects named '...-1', '...-2', '...-3'... normally belong")
    print("   to consecutive semesters. If two of them share the same semester value")
    print("   in the DB, one is probably mistagged.")
    for (dept, base), entries in sorted(name_semester_clues.items()):
        by_sem = defaultdict(list)
        for tn, sem_int, code, name in entries:
            by_sem[sem_int].append((tn, code, name))
        # A family is suspicious if the same semester has 2+ different trailing numbers.
        for sem_int, items in by_sem.items():
            distinct_nums = {tn for tn, _, _ in items}
            if len(distinct_nums) > 1:
                print(f"  {dept} / \"{base}\" — semester {sem_int} contains multiple parts:")
                for tn, code, name in items:
                    print(f"      part {tn}: {code!r} {name!r}")
                high_severity += 1

    print()
    print("=" * 78)
    print(f"SUMMARY: {high_severity} issue(s) flagged for review.")
    print("These need correction in the source XLSX files (csv/*.xlsx) — the")
    print("correct SEMESTER value for each course requires the KTU curriculum")
    print("scheme and cannot be safely auto-corrected.")
    print("=" * 78)

    return 1 if high_severity else 0


if __name__ == '__main__':
    db = sys.argv[1] if len(sys.argv) > 1 else 'timetable.db'
    sys.exit(main(db))
