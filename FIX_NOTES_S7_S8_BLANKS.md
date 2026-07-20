# S7 / S8 Blank-Period Fix — Root Cause & Resolution

## Verified facts
- Weekly grid for Year-4 (sem 7 & 8) = 6+6+6+6+5 = **29 slots** (Mon–Thu 6, Fri 5).
- Required LTPR per class (from `courses.hours_per_week`, L+T+P+R for theory /
  P for labs) = **29 for every S7/S8 class EXCEPT CS S7 (CS1/CS2/CS3) = 31**.
- `subject_assignments.subject_code` stores the `courses.id` FK (not the code).
- Grid capacity (29) == required load (29) => **ZERO SLACK**; packing must be perfect.

## Root cause (confirmed by instrumented runs, not assumptions)
1. **Greedy-only, stochastic scheduling.** `app.py` calls
   `generate(use_cpsat=False)` and `ortools` is not installed, so CP-SAT (the
   intended deterministic solver) never runs. The greedy heuristic is seeded
   randomly and does NOT reliably achieve a perfect packing in a zero-slack
   grid. Proof: repeated runs made AIDS S8 ai/ds blanks oscillate 0<->5.
2. **Soft daily caps starved the grid.** Residual unplaced periods were always
   block subjects (Project Phase / Seminar). The assigned teacher was actually
   FREE in a free slot; placement was blocked only by the soft caps
   (`_teacher_can_theory` theory-per-day and `_teacher_day_ok`
   MAX_TOTAL_PER_DAY). The pre-existing `_relaxed_fill_pass` relaxed only the
   theory cap and skipped projects, so it could not finish.
3. **CS S7 is mathematically infeasible:** required 31 > 29 capacity. No solver
   can fit 31 periods into 29 slots (Project Phase II = 11-1-0-1 = 13 plus a
   6-period Seminar plus six 2-hr theory/electives). This is a DATA issue, not
   a generator bug.

Note: creating dummy teachers (requirement #5) did NOT change the outcome —
teacher availability was not the true cause; the soft caps were.

## Fix (in `timetable_generator_engine.py`)
Added a **Phase-3 guaranteed-fill pass** run at the end of `generate()`
(new arg `guarantee_fill=True`, default on):
- `_rebuild_full_teacher_state_from_grid()` — accurate clash state on the
  committed grid.
- `_guaranteed_fill_pass()` — fills every division to `min(required, 29)`:
  places each missing period with the REAL teacher in any free slot (soft caps
  relaxed; the two HARD rules — no teacher clash, no class clash — kept). If the
  real teacher is busy in every free slot, substitutes a temporary dummy
  teacher (requirement #5, verified functional in `test_dummy_path.py`). If no
  free slot exists at all, logs a capacity overflow (CS S7).
- `fill_report()` — per-division expected/generated/blank/shortfall accounting.

`app.py` call made explicit: `generate(use_cpsat=False, guarantee_fill=True)`.

## Result (persisted to timetable.db)
All 20 S7/S8 classes: **29 occupied / 0 blank**. CS S7 fills all 29 slots; the
unavoidable 2-period overflow (31-29) is logged, not silently dropped.
0 teacher clashes, 0 class clashes, 0 cross-run clashes introduced.

## Helper scripts added
- `diagnose_s7s8.py` — per-class Expected/Generated/Blank + blocking reason.
- `regenerate_s7_s8.py` — regenerates & persists S7/S8 timetables.
- `verify.py`, `test_dummy_path.py` — determinism / clash / dummy-path proofs.
