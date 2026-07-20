import sqlite3
import random
from subject_assignment_engine import load_from_db, Teacher as EngTeacher, SubjectAssignment as EngSA, Division as EngDiv, Designation as EngDesig
from timetable_generator_engine import TimetableGeneratorEngine

DB = 'timetable.db'
CASES = [
    ('AIDS', 8, 'even', 'ai'),
    ('AIDS', 8, 'even', 'ds'),
    ('CS', 7, 'odd', 'CS1'),
    ('CS', 7, 'odd', 'CS2'),
    ('CS', 7, 'odd', 'CS3'),
]
for dept, sem, semtype, div in CASES:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT teacher_id, teacher_name, subject_code, role, division_id FROM subject_assignments WHERE dept_id=? AND semester=? AND semester_type=?', (dept, sem, semtype))
    rows = cur.fetchall()
    engine = load_from_db(DB, dept, sem)
    teacher_map = {t.teacher_id: t for t in engine.teachers}
    subject_map = {s.subject_id: s for s in engine.subjects}
    division_map = {d.division_id: d for d in engine.divisions}
    assigns = []
    for tid, tname, scode, role, did in rows:
        tid = str(tid)
        scode = str(scode)
        did = str(did)
        t = teacher_map.get(tid)
        if t is None and tid.startswith('dummy-'):
            t = EngTeacher(teacher_id=tid, name=str(tname or tid), dept_id=dept, designation=EngDesig.LECTURER, is_dummy=True)
            teacher_map[tid] = t
        s = subject_map.get(scode)
        d = division_map.get(did)
        if d is None and did:
            d = EngDiv(division_id=did, student_dept_id=dept, semester=sem)
            division_map[did] = d
        if t and s and d:
            assigns.append(EngSA(subject=s, division=d, teacher=t, role=role))
    tt = TimetableGeneratorEngine()
    cur.execute('SELECT lab_room_capacity FROM departments WHERE id=?', (dept,))
    r = cur.fetchone(); labs = {dept: r[0] if r and r[0] is not None else 2}
    tt.load_data(assigns, lab_capacity_map=labs)
    tt.block_external_busy_slots(set())
    random.seed(0)
    res = tt.generate(use_cpsat=True)
    planned = {}
    for a in assigns:
        planned[(a.subject.subject_id, a.division.division_id, a.teacher.teacher_id)] = max(a.subject.periods_per_week, 1)
    placed = {}
    for (div_id, day, pnum), a in tt._grid.items():
        key = (a.subject.subject_id, a.division.division_id, a.teacher.teacher_id)
        placed[key] = placed.get(key, 0) + 1
    print('CASE', dept, sem, semtype, div)
    for k, req in planned.items():
        if placed.get(k, 0) != req:
            subj_id, div_id, tid = k
            print('  MISSING', k, 'required', req, 'scheduled', placed.get(k, 0))
    print('  total grid slots', len(tt._grid))
