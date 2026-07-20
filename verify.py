import io, contextlib, logging
logging.disable(logging.CRITICAL)
import timetable_generator_engine as G

DEPTS = ["AIDS","AU","CE","CS","EC","EEE","ME"]
RUNS = 5
worst = {}
clash_total = 0
subs_total = 0
for run in range(RUNS):
    for dept in DEPTS:
        for sem in (7,8):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                gen = G.generate_from_db("timetable.db", dept, sem, use_cpsat=False)
            tc = gen.check_teacher_clash()
            cc = gen.check_class_clash()
            clash_total += len(tc) + len(cc)
            subs_total += len(getattr(gen, "_fill_substitutions", []))
            for div, cells in gen._result.class_timetables.items():
                blank = sum(1 for c in cells if c.is_free)
                key = f"{dept} S{sem} {div}"
                worst[key] = max(worst.get(key, 0), blank)

print(f"Over {RUNS} runs x {len(DEPTS)} depts x 2 sems:")
print(f"  total teacher+class clashes introduced: {clash_total}")
print(f"  total temporary-teacher substitutions used: {subs_total}")
print(f"  MAX blank per class across all runs:")
anyblank = False
for k in sorted(worst):
    if worst[k] > 0:
        anyblank = True
        print(f"    {k}: max_blank={worst[k]}")
if not anyblank:
    print("    ALL classes: 0 blank in every run  ✔")
