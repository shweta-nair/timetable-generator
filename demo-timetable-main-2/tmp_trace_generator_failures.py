#!/usr/bin/env python3
import inspect
import json
import random
import sqlite3
import logging
from collections import defaultdict, deque
from pathlib import Path

from subject_assignment_engine import (
    load_from_db,
    Teacher as EngTeacher,
    SubjectAssignment as EngSA,
    Division as EngDiv,
    Designation as EngDesig,
)
from timetable_generator_engine import (
    TimetableGeneratorEngine,
    SubjectType,
    DAYS,
    get_periods,
    get_lunch_break_after,
    MAX_THEORY_PER_DAY,
    THEORY_CAP_WITH_LAB,
    MAX_TOTAL_PER_DAY,
    PARALLEL_LAB_PPW,
    PARALLEL_LAB_BLOCK,
    _is_project,
    _is_cw,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'timetable.db'

CASES = [
    {'label': 'AIDS S8 AI', 'dept': 'AIDS', 'semester': 8, 'semester_type': 'even', 'division': 'ai'},
    {'label': 'AIDS S8 DS', 'dept': 'AIDS', 'semester': 8, 'semester_type': 'even', 'division': 'ds'},
    {'label': 'CS S7 CS1', 'dept': 'CS', 'semester': 7, 'semester_type': 'odd', 'division': 'CS1'},
    {'label': 'CS S7 CS2', 'dept': 'CS', 'semester': 7, 'semester_type': 'odd', 'division': 'CS2'},
    {'label': 'CS S7 CS3', 'dept': 'CS', 'semester': 7, 'semester_type': 'odd', 'division': 'CS3'},
]

LINE_NUMBERS = {}
for name, substring in [
    ('schedule_theory_failure', 'if not scored:'),
    ('find_elective_return_none', 'return None'),
    ('try_pin_elective_return_false', 'return False'),
    ('place_block_return_false', 'return False'),
    ('place_parallel_return_false', 'return False'),
]:
    fn = getattr(TimetableGeneratorEngine, {
        'schedule_theory_failure': '_schedule_theory_for_division',
        'find_elective_return_none': '_find_elective_slot_scored',
        'try_pin_elective_return_false': '_try_pin_elective_set',
        'place_block_return_false': '_place_block_scored',
        'place_parallel_return_false': '_place_parallel_lab_pair',
    }[name])
    src, ln = inspect.getsourcelines(fn)
    for idx, line in enumerate(src):
        if substring in line:
            LINE_NUMBERS[name] = ln + idx
            break


def get_assignment_key(a):
    return f"{a.subject.subject_id}|{a.division.division_id}|{a.teacher.teacher_id}|{a.role}"


def simplify_reason(reason):
    if reason is None:
        return 'valid'
    if 'division' in reason.lower() and 'occupied' in reason.lower():
        return 'Division already occupied'
    if 'teacher unavailable' in reason.lower() or 'busy with' in reason.lower():
        return 'Teacher unavailable'
    if 'theory limit' in reason.lower():
        return 'Teacher daily theory limit reached'
    if 'total daily limit' in reason.lower():
        return 'Teacher total daily limit reached'
    if 'lab block' in reason.lower():
        return 'Lab block conflict'
    if 'anchor' in reason.lower() or 'elective' in reason.lower():
        return 'Elective conflict'
    return reason


class DebugTimetableGenerator(TimetableGeneratorEngine):
    def __init__(self):
        super().__init__()
        self.debug_attempts = []
        self.debug_context = None
        self.debug_best_candidates = []

    def _reset_state(self):
        super()._reset_state()
        self.debug_attempts = []
        self.debug_context = None
        self.debug_best_candidates = []

    def _set_context(self, phase, description, target):
        self.debug_context = {'phase': phase, 'description': description, 'target': target}

    def _record(self, day, pnums, reason, valid):
        if self.debug_context is None:
            return
        if isinstance(pnums, int):
            pnums = [pnums]
        self.debug_attempts.append({
            'phase': self.debug_context['phase'],
            'description': self.debug_context['description'],
            'target': self.debug_context['target'],
            'day': day,
            'periods': pnums,
            'reason': reason,
            'valid': valid,
        })

    def _teacher_can_theory_reason(self, tid, day):
        if self._teacher_is_dummy.get(tid):
            return True, None
        has_cap_reducer = ((tid, day) in self._teacher_lab_day
                           or (tid, day) in self._teacher_project_day)
        base_cap = 3 if self._teacher_is_bsh.get(tid) else 2
        cap = THEORY_CAP_WITH_LAB if has_cap_reducer else base_cap
        used = self._teacher_theory_day.get((tid, day), 0)
        if used < cap:
            return True, None
        return False, f'Teacher daily theory limit reached ({used}/{cap})'

    def _teacher_day_ok_reason(self, tid, day, extra=1):
        if self._teacher_is_dummy.get(tid):
            return True, None
        used = self._teacher_total_day.get((tid, day), 0)
        if used + extra <= MAX_TOTAL_PER_DAY:
            return True, None
        return False, f'Teacher total daily limit reached ({used}/{MAX_TOTAL_PER_DAY})'

    def _teacher_free_reason(self, tid, day, pnum, subject_id=''):
        existing = self._teacher_busy.get((tid, day, pnum))
        if existing is None:
            return True, None
        if subject_id and existing == subject_id:
            return True, None
        return False, f'Teacher unavailable (busy with {existing})'

    def _find_consec_slots(self, a, consec, is_lab=True):
        results = []
        self._set_context('schedule_labs', 'lab block candidate', get_assignment_key(a))
        year = self.year_map.get(a.division.division_id, 2)
        tid = a.teacher.teacher_id
        div_id = a.division.division_id
        asst = self._assistant_map.get((a.subject.subject_id, div_id))

        for day in DAYS:
            if is_lab and (div_id, day) in self._div_lab_day:
                reason = 'Lab block conflict: lab already scheduled same day'
                self._record(day, [], reason, False)
                continue
            if not self._teacher_day_ok_reason(tid, day, extra=consec)[0]:
                ok, reason = self._teacher_day_ok_reason(tid, day, extra=consec)
                self._record(day, [], reason, False)
                continue

            periods = get_periods(year, day)
            lunch = get_lunch_break_after(year, day)
            for i in range(len(periods) - consec + 1):
                run = [periods[i + k].number for k in range(consec)]
                if run[0] <= lunch < run[-1]:
                    self._record(day, run, 'Lab block conflict: spans lunch', False)
                    continue
                if not all(self._slot_free(div_id, day, p) for p in run):
                    self._record(day, run, 'Division already occupied', False)
                    continue
                if not all(self._teacher_free(tid, day, p, a.subject.subject_id) for p in run):
                    self._record(day, run, 'Teacher unavailable', False)
                    continue
                if asst and not all(self._teacher_free(asst.teacher_id, day, p) for p in run):
                    self._record(day, run, 'Teacher unavailable (assistant busy)', False)
                    continue
                self._record(day, run, None, True)
                results.append((day, run))
        return results

    def _find_consec_slots_parallel(self, lab1, lab2, consec):
        self._set_context('schedule_labs', 'parallel lab candidate', get_assignment_key(lab1))
        results = []
        div_id = lab1.division.division_id
        year = self.year_map.get(div_id, 2)
        tid1 = lab1.teacher.teacher_id
        tid2 = lab2.teacher.teacher_id
        sid1 = lab1.subject.subject_id
        sid2 = lab2.subject.subject_id
        asst1 = self._assistant_map.get((sid1, div_id))
        asst2 = self._assistant_map.get((sid2, div_id))

        for day in DAYS:
            if (div_id, day) in self._div_lab_day:
                self._record(day, [], 'Lab block conflict: lab already scheduled same day', False)
                continue
            if not self._teacher_day_ok_reason(tid1, day, extra=consec)[0]:
                ok, reason = self._teacher_day_ok_reason(tid1, day, extra=consec)
                self._record(day, [], f'Teacher total daily limit reached for {tid1}', False)
                continue
            if not self._teacher_day_ok_reason(tid2, day, extra=consec)[0]:
                ok, reason = self._teacher_day_ok_reason(tid2, day, extra=consec)
                self._record(day, [], f'Teacher total daily limit reached for {tid2}', False)
                continue
            periods = get_periods(year, day)
            lunch = get_lunch_break_after(year, day)
            for i in range(len(periods) - consec + 1):
                run = [periods[i + k].number for k in range(consec)]
                if run[0] <= lunch < run[-1]:
                    self._record(day, run, 'Lab block conflict: spans lunch', False)
                    continue
                if not all(self._slot_free(div_id, day, p) for p in run):
                    self._record(day, run, 'Division already occupied', False)
                    continue
                if not all(self._teacher_free(tid1, day, p, sid1) for p in run):
                    self._record(day, run, 'Teacher unavailable', False)
                    continue
                if not all(self._teacher_free(tid2, day, p, sid2) for p in run):
                    self._record(day, run, 'Teacher unavailable', False)
                    continue
                if asst1 and not all(self._teacher_free(asst1.teacher_id, day, p) for p in run):
                    self._record(day, run, 'Teacher unavailable (assistant busy)', False)
                    continue
                if asst2 and not all(self._teacher_free(asst2.teacher_id, day, p) for p in run):
                    self._record(day, run, 'Teacher unavailable (assistant busy)', False)
                    continue
                self._record(day, run, None, True)
                results.append((day, run))
        return results

    def _find_elective_slot_scored(self, asgns):
        self._set_context('schedule_electives', 'elective slot candidate', f"{asgns[0].subject.elective_group}|{asgns[0].subject.subject_id}")
        year = self.year_map.get(asgns[0].division.division_id, 2)
        candidates = []
        for day in DAYS:
            for period in get_periods(year, day):
                pnum = period.number
                valid = True
                reason = None
                for a in asgns:
                    if not self._slot_free(a.division.division_id, day, pnum):
                        valid = False
                        reason = 'Division already occupied'
                        break
                    if not self._teacher_free(a.teacher.teacher_id, day, pnum, a.subject.subject_id):
                        valid = False
                        reason = 'Teacher unavailable'
                        break
                    if not self._teacher_can_theory_reason(a.teacher.teacher_id, day)[0]:
                        valid = False
                        reason = 'Teacher daily theory limit reached'
                        break
                    if not self._teacher_day_ok_reason(a.teacher.teacher_id, day)[0]:
                        valid = False
                        reason = 'Teacher total daily limit reached'
                        break
                if valid:
                    sc = self._elective_slot_score(day, pnum, year) + random.randint(0, 2)
                    candidates.append((sc, day, pnum))
                    self._record(day, pnum, None, True)
                else:
                    self._record(day, pnum, reason, False)
        if not candidates:
            return None
        candidates.sort(key=lambda t: -t[0])
        _, best_day, best_pnum = candidates[0]
        return (best_day, best_pnum)

    def _try_pin_elective_set(self, asgns, day, pnum):
        self._set_context('schedule_electives', 'elective anchor test', f"{asgns[0].subject.elective_group}|{asgns[0].subject.subject_id}@{day}P{pnum}")
        year = self.year_map.get(asgns[0].division.division_id, 2)
        if not any(p.number == pnum for p in get_periods(year, day)):
            self._record(day, pnum, 'Invalid period for day', False)
            return False
        for a in asgns:
            if not self._slot_free(a.division.division_id, day, pnum):
                self._record(day, pnum, 'Division already occupied', False)
                return False
            if not self._teacher_free(a.teacher.teacher_id, day, pnum, a.subject.subject_id):
                self._record(day, pnum, 'Teacher unavailable', False)
                return False
            if not self._teacher_can_theory_reason(a.teacher.teacher_id, day)[0]:
                self._record(day, pnum, 'Teacher daily theory limit reached', False)
                return False
            if not self._teacher_day_ok_reason(a.teacher.teacher_id, day)[0]:
                self._record(day, pnum, 'Teacher total daily limit reached', False)
                return False
        for a in asgns:
            self._book(a, day, pnum, is_lab=False)
        return True

    def _schedule_theory_for_division(self, div_id, asgns):
        self._set_context('schedule_theory', 'schedule theory queue', div_id)
        year = self.year_map.get(div_id, 2)
        queue = []
        for a in asgns:
            already = self._count_placed(a.subject.subject_id, div_id)
            ppw = max(a.subject.periods_per_week, 1)
            remaining = ppw - already
            queue.extend([a] * remaining)
        if not queue:
            return
        theory_queue = []
        for a in queue:
            if _is_project(a.subject):
                self._place_project_blocks(a, a.subject.periods_per_week or 1)
            else:
                theory_queue.append(a)
        queue = self._interleave(theory_queue)
        day_scan_order = list(DAYS)
        random.shuffle(day_scan_order)
        max_periods = max(len(get_periods(year, d)) for d in DAYS)
        all_slots = []
        for p_idx in range(max_periods):
            for day in day_scan_order:
                periods = get_periods(year, day)
                if p_idx < len(periods):
                    all_slots.append((day, periods[p_idx].number))
        for a in queue:
            tid = a.teacher.teacher_id
            scored = []
            self._set_context('schedule_theory', f'theory slot candidate {get_assignment_key(a)}', get_assignment_key(a))
            for day, pnum in all_slots:
                if self._grid.get((div_id, day, pnum)) is not None:
                    self._record(day, pnum, 'Division already occupied', False)
                    continue
                if not self._teacher_free(tid, day, pnum, a.subject.subject_id):
                    self._record(day, pnum, 'Teacher unavailable', False)
                    continue
                if not self._teacher_can_theory_reason(tid, day)[0]:
                    self._record(day, pnum, 'Teacher daily theory limit reached', False)
                    continue
                if not self._teacher_day_ok_reason(tid, day)[0]:
                    self._record(day, pnum, 'Teacher total daily limit reached', False)
                    continue
                sc = self._theory_slot_score(a, day, pnum) + random.randint(0, 3)
                scored.append((sc, day, pnum))
                self._record(day, pnum, None, True)
            if not scored:
                self.debug_unplaced.append({'phase': 'schedule_theory', 'assignment': get_assignment_key(a), 'subject': a.subject.name, 'teacher': a.teacher.teacher_id, 'remaining': 1})
                self._log_unplaced(a, 1, 'theory period')
                continue
            scored.sort(key=lambda t: -t[0])
            _, best_day, best_pnum = scored[0]
            self._book(a, best_day, best_pnum, is_lab=False)

    def _relaxed_fill_pass(self, parallel_groups):
        self._set_context('relaxed_pass', 'relaxed fill candidate', None)
        super()._relaxed_fill_pass(parallel_groups)

    def _build_result(self):
        self.debug_context = None
        return super()._build_result()


def load_assignments(conn, case):
    cur = conn.cursor()
    cur.execute(
        'SELECT teacher_id, teacher_name, subject_code, role, division_id FROM subject_assignments '
        'WHERE dept_id=? AND semester=? AND semester_type=?',
        (case['dept'], case['semester'], case['semester_type'])
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


def analyze_case(case):
    print('\n' + '=' * 80)
    print(f"CASE: {case['label']}")
    conn = sqlite3.connect(DB_PATH)
    assign_rows = load_assignments(conn, case)
    engine = load_from_db(str(DB_PATH), case['dept'], case['semester'])
    assignments, skipped = rebuild_engine_assignments(assign_rows, engine, case['dept'], case['semester'])
    if skipped:
        print('Skipped assignments:', skipped)
    debug_eng = DebugTimetableGenerator()
    cur = conn.cursor()
    cur.execute('SELECT lab_room_capacity FROM departments WHERE id=?', (case['dept'],))
    row = cur.fetchone()
    lab_capacity_map = {case['dept']: row[0] if row and row[0] is not None else 2}
    debug_eng.load_data(assignments, lab_capacity_map=lab_capacity_map)
    debug_eng.block_external_busy_slots(set())
    random.seed(0)
    result = debug_eng.generate(use_cpsat=True)

    counts = defaultdict(int)
    planned = defaultdict(int)
    actual = defaultdict(int)
    for a in assignments:
        key = (a.subject.subject_id, a.subject.name, a.teacher.teacher_id, a.teacher.name, a.division.division_id)
        planned[key] = max(a.subject.periods_per_week, 1)
    for (div_id, day, pnum), a in debug_eng._grid.items():
        key = (a.subject.subject_id, a.subject.name, a.teacher.teacher_id, a.teacher.name, a.division.division_id)
        actual[key] += 1
    print('\nRequired periods vs scheduled counts:')
    for key, req in sorted(planned.items(), key=lambda x: (x[0][4], x[0][0])):
        sched = actual.get(key, 0)
        print(f"  {key[1]} / {key[3]} / div={key[4]} required={req} scheduled={sched}")
    unscheduled = [
        {'subject': key[1], 'teacher': key[3], 'division': key[4], 'required': req, 'scheduled': actual.get(key, 0), 'key': key}
        for key, req in planned.items()
        if actual.get(key, 0) < req
    ]
    if not unscheduled:
        print('No unscheduled subjects in final schedule.')
        return
    print('\nUnscheduled subjects:')
    for info in unscheduled:
        rem = info['required'] - info['scheduled']
        print(f"  {info['subject']} ({info['teacher']}) div={info['division']} missing={rem}")
    print('\nFailed placement attempts for unscheduled subjects:')
    failures_by_assignment = defaultdict(list)
    for entry in debug_eng.debug_attempts:
        target = entry['target']
        if target is None:
            continue
        for info in unscheduled:
            subj_id = info['key'][0]
            div_id = info['key'][4]
            if subj_id in target and div_id in target:
                failures_by_assignment[target].append(entry)
    for info in unscheduled:
        key = info['key']
        subject_id, subject_name, tid, teacher_name, division_id = key
        print('\n---')
        print(f"Subject: {subject_name}")
        print(f"Teacher: {teacher_name}")
        print(f"Division: {division_id}")
        print(f"Required periods: {info['required']}")
        print(f"Scheduled periods: {info['scheduled']}")
        print(f"Remaining periods: {info['required'] - info['scheduled']}")
        relevant = [e for e in debug_eng.debug_attempts if e['target'] and subject_id in e['target'] and division_id in e['target']]
        if not relevant:
            print('  No debug attempts recorded for this subject.')
            continue
        by_phase = defaultdict(list)
        for entry in relevant:
            by_phase[entry['phase']].append(entry)
        for phase, entries in by_phase.items():
            print(f"  Phase: {phase}")
            for i, entry in enumerate(entries, 1):
                print(f"    Attempt {i}: {entry['day']} P{entry['periods']} -> {simplify_reason(entry['reason'])}")
    print('\nConstraint rejection counts:')
    counts = defaultdict(int)
    for entry in debug_eng.debug_attempts:
        if not entry['valid']:
            counts[simplify_reason(entry['reason'])] += 1
    for reason, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {reason}: {cnt}")
    print('\nLine numbers for failure conditions:')
    for name, ln in LINE_NUMBERS.items():
        print(f"  {name}: line {ln}")


if __name__ == '__main__':
    for case in CASES:
        analyze_case(case)
    print('\nDone.')
