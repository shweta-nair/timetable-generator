import json, sqlite3, sys
from pathlib import Path
root = Path('/Users/bhavyasasok/Downloads/demo-timetable-main-2')
sys.path.insert(0, str(root))
from timetable_generator_engine import TimetableGeneratorEngine
from subject_assignment_engine import load_from_db, Teacher as EngTeacher, SubjectAssignment as EngSA, Division as EngDiv, Designation as EngDesig

conn = sqlite3.connect(str(root / 'timetable.db'))
cur = conn.cursor()
classes = [
    ('AIDS', 8, 'AIDS S8 ai'),
    ('AIDS', 8, 'AIDS S8 ds'),
    ('CS', 7, 'CS S7 CS1'),
    ('CS', 7, 'CS S7 CS2'),
    ('CS', 7, 'CS S7 CS3'),
    ('CS', 8, 'CS S8 CS1'),
]
lab_capacity_map = {}
for dept in ['AU','CE','EEE','ME','CS','AIDS','EC']:
    cur.execute('SELECT lab_room_capacity FROM departments WHERE id=?', (dept,))
    row = cur.fetchone()
    lab_capacity_map[dept] = row[0] if row and row[0] is not None else 2

def db_timetable_cells(dept, sem, div_id):
    cur.execute("SELECT data_json FROM timetables WHERE dept_id=? AND semester_type='odd' AND semester=? AND timetable_type='class' AND key=? ORDER BY id DESC LIMIT 1",
                (dept, str(sem), div_id))
    row = cur.fetchone()
    return json.loads(row[0]) if row else None

for dept, sem, div in classes:
    cur.execute('SELECT count(*) FROM subject_assignments WHERE dept_id=? AND semester=? AND semester_type=?', (dept, sem, 'odd'))
    sa_count = cur.fetchone()[0]
    db_cells = db_timetable_cells(dept, sem, div)
    db_count = len(db_cells) if db_cells is not None else None
    db_occupied = sum(1 for c in db_cells if not c.get('is_free') and c.get('subject_name')) if db_cells is not None else None
    print('---', dept, sem, div)
    print('subject_assignments_count=', sa_count)
    print('persisted_cells_count=', db_count, 'persisted_occupied=', db_occupied)
    if sa_count == 0:
        print('no saved assignments for this dept+sem')
        continue
    cur.execute('SELECT teacher_id, teacher_name, subject_code, role, division_id FROM subject_assignments WHERE dept_id=? AND semester=? AND semester_type=?', (dept, sem, 'odd'))
    rows = cur.fetchall()
    engine = load_from_db(str(root / 'timetable.db'), dept, sem)
    teacher_map = {t.teacher_id: t for t in engine.teachers}
    subject_map = {s.subject_id: s for s in engine.subjects}
    division_map = {d.division_id: d for d in engine.divisions}
    assignments = []
    skipped = []
    for tid, teacher_name, subject_code, role, division_id in rows:
        tid = str(tid)
        sc = str(subject_code)
        diid = str(division_id)
        t = teacher_map.get(tid)
        if t is None and tid.startswith('dummy-'):
            t = EngTeacher(teacher_id=tid, name=str(teacher_name or tid), dept_id=dept, designation=EngDesig.LECTURER, is_dummy=True)
            teacher_map[tid] = t
        s = subject_map.get(sc)
        div_obj = division_map.get(diid)
        if div_obj is None and diid:
            div_obj = EngDiv(division_id=diid, student_dept_id=dept, semester=sem)
            division_map[diid] = div_obj
        if t and s and div_obj:
            assignments.append(EngSA(subject=s, division=div_obj, teacher=t, role=role))
        else:
            skipped.append((tid, sc, diid, t is not None, s is not None, div_obj is not None))
    print('assignments_rebuilt=', len(assignments), 'skipped=', len(skipped))
    if skipped:
        print(' skipped_example=', skipped[:5])
    if not assignments:
        continue
    tt = TimetableGeneratorEngine()
    tt.load_data(assignments, lab_capacity_map=lab_capacity_map)
    tt.generate(use_cpsat=False)
    exported = tt.export_to_dict()
    cells = exported['class_timetables'].get(div)
    gen_count = len(cells) if cells is not None else None
    gen_occupied = sum(1 for c in cells if not c.get('is_free') and c.get('subject_name')) if cells is not None else None
    print('generated_cells_count=', gen_count, 'generated_occupied=', gen_occupied)
    if db_cells is not None and cells is not None:
        gen_map = {(c['day'], c['period_number']): c for c in cells}
        db_map = {(c['day'], c['period_number']): c for c in db_cells}
        diffs = []
        for key in sorted(set(gen_map) | set(db_map)):
            if gen_map.get(key) != db_map.get(key):
                diffs.append(key)
        print('diff_keys=', len(diffs))
        print('diff_keys_sample=', diffs[:8])
