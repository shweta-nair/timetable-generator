"""
Regenerate S7 and S8 class + teacher timetables into timetable.db using the
fixed generator (Phase-3 guaranteed fill). Mirrors app.py's generation flow:
per-department assign() then generate(use_cpsat=False, guarantee_fill=True),
with cross-run teacher-busy blocking within each semester group so no teacher
is double-booked at the same clock time across departments.
"""
import sqlite3, json
from datetime import datetime as _dt
from subject_assignment_engine import load_from_db
from timetable_generator_engine import (
    TimetableGeneratorEngine, _build_year_map,
)

DB = "timetable.db"
DEPTS = ["AIDS", "AU", "CE", "CS", "EC", "EEE", "ME"]
SEM_TYPE = {7: "odd", 8: "even"}

now_str   = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
today_str = _dt.now().strftime('%Y-%m-%d')

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# lab-room capacity per dept
lab_cap = {r["id"]: (r["lab_room_capacity"] or 2)
           for r in cur.execute("SELECT id, lab_room_capacity FROM departments")}

summary = []
for sem in (7, 8):
    sem_type = SEM_TYPE[sem]
    cross_run_busy = {}
    for dept in DEPTS:
        ae = load_from_db(DB, dept, sem)
        assignments = ae.assign()
        if not assignments:
            continue
        tt = TimetableGeneratorEngine()
        tt.load_data(
            assignments = assignments,
            teachers    = ae.teachers,
            divisions   = ae.divisions,
            departments = ae.departments,
            year_map    = _build_year_map(ae.divisions),
            lab_capacity_map = lab_cap,
        )
        tt.block_external_busy_slots(cross_run_busy)
        tt.generate(use_cpsat=False, guarantee_fill=True)
        exported = tt.export_to_dict()

        # cross-run clash accounting (within this semester group)
        new_busy = tt.export_teacher_busy_intervals()
        clashes = TimetableGeneratorEngine.find_cross_run_teacher_clashes(
            cross_run_busy, new_busy)
        for tid, intervals in new_busy.items():
            cross_run_busy.setdefault(tid, []).extend(intervals)

        # ── wipe old rows for this dept+sem, write fresh ──
        cur.execute("DELETE FROM timetables WHERE dept_id=? AND semester=?",
                    (dept, str(sem)))

        for div_id, cells in exported.get("class_timetables", {}).items():
            cur.execute(
                "INSERT INTO timetables (dept_id,semester,semester_type,division,"
                "timetable_type,key,effective_date,data_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (dept, str(sem), sem_type, div_id, "class", div_id,
                 today_str, json.dumps(cells), now_str))

        for tid, cells in exported.get("teacher_timetables", {}).items():
            cur.execute(
                "INSERT INTO timetables (dept_id,semester,semester_type,division,"
                "timetable_type,key,effective_date,data_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (dept, str(sem), sem_type, tid, "teacher", tid,
                 today_str, json.dumps(cells), now_str))

        rep = tt.fill_report()
        for div_id, r in sorted(rep.items()):
            summary.append((dept, sem, div_id, r["expected"], r["generated"],
                            r["blank"], len(clashes), len(getattr(tt, "_fill_substitutions", []))))

conn.commit()
conn.close()

print(f"{'CLASS':<18}{'exp':>4}{'gen':>4}{'blank':>6}{'xclash':>7}{'subs':>5}")
for dept, sem, div, exp, gen, blank, xc, subs in summary:
    print(f"{dept+' S'+str(sem)+' '+div:<18}{exp:>4}{gen:>4}{blank:>6}{xc:>7}{subs:>5}")
print("\nRegeneration complete — timetables written to timetable.db")
