#!/usr/bin/env python3
import os
import sys
import sqlite3
import json
import tempfile
import shutil
import logging
import inspect
from pathlib import Path

from subject_assignment_engine import (
    load_from_db,
    Teacher as EngTeacher,
    SubjectAssignment as EngSA,
    Division as EngDiv,
    Designation as EngDesig,
)
from timetable_generator_engine import TimetableGeneratorEngine

logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')
log = logging.getLogger()

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'timetable.db'

CASES = [
    {
        'label': 'AIDS S8 AI',
        'dept': 'AIDS',
        'semester': 8,
        'semester_type': 'even',
        'division': 'ai',
    },
    {
        'label': 'CS S7 CS1',
        'dept': 'CS',
        'semester': 7,
        'semester_type': 'odd',
        'division': 'CS1',
    },
]


def cell_to_dict_obj(c):
    return {
        'division_id': getattr(c, 'division_id', None),
        'day': getattr(c, 'day', None),
        'period_number': getattr(getattr(c, 'period', None), 'number', None),
        'period_start': getattr(getattr(c, 'period', None), 'start', None),
        'period_end': getattr(getattr(c, 'period', None), 'end', None),
        'subject_id': getattr(getattr(c, 'subject', None), 'subject_id', None),
        'subject_code': getattr(getattr(c, 'subject', None), 'code', None),
        'subject_name': getattr(getattr(c, 'subject', None), 'name', None),
        'teacher_id': getattr(getattr(c, 'teacher', None), 'teacher_id', None),
        'teacher_name': getattr(getattr(c, 'teacher', None), 'name', None),
        'is_free': getattr(c, 'is_free', None),
        'paired_subject_id': getattr(getattr(c, 'paired_subject', None), 'subject_id', None),
        'paired_teacher_id': getattr(getattr(c, 'paired_teacher', None), 'teacher_id', None),
    }


def occupied_count_obj(cells):
    return sum(1 for c in cells if not getattr(c, 'is_free', True) and getattr(getattr(c, 'subject', None), 'name', None))


def occupied_count_export(cells):
    return sum(1 for c in cells if not c.get('is_free') and c.get('subject_name'))


def key_for_cell_dict(c):
    return (
        c['day'],
        c['period_number'],
    )


def key_for_cell_obj(c):
    return (
        getattr(c, 'day', None),
        getattr(getattr(c, 'period', None), 'number', None),
    )


def compare_cells(before, after, from_obj=False):
    before_map = {}
    after_map = {}
    if from_obj:
        for c in before:
            before_map[key_for_cell_obj(c)] = cell_to_dict_obj(c)
    else:
        for c in before:
            before_map[key_for_cell_dict(c)] = c

    for c in after:
        after_map[key_for_cell_dict(c)] = c

    missing = []
    changed_to_free = []
    modified = []
    for key, b in before_map.items():
        a = after_map.get(key)
        if a is None:
            missing.append((key, b))
            continue
        if not b.get('is_free', False) and a.get('is_free', False):
            changed_to_free.append((key, b, a))
            continue
        if not b.get('is_free', False) and not a.get('is_free', False):
            if (b.get('subject_id') != a.get('subject_id') or
                    b.get('teacher_id') != a.get('teacher_id') or
                    b.get('subject_name') != a.get('subject_name') or
                    b.get('teacher_name') != a.get('teacher_name')):
                modified.append((key, b, a))
    return missing, changed_to_free, modified


def load_subject_assignments(conn, dept, semester, semester_type):
    cur = conn.cursor()
    cur.execute(
        'SELECT teacher_id, teacher_name, subject_code, role, division_id FROM subject_assignments '
        'WHERE dept_id=? AND semester=? AND semester_type=?',
        (dept, semester, semester_type)
    )
    return cur.fetchall()


def rebuild_engine_assignments(assign_rows, engine, dept, sem):
    teacher_map = {t.teacher_id: t for t in engine.teachers}
    subject_map = {s.subject_id: s for s in engine.subjects}
    division_map = {d.division_id: d for d in engine.divisions}
    assignments = []
    skipped = []
    for tid, teacher_name, subject_code, role, division_id in assign_rows:
        tid = str(tid)
        sc = str(subject_code)
        diid = str(division_id)
        t = teacher_map.get(tid)
        if t is None and tid.startswith('dummy-'):
            t = EngTeacher(
                teacher_id=tid,
                name=str(teacher_name or tid),
                dept_id=dept,
                designation=EngDesig.LECTURER,
                is_dummy=True,
            )
            teacher_map[tid] = t
        s = subject_map.get(sc)
        div = division_map.get(diid)
        if div is None and diid:
            div = EngDiv(division_id=diid, student_dept_id=dept, semester=sem)
            division_map[diid] = div
        if t and s and div:
            assignments.append(EngSA(subject=s, division=div, teacher=t, role=role))
        else:
            skipped.append((tid, sc, diid, t is not None, s is not None, div is not None))
    return assignments, skipped


