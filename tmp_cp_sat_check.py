import sqlite3
import random
from subject_assignment_engine import load_from_db
from timetable_generator_engine import TimetableGeneratorEngine
from subject_assignment_engine import Teacher as EngTeacher, SubjectAssignment as EngSA, Division as EngDiv, Designation as EngDesig

DB = 'timetable.db'
for dept, sem, semtype, div in [('AIDS', 8, 'even', 'ai'), ('CS', 7, 'odd', 'CS1')]:
    print('===', dept, sem, semtype, div)
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
    r = cur.fetchone()
    labs = {dept: r[0] if r and r[0] is not None else 2}
    tt.load_data(assigns, lab_capacity_map=labs)
    tt.block_external_busy_slots(set())
    random.seed(0)
    res = tt.generate(use_cpsat=True)
    print(' score', tt._last_score, 'grid', len(tt._grid))
    print(' class cells', len(res.class_timetables.get(div, [])), 'occupied', sum(1 for c in res.class_timetables.get(div, []) if not c.is_free))
