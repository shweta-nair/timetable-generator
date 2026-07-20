# Forces the "real teacher unavailable everywhere" branch to prove the
# temporary-teacher substitution (requirement #5) works and stays clash-free.
import io, contextlib, logging
logging.disable(logging.CRITICAL)
import timetable_generator_engine as G

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    # Generate WITHOUT the fill pass so blanks + unplaced remain.
    from subject_assignment_engine import load_from_db
    ae = load_from_db("timetable.db", "AIDS", 8)
    asg = ae.assign()
    from timetable_generator_engine import _build_year_map, TimetableGeneratorEngine
    gen = TimetableGeneratorEngine()
    gen.load_data(assignments=asg, teachers=ae.teachers, divisions=ae.divisions,
                  departments=ae.departments, year_map=_build_year_map(ae.divisions))
    gen.generate(use_cpsat=False, guarantee_fill=False)

    # Rebuild state, then artificially mark EVERY real teacher busy in EVERY slot
    gen._rebuild_full_teacher_state_from_grid()
    from timetable_generator_engine import DAYS, get_periods
    for div in gen.divisions:
        yr = gen.year_map.get(div.division_id, 4)
        for d in DAYS:
            for p in get_periods(yr, d):
                for t in list(gen.teachers):
                    if not gen._teacher_is_dummy.get(t.teacher_id):
                        gen._teacher_busy.setdefault((t.teacher_id, d, p.number), "__FORCED_BUSY__")
    before_blank = {div.division_id: sum(
        1 for d in DAYS for p in get_periods(gen.year_map.get(div.division_id,4), d)
        if gen._slot_free(div.division_id, d, p.number)) for div in gen.divisions}

    gen._guaranteed_fill_pass(use_dummy_for_unavailable=True)
    tc = gen.check_teacher_clash(); cc = gen.check_class_clash()
    gen._result = gen._build_result()

after_blank = {div: sum(1 for c in cells if c.is_free)
               for div, cells in gen._result.class_timetables.items()}
print("Forced scenario: every REAL teacher busy in every slot")
print("  blanks BEFORE fill:", before_blank)
print("  blanks AFTER  fill:", after_blank)
print("  temporary-teacher substitutions used:", len(gen._fill_substitutions))
print("  teacher clashes:", len(tc), " class clashes:", len(cc))
print("  sample substitution:", (gen._fill_substitutions[0] if gen._fill_substitutions else "none"))
