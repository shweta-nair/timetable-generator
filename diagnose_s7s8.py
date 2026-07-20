"""
S7/S8 blank-period diagnostic.
Prints, per class: Expected / Generated / Blank / short subjects / blocking reason.
Reusable before and after the fix.
"""
import io, contextlib, logging, sys
logging.disable(logging.CRITICAL)
import timetable_generator_engine as G
from timetable_generator_engine import DAYS, get_periods, SubjectType, _is_project, _is_cw
from subject_assignment_engine import load_from_db

DEPTS = ["AIDS","AU","CE","CS","EC","EEE","ME"]

def required_map(engine):
    """(div_id, subject_id) -> (subject_name, required_periods) for main assignments."""
    req = {}
    for a in engine.assignments:
        if a.role != "main":
            continue
        if a.subject.subject_type == SubjectType.LAB:
            need = a.subject.periods_per_week if a.subject.periods_per_week > 0 else 3
        else:
            need = max(a.subject.periods_per_week, 1)
        req[(a.division.division_id, a.subject.subject_id)] = (a.subject.name, need, a)
    return req

def build_busy(engine):
    """teacher_busy {(tid,day,pnum): subject_id} and per-day totals from final grid."""
    busy = {}
    total_day = {}
    for (div_id, day, pnum), a in list(engine._grid.items()):
        busy[(a.teacher.teacher_id, day, pnum)] = a.subject.subject_id
        total_day[(a.teacher.teacher_id, day)] = total_day.get((a.teacher.teacher_id, day), 0) + 1
    for k in engine._external_busy_keys:
        busy.setdefault(k, "__EXTERNAL__")
    return busy, total_day

def diagnose(dept, sem):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        gen = G.generate_from_db("timetable.db", dept, sem, use_cpsat=False)
    res = gen._result
    req = required_map(gen)
    busy, total_day = build_busy(gen)
    MAX_TOTAL = G.MAX_TOTAL_PER_DAY

    reports = []
    for div_id in sorted(res.class_timetables.keys()):
        cells = res.class_timetables[div_id]
        year = gen.year_map.get(div_id, 4)
        occ = sum(1 for c in cells if not c.is_free)
        blank = sum(1 for c in cells if c.is_free)
        expected = sum(n for (d,sid),(nm,n,a) in req.items() if d == div_id)
        # placed per subject
        placed = {}
        for c in cells:
            if not c.is_free and c.subject is not None:
                placed[c.subject.subject_id] = placed.get(c.subject.subject_id, 0) + 1
        # free slots for this division
        free_slots = [(c.day, c.period.number) for c in cells if c.is_free]
        shorts = []
        for (d, sid), (nm, need, a) in req.items():
            if d != div_id:
                continue
            got = placed.get(sid, 0)
            if got < need:
                tid = a.teacher.teacher_id
                is_dummy = gen._teacher_is_dummy.get(tid, False)
                # categorize why the (need-got) missing periods cannot be placed
                reasons = set()
                if not free_slots:
                    reasons.add("division grid FULL (0 free slots) -> capacity overflow (required>29)")
                else:
                    for (day, pnum) in free_slots:
                        if busy.get((tid, day, pnum)) not in (None,) and not is_dummy:
                            reasons.add("assigned teacher BUSY at every free slot (teacher-availability)")
                        elif (not is_dummy) and total_day.get((tid, day), 0) >= MAX_TOTAL:
                            reasons.add(f"teacher at absolute daily cap ({MAX_TOTAL}) on days with free slots")
                        else:
                            reasons.add("placeable free slot exists (solver ordering failure)")
                shorts.append((nm, need, got, sorted(reasons)))
        reports.append((div_id, expected, occ, blank, shorts))
    return reports

if __name__ == "__main__":
    grand_blank = 0
    for dept in DEPTS:
        for sem in (7,8):
            for (div_id, expected, occ, blank, shorts) in diagnose(dept, sem):
                grand_blank += blank
                tag = "  <== BLANKS" if blank else ""
                print(f"{dept} S{sem} {div_id:<6} | Expected={expected:<3} Generated={occ:<3} Blank={blank:<3}{tag}")
                for (nm, need, got, reasons) in shorts:
                    print(f"        - {nm[:44]:<44} need={need} placed={got} missing={need-got}")
                    for r in reasons:
                        print(f"            reason: {r}")
    print(f"\nGRAND TOTAL blank periods across all S7/S8 classes: {grand_blank}")