def inspect_case(case):
    print('\n' + '=' * 90)
    print(f"CASE: {case['label']}\n")
    tmpdir = Path(tempfile.mkdtemp(prefix='tt_trace_'))
    tmp_db = tmpdir / 'timetable.db'
    shutil.copy(DB_PATH, tmp_db)
    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()

    print('Source DB row counts for subject_assignments and timetables:')
    cur.execute(
        'SELECT COUNT(*) FROM subject_assignments WHERE dept_id=? AND semester=? AND semester_type=?',
        (case['dept'], case['semester'], case['semester_type'])
    )
    print('  subject_assignments:', cur.fetchone()[0])
    cur.execute(
        'SELECT COUNT(*) FROM timetables WHERE dept_id=? AND semester=? AND semester_type=? AND timetable_type="class" AND key=?',
        (case['dept'], str(case['semester']), case['semester_type'], case['division'])
    )
    print('  persisted class timetables rows:', cur.fetchone()[0])

    assign_rows = load_subject_assignments(conn, case['dept'], case['semester'], case['semester_type'])
    if not assign_rows:
        print('  No assignment rows found for this case. Cannot trace.');
        conn.close();
        return

    engine = load_from_db(str(tmp_db), case['dept'], case['semester'])
    assignments, skipped = rebuild_engine_assignments(assign_rows, engine, case['dept'], case['semester'])
    print(f'  Rebuilt assignments: {len(assignments)} rows, skipped={len(skipped)}')
    if skipped:
        print('   skipped example:', skipped[:5])

    tt_eng = TimetableGeneratorEngine()
    cur.execute('SELECT lab_room_capacity FROM departments WHERE id=?', (case['dept'],))
    row = cur.fetchone()
    lab_capacity_map = {case['dept']: row[0] if row and row[0] is not None else 2}
    tt_eng.load_data(assignments, lab_capacity_map=lab_capacity_map)
    tt_eng.block_external_busy_slots(set())
    result = tt_eng.generate(use_cpsat=False)

    # Stage 1: immediately before export_to_dict
    before_cells = result.class_timetables.get(case['division'], [])
    before_count = occupied_count_obj(before_cells)
    print('\n1) immediately before export_to_dict()')
    print('  occupied_count=', before_count)
    print('  occupied_cells=')
    for c in before_cells:
        if not c.is_free and getattr(getattr(c, 'subject', None), 'name', None):
            print('   ', key_for_cell_obj(c),
                  'subject=', getattr(getattr(c, 'subject', None), 'code', None),
                  getattr(getattr(c, 'subject', None), 'name', None),
                  'teacher=', getattr(getattr(c, 'teacher', None), 'teacher_id', None),
                  getattr(getattr(c, 'teacher', None), 'name', None))

    # Stage 2: immediately after export_to_dict()
    print('\n2) immediately after export_to_dict()')
    exported = tt_eng.export_to_dict()
    exported_cells = exported.get('class_timetables', {}).get(case['division'], [])
    after_export_count = occupied_count_export(exported_cells)
    print('  occupied_count=', after_export_count)
    missing, changed_to_free, modified = compare_cells(before_cells, exported_cells, from_obj=True)
    print('  before->after_export comparisons:')
    print('    missing slots:', len(missing))
    print('    changed to free:', len(changed_to_free))
    print('    modified occupied cells:', len(modified))
    if missing or changed_to_free or modified:
        for key, b in missing[:5]:
            print('     MISSING', key, b)
        for key, b, a in changed_to_free[:5]:
            print('     CHANGED_TO_FREE', key, b, '->', a)
        for key, b, a in modified[:5]:
            print('     MODIFIED', key, b, '->', a)
        print('  If this is non-empty, export_to_dict() at timetable_generator_engine.py:3871-3895 is responsible.')

    # Stage 3: before writing data_json to the database
    cells_to_persist = exported_cells
    json_text = json.dumps(cells_to_persist)
    print('\n3) immediately before writing data_json to DB')
    print('  json_length=', len(json_text))
    print('  occupied_count=', occupied_count_export(cells_to_persist))

    # Persist row to temp DB and read it back
    cur.execute(
        'DELETE FROM timetables WHERE dept_id=? AND semester=? AND semester_type=? AND timetable_type="class" AND key=?',
        (case['dept'], str(case['semester']), case['semester_type'], case['division'])
    )
    cur.execute(
        'INSERT INTO timetables (dept_id, semester, semester_type, division, timetable_type, key, effective_date, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (case['dept'], str(case['semester']), case['semester_type'], case['division'], 'class', case['division'], '2026-07-20', json_text, '2026-07-20 00:00:00')
    )
    conn.commit()

    cur.execute(
        'SELECT data_json FROM timetables WHERE dept_id=? AND semester=? AND semester_type=? AND timetable_type="class" AND key=? ORDER BY id DESC LIMIT 1',
        (case['dept'], str(case['semester']), case['semester_type'], case['division'])
    )
    row = cur.fetchone()
    assert row is not None, 'Persisted row missing'
    read_json = row[0]
    read_cells = json.loads(read_json)
    read_count = occupied_count_export(read_cells)

    print('\n4) immediately after reading same row back from DB')
    print('  read_json_length=', len(read_json))
    print('  occupied_count=', read_count)
    if read_json != json_text:
        print('  JSON text changed during DB round-trip: length', len(json_text), '->', len(read_json))
    else:
        print('  JSON text identical after DB write/read')

    if occupied_count_export(cells_to_persist) != read_count:
        print('  OCCUPIED count mismatch between pre-write and post-read')
    else:
        print('  OCCUPIED count preserved through DB persistence')

    missing2, changed_to_free2, modified2 = compare_cells(cells_to_persist, read_cells)
    print('  persisted->read comparisons: missing=', len(missing2), 'changed_to_free=', len(changed_to_free2), 'modified=', len(modified2))
    if missing2 or changed_to_free2 or modified2:
        for key, b in missing2[:5]:
            print('     MISSING', key, b)
        for key, b, a in changed_to_free2[:5]:
            print('     CHANGED_TO_FREE', key, b, '->', a)
        for key, b, a in modified2[:5]:
            print('     MODIFIED', key, b, '->', a)

    # Stage 5: before rendering the template
    print('\n5) immediately before rendering the template')
    template_data = json.loads(read_json)
    template_count = occupied_count_export(template_data)
    print('  occupied_count passed to frontend=', template_count)
    if template_count != read_count:
        print('  count changed between DB read and template data load')
    else:
        print('  count unchanged between DB read and template load')

    conn.close()
    shutil.rmtree(tmpdir)


def trace_generate_phases(case):
    print('\n' + '=' * 90)
    print(f"TRACE generate() phases: {case['label']}\n")
    tmpdir = Path(tempfile.mkdtemp(prefix='tt_trace_'))
    tmp_db = tmpdir / 'timetable.db'
    shutil.copy(DB_PATH, tmp_db)
    conn = sqlite3.connect(tmp_db)

    assign_rows = load_subject_assignments(conn, case['dept'], case['semester'], case['semester_type'])
    if not assign_rows:
        print('  No assignment rows found for generate-phase trace.');
        conn.close();
        shutil.rmtree(tmpdir)
        return

    engine = load_from_db(str(tmp_db), case['dept'], case['semester'])
    assignments, skipped = rebuild_engine_assignments(assign_rows, engine, case['dept'], case['semester'])

    tt_eng = TimetableGeneratorEngine()
    cur = conn.cursor()
    cur.execute('SELECT lab_room_capacity FROM departments WHERE id=?', (case['dept'],))
    row = cur.fetchone()
    lab_capacity_map = {case['dept']: row[0] if row and row[0] is not None else 2}
    tt_eng.load_data(assignments, lab_capacity_map=lab_capacity_map)
    tt_eng.block_external_busy_slots(set())

    def count_division():
        result = tt_eng._build_result()
        cells = result.class_timetables.get(case['division'], [])
        return occupied_count_export([cell_to_dict_obj(c) for c in cells])

    tt_eng._reset_state()
    count0 = count_division()
    print('  after _reset_state(): occupied=', count0)

    tt_eng.schedule_labs()
    count1 = count_division()
    print('  after schedule_labs(): occupied=', count1)

    tt_eng.schedule_electives()
    count2 = count_division()
    print('  after schedule_electives(): occupied=', count2)

    tt_eng.schedule_theory()
    count3 = count_division()
    print('  after schedule_theory(): occupied=', count3)

    result_final = tt_eng._build_result()
    final_cells = result_final.class_timetables.get(case['division'], [])
    final_count = occupied_count_export([cell_to_dict_obj(c) for c in final_cells])
    print('  after _build_result(): occupied=', final_count)
    if count3 != final_count:
        print('  NOTE: count differed between schedule_theory and final _build_result()')

    conn.close()
    shutil.rmtree(tmpdir)


if __name__ == '__main__':
    for case in CASES:
        inspect_case(case)
        trace_generate_phases(case)
    print('\nDone.')
