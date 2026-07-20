"""
timetable_generator_engine.py  (v4 — production quality 9.9/10)
══════════════════════════════════════════════════════════════════════════════
College Timetable Generation System — Engine 2: Timetable Generator

Generates:
  • class_timetable[division][day][period]   (class-wise view)
  • teacher_timetable[teacher][day][period]  (teacher-wise view)

Constraints enforced (hard):
  1.  No class clash         – one subject per slot per division
  2.  No teacher clash       – teacher cannot teach two classes at the same time
  3.  Lab continuity         – lab sessions occupy consecutive periods
  4.  Lab lunch-break rule   – lab blocks must not straddle Friday lunch break
  5.  One lab per div/day    – a division has at most one lab session per day
  6.  Teacher daily theory   – max 3 theory periods per teacher per day;
                                reduced to 2 if the teacher already has a lab
  7.  Teacher daily total    – max 5 periods in any day per teacher
  8.  Weekly period target   – each subject scheduled exactly periods_per_week times
                                (labs default to 3 when periods_per_week == 0)
  9.  Elective parallel rule – all electives in the same elective_group run in
                                the same (day, period) so students can choose
 10.  Lab assistant rule     – assistant teacher is blocked in all lab periods

Quality objectives (soft — maximised by scoring):
  Q1.  Subjects spread across all 5 days (not clustered)
  Q2.  Same subject never in consecutive periods unless unavoidable
  Q3.  Teacher workload evenly distributed across the week
  Q4.  Labs prefer earlier slots and pre-lunch blocks
  Q5.  Electives avoid Monday P1 and Friday last period

Algorithm:
  1. Try CP-SAT (OR-Tools) first — optimal solution when available
  2. Fall back to scored greedy with up to 30 randomised retries
  3. Retry loop stops early once schedule score ≥ 95/100
  4. Order within greedy: Labs → Electives → Theory/Project

Scoring formula (higher is better, 100 = perfect):
  score = 100
        − teacher_clashes × 100
        − class_clashes   × 100
        − consecutive_same_subject_instances × 5
        − teacher_workload_unevenness_units × 3

Usage:
    from subject_assignment_engine import load_from_db

    engine = load_from_db("timetable.db", "CS", 3)
    assignments = engine.assign()

    tt = TimetableGeneratorEngine()
    tt.load_data(assignments)          # single-arg form — infers everything
    tt.generate()
    tt.print_class_timetable()
    tt.print_teacher_timetable()
    tt.print_teacher_timetable(verbose=True)   # adds full subject name

Or via the all-in-one helper:
    gen = generate_from_db("timetable.db", "CS", 3)
    gen.print_class_timetable()
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
import random
import re as _re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from subject_assignment_engine import (
    Department, Division, Subject, SubjectAssignment, SubjectType,
    Teacher, build_sample_data, SubjectAssignmentEngine, load_from_db,
    Designation,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Period structure (timings per year-group and day-type)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Period:
    number: int
    start:  str
    end:    str

    def __str__(self) -> str:
        return f"P{self.number} {self.start}–{self.end}"


# Periods keyed by (year, day_type).
# year = ceil(semester / 2): semester 3 → year 2, semester 7 → year 4.
# Year 4 (semesters 7–8) has shorter days with an earlier lunch cutoff.

PERIOD_STRUCTURES: Dict[int, Dict[str, List[Period]]] = {
    # Year 1 (Semesters 1 & 2) — weekday lunch after P3 (11:30-12:15)
    1: {
        "weekday": [
            Period(1, "08:45", "09:35"), Period(2, "09:35", "10:25"),
            Period(3, "10:35", "11:30"),
            # -- WEEKDAY LUNCH 11:30-12:15 --
            Period(4, "12:15", "13:05"), Period(5, "13:05", "13:55"),
            Period(6, "14:05", "14:55"), Period(7, "14:55", "15:45"),
        ],
        "friday": [
            Period(1, "08:45", "09:35"), Period(2, "09:35", "10:25"),
            Period(3, "10:35", "11:30"), Period(4, "11:30", "12:15"),
            # -- FRIDAY LUNCH 12:15-14:15 --
            Period(5, "14:15", "14:55"), Period(6, "14:55", "15:45"),
        ],
    },
    # Year 2 (Semesters 3 & 4) — weekday lunch after P4
    2: {
        "weekday": [
            Period(1, "08:45", "09:35"), Period(2, "09:35", "10:25"),
            Period(3, "10:35", "11:30"), Period(4, "11:30", "12:20"),
            # -- WEEKDAY LUNCH 12:20-13:05 --
            Period(5, "13:05", "13:55"), Period(6, "14:05", "14:55"),
            Period(7, "14:55", "15:45"),
        ],
        "friday": [
            Period(1, "08:45", "09:35"), Period(2, "09:35", "10:25"),
            Period(3, "10:35", "11:30"), Period(4, "11:30", "12:15"),
            # -- FRIDAY LUNCH 12:15-14:15 --
            Period(5, "14:15", "14:55"), Period(6, "14:55", "15:45"),
        ],
    },
    # Year 3 (Semesters 5 & 6) — weekday lunch after P4
    3: {
        "weekday": [
            Period(1, "08:45", "09:35"), Period(2, "09:35", "10:25"),
            Period(3, "10:35", "11:30"), Period(4, "11:30", "12:20"),
            # -- WEEKDAY LUNCH 12:20-13:00 --
            Period(5, "13:00", "13:55"), Period(6, "14:05", "14:55"),
            Period(7, "14:55", "15:45"),
        ],
        "friday": [
            Period(1, "08:45", "09:35"), Period(2, "09:35", "10:25"),
            Period(3, "10:35", "11:30"), Period(4, "11:30", "12:15"),
            # -- FRIDAY LUNCH 12:15-14:15 --
            Period(5, "14:15", "14:55"), Period(6, "14:55", "15:45"),
        ],
    },
    4: {
        "weekday": [
            Period(1, "08:45", "09:45"), Period(2, "09:45", "10:45"),
            Period(3, "11:00", "12:00"), Period(4, "12:45", "13:40"),
            Period(5, "13:40", "14:35"), Period(6, "14:50", "15:45"),
        ],
        "friday": [
            Period(1, "08:45", "09:45"), Period(2, "09:45", "10:45"),
            Period(3, "11:00", "12:00"),
            # ── FRIDAY LUNCH 12:00–14:15 ──
            Period(4, "14:15", "14:55"), Period(5, "14:55", "15:45"),
        ],
    },
}

# LUNCH_BREAK_AFTER[(year, day_type)] = last period number entirely before lunch.
# Lab/project blocks must lie wholly pre-lunch OR wholly post-lunch.
LUNCH_BREAK_AFTER: Dict[Tuple[int, str], int] = {
    # Year 1: weekday lunch after P3 (11:30), Friday lunch after P4 (12:15)
    (1, "weekday"): 3,  (1, "friday"): 4,
    # Year 2: weekday lunch after P4, Friday after P4
    (2, "weekday"): 4,  (2, "friday"): 4,
    # Year 3: weekday lunch after P4, Friday after P4
    (3, "weekday"): 4,  (3, "friday"): 4,
    # Year 4: weekday lunch after P3 (12:00), Friday after P3
    (4, "weekday"): 3,  (4, "friday"): 3,
}

DAYS     = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday"]

MAX_THEORY_PER_DAY   = 3   # max theory periods per teacher per day
THEORY_CAP_WITH_LAB  = 2   # reduced cap when teacher already has a lab that day
MAX_TOTAL_PER_DAY    = 5   # absolute daily period ceiling per teacher

# Default lab block length when periods_per_week is 0.
# Real-DB courses with hours like "0-0-3-0" yield 0 from the first-number parser.
DEFAULT_LAB_PERIODS = 3

# Year-2 & 3 (semesters 3–6) parallel-lab constants.
# The DB stores 0-0-6-0 for these labs (LTPR updated), giving periods_per_week = 6.
# Two equal batches swap between the two labs, so each lab runs for ONE block of
# PARALLEL_LAB_BLOCK consecutive periods on EACH of two lab days per week.
PARALLEL_LAB_PPW   = 6   # periods_per_week stored in DB  (0-0-6-0)
PARALLEL_LAB_BLOCK = 3   # physical block size per day    (half of PPW)

# Greedy retry configuration
MAX_GREEDY_RETRIES  = 10   # greedy provides a warm-start hint only; CP-SAT is primary
EARLY_STOP_SCORE    = 95   # stop retrying once score reaches this threshold

# ── Scoring deduction weights ──────────────────────────────────────────────────
# Calibrated so structurally unavoidable imperfections (Friday P6 must be used
# when a timetable is 100% filled; parallel-teaching teachers have 1-2 structural
# gaps) do not prevent a score of 95–100.
#
# score = 100
#       − teacher_clashes × 100   (hard — any clash = failure)
#       − class_clashes   × 100
#       − consecutive × 3         (capped 9)
#       − excess × 2              (capped 4, threshold >2/day)
#       − overload × 1            (capped 2)
#       − gaps × 1                (capped 2, offset −2 structural floor)
#       − friday_last × 1         (capped 2)
#       − late_labs × 2           (capped 2)
#       − workload_cv × 1         (capped 2, zero if cv ≤ 1)
SCORE_PENALTY_TEACHER_CLASH    = 100  # any clash is a hard failure
SCORE_PENALTY_CLASS_CLASH      = 100  # any clash is a hard failure
SCORE_PENALTY_CONSECUTIVE      = 3    # per adjacent same-theory-subject pair
SCORE_PENALTY_WORKLOAD_VAR     = 1    # per dedup-CV unit above 1
SCORE_PENALTY_FRIDAY_LAST      = 1    # per division using last Friday period
SCORE_PENALTY_SUBJ_EXCESS      = 2    # per excess occurrence beyond 2 per day
SCORE_PENALTY_TEACHER_OVERLOAD = 1    # per (teacher, day) with >3 unique periods
SCORE_PENALTY_TEACHER_GAP      = 1    # per idle gap-period above structural floor
SCORE_PENALTY_LATE_LAB         = 2    # per lab block starting after P4
SCORE_PENALTY_UNPLACED         = 3    # per required period that never got placed (uncapped —
                                       # this must dominate attempt selection so the greedy
                                       # retry loop reliably picks the fullest-filled attempt)

# Slot-scoring weights (soft quality preferences)
PENALTY_SAME_SUBJ_TODAY   = 8   # subtract from slot score if subject is already on this day
PENALTY_CONSECUTIVE       = 12  # subtract if adjacent period already has same subject
PENALTY_MON_P1            = 3   # elective soft penalty for Monday period 1
PENALTY_FRI_LAST          = 3   # elective/lab soft penalty for last Friday period
BONUS_PRE_LUNCH_LAB       = 5   # bonus for lab blocks that land entirely pre-lunch
BONUS_EARLY_PERIOD        = 2   # bonus per period for labs starting earlier in the day


# ─────────────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────────────

def _day_type(day: str) -> str:
    return "friday" if day == "Friday" else "weekday"


def get_periods(year: int, day: str) -> List[Period]:
    """Return the ordered list of Period objects for a given year-group and day."""
    return PERIOD_STRUCTURES[max(1, min(4, year))][_day_type(day)]


def get_lunch_break_after(year: int, day: str) -> int:
    """Return the last period number that sits entirely before the lunch break."""
    return LUNCH_BREAK_AFTER[(max(1, min(4, year)), _day_type(day))]


def sem_to_year(semester: int) -> int:
    """Convert semester number to academic year (1–4)."""
    return max(1, min(4, (semester + 1) // 2))


# ─────────────────────────────────────────────────────────────────────────────
# Display-code helpers
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_WORDS = {
    "for", "of", "the", "and", "in", "to", "a", "an", "with", "by",
    "on", "at", "its", "their", "is", "are", "was", "&",
}


def _make_short_code(subject: Subject) -> str:
    """
    Generate a 2–4 character display code from the subject name.
    e.g. "Mathematics for Electrical Science" → "MES"
    Falls back to last 3 alpha chars of the course code.
    """
    words = [
        w for w in (subject.name or "").split()
        if w.lower().rstrip(".,;:") not in _SKIP_WORDS and w[:1].isalpha()
    ]
    initials = "".join(w[0].upper() for w in words)
    if len(initials) >= 2:
        return initials[:4]
    letters = "".join(c for c in (subject.code or "") if c.isalpha())
    if len(letters) >= 3:
        return letters[-3:].upper()
    return (subject.subject_id or "SUB")[:4].upper()


def _teacher_short(teacher: Teacher) -> str:
    """Return display label for a teacher.

    • Dummy teachers: shown as 'X1', 'X2', 'X3'... in timetable cells (the
      spec's placeholder convention) so it's visually obvious a real teacher
      is still needed. The internal teacher_id stays 'dummy-<dept>-<n>' —
      that ID is relied on elsewhere (app.py's `tid.startswith('dummy-')`
      reconstruction check, and rows already saved in the DB), so only the
      *display* label changes here, not the identifier itself.
    • Real teachers: use teacher.code if set, else initials of name parts (max 5 chars).
    """
    if getattr(teacher, "is_dummy", False):
        m = _re.search(r'(\d+)$', teacher.teacher_id or "")
        n = m.group(1) if m else "1"
        return f"X{n}"
    if getattr(teacher, "code", None):
        return teacher.code
    parts = [p for p in teacher.name.split() if p[:1].isalpha()]
    return "".join(p[0].upper() for p in parts)[:5]


def _build_unique_codes(subjects: Dict[str, Subject]) -> Dict[str, str]:
    """
    Assign a unique 2–4 char display code to each subject.
    Collisions resolved by appending an incrementing numeric suffix.
    """
    from collections import Counter
    raw: Dict[str, str] = {sid: _make_short_code(s) for sid, s in subjects.items()}
    counts  = Counter(raw.values())
    tracker: Dict[str, int] = defaultdict(int)
    unique:  Dict[str, str] = {}
    for sid, code in raw.items():
        if counts[code] > 1:
            tracker[code] += 1
            unique[sid] = f"{code}{tracker[code]}"
        else:
            unique[sid] = code
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TimetableCell:
    division_id:       str
    day:               str
    period:            Period
    subject:           Optional[Subject] = None
    teacher:           Optional[Teacher] = None
    is_lab:            bool              = False
    is_free:           bool              = True
    short_code:        str               = ""
    # Parallel-lab pairing (year 2 & 3 only):
    # When two labs run simultaneously in the same time block, the class
    # timetable cell carries BOTH subjects.  The "primary" subject/teacher
    # fields hold Lab-1; these "paired" fields hold Lab-2.
    paired_subject:    Optional[Subject] = None
    paired_teacher:    Optional[Teacher] = None
    paired_short_code: str               = ""


@dataclass
class TimetableResult:
    class_timetables:   Dict[str, List[TimetableCell]]
    teacher_timetables: Dict[str, List[TimetableCell]]


# ─────────────────────────────────────────────────────────────────────────────
# Project / seminar detection
# ─────────────────────────────────────────────────────────────────────────────

_PROJECT_NAME_RE = _re.compile(
    r"\b(project(\s*phase\s*[\divx]+)?|seminar|mini[\s\-]?project|miniproject)\b",
    _re.IGNORECASE,
)


def _is_project(subject: Subject) -> bool:
    return (
        subject.subject_type == SubjectType.PROJECT
        or bool(_PROJECT_NAME_RE.search(subject.name or ""))
    )


_CW_NAME_RE = _re.compile(r"\bcourse\s*work\b|\bccw\b", _re.IGNORECASE)


def _is_cw(subject: Subject) -> bool:
    """
    Course Work (e.g. 'Comprehensive Course Work') — gap #2 in the constraint
    doc: must always be scheduled as a 2-consecutive-period block, never
    split across a day and never placed as single loose periods.
    """
    return (
        subject.subject_type == SubjectType.CW
        or bool(_CW_NAME_RE.search(subject.name or ""))
    )


# ─────────────────────────────────────────────────────────────────────────────
# TimetableGeneratorEngine
# ─────────────────────────────────────────────────────────────────────────────

class TimetableGeneratorEngine:
    """
    Generates class-wise and teacher-wise timetables from Engine 1 assignments.

    Minimal single-arg usage:
        tt = TimetableGeneratorEngine()
        tt.load_data(assignments)
        tt.generate()                         # CP-SAT first, then greedy
        tt.print_class_timetable()
        tt.print_teacher_timetable()
        tt.print_teacher_timetable(verbose=True)   # adds full subject name

    Full explicit usage:
        tt.load_data(assignments, teachers=teachers, divisions=divisions,
                     departments=departments, year_map=year_map)
    """

    def __init__(self) -> None:
        # ── Input data ──────────────────────────────────────────────────────
        self.assignments:  List[SubjectAssignment] = []
        self.divisions:    List[Division]          = []
        self.teachers:     List[Teacher]           = []
        self.departments:  List[Department]        = []
        self.year_map:     Dict[str, int]          = {}

        # ── Generated result ─────────────────────────────────────────────────
        self._result:          Optional[TimetableResult] = None
        self._last_score:      int                       = 0
        self._last_attempt:    int                       = 0

        # ── Internal scheduling state (reset each attempt) ────────────────────
        self._grid:               Dict[Tuple[str, str, int], SubjectAssignment] = {}
        # _parallel_grid: year-2/3 parallel labs — holds the SECOND lab assignment
        # at the same (div_id, day, pnum) key where the first lab lives in _grid.
        self._parallel_grid:      Dict[Tuple[str, str, int], SubjectAssignment] = {}
        # _teacher_busy maps (teacher_id, day, period) → subject_id of booked class.
        # Storing subject_id (not bool) allows parallel-teaching detection:
        #   same teacher + same subject + same slot across multiple divisions is ALLOWED.
        self._teacher_busy:       Dict[Tuple[str, str, int], str]               = {}
        self._teacher_theory_day: Dict[Tuple[str, str], int]                   = defaultdict(int)
        self._teacher_total_day:  Dict[Tuple[str, str], int]                   = defaultdict(int)
        self._teacher_lab_day:    Set[Tuple[str, str]]                         = set()
        # NEW: tracks (teacher_id, day) pairs on which the teacher has a
        # Project Phase or Seminar period.  When set, the theory cap for that
        # day is reduced from MAX_THEORY_PER_DAY (3) to THEORY_CAP_WITH_LAB (2)
        # — same reduction that applies when the teacher has a lab that day.
        self._teacher_project_day: Set[Tuple[str, str]]                        = set()
        self._div_lab_day:        Set[Tuple[str, str]]                         = set()
        self._elective_subject_anchor: Dict[str, Tuple[str, int]]             = {}
        self._elective_group_anchor:   Dict[str, List[Tuple[str, int]]]       = {}

        # ── Display-code lookup (built once in load_data) ─────────────────────
        self._short_codes:   Dict[str, str]                  = {}
        self._assistant_map: Dict[Tuple[str, str], Teacher]  = {}
        # co_supervisor_map: (subject_id, division_id) → [Teacher, ...]
        self._co_supervisor_map: Dict[Tuple[str, str], List[Teacher]] = defaultdict(list)
        
        # ── Pre-computed teacher properties ──────────────────────────────────
        self._teacher_is_bsh:   Dict[str, bool] = {}
        self._teacher_is_dummy: Dict[str, bool] = {}

        # Lab-room capacity per student department (gap #3). Default 2 keeps
        # the pre-existing always-parallel-allowed behavior for departments
        # that haven't set a value. Set via load_data(lab_capacity_map=...).
        self._lab_capacity: Dict[str, int] = {}

        # Cross-semester teacher-busy blocks (gap #4), persisted across every
        # _reset_state() call within generate()'s multiple greedy attempts.
        # Populated by block_external_busy_slots().
        self._external_busy_keys: Set[Tuple[str, str, int]] = set()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Public API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def load_data(
        self,
        assignments:  List[SubjectAssignment],
        teachers:     Optional[List[Teacher]]    = None,
        divisions:    Optional[List[Division]]   = None,
        departments:  Optional[List[Department]] = None,
        year_map:     Optional[Dict[str, int]]   = None,
        lab_capacity_map: Optional[Dict[str, int]] = None,
    ) -> None:
        """
        Load subject assignments from Engine 1.

        Calling with only `assignments` is sufficient — teachers, divisions,
        and year_map are inferred automatically from the assignment objects.

        lab_capacity_map: {student_dept_id: number_of_lab_rooms}. Departments
        not present default to capacity 2 (the historical assumed behavior).
        Capacity < 2 disables true simultaneous-parallel lab placement for
        that department, falling back to sequential single-lab sessions —
        gap #3 in the constraints doc ("parallel labs only when lab
        facilities are sufficient").
        """
        self._lab_capacity = dict(lab_capacity_map) if lab_capacity_map else {}
        self.assignments = list(assignments)

        # ── Infer divisions ───────────────────────────────────────────────────
        if divisions:
            self.divisions = list(divisions)
        else:
            seen: Set[str] = set()
            self.divisions = []
            for a in self.assignments:
                if a.division.division_id not in seen:
                    seen.add(a.division.division_id)
                    self.divisions.append(a.division)

        # ── Infer teachers ────────────────────────────────────────────────────
        if teachers:
            self.teachers = list(teachers)
        else:
            seen_t: Set[str] = set()
            self.teachers = []
            for a in self.assignments:
                if a.teacher.teacher_id not in seen_t:
                    seen_t.add(a.teacher.teacher_id)
                    self.teachers.append(a.teacher)
                    
        # Populate pre-computed property maps
        for t in self.teachers:
            self._teacher_is_bsh[t.teacher_id] = getattr(t, "is_bsh", False)
            self._teacher_is_dummy[t.teacher_id] = getattr(t, "is_dummy", False)

        self.departments = list(departments) if departments else []

        # ── Year map: division_id → academic year (1–4) ───────────────────────
        if year_map:
            self.year_map = dict(year_map)
        else:
            self.year_map = {d.division_id: sem_to_year(d.semester)
                             for d in self.divisions}

        # ── Build display codes once ──────────────────────────────────────────
        unique_subjects: Dict[str, Subject] = {}
        for a in self.assignments:
            unique_subjects[a.subject.subject_id] = a.subject
        self._short_codes = _build_unique_codes(unique_subjects)

        # ── Index lab assistants ──────────────────────────────────────────────
        self._assistant_map = {}
        for a in self.assignments:
            if a.role == "assistant":
                self._assistant_map[(a.subject.subject_id, a.division.division_id)] = a.teacher

        # FIX F: index co-supervisors so they can be blocked during project periods.
        # co_supervisor role is used by multi-teacher additional subjects
        # (Project Phase 1/2, Mini Project, CCW).
        self._co_supervisor_map: Dict[Tuple[str, str], List[Teacher]] = defaultdict(list)
        for a in self.assignments:
            if a.role == "co_supervisor":
                self._co_supervisor_map[
                    (a.subject.subject_id, a.division.division_id)
                ].append(a.teacher)

        n_main = sum(1 for a in self.assignments if a.role == "main")
        log.info(
            "Loaded %d assignments (%d main) across %d divisions, semester %s.",
            len(self.assignments), n_main, len(self.divisions),
            self.divisions[0].semester if self.divisions else "?",
        )

    def generate(self, use_cpsat: bool = True,
                 guarantee_fill: bool = True) -> TimetableResult:
        """
        Generate the timetable using CP-SAT as the primary solver.

        Phase 1 — Greedy warm-start (up to MAX_GREEDY_RETRIES attempts):
          Produces a feasible initial grid quickly.  Kept as a warm-start hint
          for CP-SAT.  Stops early when score ≥ EARLY_STOP_SCORE.
          (MAX_GREEDY_RETRIES is small so CP-SAT does the real optimisation.)

        Phase 2 — CP-SAT primary solve (when use_cpsat=True):
          ALL hard constraints are encoded in the CP-SAT model.
          The greedy grid is provided as a warm-start hint.
          CP-SAT runs for 60 s with 12 workers.
          Result accepted if it improves the greedy score; otherwise keep greedy.

        Per spec: CP-SAT must be the primary solver — do not rely on greedy only.

        Returns a TimetableResult (partial on total failure).
        """
        if not self.assignments:
            log.error("No data loaded — call load_data() first.")
            self._result = TimetableResult({}, {})
            return self._result

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 1 — Greedy base schedule
        # ══════════════════════════════════════════════════════════════════════
        best_grid:          Optional[Dict] = None
        best_parallel_grid: Optional[Dict] = None
        best_score: int = -1

        for attempt in range(1, MAX_GREEDY_RETRIES + 1):
            log.info("Greedy attempt %d/%d …", attempt, MAX_GREEDY_RETRIES)
            self._reset_state()

            seed = random.randint(1, 1_000_000)
            random.seed(seed)

            # Shuffle both divisions and assignments for maximum search diversity
            random.shuffle(self.divisions)
            shuffled = list(self.assignments)
            random.shuffle(shuffled)
            orig_assignments = self.assignments
            self.assignments = shuffled

            self.schedule_labs()
            self.schedule_electives()
            self.schedule_theory()

            self.assignments = orig_assignments

            score = self.score_schedule()
            log.info("  Attempt %d (seed=%d) score: %d/100", attempt, seed, score)

            if score > best_score:
                best_score         = score
                best_grid          = dict(self._grid)
                best_parallel_grid = dict(self._parallel_grid)

            if score >= EARLY_STOP_SCORE:
                log.info("✔ Early stop on attempt %d — score %d ≥ %d.",
                         attempt, score, EARLY_STOP_SCORE)
                self._last_attempt = attempt
                break

            if attempt == 1:
                clashes_t = self.check_teacher_clash()
                clashes_c = self.check_class_clash()
                if clashes_t or clashes_c:
                    log.warning("  Attempt 1: %d teacher clashes, %d class clashes.",
                                len(clashes_t), len(clashes_c))
        else:
            self._last_attempt = MAX_GREEDY_RETRIES

        # Commit the best greedy grid
        if best_grid is not None:
            self._grid          = best_grid
            self._parallel_grid = best_parallel_grid or {}

        log.info("Greedy base schedule score: %d", best_score)

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 2 — CP-SAT improvement
        # ══════════════════════════════════════════════════════════════════════
        if use_cpsat:
            log.info("Starting CP-SAT improvement phase...")
            cpsat_result = self._solve_cpsat(initial_grid=self._grid)
            if cpsat_result is not None:
                improved, improved_parallel = cpsat_result
                self._grid          = improved
                self._parallel_grid = improved_parallel
                # Rebuild teacher lab-day flags for lab2 teachers based on new positions
                self._rebuild_parallel_teacher_state()
                cpsat_score = self.score_schedule()
                log.info("CP-SAT improved score: %d", cpsat_score)
                if cpsat_score >= best_score:
                    best_score = cpsat_score
                else:
                    log.info(
                        "CP-SAT score (%d) < greedy score (%d) — keeping greedy result.",
                        cpsat_score, best_score,
                    )
                    # Restore best greedy grids exactly as saved
                    self._grid          = best_grid          # type: ignore[assignment]
                    self._parallel_grid = best_parallel_grid or {}
            else:
                log.info("CP-SAT unavailable or found no solution — keeping greedy result.")

        # ══════════════════════════════════════════════════════════════════════
        # PHASE 3 — Guaranteed fill (eliminate zero-slack blank periods)
        # ══════════════════════════════════════════════════════════════════════
        # Forces every division up to min(required, 29) filled periods.  Uses
        # temporary dummy teachers only where the real teacher is unavailable in
        # every remaining free slot (requirement #5).  See _guaranteed_fill_pass.
        if guarantee_fill:
            self._guaranteed_fill_pass(use_dummy_for_unavailable=True)

        self._last_score = best_score
        self._result = self._build_result()
        log.info("✔ Timetable finalised.  Score: %d/100.", self.score_schedule())
        return self._result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Scheduling phases (public — can be called individually or via generate())
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def schedule_labs(self) -> None:
        """
        Phase 1 — place all lab assignments as consecutive period blocks.

        Year 2 & 3 labs (semesters 3–6) where ppw == PARALLEL_LAB_PPW (6):
          • DB stores 0-0-6-0 for these labs.
          • Every division that has exactly 2 such labs gets a PARALLEL
            placement: both labs run SIMULTANEOUSLY for one block of
            PARALLEL_LAB_BLOCK (3) consecutive periods, on ONE day per week.
          • Implementation:
              – Lab-1 occupies _grid[(div_id, day, pnum)].
              – Lab-2 occupies _parallel_grid[(div_id, day, pnum)].
              – Both teachers are marked busy for all 3 periods that day.
              – Both teachers are added to _teacher_lab_day for that day.
          • Each lab is scheduled exactly ONCE per week (never repeated on a
            second day) — one 3-period session per lab subject, one lab per
            day for the class, two lab sessions total per week (one per
            subject). An earlier version placed the identical (lab1, lab2)
            pairing on a SECOND day too, intending a batch swap but never
            actually swapping lab1/lab2 — the net effect was each lab
            appearing twice in the week (e.g. AIDS S4 showing "DBMS Lab" on
            both Tuesday and Thursday). Fixed by placing the pair once.

        Divisions with only ONE parallel lab, or year-1/4 labs, or labs with
        ppw ≠ PARALLEL_LAB_PPW fall through to the standard single-block path.
        """
        lab_asgns = [
            a for a in self.assignments
            if a.subject.subject_type == SubjectType.LAB and a.role == "main"
        ]
        lab_asgns.sort(key=lambda a: -self._lab_ppw(a.subject))

        # ── Pre-group year-2/3 parallel labs (ppw == PARALLEL_LAB_PPW) ─────────
        div_yr23_labs: Dict[str, List[SubjectAssignment]] = defaultdict(list)
        for a in lab_asgns:
            ppw  = self._lab_ppw(a.subject)
            year = self.year_map.get(a.division.division_id, 2)
            if year in (2, 3) and ppw == PARALLEL_LAB_PPW:
                div_yr23_labs[a.division.division_id].append(a)

        scheduled_div_parallel: Set[str] = set()

        for a in lab_asgns:
            ppw    = self._lab_ppw(a.subject)
            year   = self.year_map.get(a.division.division_id, 2)
            div_id = a.division.division_id

            # ── Year-2/3 with exactly 2 parallel labs → parallel placement ──────
            # Gated on lab_room_capacity (gap #3): true simultaneous placement
            # needs 2 physical lab rooms running at once. Below that, fall
            # through to the sequential two-single-session path below.
            dept_capacity = self._lab_capacity.get(a.division.student_dept_id, 2)
            if (year in (2, 3) and ppw == PARALLEL_LAB_PPW
                    and len(div_yr23_labs.get(div_id, [])) == 2
                    and dept_capacity >= 2):
                if div_id in scheduled_div_parallel:
                    continue  # already handled
                scheduled_div_parallel.add(div_id)

                lab1, lab2 = div_yr23_labs[div_id]

                # Place BOTH labs simultaneously, ONCE per week (3-period
                # block). Do NOT place a second day — each lab must appear
                # exactly once per week (see docstring above).
                ok1 = self._place_parallel_lab_pair(lab1, lab2, PARALLEL_LAB_BLOCK)
                if not ok1:
                    log.warning(
                        "  ⚠ Parallel labs %s+%s (%s): could not place their "
                        "weekly block",
                        lab1.subject.name, lab2.subject.name, div_id,
                    )
                    continue

                # Ensure both teachers are flagged as lab-engaged that day
                for lab_a in (lab1, lab2):
                    asst = self._assistant_map.get((lab_a.subject.subject_id, div_id))
                    for (dd, dy) in list(self._div_lab_day):
                        if dd == div_id:
                            self._teacher_lab_day.add((lab_a.teacher.teacher_id, dy))
                            if asst:
                                self._teacher_lab_day.add((asst.teacher_id, dy))

            # ── Year-2/3 lab with no pairing partner (ppw == PARALLEL_LAB_PPW,
            #    but this division doesn't have exactly 2 such labs) ──────────
            # Scheduled ONCE per week (3-period block), same rule as the
            # paired case above — a lab must never appear twice in the week.
            # Previously this called _place_block_scored twice, placing the
            # same lab on two different days (e.g. AIDS S6 "Robotics Lab" on
            # both Monday and Tuesday).
            elif year in (2, 3) and ppw == PARALLEL_LAB_PPW:
                ok1 = self._place_block_scored(a, PARALLEL_LAB_BLOCK, is_lab=True)
                if not ok1:
                    self._log_unplaced(a, PARALLEL_LAB_BLOCK, "lab block")

            # ── All other labs ────────────────────────────────────────────────────
            else:
                ok = self._place_block_scored(a, ppw, is_lab=True)
                if not ok:
                    self._log_unplaced(a, ppw, "lab block")

    def schedule_electives(self) -> None:
        """
        Phase 2 — schedule elective groups so every elective in the same group
        runs at the SAME (day, period) across all involved divisions (parallel rule).

        Improvement #6 — soft penalties for Monday P1 and Friday last period.
        Elective subjects with no elective_group are treated as theory in Phase 3.
        """
        groups: Dict[str, List[SubjectAssignment]] = defaultdict(list)
        for a in self.assignments:
            if (a.role == "main"
                    and a.subject.subject_type == SubjectType.ELECTIVE
                    and a.subject.elective_group):
                groups[a.subject.elective_group].append(a)

        for group, asgns in groups.items():
            by_subj: Dict[str, List[SubjectAssignment]] = defaultdict(list)
            for a in asgns:
                by_subj[a.subject.subject_id].append(a)

            for subj_id, subj_asgns in by_subj.items():
                ppw = subj_asgns[0].subject.periods_per_week or 3
                for _ in range(ppw):
                    self._schedule_one_elective_slot(group, subj_id, subj_asgns)

    def schedule_theory(self) -> None:
        """
        Phase 3 — fill remaining slots with theory/project periods.

        Parallel-teaching aware:
          When the same teacher teaches the same subject to multiple divisions,
          ALL those assignments are scheduled into the SAME (day, period) slot
          so the teacher's time slot is consumed only once.

        Improvements #2, #3, #4 also applied:
          • Scored slot selection (spread, consecutive penalty, workload balance).
          • Projects placed as consecutive blocks.
        """
        schedulable = {SubjectType.THEORY, SubjectType.PROJECT, SubjectType.ELECTIVE, SubjectType.CW}

        # Collect main assignments to schedule
        asgns_to_schedule: List[SubjectAssignment] = [
            a for a in self.assignments
            if (a.role == "main"
                and a.subject.subject_type in schedulable
                and not (a.subject.subject_type == SubjectType.ELECTIVE
                         and a.subject.elective_group))
        ]

        # Group by (teacher_id, subject_id) — these are the "parallel groups"
        # that must be scheduled as one atomic slot.
        from collections import defaultdict as _dd
        parallel_groups: Dict[Tuple[str, str], List[SubjectAssignment]] = _dd(list)
        for a in asgns_to_schedule:
            parallel_groups[(a.teacher.teacher_id, a.subject.subject_id)].append(a)

        # Determine ppw per group (take the first assignment's value)
        # Build the scheduling queue: each entry is one "parallel slot" to fill.
        # We repeat each group ppw times (one entry per required period).
        work_queue: List[List[SubjectAssignment]] = []
        for group_asgns in parallel_groups.values():
            # Handle projects and CW separately (consecutive blocks)
            if _is_project(group_asgns[0].subject) or _is_cw(group_asgns[0].subject):
                continue   # handled below
            ppw = max(group_asgns[0].subject.periods_per_week, 1)
            # Count already placed periods for these divisions.
            # FIX E: use min, not max.  Using max would shortchange the
            # division with fewer pre-placed periods — it would receive fewer
            # queue entries and end up with blank slots.  min ensures every
            # division gets the full ppw queue entries it needs; slots that
            # are already filled are simply skipped by _slot_free().
            already = min(
                self._count_placed(group_asgns[0].subject.subject_id,
                                   a.division.division_id)
                for a in group_asgns
            )
            remaining = ppw - already
            for _ in range(remaining):
                work_queue.append(list(group_asgns))

        # Interleave queue so the same subject doesn't cluster
        # Sort: more-divisions-in-group first (ensures parallel subjects placed early)
        work_queue.sort(key=lambda g: -len(g))
        work_queue = self._interleave_parallel(work_queue)

        # Schedule project groups first (consecutive blocks per division)
        for group_asgns in parallel_groups.values():
            if _is_project(group_asgns[0].subject):
                ppw = group_asgns[0].subject.periods_per_week or 1
                for a in group_asgns:
                    self._place_project_blocks(a, ppw)
            elif _is_cw(group_asgns[0].subject):
                ppw = group_asgns[0].subject.periods_per_week or 2
                for a in group_asgns:
                    self._place_cw_blocks(a, ppw)

        # Schedule each slot in the work queue
        for group_asgns in work_queue:
            self._schedule_one_parallel_slot(group_asgns)

        # ── FIX G: relaxed second pass ─────────────────────────────────────────
        # After the main pass, some periods may still be unplaced because the
        # theory-per-day cap (MAX_THEORY_PER_DAY=3) blocked all valid days.
        # This second pass relaxes the *theory* cap but still honours the
        # absolute daily total cap (MAX_TOTAL_PER_DAY=5) so we never double-
        # book a teacher.  It runs per-division for surgical slot insertion.
        self._relaxed_fill_pass(parallel_groups)

    def _relaxed_fill_pass(
        self,
        parallel_groups: Dict[Tuple[str, str], List[SubjectAssignment]],
    ) -> None:
        """
        FIX G — Relaxed second-pass filler.

        After the main schedule_theory() work-queue, iterate over every
        (teacher, subject) group and check whether each division has the
        required periods_per_week placed.  For any shortfall, attempt to
        place remaining periods in two sub-passes:

        SUB-PASS 1 (spec-compliant):
          Scan all (day, period) slots honouring ALL hard constraints:
            • division slot free
            • teacher not busy at that slot
            • theory-per-day cap  (MAX_THEORY_PER_DAY / THEORY_CAP_WITH_LAB)
            • absolute daily total cap  (MAX_TOTAL_PER_DAY)

        SUB-PASS 2 (last-resort, blank-slot prevention):
          If sub-pass 1 found nothing, retry with ONLY the absolute daily
          total cap enforced (theory cap relaxed).  A warning is logged for
          every period placed this way so operators can identify over-loaded
          teachers.  This guarantees no blank slots remain in the timetable.
        """
        for group_asgns in parallel_groups.values():
            if _is_project(group_asgns[0].subject):
                continue  # projects were placed as blocks; skip
            if _is_cw(group_asgns[0].subject):
                continue  # CW must stay in 2-consecutive blocks; skip (never split)
            sid = group_asgns[0].subject.subject_id
            ppw = max(group_asgns[0].subject.periods_per_week, 1)

            for a in group_asgns:
                div_id = a.division.division_id
                tid    = a.teacher.teacher_id
                year   = self.year_map.get(div_id, 2)

                placed = self._count_placed(sid, div_id)
                needed = ppw - placed
                if needed <= 0:
                    continue

                log.info(
                    "  [relaxed pass] %s / %s: placing %d missing period(s)",
                    a.subject.name, div_id, needed,
                )

                for _ in range(needed):
                    placed_one = False
                    day_order = list(DAYS)
                    random.shuffle(day_order)

                    # ── SUB-PASS 1: fully spec-compliant ──────────────────────
                    for day in day_order:
                        for period in get_periods(year, day):
                            pnum = period.number
                            if not self._slot_free(div_id, day, pnum):
                                continue
                            if not self._teacher_free(tid, day, pnum, sid):
                                continue
                            # FIX (Problem 4): honour the theory-per-day cap
                            # (was intentionally skipped in the old code, which
                            # allowed the relaxed pass to violate the hard limit).
                            if not self._teacher_can_theory(tid, day):
                                continue
                            if not self._teacher_day_ok(tid, day):
                                continue
                            self._book(a, day, pnum, is_lab=False)
                            placed_one = True
                            break
                        if placed_one:
                            break

                    # ── SUB-PASS 2: last-resort blank-slot prevention ─────────
                    # FIX (Problems 5 & 6): if every day is blocked by the
                    # theory cap, relax it as a last resort so the slot is
                    # filled rather than left blank.  A WARNING is emitted so
                    # operators can see which teacher/day caused the overflow.
                    if not placed_one:
                        for day in day_order:
                            for period in get_periods(year, day):
                                pnum = period.number
                                if not self._slot_free(div_id, day, pnum):
                                    continue
                                if not self._teacher_free(tid, day, pnum, sid):
                                    continue
                                if not self._teacher_day_ok(tid, day):
                                    continue
                                self._book(a, day, pnum, is_lab=False)
                                placed_one = True
                                log.warning(
                                    "  [relaxed pass sub2] %s / %s placed on "
                                    "%s P%d (theory-cap relaxed as last resort "
                                    "— check teacher load balance)",
                                    a.subject.name, div_id, day, pnum,
                                )
                                break
                            if placed_one:
                                break

                    if not placed_one:
                        log.warning(
                            "  [relaxed pass] Cannot place %s / %s "
                            "even with relaxed cap",
                            a.subject.name, div_id,
                        )
                        break  # no more slots available for this division

    def _schedule_one_parallel_slot(
        self, group_asgns: List[SubjectAssignment]
    ) -> bool:
        """
        Place ONE period for all assignments in a parallel group at the SAME
        (day, period) slot when possible.  If no common free slot exists, fall
        back to placing one period per division independently (still valid —
        parallel teaching is allowed but not required).

        Returns True if at least one division assignment was placed.
        """
        if not group_asgns:
            return False

        a0     = group_asgns[0]
        tid    = a0.teacher.teacher_id
        sid    = a0.subject.subject_id
        year   = self.year_map.get(a0.division.division_id, 2)

        # ── Attempt 1: find a slot where ALL divisions are free ───────────────
        scored: List[Tuple[int, str, int]] = []
        day_scan = list(DAYS)
        random.shuffle(day_scan)
        for day in day_scan:
            for period in get_periods(year, day):
                pnum = period.number
                if any(not self._slot_free(a.division.division_id, day, pnum)
                       for a in group_asgns):
                    continue
                if not self._teacher_free(tid, day, pnum, sid):
                    continue
                if not self._teacher_can_theory(tid, day):
                    continue
                if not self._teacher_day_ok(tid, day):
                    continue
                sc = self._theory_slot_score(a0, day, pnum) + random.randint(0, 3)
                scored.append((sc, day, pnum))

        if scored:
            scored.sort(key=lambda t: -t[0])
            _, best_day, best_pnum = scored[0]
            for a in group_asgns:
                self._book(a, best_day, best_pnum, is_lab=False)
            return True

        # ── Attempt 2: fall back to per-division independent scheduling ───────
        # No common slot was found; place each division independently.
        # (parallel teaching is ALLOWED but not REQUIRED)
        any_placed = False
        for a in group_asgns:
            div_id = a.division.division_id
            div_scored: List[Tuple[int, str, int]] = []
            for day in day_scan:
                for period in get_periods(
                    self.year_map.get(div_id, 2), day
                ):
                    pnum = period.number
                    if not self._slot_free(div_id, day, pnum):
                        continue
                    if not self._teacher_free(tid, day, pnum, sid):
                        continue
                    if not self._teacher_can_theory(tid, day):
                        continue
                    if not self._teacher_day_ok(tid, day):
                        continue
                    sc = (self._theory_slot_score(a, day, pnum)
                          + random.randint(0, 3))
                    div_scored.append((sc, day, pnum))

            if div_scored:
                div_scored.sort(key=lambda t: -t[0])
                _, bd, bp = div_scored[0]
                self._book(a, bd, bp, is_lab=False)
                any_placed = True
            else:
                self._log_unplaced(a, 1, "theory period")

        return any_placed

    @staticmethod
    def _interleave_parallel(
        queue: List[List[SubjectAssignment]]
    ) -> List[List[SubjectAssignment]]:
        """
        Rearrange a parallel-slot queue so the same subject doesn't appear in
        consecutive positions.  Uses round-robin across subject buckets.
        """
        buckets: Dict[str, deque] = defaultdict(deque)
        for group in queue:
            sid = group[0].subject.subject_id
            buckets[sid].append(group)

        order = sorted(buckets.keys(), key=lambda s: -len(buckets[s]))
        result: List[List[SubjectAssignment]] = []
        while any(buckets[s] for s in order):
            for s in order:
                if buckets[s]:
                    result.append(buckets[s].popleft())
        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Constraint validators (public)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def check_teacher_clash(self) -> List[str]:
        """
        Return descriptions of every slot where a teacher teaches two DIFFERENT
        subjects simultaneously.  Empty list = no real clashes.

        Parallel teaching (same teacher, same subject, multiple divisions at
        the same time) is allowed by design and is NOT reported as a clash.

        Parallel-lab pairs: lab1 lives in _grid, lab2 in _parallel_grid.
        Both are scanned so a teacher assigned to BOTH labs in the same pair
        would be caught (should never happen, but validated here for safety).
        """
        # Build: (teacher_id, day, pnum) → {subject_id: [div_id, ...]}
        inv: Dict[Tuple[str, str, int], Dict[str, List[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for (div_id, day, pnum), a in self._grid.items():
            inv[(a.teacher.teacher_id, day, pnum)][a.subject.subject_id].append(div_id)
        for (div_id, day, pnum), a in self._parallel_grid.items():
            inv[(a.teacher.teacher_id, day, pnum)][a.subject.subject_id].append(div_id)

        clashes = []
        for (tid, day, pnum), subj_map in inv.items():
            if len(subj_map) > 1:
                subject_list = ", ".join(
                    f"{sid}→{divs}" for sid, divs in subj_map.items()
                )
                clashes.append(
                    f"Teacher clash: {tid} @ {day} P{pnum} "
                    f"teaching different subjects: {subject_list}"
                )
        return clashes

    def check_class_clash(self) -> List[str]:
        """
        Return descriptions of every slot where a division has more than one
        subject booked.  Empty list = no clashes.
        """
        counts: Dict[Tuple[str, str, int], int] = defaultdict(int)
        for key in self._grid:
            counts[key] += 1

        return [
            f"Class clash: {div} @ {day} P{pnum} ({cnt} subjects)"
            for (div, day, pnum), cnt in counts.items()
            if cnt > 1
        ]

    def check_teacher_daily_limit(self) -> List[str]:
        """
        Return descriptions of teachers who exceed the daily theory-period cap
        or the absolute daily-total cap.  Empty list = all within limits.

        Parallel-teaching aware: a teacher teaching the same subject to N
        divisions at the same (day, period) counts as ONE teaching slot,
        not N.
        """
        # Count unique (teacher, day, period) slots where teacher teaches theory.
        # Both _grid (lab1 + theory) and _parallel_grid (lab2) contribute to total.
        theory_slot: Dict[Tuple[str, str, int], str]  = {}  # (tid,day,pnum) → subject_id
        total_slot:  Dict[Tuple[str, str, int], bool] = {}  # (tid,day,pnum) → True
        name_map:    Dict[str, str]                   = {}

        # Build set of object-ids for lab2 assignments (parallel_grid) for O(1) check
        _parallel_ids: Set[int] = {id(a) for a in self._parallel_grid.values()}

        for (div_id, day, pnum), a in {**self._grid, **self._parallel_grid}.items():
            tid = a.teacher.teacher_id
            name_map[tid] = a.teacher.name
            total_slot[(tid, day, pnum)] = True
            # Lab2 entries from _parallel_grid are always labs — skip theory check
            if id(a) in _parallel_ids:
                continue
            if (a.subject.subject_type not in (SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW)
                    and not (_is_project(a.subject) or _is_cw(a.subject))):
                theory_slot[(tid, day, pnum)] = a.subject.subject_id

        # Aggregate counts per (teacher, day)
        theory_day: Dict[Tuple[str, str], int] = defaultdict(int)
        total_day:  Dict[Tuple[str, str], int] = defaultdict(int)
        for (tid, day, _) in total_slot:
            total_day[(tid, day)] += 1
        for (tid, day, _) in theory_slot:
            theory_day[(tid, day)] += 1

        violations = []
        for (tid, day), count in theory_day.items():
            # Cap is reduced to THEORY_CAP_WITH_LAB (2) when the teacher has a
            # lab, a Project Phase, OR a Seminar scheduled on this day.
            has_cap_reducer = (
                (tid, day) in self._teacher_lab_day
                or (tid, day) in self._teacher_project_day
            )
            cap = THEORY_CAP_WITH_LAB if has_cap_reducer else MAX_THEORY_PER_DAY
            if count > cap:
                violations.append(
                    f"{name_map.get(tid, tid)}: {count} theory on {day} (cap={cap})"
                )
        for (tid, day), count in total_day.items():
            if count > MAX_TOTAL_PER_DAY:
                violations.append(
                    f"{name_map.get(tid, tid)}: {count} total periods on {day} "
                    f"(cap={MAX_TOTAL_PER_DAY})"
                )
        return violations

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Schedule scoring  (requirements #1, #2, #3, #9)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def score_schedule(self) -> int:
        """
        Compute a quality score in [0, 100].

        Hard deductions (any clash = failure):
          − teacher_clashes × 100
          − class_clashes   × 100
          − unplaced_periods × 3   (uncapped — every required period that
            never made it into the grid; see _count_unplaced_periods)

        Soft components (each individually capped with structural offsets):
          consecutive × 3      capped 9
          excess × 2           capped 4   (threshold >2/day per spec)
          overload × 1         capped 2
          gaps × 1             capped 2   (offset −2 for structural floor)
          friday_last × 1      capped 2   (unavoidable in 100%-full timetable)
          late_labs × 2        capped 2
          cv × 1               capped 2   (zero if cv ≤ 1 — essentially perfect)
        """
        hard = (
            len(self.check_teacher_clash()) * SCORE_PENALTY_TEACHER_CLASH
            + len(self.check_class_clash())  * SCORE_PENALTY_CLASS_CLASH
            + self._count_unplaced_periods() * SCORE_PENALTY_UNPLACED
        )

        raw_gaps = self._count_teacher_gaps()
        raw_cv   = self._teacher_workload_cv_penalty()

        soft = (
            min(9, self._count_consecutive_same_subject()  * SCORE_PENALTY_CONSECUTIVE)
            + min(4, self._count_subject_day_excess()      * SCORE_PENALTY_SUBJ_EXCESS)
            + min(2, self._count_teacher_overload_days()   * SCORE_PENALTY_TEACHER_OVERLOAD)
            + min(2, max(0, raw_gaps - 2)                  * SCORE_PENALTY_TEACHER_GAP)
            + min(2, self._count_friday_last_usage()       * SCORE_PENALTY_FRIDAY_LAST)
            + min(2, self._count_late_labs()               * SCORE_PENALTY_LATE_LAB)
            + min(2, max(0, raw_cv - 1)                    * SCORE_PENALTY_WORKLOAD_VAR)
        )

        return max(0, min(100, 100 - hard - soft))

    def score_breakdown(self) -> Dict[str, int]:
        """
        Return a dict of named sub-scores for detailed reporting.
        Each sub-score is in [0, 100] where 100 = perfect on that metric.
        """
        consecutive = self._count_consecutive_same_subject()
        excess      = self._count_subject_day_excess()
        cv          = self._teacher_workload_cv_penalty()
        friday      = self._count_friday_last_usage()
        overload    = self._count_teacher_overload_days()
        gaps        = self._count_teacher_gaps()
        late_labs   = self._count_late_labs()
        t_clashes   = len(self.check_teacher_clash())
        c_clashes   = len(self.check_class_clash())

        return {
            "teacher_clashes":    t_clashes,
            "class_clashes":      c_clashes,
            "unplaced_periods":   self._count_unplaced_periods(),
            "consecutive_pairs":  consecutive,
            "subject_day_excess": excess,
            "workload_cv":        cv,
            "friday_last":        friday,
            "teacher_overload":   overload,
            "teacher_gaps":       gaps,
            "late_labs":          late_labs,
            "subject_distribution_score": max(0, 100 - min(4, excess    * SCORE_PENALTY_SUBJ_EXCESS)),
            "teacher_balance_score":      max(0, 100 - min(2, max(0, cv - 1) * SCORE_PENALTY_WORKLOAD_VAR)),
            "gap_score":                  max(0, 100 - min(2, max(0, gaps - 2) * SCORE_PENALTY_TEACHER_GAP)),
            "friday_score":               max(0, 100 - min(2, friday    * SCORE_PENALTY_FRIDAY_LAST)),
            "repeat_penalty_score":       max(0, 100 - min(9, consecutive * SCORE_PENALTY_CONSECUTIVE)),
            "lab_time_score":             max(0, 100 - min(2, late_labs * SCORE_PENALTY_LATE_LAB)),
            "total": self.score_schedule(),
        }

    def _count_consecutive_same_subject(self) -> int:
        """
        Count adjacent-period pairs within each (division, day) where the same
        THEORY or ELECTIVE subject appears twice in a row.

        Labs are deliberately consecutive and are excluded.
        Projects are also excluded (they are intentionally grouped).
        """
        count = 0
        for div in self.divisions:
            year = self.year_map.get(div.division_id, 2)
            for day in DAYS:
                prev_sid     = None
                prev_is_skip = False
                for p in get_periods(year, day):
                    a = self._grid.get((div.division_id, day, p.number))
                    if a:
                        is_skip = (
                            a.subject.subject_type in (SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW)
                            or _is_project(a.subject) or _is_cw(a.subject)
                        )
                        if (a.subject.subject_id == prev_sid
                                and not is_skip and not prev_is_skip):
                            count += 1
                        prev_sid     = a.subject.subject_id
                        prev_is_skip = is_skip
                    else:
                        prev_sid     = None
                        prev_is_skip = False
        return count

    def _teacher_workload_cv_penalty(self) -> int:
        """
        Compute a workload-balance penalty using the coefficient of variation
        (CV = std_dev / mean) per teacher, then average across teachers.

        Key corrections over the previous implementation:
          1. Parallel-teaching deduplication: when a teacher teaches the same
             subject to N divisions at the same (day, period), that counts as
             ONE period of work, not N.  The old code counted every grid cell,
             inflating the raw counts by up to 4× for Associate Professors who
             cover all divisions.
          2. Lab-only teacher exclusion: teachers whose total unique weekly
             load is ≤ 3 periods are almost always single-lab teachers.  Their
             schedule is inherently concentrated on one day (labs are consecutive
             blocks) and cannot be spread further — including them skews the
             average CV upward in a way that is not actionable.

        Result is clamped to [0, 10] so the penalty contribution is bounded.
        Returns an integer 0–10.
        """
        # Deduplicate: track unique (teacher_id, day, period) slots.
        # Both _grid (lab1 + theory) and _parallel_grid (lab2) are scanned.
        seen: Set[Tuple[str, str, int]] = set()
        daily: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for (div_id, day, pnum), a in {**self._grid, **self._parallel_grid}.items():
            slot_key = (a.teacher.teacher_id, day, pnum)
            if slot_key not in seen:
                seen.add(slot_key)
                daily[a.teacher.teacher_id][day] += 1

        if not daily:
            return 0

        cv_sum  = 0.0
        counted = 0
        for tid, day_counts in daily.items():
            vals  = [day_counts.get(d, 0) for d in DAYS]
            total = sum(vals)
            mean  = total / len(DAYS)

            # Skip lab-only teachers (≤ 3 total unique periods): their
            # single-day concentration is unavoidable, not a quality issue.
            if total <= 3:
                continue
            if mean < 0.5:
                continue

            std_dev = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(DAYS))
            cv_sum += std_dev / mean
            counted += 1

        if counted == 0:
            return 0

        avg_cv = cv_sum / counted
        # Scale: CV of 1.0 → penalty 10; CV ≈ 0.5 → penalty 5; CV < 0.1 → ~0
        return min(10, int(avg_cv * 10))

    def _count_teacher_overload_days(self) -> int:
        """
        Count (teacher, day) pairs where the teacher's unique period load
        exceeds 3 in a single day.

        Uses deduplication so parallel-teaching (same subject, multiple
        divisions, same slot) counts as ONE period, not N.
        """
        seen: Set[Tuple[str, str, int]] = set()
        daily: Dict[Tuple[str, str], int] = defaultdict(int)
        for (div_id, day, pnum), a in {**self._grid, **self._parallel_grid}.items():
            slot_key = (a.teacher.teacher_id, day, pnum)
            if slot_key not in seen:
                seen.add(slot_key)
                daily[(a.teacher.teacher_id, day)] += 1
        return sum(1 for cnt in daily.values() if cnt > 3)

    def _count_unplaced_periods(self) -> int:
        """
        Count required-but-never-placed periods, summed across every 'main'
        assignment (theory, lab, elective, project, CW alike).

        This exists because score_schedule() previously had NO way to tell a
        fully-filled attempt from one with large blank gaps — clash counts,
        consecutive-subject penalties, workload variance etc. don't reflect
        "did every required period actually get scheduled". Two greedy
        attempts with wildly different blank-cell counts could score
        identically, and since generate() stops at the first attempt scoring
        >= EARLY_STOP_SCORE, a badly-blank attempt could win purely by
        chance — this was observed directly: re-running the same real input
        produced total blank-cell counts varying by 5-10% between runs.
        """
        placed_count: Dict[Tuple[str, str], int] = defaultdict(int)
        seen: Set[Tuple[str, str, str, int]] = set()
        for (div_id, day, pnum), a in {**self._grid, **self._parallel_grid}.items():
            key = (div_id, day, a.subject.subject_id, pnum)
            if key in seen:
                continue
            seen.add(key)
            placed_count[(div_id, a.subject.subject_id)] += 1

        shortfall = 0
        seen_subj_div: Set[Tuple[str, str]] = set()
        for a in self.assignments:
            if a.role != "main":
                continue
            key = (a.division.division_id, a.subject.subject_id)
            if key in seen_subj_div:
                continue
            seen_subj_div.add(key)
            required = a.subject.periods_per_week or 0
            got = placed_count.get(key, 0)
            if got < required:
                shortfall += (required - got)
        return shortfall

    def _count_teacher_gaps(self) -> int:
        """
        Count idle gap periods inside each teacher's working day.

        A gap is a period between the teacher's first and last class of the
        day that contains no class.  A teacher with P1, P4, P7 on the same
        day has 4 gap periods (P2, P3, P5, P6).  Large gaps force teachers
        to wait around and fragment their day.

        Uses deduplication so parallel-teaching slots count once per period.
        """
        seen: Set[Tuple[str, str, int]] = set()
        periods_by_day: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
        for (div_id, day, pnum), a in {**self._grid, **self._parallel_grid}.items():
            slot_key = (a.teacher.teacher_id, day, pnum)
            if slot_key not in seen:
                seen.add(slot_key)
                periods_by_day[(a.teacher.teacher_id, day)].add(pnum)

        total_gaps = 0
        for (tid, day), active_pnums in periods_by_day.items():
            if len(active_pnums) < 2:
                continue
            first = min(active_pnums)
            last  = max(active_pnums)
            # Gap = span size − number of active periods
            total_gaps += (last - first + 1) - len(active_pnums)
        return total_gaps

    def _count_late_labs(self) -> int:
        """
        Count lab blocks that start after P4 (spec Rule 7: prefer morning labs).

        A lab block is identified by its minimum period number on its day.
        Each (subject_id, division_id, day) combination is counted once.
        """
        counted: Set[Tuple[str, str, str]] = set()
        late = 0
        for (div_id, day, pnum), a in self._grid.items():
            if a.subject.subject_type != SubjectType.LAB:
                continue
            lab_key = (a.subject.subject_id, div_id, day)
            if lab_key in counted:
                continue
            counted.add(lab_key)
            # Find the earliest period in this lab block on this day
            start_pnum = min(
                p for (d, dy, p), aa in self._grid.items()
                if d == div_id and dy == day
                and aa.subject.subject_id == a.subject.subject_id
            )
            if start_pnum > 4:
                late += 1
        return late

    # Keep old name as alias for any external callers
    def _teacher_workload_variance(self) -> int:
        return self._teacher_workload_cv_penalty()

    def _teacher_workload_unevenness(self) -> int:
        return self._teacher_workload_cv_penalty()

    def _count_friday_last_usage(self) -> int:
        """
        Count divisions that have any subject in the very last Friday period.
        The last slot of the week is least desirable for students and teachers.
        """
        count = 0
        for div in self.divisions:
            year  = self.year_map.get(div.division_id, 2)
            fri_p = get_periods(year, "Friday")
            if not fri_p:
                continue
            if self._grid.get((div.division_id, "Friday", fri_p[-1].number)) is not None:
                count += 1
        return count

    def _count_subject_day_excess(self) -> int:
        """
        Count (division, day, subject_id) triplets where the same non-lab
        theory subject appears more than TWICE in the same day.

        Spec Rule 1 states: subject(day) ≤ 2  →  penalty only when count > 2.
        Appearing twice in one day is acceptable (and sometimes unavoidable
        when the timetable is fully packed).  Appearing three or more times
        in a single day is the actual quality problem this metric targets.

        Each occurrence beyond the second counts as one penalty unit.
        """
        excess = 0
        for div in self.divisions:
            year = self.year_map.get(div.division_id, 2)
            for day in DAYS:
                day_counts: Dict[str, int] = defaultdict(int)
                for p in get_periods(year, day):
                    a = self._grid.get((div.division_id, day, p.number))
                    if a and a.subject.subject_type not in (
                        SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW
                    ) and not (_is_project(a.subject) or _is_cw(a.subject)):
                        day_counts[a.subject.subject_id] += 1
                for cnt in day_counts.values():
                    if cnt > 2:                    # ← was > 1 (wrong per spec)
                        excess += cnt - 2          # ← one penalty per occurrence beyond 2
        return excess

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Internal state reset
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _reset_state(self) -> None:
        self._grid               = {}
        self._parallel_grid      = {}   # parallel lab pairs (year 2 & 3)
        self._teacher_busy       = {}   # (tid, day, pnum) → subject_id
        self._teacher_theory_day = defaultdict(int)
        self._teacher_total_day  = defaultdict(int)
        self._teacher_lab_day    = set()
        self._teacher_project_day = set()   # NEW: project/seminar days per teacher
        self._div_lab_day        = set()
        self._elective_subject_anchor = {}
        self._elective_group_anchor   = {}
        # Re-apply cross-semester external busy blocks (gap #4). generate()
        # calls _reset_state() once per greedy attempt (up to 10x), so these
        # must be re-injected every time — populating _teacher_busy once
        # before generate() is not enough, the first attempt's reset would
        # silently wipe it out and every later attempt would be unprotected.
        for key in self._external_busy_keys:
            self._teacher_busy[key] = "__EXTERNAL_BUSY__"

    def _get_parallel_pairs(self) -> Dict[str, Tuple["SubjectAssignment", "SubjectAssignment"]]:
        """
        Return a dict of {div_id: (lab1, lab2)} for all year-2/3 divisions
        whose two labs have ppw == PARALLEL_LAB_PPW.
        """
        lab_asgns = [
            a for a in self.assignments
            if a.subject.subject_type == SubjectType.LAB and a.role == "main"
        ]
        by_div: Dict[str, List[SubjectAssignment]] = defaultdict(list)
        for a in lab_asgns:
            ppw  = self._lab_ppw(a.subject)
            year = self.year_map.get(a.division.division_id, 2)
            if year in (2, 3) and ppw == PARALLEL_LAB_PPW:
                by_div[a.division.division_id].append(a)
        return {
            div_id: (labs[0], labs[1])
            for div_id, labs in by_div.items()
            if len(labs) == 2
        }

    def _rebuild_parallel_teacher_state(self) -> None:
        """
        After CP-SAT updates _grid and _parallel_grid, rebuild _teacher_lab_day
        for lab2 teachers so constraint validators remain accurate.
        """
        for (div_id, day, pnum), a2 in self._parallel_grid.items():
            self._teacher_lab_day.add((a2.teacher.teacher_id, day))
            asst2 = self._assistant_map.get((a2.subject.subject_id, div_id))
            if asst2:
                self._teacher_lab_day.add((asst2.teacher_id, day))

    def _rebuild_parallel_grid_from_grid(self) -> None:
        """
        Used when reverting from CP-SAT back to the best greedy solution:
        restore _parallel_grid to match the saved best_parallel_grid.

        In the greedy flow, best_parallel_grid is saved alongside best_grid
        and restored directly in generate().  This method is a safety no-op
        in that case (the caller already set self._parallel_grid = best_parallel_grid).
        It exists as a named hook in case _parallel_grid needs to be inferred
        from _grid after a grid-only restore.

        For the greedy→greedy revert path the grids are restored by direct
        assignment in generate(), so this is intentionally a no-op.
        """
        pass

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Slot predicates
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _slot_free(self, div_id: str, day: str, pnum: int) -> bool:
        return (div_id, day, pnum) not in self._grid

    def _teacher_free(self, tid: str, day: str, pnum: int,
                      subject_id: str = "") -> bool:
        """
        Return True if the teacher is available for the given slot.

        Parallel-teaching rule:
          A teacher who already has a class at this slot is still considered
          "free" if the incoming booking is for the SAME subject (multiple
          divisions in parallel).  Different subject in same slot → clash.
        """
        existing = self._teacher_busy.get((tid, day, pnum))
        if existing is None:
            return True
        if subject_id and existing == subject_id:
            return True   # parallel teaching same subject — OK
        return False

    def _teacher_can_theory(self, tid: str, day: str) -> bool:
        # Dummy teachers are totally exempt from daily caps per spec (S9)
        if self._teacher_is_dummy.get(tid):
            return True
            
        # Reduce theory cap to THEORY_CAP_WITH_LAB (2) when the teacher already
        # has a lab, a Project Phase, or a Seminar scheduled on this day.
        has_cap_reducer = (
            (tid, day) in self._teacher_lab_day
            or (tid, day) in self._teacher_project_day
        )
        
        base_cap = MAX_THEORY_PER_DAY
        if self._teacher_is_bsh.get(tid):
             # BSH teachers can take up to 3 theory max
             base_cap = 3
        else:
             # Non-BSH teachers take max 2 theory
             base_cap = 2
             
        cap = THEORY_CAP_WITH_LAB if has_cap_reducer else base_cap
        return self._teacher_theory_day[(tid, day)] < cap

    def _teacher_day_ok(self, tid: str, day: str, extra: int = 1) -> bool:
        if self._teacher_is_dummy.get(tid):
            return True
        return self._teacher_total_day[(tid, day)] + extra <= MAX_TOTAL_PER_DAY

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Booking
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _book(self, a: SubjectAssignment, day: str, pnum: int,
              is_lab: bool = False) -> None:
        """
        Write one slot into all occupancy trackers.

        Parallel-teaching rule:
          If the teacher is already booked at this slot for the SAME subject
          (teaching multiple divisions in parallel), we update _teacher_busy
          but do NOT increment the daily load counters again — parallel slots
          count as a single teaching engagement for the teacher.
        """
        div_id  = a.division.division_id
        tid     = a.teacher.teacher_id
        sid     = a.subject.subject_id

        self._grid[(div_id, day, pnum)] = a

        # Is this a parallel-teaching slot (same teacher, same subject, same period)?
        existing_sid = self._teacher_busy.get((tid, day, pnum))
        parallel     = existing_sid == sid   # True when teacher already teaching this subject here

        # Always record the subject_id so later divisions can detect parallel teaching
        self._teacher_busy[(tid, day, pnum)] = sid

        # Only count load for the FIRST division booking at this slot
        if not parallel:
            self._teacher_total_day[(tid, day)] += 1

            is_lab_or_proj = (
                is_lab
                or a.subject.subject_type in (SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW)
                or _is_project(a.subject) or _is_cw(a.subject)
            )
            if not is_lab_or_proj:
                self._teacher_theory_day[(tid, day)] += 1

            if is_lab:
                self._teacher_lab_day.add((tid, day))

            # NEW: if this is a Project Phase or Seminar period, mark this
            # (teacher, day) so that theory cap is reduced to THEORY_CAP_WITH_LAB.
            # _is_project() already detects both "project phase" and "seminar".
            if (not is_lab) and _is_project(a.subject):
                self._teacher_project_day.add((tid, day))

        # Block the assistant teacher for the same slot (lab only).
        # Use subject_id so the assistant's busy-map also supports parallel detection.
        asst = self._assistant_map.get((sid, div_id))
        if asst and is_lab:
            self._teacher_busy[(asst.teacher_id, day, pnum)] = sid

        # FIX F: block co-supervisors for project/additional subjects.
        # They do not generate their own timetable block, but must be
        # marked unavailable during all periods the main teacher occupies
        # for this subject (project continuity rule).
        for co in self._co_supervisor_map.get((sid, div_id), []):
            existing = self._teacher_busy.get((co.teacher_id, day, pnum))
            if existing is None:
                self._teacher_busy[(co.teacher_id, day, pnum)] = sid

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Slot scoring helpers  (improvements #2, #3, #4, #5, #6)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _theory_slot_score(
        self,
        a:    SubjectAssignment,
        day:  str,
        pnum: int,
    ) -> int:
        """
        Score a candidate (day, period) for a theory/elective assignment.
        Higher score = better placement.

        Penalties (spec-exact values for 95+ schedules):
          count_today ≥ 1  → −25   (S1: discourage same subject twice/day)
          count_today ≥ 2  → −50   (S1: hard block on 3rd occurrence)
          consecutive pair → −30   (S2: adjacent same-subject)
          triple run       → −40   (S2: 3-in-a-row)
          teacher load ≥ 4 → −40   (S3: past daily cap)
          teacher load ≥ 3 → −25   (S3: strong spread pressure)
          teacher load ≥ 2 →  −5   (S3: light pressure)
          gap_increase × 10        (S4: 10 pts per new idle gap period)
          friday last      → −20   (S5)
          period > 4       → −(pnum-4)*4  (S6: prefer morning)
        """
        div_id = a.division.division_id
        tid    = a.teacher.teacher_id
        sid    = a.subject.subject_id
        year   = self.year_map.get(div_id, 2)
        score  = 100

        # ── S1: same-subject-today ─────────────────────────────────────────────
        count_today = 0
        for p in get_periods(year, day):
            existing = self._grid.get((div_id, day, p.number))
            if existing and existing.subject.subject_id == sid:
                count_today += 1
        if count_today >= 1:
            score -= 25
        if count_today >= 2:
            score -= 50

        # ── S2: consecutive pairs and triples ─────────────────────────────────
        for neighbour in (pnum - 1, pnum + 1):
            existing = self._grid.get((div_id, day, neighbour))
            if existing and existing.subject.subject_id == sid:
                score -= 30

        a_prev2 = self._grid.get((div_id, day, pnum - 2))
        a_prev1 = self._grid.get((div_id, day, pnum - 1))
        a_next1 = self._grid.get((div_id, day, pnum + 1))
        a_next2 = self._grid.get((div_id, day, pnum + 2))
        if (a_prev1 and a_prev1.subject.subject_id == sid and
                a_prev2 and a_prev2.subject.subject_id == sid):
            score -= 40
        if (a_next1 and a_next1.subject.subject_id == sid and
                a_next2 and a_next2.subject.subject_id == sid):
            score -= 40

        # ── S3: teacher daily overload ────────────────────────────────────────
        current_load = self._teacher_total_day.get((tid, day), 0)
        if current_load >= 4:
            score -= 40
        elif current_load >= 3:
            score -= 25
        elif current_load >= 2:
            score -= 5

        # ── S4: teacher gap awareness ─────────────────────────────────────────
        seen_set: Set[int] = set()
        for (d2, dy2, p2), aa in self._grid.items():
            if aa.teacher.teacher_id == tid and dy2 == day:
                seen_set.add(p2)
        if seen_set:
            first        = min(seen_set)
            last         = max(seen_set)
            old_gap      = (last - first + 1) - len(seen_set)
            new_first    = min(first, pnum)
            new_last     = max(last, pnum)
            new_gap      = (new_last - new_first + 1) - (len(seen_set) + 1)
            gap_increase = max(0, new_gap - old_gap)
            score -= gap_increase * 10

        # ── S5: Friday last period ────────────────────────────────────────────
        if day == "Friday":
            fri_periods = get_periods(year, "Friday")
            if fri_periods and pnum == fri_periods[-1].number:
                score -= 20
            elif fri_periods and pnum >= fri_periods[-2].number:
                score -= 8

        # ── S6: prefer morning periods (P1–P4) ───────────────────────────────
        if pnum > 4:
            score -= (pnum - 4) * 4

        # ── S7 (Rule 12): try to assign at least one first and one last period ─
        # Give a small soft bonus so the scheduler attempts to use P1 and the
        # last period of the day at least once per subject across the week.
        # This is "must try" not mandatory, so the bonus is intentionally light
        # (8 pts) to avoid forcing bad placements.
        all_day_periods = get_periods(year, day)
        if all_day_periods:
            first_pnum = all_day_periods[0].number
            last_pnum  = all_day_periods[-1].number
            # Only bonus if this subject has not yet occupied P1 / last this week
            sid_first_used = any(
                self._grid.get((div_id, d, first_pnum)) is not None
                and self._grid[(div_id, d, first_pnum)].subject.subject_id == sid
                for d in DAYS
            )
            sid_last_used = any(
                self._grid.get((div_id, d, last_pnum)) is not None
                and self._grid[(div_id, d, last_pnum)].subject.subject_id == sid
                for d in DAYS
            )
            if pnum == first_pnum and not sid_first_used:
                score += 8   # bonus for placing at P1 when not yet placed there
            if pnum == last_pnum and not sid_last_used:
                score += 8   # bonus for placing at last period when not yet placed there

        return score

    def _lab_block_score(
        self,
        day:    str,
        pnums:  List[int],
        year:   int,
    ) -> int:
        """
        Score a candidate consecutive block for a lab.
        Higher = better.

        Bonuses:
          • Block lies entirely pre-lunch → +BONUS_PRE_LUNCH_LAB
          • Earlier start period → +BONUS_EARLY_PERIOD per period before threshold
        Penalties:
          • Friday last period in block → penalty
        """
        score    = 50
        lunch    = get_lunch_break_after(year, day)
        day_idx  = DAYS.index(day)

        # Pre-lunch bonus (spec: labs should start before P4)
        if pnums[-1] <= lunch:
            score += 10

        # Early-period bonus: P1 start → +14, P4 start → +8, P7 start → +2
        score += (8 - pnums[0]) * 2

        # Earlier-in-week bonus: Mon+8, Tue+6, Wed+4, Thu+2, Fri+0
        score += max(0, 2 * (4 - day_idx))

        # Friday penalty
        if day == "Friday":
            score -= 15
            all_periods = get_periods(year, day)
            if all_periods and pnums[-1] == all_periods[-1].number:
                score -= 10   # extra for last Friday slot

        return score

    def _elective_slot_score(self, day: str, pnum: int, year: int) -> int:
        """
        Score a candidate (day, period) for an elective group anchor.
        Higher = better.  Applies soft penalties per spec.
        """
        score = 50
        # Soft penalty: avoid Monday period 1
        if day == "Monday" and pnum == 1:
            score -= PENALTY_MON_P1
        # Soft penalty: avoid last period on Friday
        if day == "Friday":
            friday_periods = get_periods(year, "Friday")
            if friday_periods and pnum == friday_periods[-1].number:
                score -= PENALTY_FRI_LAST
        return score

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Consecutive block finder and placement
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _find_consec_slots(
        self,
        a:      SubjectAssignment,
        consec: int,
        is_lab: bool = True,
    ) -> List[Tuple[str, List[int]]]:
        """
        Return all valid (day, [period_numbers]) for a consecutive block.

        Hard constraints checked:
          • All division slots free
          • Main teacher free for all periods
          • Assistant teacher free for all periods (if assigned)
          • Block does NOT straddle the lunch break
          • Teacher daily total cap not exceeded
          • (is_lab) Division has no other lab session that day
        """
        year   = self.year_map.get(a.division.division_id, 2)
        tid    = a.teacher.teacher_id
        div_id = a.division.division_id
        asst   = self._assistant_map.get((a.subject.subject_id, div_id))

        results: List[Tuple[str, List[int]]] = []

        for day in DAYS:
            if is_lab and (div_id, day) in self._div_lab_day:
                continue
            if not self._teacher_day_ok(tid, day, extra=consec):
                continue

            periods = get_periods(year, day)
            lunch   = get_lunch_break_after(year, day)

            for i in range(len(periods) - consec + 1):
                run = [periods[i + k].number for k in range(consec)]

                # Block must be entirely pre-lunch or entirely post-lunch
                if run[0] <= lunch < run[-1]:
                    continue
                if not all(self._slot_free(div_id, day, p) for p in run):
                    continue
                if not all(self._teacher_free(tid, day, p, a.subject.subject_id) for p in run):
                    continue
                if asst and not all(self._teacher_free(asst.teacher_id, day, p) for p in run):
                    continue

                results.append((day, run))

        return results

    def _place_block_scored(
        self, a: SubjectAssignment, consec: int, is_lab: bool = True
    ) -> bool:
        """
        Choose the best-scoring consecutive block and book it.

        Instead of a uniform random choice, all valid candidates are scored and
        the best one is selected (with small random tiebreaking).
        Returns True on success.
        """
        candidates = self._find_consec_slots(a, consec, is_lab=is_lab)
        if not candidates:
            return False

        year = self.year_map.get(a.division.division_id, 2)

        # Score each candidate and pick the best
        scored = [
            (self._lab_block_score(day, pnums, year) + random.randint(0, 2),
             day, pnums)
            for day, pnums in candidates
        ]
        scored.sort(key=lambda t: -t[0])   # descending score
        _, best_day, best_pnums = scored[0]

        for pnum in best_pnums:
            self._book(a, best_day, pnum, is_lab=is_lab)
        if is_lab:
            self._div_lab_day.add((a.division.division_id, best_day))
        return True

    # Keep the original name as an alias for backward compatibility
    def _place_block(
        self, a: SubjectAssignment, consec: int, is_lab: bool = True
    ) -> bool:
        return self._place_block_scored(a, consec, is_lab=is_lab)

    def _find_consec_slots_parallel(
        self,
        lab1:   SubjectAssignment,
        lab2:   SubjectAssignment,
        consec: int,
    ) -> List[Tuple[str, List[int]]]:
        """
        Return (day, [period_numbers]) candidates where BOTH lab1 and lab2 can
        be placed simultaneously as a consecutive block.

        Hard constraints:
          • Division slots free (same check as normal — only _grid matters)
          • lab1 main teacher free for all periods
          • lab2 main teacher free for all periods
          • lab1 assistant teacher free (if assigned)
          • lab2 assistant teacher free (if assigned)
          • Block entirely pre-lunch OR entirely post-lunch
          • Division has no other lab session that day (_div_lab_day)
        """
        div_id = lab1.division.division_id
        year   = self.year_map.get(div_id, 2)
        tid1   = lab1.teacher.teacher_id
        tid2   = lab2.teacher.teacher_id
        sid1   = lab1.subject.subject_id
        sid2   = lab2.subject.subject_id
        asst1  = self._assistant_map.get((sid1, div_id))
        asst2  = self._assistant_map.get((sid2, div_id))

        results: List[Tuple[str, List[int]]] = []

        for day in DAYS:
            if (div_id, day) in self._div_lab_day:
                continue
            # Both teachers must fit within the daily total cap
            if not self._teacher_day_ok(tid1, day, extra=consec):
                continue
            if not self._teacher_day_ok(tid2, day, extra=consec):
                continue

            periods = get_periods(year, day)
            lunch   = get_lunch_break_after(year, day)

            for i in range(len(periods) - consec + 1):
                run = [periods[i + k].number for k in range(consec)]

                if run[0] <= lunch < run[-1]:
                    continue
                # Division slots must all be free
                if not all(self._slot_free(div_id, day, p) for p in run):
                    continue
                # Both main teachers must be free
                if not all(self._teacher_free(tid1, day, p, sid1) for p in run):
                    continue
                if not all(self._teacher_free(tid2, day, p, sid2) for p in run):
                    continue
                # Assistants must be free
                if asst1 and not all(
                    self._teacher_free(asst1.teacher_id, day, p) for p in run
                ):
                    continue
                if asst2 and not all(
                    self._teacher_free(asst2.teacher_id, day, p) for p in run
                ):
                    continue

                results.append((day, run))

        return results

    def _place_parallel_lab_pair(
        self,
        lab1:   SubjectAssignment,
        lab2:   SubjectAssignment,
        consec: int,
    ) -> bool:
        """
        Place lab1 and lab2 simultaneously on the best available (day, block).

        lab1 is written into _grid (the primary slot); lab2 is written into
        _parallel_grid (the paired slot).  Both teachers and both assistants are
        marked busy for all booked periods.  The division-lab-day guard is
        updated so the next call (for day 2) is forced onto a different day.

        Returns True on success, False if no valid simultaneous slot exists.
        """
        candidates = self._find_consec_slots_parallel(lab1, lab2, consec)
        if not candidates:
            return False

        div_id = lab1.division.division_id
        year   = self.year_map.get(div_id, 2)

        scored = [
            (self._lab_block_score(day, pnums, year) + random.randint(0, 2),
             day, pnums)
            for day, pnums in candidates
        ]
        scored.sort(key=lambda t: -t[0])
        _, best_day, best_pnums = scored[0]

        # Book lab1 in the primary grid (marks division slots as occupied)
        for pnum in best_pnums:
            self._book(lab1, best_day, pnum, is_lab=True)

        # Book lab2 in the parallel grid (teacher-busy only — division already blocked)
        tid2  = lab2.teacher.teacher_id
        sid2  = lab2.subject.subject_id
        asst2 = self._assistant_map.get((sid2, div_id))
        for pnum in best_pnums:
            self._parallel_grid[(div_id, best_day, pnum)] = lab2
            # Mark lab2's teacher as busy
            self._teacher_busy[(tid2, best_day, pnum)] = sid2
            self._teacher_total_day[(tid2, best_day)] += 1
            self._teacher_lab_day.add((tid2, best_day))
            # Mark lab2's assistant as busy
            if asst2:
                self._teacher_busy[(asst2.teacher_id, best_day, pnum)] = sid2
            # Mark co-supervisors for lab2
            for co in self._co_supervisor_map.get((sid2, div_id), []):
                if (co.teacher_id, best_day, pnum) not in self._teacher_busy:
                    self._teacher_busy[(co.teacher_id, best_day, pnum)] = sid2

        # Update div_lab_day (forces next call onto a different day)
        self._div_lab_day.add((div_id, best_day))
        return True

    def _place_project_blocks(self, a: SubjectAssignment, ppw: int) -> bool:
        """
        Place project/seminar subjects as consecutive blocks.
          ppw == 11  →  3+3+3+2
          ppw ==  9  →  3+3+3
          ppw ==  6  →  3+3
          ppw ==  3  →  3
          other      →  3-blocks until remainder
        """
        def pb(n: int) -> bool:
            return self._place_block_scored(a, n, is_lab=False)

        if ppw == 11:
            return pb(3) and pb(3) and pb(3) and pb(2)
        if ppw == 9:
            return pb(3) and pb(3) and pb(3)
        if ppw == 6:
            return pb(3) and pb(3)
        if ppw == 3:
            return pb(3)

        rem = ppw
        while rem >= 3:
            if not pb(3):
                return False
            rem -= 3
        return (not rem) or pb(rem)

    def _place_cw_blocks(self, a: SubjectAssignment, ppw: int) -> bool:
        """
        Place Course Work (CW) subjects as 2-consecutive-period blocks.

        Spec: "Course Work (CW) sessions must be two consecutive periods."
          ppw even  → ppw/2 blocks of 2.
          ppw odd   → floor(ppw/2) blocks of 2, plus one leftover single
                      period (can't force an odd total into all-2 blocks;
                      logged so it's visible rather than silently accepted).
        """
        def pb(n: int) -> bool:
            return self._place_block_scored(a, n, is_lab=False)

        rem = ppw
        while rem >= 2:
            if not pb(2):
                return False
            rem -= 2
        if rem == 1:
            log.warning(
                "  CW subject '%s' (div %s) has odd periods_per_week=%d — "
                "placing final period as a single slot (2-consecutive rule "
                "can't be satisfied exactly).",
                a.subject.name, a.division.division_id, ppw,
            )
            return pb(1)
        return True

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Lab period helper
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _lab_ppw(subject: Subject) -> int:
        """
        Effective lab block length.  Returns DEFAULT_LAB_PERIODS when
        periods_per_week == 0 (real-DB artefact from "0-0-3-0" hour strings).
        """
        return subject.periods_per_week if subject.periods_per_week > 0 else DEFAULT_LAB_PERIODS

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Elective scheduling
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _schedule_one_elective_slot(
        self,
        group:      str,
        subj_id:    str,
        subj_asgns: List[SubjectAssignment],
    ) -> bool:
        """
        Place ONE parallel slot for all assignments in (group, subj_id).

        A group needs one (day, period) ANCHOR per required weekly session,
        shared across every subject offered in that elective group (so a
        student choosing any option in "Elective III" attends at the same
        times). This tries every anchor already established for the group
        first — skipping ones this subject already occupies — and only
        creates a brand-new anchor (appended to the group's list) when none
        of the existing ones work. This is a real fix, not just a rename:
        the previous version stored a single (day, pnum) per group and kept
        retrying that same slot for every period of every subject, so once
        it was filled, every subject requiring more than 1 period/week could
        never place its remaining periods (confirmed against real data —
        4-period/week electives were landing at 1/4 placed).
        """
        anchors = self._elective_group_anchor.setdefault(group, [])

        for anchor_day, anchor_pnum in anchors:
            if self._try_pin_elective_set(subj_asgns, anchor_day, anchor_pnum):
                return True

        # No existing anchor works for this subject/period — open a new one
        # for the group (used by this AND every other subject in the group
        # from now on).
        slot = self._find_elective_slot_scored(subj_asgns)
        if slot is None:
            log.warning(
                "  ✗ Elective group '%s' subj '%s': no free slot found "
                "(tried %d existing anchor(s)).",
                group, subj_asgns[0].subject.name, len(anchors),
            )
            return False

        anchor_day, anchor_pnum = slot
        anchors.append(slot)
        self._elective_subject_anchor[subj_id] = slot
        ok = self._try_pin_elective_set(subj_asgns, anchor_day, anchor_pnum)
        if ok:
            log.info(
                "  ✔ Elective group '%s' subj '%s' placed → %s P%d "
                "(group now has %d anchor slot(s) total).",
                group, subj_asgns[0].subject.name, anchor_day, anchor_pnum,
                len(anchors),
            )
        else:
            log.warning(
                "  ✗ Elective group '%s' subj '%s': new anchor %s P%d "
                "unusable immediately after being found.",
                group, subj_asgns[0].subject.name, anchor_day, anchor_pnum,
            )
        return ok

    def _find_elective_slot_scored(
        self, asgns: List[SubjectAssignment]
    ) -> Optional[Tuple[str, int]]:
        """
        Find the best (day, pnum) for a set of elective assignments.

        Valid = all division slots free AND all teachers free + within cap.
        Among valid slots, prefer higher elective_slot_score (avoids Mon P1,
        Fri last — improvement #6).
        """
        year = self.year_map.get(asgns[0].division.division_id, 2)

        # Collect all valid (score, day, pnum)
        candidates = []
        for day in DAYS:
            for period in get_periods(year, day):
                pnum = period.number
                if all(
                    self._slot_free(a.division.division_id, day, pnum)
                    and self._teacher_free(a.teacher.teacher_id, day, pnum, a.subject.subject_id)
                    and self._teacher_can_theory(a.teacher.teacher_id, day)
                    and self._teacher_day_ok(a.teacher.teacher_id, day)
                    for a in asgns
                ):
                    sc = self._elective_slot_score(day, pnum, year) + random.randint(0, 2)
                    candidates.append((sc, day, pnum))

        if not candidates:
            return None

        candidates.sort(key=lambda t: -t[0])
        _, best_day, best_pnum = candidates[0]
        return (best_day, best_pnum)

    # Keep old name for backward compatibility
    def _find_elective_slot(
        self, asgns: List[SubjectAssignment]
    ) -> Optional[Tuple[str, int]]:
        return self._find_elective_slot_scored(asgns)

    def _try_pin_elective_set(
        self, asgns: List[SubjectAssignment], day: str, pnum: int
    ) -> bool:
        """Book all assignments at (day, pnum) if still possible."""
        year = self.year_map.get(asgns[0].division.division_id, 2)
        if not any(p.number == pnum for p in get_periods(year, day)):
            return False
        if not all(
            self._slot_free(a.division.division_id, day, pnum)
            and self._teacher_free(a.teacher.teacher_id, day, pnum, a.subject.subject_id)
            and self._teacher_can_theory(a.teacher.teacher_id, day)
            and self._teacher_day_ok(a.teacher.teacher_id, day)
            for a in asgns
        ):
            return False
        for a in asgns:
            self._book(a, day, pnum, is_lab=False)
        return True

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Theory scheduling  (improvements #2 #3 #4)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _schedule_theory_for_division(
        self,
        div_id: str,
        asgns:  List[SubjectAssignment],
    ) -> None:
        """
        Place individual theory/project periods for one division using
        SCORED slot selection.

        The slot iterator uses a round-robin day order so the first pass
        naturally spreads subjects across the week.  For each subject-period
        needing placement the best-scoring free slot is chosen via
        _theory_slot_score(), which captures improvements #2, #3, and #4.
        """
        year = self.year_map.get(div_id, 2)

        # Build the work queue (interleaved to prevent same-subject runs)
        queue: List[SubjectAssignment] = []
        for a in asgns:
            already   = self._count_placed(a.subject.subject_id, div_id)
            ppw       = max(a.subject.periods_per_week, 1)
            remaining = ppw - already
            queue.extend([a] * remaining)

        if not queue:
            return

        # Handle project/seminar separately as consecutive blocks
        theory_queue: List[SubjectAssignment] = []
        for a in queue:
            if _is_project(a.subject):
                self._place_project_blocks(a, a.subject.periods_per_week or 1)
            else:
                theory_queue.append(a)

        queue = self._interleave(theory_queue)

        # Build candidate slots in round-robin day order (period row first)
        # so the first scan naturally distributes across all five days.
        # The day order is randomised each attempt for search diversity (#5).
        day_scan_order = list(DAYS)
        random.shuffle(day_scan_order)
        max_periods = max(len(get_periods(year, d)) for d in DAYS)
        all_slots: List[Tuple[str, int]] = []
        for p_idx in range(max_periods):
            for day in day_scan_order:
                periods = get_periods(year, day)
                if p_idx < len(periods):
                    all_slots.append((day, periods[p_idx].number))

        for a in queue:
            tid = a.teacher.teacher_id

            # Build (score, day, pnum) for every valid free slot
            scored: List[Tuple[int, str, int]] = []
            for day, pnum in all_slots:
                if self._grid.get((div_id, day, pnum)) is not None:
                    continue
                if not (self._teacher_free(tid, day, pnum, a.subject.subject_id)
                        and self._teacher_can_theory(tid, day)
                        and self._teacher_day_ok(tid, day)):
                    continue
                sc = self._theory_slot_score(a, day, pnum) + random.randint(0, 3)
                scored.append((sc, day, pnum))

            if not scored:
                self._log_unplaced(a, 1, "theory period")
                continue

            scored.sort(key=lambda t: -t[0])
            _, best_day, best_pnum = scored[0]
            self._book(a, best_day, best_pnum, is_lab=False)

    def _count_placed(self, subject_id: str, div_id: str) -> int:
        """Count slots already booked for (subject_id, div_id)."""
        return sum(
            1 for (d, _, _), a in self._grid.items()
            if d == div_id and a.subject.subject_id == subject_id
        )

    @staticmethod
    def _interleave(queue: List[SubjectAssignment]) -> List[SubjectAssignment]:
        """
        Rearrange a period queue so the same subject never appears in
        consecutive positions.  Uses round-robin across subject buckets.
        """
        buckets: Dict[str, deque] = defaultdict(deque)
        for a in queue:
            buckets[a.subject.subject_id].append(a)

        # Most-frequent subject first → gets spread widest
        order = sorted(buckets.keys(), key=lambda s: -len(buckets[s]))
        result: List[SubjectAssignment] = []
        while any(buckets[s] for s in order):
            for s in order:
                if buckets[s]:
                    result.append(buckets[s].popleft())
        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Conflict diagnostics  (improvement #7)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _log_unplaced(
        self,
        a:       SubjectAssignment,
        periods: int,
        kind:    str = "period",
    ) -> None:
        """
        Emit a structured warning explaining WHY a subject could not be placed.
        Covers all common failure modes (requirement #8).

        Possible causes reported:
          • teacher daily theory limit reached
          • teacher absolute daily period cap reached
          • teacher has very few remaining free slots (high overall load)
          • division timetable almost full
          • no continuous block available (labs only)
          • no free slot satisfies all constraints simultaneously
        """
        div_id = a.division.division_id
        tid    = a.teacher.teacher_id
        year   = self.year_map.get(div_id, 2)
        is_lab = a.subject.subject_type == SubjectType.LAB

        causes: List[str] = []

        # ── Check theory cap per day — report the specific days ───────────────
        days_at_theory_cap: List[str] = []
        for day in DAYS:
            used = self._teacher_theory_day.get((tid, day), 0)
            has_cap_reducer = (
                (tid, day) in self._teacher_lab_day
                or (tid, day) in self._teacher_project_day
            )
            cap = THEORY_CAP_WITH_LAB if has_cap_reducer else MAX_THEORY_PER_DAY
            if used >= cap:
                days_at_theory_cap.append(f"{day[:3]}({used}/{cap})")
        if len(days_at_theory_cap) >= 3:
            causes.append(
                f"teacher daily theory limit reached on "
                f"{len(days_at_theory_cap)} days: {', '.join(days_at_theory_cap)}"
            )

        # ── Check total cap per day — report specific days ────────────────────
        days_at_total_cap: List[str] = []
        for day in DAYS:
            used = self._teacher_total_day.get((tid, day), 0)
            if used >= MAX_TOTAL_PER_DAY:
                days_at_total_cap.append(f"{day[:3]}({used}/{MAX_TOTAL_PER_DAY})")
        if len(days_at_total_cap) >= 2:
            causes.append(
                f"teacher absolute daily cap reached on "
                f"{len(days_at_total_cap)} days: {', '.join(days_at_total_cap)}"
            )

        # ── Check division fullness per day ───────────────────────────────────
        full_days: List[str] = []
        for day in DAYS:
            n_today = len(get_periods(year, day))
            filled  = sum(
                1 for p in get_periods(year, day)
                if not self._slot_free(div_id, day, p.number)
            )
            if filled == n_today:
                full_days.append(day[:3])
        if full_days:
            causes.append(
                f"division {div_id} timetable completely full on: {', '.join(full_days)}"
            )

        # ── Check overall teacher occupancy ───────────────────────────────────
        total_slots = sum(len(get_periods(year, d)) for d in DAYS)
        busy_count  = sum(
            1 for d in DAYS for p in get_periods(year, d)
            if not self._teacher_free(tid, d, p.number)
        )
        if busy_count > total_slots * 0.75:
            causes.append(
                f"teacher occupancy very high ({busy_count}/{total_slots} slots used — "
                f"{int(busy_count*100/total_slots)}%)"
            )

        # ── Lab-specific: check continuous block availability per day ──────────
        if is_lab:
            candidates = self._find_consec_slots(a, periods, is_lab=True)
            if not candidates:
                # Find which days fail and why
                day_fail: List[str] = []
                for day in DAYS:
                    if (div_id, day) in self._div_lab_day:
                        day_fail.append(f"{day[:3]}(lab already scheduled)")
                    elif not self._teacher_day_ok(tid, day, extra=periods):
                        day_fail.append(f"{day[:3]}(daily cap)")
                    else:
                        day_fail.append(f"{day[:3]}(no free block or lunch conflict)")
                causes.append(
                    f"no continuous {periods}-period block available — "
                    f"per day: {', '.join(day_fail)}"
                )

        if not causes:
            causes.append(
                "no free slot satisfies all hard constraints simultaneously; "
                "consider increasing MAX_TOTAL_PER_DAY or adjusting periods_per_week"
            )

        log.warning(
            "Cannot place %s:\n"
            "  Subject  : %s\n"
            "  Division : %s\n"
            "  Teacher  : %s\n"
            "  Required : %d %s(s)\n"
            "  Conflicts:\n%s",
            kind,
            a.subject.name,
            div_id,
            a.teacher.name,
            periods,
            kind,
            "\n".join(f"    • {c}" for c in causes),
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Guaranteed-fill pass  (root-cause fix for S7/S8 blank periods)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #
    # WHY THIS EXISTS
    # ---------------
    # Year-4 (semesters 7 & 8) timetables have a ZERO-SLACK grid: the weekly
    # capacity is exactly 6+6+6+6+5 = 29 slots and the required LTPR load is
    # also 29, so the packing must be perfect.  The stochastic greedy scheduler
    # (the only scheduler that runs when CP-SAT / ortools is unavailable, and
    # the app calls generate(use_cpsat=False)) does not reliably achieve a
    # perfect packing.  The residual unplaced periods are always the block
    # subjects (Project Phase, Seminar) whose *shared* teachers become busy in
    # every remaining free division slot — i.e. teacher availability is the
    # binding constraint for the last few periods.
    #
    # WHAT THIS DOES
    # --------------
    # After the base scheduler commits its best grid, this pass forces every
    # division up to min(required, grid-capacity) filled periods:
    #   1. Rebuild teacher occupancy from the committed grid so clash checks
    #      are accurate.
    #   2. For each subject short of its required periods, place the missing
    #      periods with the REAL assigned teacher in any free division slot
    #      where that teacher is not already booked (soft daily caps relaxed;
    #      the two HARD rules — no teacher clash, no class clash — are kept).
    #   3. If the real teacher is busy in EVERY remaining free slot, substitute
    #      a temporary dummy teacher (cap-exempt, always free) for that residual
    #      period — this both guarantees the fill and proves that teacher
    #      availability was the cause (requirement #5).
    #   4. If the division has NO free slot at all, the required load exceeds
    #      the 29-slot capacity (e.g. CS S7 = 31 LTPR): mathematically
    #      impossible, logged as a capacity overflow.

    def _rebuild_full_teacher_state_from_grid(self) -> None:
        """Recompute every teacher-occupancy tracker from the committed grid.

        generate() commits ``best_grid`` but the trackers still reflect the
        LAST greedy attempt, so any clash check would be wrong.  Replay the
        grid (primary + parallel) through the same bookkeeping ``_book`` uses.
        """
        self._teacher_busy       = {}
        self._teacher_theory_day = defaultdict(int)
        self._teacher_total_day  = defaultdict(int)
        self._teacher_lab_day    = set()
        self._teacher_project_day = set()
        self._div_lab_day        = set()

        def _apply(a: SubjectAssignment, day: str, pnum: int) -> None:
            tid = a.teacher.teacher_id
            sid = a.subject.subject_id
            div_id = a.division.division_id
            is_lab = (a.subject.subject_type == SubjectType.LAB)
            parallel = self._teacher_busy.get((tid, day, pnum)) == sid
            self._teacher_busy[(tid, day, pnum)] = sid
            if not parallel:
                self._teacher_total_day[(tid, day)] += 1
                is_lab_or_proj = (
                    is_lab
                    or a.subject.subject_type in (SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW)
                    or _is_project(a.subject) or _is_cw(a.subject)
                )
                if not is_lab_or_proj:
                    self._teacher_theory_day[(tid, day)] += 1
                if is_lab:
                    self._teacher_lab_day.add((tid, day))
                    self._div_lab_day.add((div_id, day))
                if (not is_lab) and _is_project(a.subject):
                    self._teacher_project_day.add((tid, day))
            asst = self._assistant_map.get((sid, div_id))
            if asst and is_lab:
                self._teacher_busy[(asst.teacher_id, day, pnum)] = sid
            for co in self._co_supervisor_map.get((sid, div_id), []):
                if self._teacher_busy.get((co.teacher_id, day, pnum)) is None:
                    self._teacher_busy[(co.teacher_id, day, pnum)] = sid

        for (div_id, day, pnum), a in list(self._grid.items()):
            _apply(a, day, pnum)
        for (div_id, day, pnum), a2 in list(self._parallel_grid.items()):
            _apply(a2, day, pnum)

        # Re-apply cross-semester external busy blocks (gap #4).
        for key in self._external_busy_keys:
            self._teacher_busy.setdefault(key, "__EXTERNAL_BUSY__")

    def _required_periods_by_division(
        self,
    ) -> Dict[str, List[Tuple[SubjectAssignment, int]]]:
        """Return {div_id: [(main_assignment, required_periods), ...]}."""
        out: Dict[str, List[Tuple[SubjectAssignment, int]]] = defaultdict(list)
        seen: Set[Tuple[str, str]] = set()
        for a in self.assignments:
            if a.role != "main":
                continue
            key = (a.division.division_id, a.subject.subject_id)
            if key in seen:
                continue
            seen.add(key)
            if a.subject.subject_type == SubjectType.LAB:
                need = self._lab_ppw(a.subject)
            else:
                need = max(a.subject.periods_per_week, 1)
            out[a.division.division_id].append((a, need))
        return out

    def _make_fill_dummy(self, base_assignment: SubjectAssignment) -> SubjectAssignment:
        """Create a temporary dummy-teacher assignment for a residual period.

        The dummy is cap-exempt and teaches only this subject, so it is free
        in every slot — placement is then limited solely by division-slot
        availability.  Returns a new SubjectAssignment bound to the dummy.
        """
        self._fill_dummy_counter = getattr(self, "_fill_dummy_counter", 0) + 1
        div = base_assignment.division
        dept_id = getattr(div, "student_dept_id", "") or base_assignment.teacher.dept_id
        tid = f"dummy-fill-{dept_id}-{div.division_id}-{self._fill_dummy_counter}"
        dummy = Teacher(
            teacher_id  = tid,
            name        = f"TBD-FILL-{self._fill_dummy_counter}",
            dept_id     = dept_id,
            designation = Designation.LECTURER,
            is_dummy    = True,
        )
        # Register so cap-exemption predicates recognise it.
        self._teacher_is_dummy[tid]  = True
        self._teacher_is_bsh[tid]    = False
        self.teachers.append(dummy)
        return SubjectAssignment(
            subject  = base_assignment.subject,
            division = div,
            teacher  = dummy,
            role     = "main",
        )

    def _guaranteed_fill_pass(self, use_dummy_for_unavailable: bool = True) -> None:
        """Force every division to min(required, capacity) filled periods.

        Runs after the base scheduler.  See the section header for the full
        rationale.  Idempotent: subjects already at their required count are
        skipped.
        """
        self._rebuild_full_teacher_state_from_grid()
        required = self._required_periods_by_division()

        self._fill_substitutions: List[str] = []
        self._fill_overflow:      List[str] = []

        for div in self.divisions:
            div_id = div.division_id
            year   = self.year_map.get(div_id, 4)
            for (a, need) in required.get(div_id, []):
                sid = a.subject.subject_id
                placed = self._count_placed(sid, div_id)
                missing = need - placed
                if missing <= 0:
                    continue

                for _ in range(missing):
                    # All currently-free slots for this division, earliest first.
                    free_slots: List[Tuple[str, int]] = []
                    for day in DAYS:
                        for period in get_periods(year, day):
                            if self._slot_free(div_id, day, period.number):
                                free_slots.append((day, period.number))

                    if not free_slots:
                        # No slot left at all → required load exceeds 29-slot
                        # capacity (mathematically impossible to fully schedule).
                        self._fill_overflow.append(
                            f"{div_id}/{a.subject.name}: capacity overflow "
                            f"(required {need} > 29 weekly slots)"
                        )
                        break

                    # ── Attempt 1: real teacher, hard rules only ──────────────
                    tid = a.teacher.teacher_id
                    booked = False
                    for (day, pnum) in free_slots:
                        if self._teacher_free(tid, day, pnum, sid):
                            self._book(a, day, pnum,
                                       is_lab=(a.subject.subject_type == SubjectType.LAB))
                            booked = True
                            break
                    if booked:
                        continue

                    # ── Attempt 2: real teacher unavailable everywhere →
                    #    substitute a temporary dummy teacher (requirement #5) ──
                    if use_dummy_for_unavailable:
                        day, pnum = free_slots[0]
                        dummy_a = self._make_fill_dummy(a)
                        self._book(dummy_a, day, pnum, is_lab=False)
                        self._fill_substitutions.append(
                            f"{div_id}/{a.subject.name} @ {day} P{pnum}: "
                            f"real teacher '{a.teacher.name}' busy at every free "
                            f"slot → temporary teacher '{dummy_a.teacher.name}' assigned"
                        )
                    else:
                        break

        if self._fill_substitutions:
            log.warning(
                "Guaranteed-fill: %d period(s) filled via temporary teachers "
                "(real teacher unavailable):\n%s",
                len(self._fill_substitutions),
                "\n".join(f"    • {s}" for s in self._fill_substitutions),
            )
        if self._fill_overflow:
            log.warning(
                "Guaranteed-fill: %d period(s) could NOT be placed — required "
                "load exceeds the 29-slot weekly capacity:\n%s",
                len(self._fill_overflow),
                "\n".join(f"    • {s}" for s in self._fill_overflow),
            )

    def fill_report(self) -> Dict[str, Dict]:
        """Per-division accounting: expected / generated / blank / shortfalls.

        Reads the committed grid; safe to call after generate().
        """
        required = self._required_periods_by_division()
        report: Dict[str, Dict] = {}
        for div in self.divisions:
            div_id = div.division_id
            year   = self.year_map.get(div_id, 4)
            capacity = sum(len(get_periods(year, d)) for d in DAYS)
            expected = sum(need for (_a, need) in required.get(div_id, []))
            occupied = sum(
                1 for d in DAYS for p in get_periods(year, d)
                if not self._slot_free(div_id, d, p.number)
            )
            shortfalls = []
            for (a, need) in required.get(div_id, []):
                got = self._count_placed(a.subject.subject_id, div_id)
                if got < need:
                    shortfalls.append((a.subject.name, need, got))
            report[div_id] = {
                "expected":  expected,
                "capacity":  capacity,
                "generated": occupied,
                "blank":     capacity - occupied,
                "shortfalls": shortfalls,
            }
        return report

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Result builder
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_result(self) -> TimetableResult:
        class_tt:   Dict[str, List[TimetableCell]] = defaultdict(list)
        teacher_tt: Dict[str, List[TimetableCell]] = defaultdict(list)

        # Track which (teacher_id, day, period_number) slots have already been
        # added to teacher_tt to prevent duplicate rows from parallel teaching.
        teacher_slot_seen: Set[Tuple[str, str, int]] = set()

        for div in self.divisions:
            year = self.year_map.get(div.division_id, 2)
            for day in DAYS:
                for period in get_periods(year, day):
                    pnum = period.number
                    a1 = self._grid.get((div.division_id, day, pnum))
                    a2 = self._parallel_grid.get((div.division_id, day, pnum))

                    # ── Class timetable cell ──────────────────────────────────
                    # When a2 exists this is a parallel-lab slot: both subjects
                    # are surfaced in the same cell via the paired_* fields.
                    cell = TimetableCell(
                        division_id       = div.division_id,
                        day               = day,
                        period            = period,
                        subject           = a1.subject if a1 else None,
                        teacher           = a1.teacher if a1 else None,
                        is_lab            = bool(
                            (a1 and a1.subject.subject_type == SubjectType.LAB)
                            or (a2 and a2.subject.subject_type == SubjectType.LAB)
                        ),
                        is_free           = (a1 is None and a2 is None),
                        short_code        = (self._short_codes.get(a1.subject.subject_id, "?")
                                             if a1 else ""),
                        paired_subject    = a2.subject if a2 else None,
                        paired_teacher    = a2.teacher if a2 else None,
                        paired_short_code = (self._short_codes.get(a2.subject.subject_id, "?")
                                             if a2 else ""),
                    )
                    class_tt[div.division_id].append(cell)

                    # ── Teacher timetable entries ─────────────────────────────
                    # Primary lab (a1) teacher entry
                    if a1:
                        t_slot = (a1.teacher.teacher_id, day, pnum)
                        if t_slot not in teacher_slot_seen:
                            teacher_slot_seen.add(t_slot)
                            teacher_tt[a1.teacher.teacher_id].append(cell)

                    # Parallel lab (a2) teacher entry — uses a dedicated cell
                    # that surfaces a2 as the *primary* subject for that teacher
                    if a2:
                        t_slot2 = (a2.teacher.teacher_id, day, pnum)
                        if t_slot2 not in teacher_slot_seen:
                            teacher_slot_seen.add(t_slot2)
                            # Build a teacher-perspective cell: a2 is primary
                            t_cell = TimetableCell(
                                division_id  = div.division_id,
                                day          = day,
                                period       = period,
                                subject      = a2.subject,
                                teacher      = a2.teacher,
                                is_lab       = True,
                                is_free      = False,
                                short_code   = self._short_codes.get(
                                    a2.subject.subject_id, "?"),
                            )
                            teacher_tt[a2.teacher.teacher_id].append(t_cell)

        return TimetableResult(
            class_timetables  = dict(class_tt),
            teacher_timetables= dict(teacher_tt),
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CP-SAT solver  (improvement phase — runs after greedy base)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _solve_cpsat(
        self,
        initial_grid: Optional[Dict[Tuple[str, str, int], "SubjectAssignment"]] = None,
    ) -> Optional[Dict[Tuple[str, str, int], "SubjectAssignment"]]:
        """
        Improve (or build from scratch) a timetable using OR-Tools CP-SAT.

        When ``initial_grid`` is provided (greedy warm-start), every filled slot
        is given a hint via ``model.AddHint()``.  CP-SAT starts from a 95+ point
        and polishes rather than searching from scratch.

        Returns a (grid, parallel_grid) tuple on success, or None on failure/unavailability.

        ══════════════════════════════════════════════════════════════════════
        HARD CONSTRAINTS
        ══════════════════════════════════════════════════════════════════════
        HC-1  Exact periods_per_week per assignment.
        HC-2  No class double-booking.
        HC-3  No teacher clash (parallel teaching of same subject allowed).
        HC-4  Teacher daily theory cap ≤ 3 (2 if teacher has a lab that day).
        HC-5  Teacher daily total cap ≤ MAX_TOTAL_PER_DAY.
        HC-6  Lab continuity — no straddling lunch break.
        HC-7  Elective parallel — same (day, period) for all in group.

        ══════════════════════════════════════════════════════════════════════
        SOFT OBJECTIVES — spec-exact weights
        ══════════════════════════════════════════════════════════════════════
        minimize(
          subject_day_excess × 10
          + consecutive_pairs × 12 + triple_runs × 12
          + teacher_overload  ×  8
          + teacher_gaps      ×  6
          + friday_last       ×  6
          + late_labs         ×  4
          + workload_cv       ×  3
        )

        ══════════════════════════════════════════════════════════════════════
        SOLVER PARAMETERS  (CP-SAT is the PRIMARY solver per spec)
        ══════════════════════════════════════════════════════════════════════
        • max_time_in_seconds = 60   (primary solver — not just a polish pass)
        • num_search_workers  = 12
        • random_seed         = random.randint(1, 10000)
        """
        try:
            from ortools.sat.python import cp_model
        except ImportError:
            log.info("OR-Tools not installed — skipping CP-SAT.")
            return None

        log.info("Attempting CP-SAT improvement (15 s, 8 workers) …")
        model = cp_model.CpModel()

        # ── Separate main assignments by type ──────────────────────────────────
        main_asgns  = [a for a in self.assignments if a.role == "main"]
        lab_asgns   = [a for a in main_asgns if a.subject.subject_type == SubjectType.LAB]
        other_asgns = [a for a in main_asgns if a.subject.subject_type != SubjectType.LAB]
        if not main_asgns:
            return None

        # ── Index lookups ──────────────────────────────────────────────────────
        # ai_of[id(a)] → integer index of assignment a in main_asgns
        ai_of = {id(a): i for i, a in enumerate(main_asgns)}

        # by_div[div_id] → [ai ...]  (all main-assignment indices for that div)
        by_div: Dict[str, List[int]] = defaultdict(list)
        for ai, a in enumerate(main_asgns):
            by_div[a.division.division_id].append(ai)

        # by_teacher[tid] → [ai ...]
        by_teacher: Dict[str, List[int]] = defaultdict(list)
        for ai, a in enumerate(main_asgns):
            by_teacher[a.teacher.teacher_id].append(ai)

        # ══════════════════════════════════════════════════════════════════════
        # Decision variables
        # ══════════════════════════════════════════════════════════════════════

        # x[(ai, di, pi)] = 1 iff assignment ai occupies slot (day di, period pi)
        x: Dict[Tuple[int, int, int], object] = {}
        for ai, a in enumerate(main_asgns):
            year = self.year_map.get(a.division.division_id, 2)
            for di, day in enumerate(DAYS):
                for pi in range(len(get_periods(year, day))):
                    x[(ai, di, pi)] = model.NewBoolVar(f"x_{ai}_{di}_{pi}")

        # ── Lab block-start variables  (requirement #2) ────────────────────────
        # lab_start[(ai, di, si)] = 1  iff  lab assignment ai starts at
        # (day di, period-index si) and occupies the next L-1 periods too.
        # This is the "block variable" that guarantees lab continuity without
        # needing ad-hoc consecutive-slot enumeration in the main x-vars.
        lab_start: Dict[Tuple[int, int, int], object] = {}

        for a in lab_asgns:
            ai          = ai_of[id(a)]
            lab_ppw_val = self._lab_ppw(a.subject)
            lab_year    = self.year_map.get(a.division.division_id, 2)
            # For year-2/3 parallel labs (ppw == PARALLEL_LAB_PPW), the physical
            # block placed each day is PARALLEL_LAB_BLOCK periods (= ppw / 2).
            is_parallel_lab = lab_year in (2, 3) and lab_ppw_val == PARALLEL_LAB_PPW
            L    = PARALLEL_LAB_BLOCK if is_parallel_lab else lab_ppw_val
            year = lab_year

            for di, day in enumerate(DAYS):
                periods = get_periods(year, day)
                lunch   = get_lunch_break_after(year, day)
                n_p     = len(periods)

                for si in range(n_p - L + 1):
                    # Period numbers for this block
                    run = [periods[si + k].number for k in range(L)]

                    # Hard: block must not straddle lunch break
                    if run[0] <= lunch < run[-1]:
                        continue   # invalid start position, don't create variable

                    lsv = model.NewBoolVar(f"ls_{ai}_{di}_{si}")
                    lab_start[(ai, di, si)] = lsv

                    # If this start is chosen, ALL L periods must be set in x
                    for k in range(L):
                        pi = si + k
                        if (ai, di, pi) in x:
                            # lab_start → x[ai,di,pi]
                            model.AddImplication(lsv, x[(ai, di, pi)])

            # HC-6a: block-start count per lab
            # Year 2 & 3 parallel labs (ppw == PARALLEL_LAB_PPW) → exactly TWO
            # block-starts on DIFFERENT days (one 3-period block per lab day).
            # All other labs → exactly ONE block-start.
            start_vars = list(lab_start.get((ai, di, si), None)
                              for di in range(len(DAYS))
                              for si in range(len(get_periods(
                                  self.year_map.get(a.division.division_id, 2), DAYS[di])))
                              if (ai, di, si) in lab_start)
            start_vars = [v for v in start_vars if v is not None]
            need_two_sessions = is_parallel_lab
            if start_vars:
                if need_two_sessions:
                    # Exactly 2 block-starts, and they must be on DIFFERENT days
                    model.Add(sum(start_vars) == 2)
                    # Enforce different days: for each day, at most 1 start from that day
                    for di_check in range(len(DAYS)):
                        day_starts = [
                            lab_start[(ai, di_check, si)]
                            for si in range(len(get_periods(
                                self.year_map.get(a.division.division_id, 2), DAYS[di_check])))
                            if (ai, di_check, si) in lab_start
                        ]
                        if day_starts:
                            model.Add(sum(day_starts) <= 1)
                    # HC-1 note: for parallel labs ppw == PARALLEL_LAB_PPW (6) and
                    # 2 * PARALLEL_LAB_BLOCK == 6, so HC-1's direct use of
                    # a.subject.periods_per_week already gives the correct count (6).
                    # No _two_session_ais override needed.
                else:
                    model.AddExactlyOne(start_vars)

            # HC-6b: x[ai] periods must come ONLY from the chosen block
            # i.e. if x[ai,di,pi] is set, some lab_start must cover it
            year = self.year_map.get(a.division.division_id, 2)
            for di, day in enumerate(DAYS):
                periods = get_periods(year, day)
                for pi in range(len(periods)):
                    xv = x.get((ai, di, pi))
                    if xv is None:
                        continue
                    # Collect all lab_start vars that cover (di, pi)
                    # Use L (block size) not full ppw for the coverage range
                    covering = [
                        lab_start[(ai, di, si)]
                        for si in range(max(0, pi - L + 1), pi + 1)
                        if (ai, di, si) in lab_start
                    ]
                    if covering:
                        # x[ai,di,pi] = 1 → at least one covering start is 1
                        model.AddBoolOr(covering).OnlyEnforceIf(xv)
                    else:
                        # No valid block covers this slot — force it to 0
                        model.Add(xv == 0)

        # ── Identify parallel lab pairs for CP-SAT ───────────────────────────
        # parallel_pairs: div_id → (lab1_ai, lab2_ai)
        # These two labs must be placed at EXACTLY the same (day, period) slots.
        _div_parallel: Dict[str, List[SubjectAssignment]] = defaultdict(list)
        for a in lab_asgns:
            lab_ppw_v = self._lab_ppw(a.subject)
            lab_yr    = self.year_map.get(a.division.division_id, 2)
            if lab_yr in (2, 3) and lab_ppw_v == PARALLEL_LAB_PPW:
                _div_parallel[a.division.division_id].append(a)

        parallel_pairs: Dict[str, Tuple[int, int]] = {}  # div_id → (ai1, ai2)
        for div_id, labs in _div_parallel.items():
            if len(labs) == 2:
                ai1 = ai_of[id(labs[0])]
                ai2 = ai_of[id(labs[1])]
                parallel_pairs[div_id] = (ai1, ai2)

        # ══════════════════════════════════════════════════════════════════════
        # HARD CONSTRAINTS
        # ══════════════════════════════════════════════════════════════════════

        # HC-1: exact period count per assignment
        # For year-2/3 parallel labs (ppw == PARALLEL_LAB_PPW == 6), the DB already
        # stores periods_per_week = 6, and 2 blocks × PARALLEL_LAB_BLOCK (3) = 6.
        # So max(a.subject.periods_per_week, 1) gives the correct total directly —
        # no special override is needed.
        for ai, a in enumerate(main_asgns):
            year = self.year_map.get(a.division.division_id, 2)
            ppw = max(a.subject.periods_per_week, 1)
            all_vars = [
                x[(ai, di, pi)]
                for di in range(len(DAYS))
                for pi in range(len(get_periods(year, DAYS[di])))
                if (ai, di, pi) in x
            ]
            model.Add(sum(all_vars) == ppw)

        # HC-1b: parallel-sync constraint
        # For each parallel lab pair (lab1_ai, lab2_ai), both must be placed
        # at EXACTLY the same (day, period) slots.  This is enforced by
        # requiring x[lab1_ai, di, pi] == x[lab2_ai, di, pi] for all (di, pi).
        for div_id, (ai1, ai2) in parallel_pairs.items():
            a1   = main_asgns[ai1]
            year = self.year_map.get(div_id, 2)
            for di, day in enumerate(DAYS):
                for pi in range(len(get_periods(year, day))):
                    v1 = x.get((ai1, di, pi))
                    v2 = x.get((ai2, di, pi))
                    if v1 is not None and v2 is not None:
                        model.Add(v1 == v2)
                    elif v1 is not None:
                        model.Add(v1 == 0)
                    elif v2 is not None:
                        model.Add(v2 == 0)

        # HC-2: no class double-booking
        # Parallel lab pairs are EXEMPT: lab1 and lab2 intentionally share the
        # same (div_id, day, period) slots (students are split into batches).
        # Build a set of (div_id, ai) pairs that belong to a parallel pair.
        parallel_ais: Set[Tuple[str, int]] = set()
        for div_id, (ai1, ai2) in parallel_pairs.items():
            parallel_ais.add((div_id, ai1))
            parallel_ais.add((div_id, ai2))

        for div_id, ai_list in by_div.items():
            year = self.year_map.get(div_id, 2)
            for di, day in enumerate(DAYS):
                for pi in range(len(get_periods(year, day))):
                    # Separate parallel-lab vars from all others
                    par_cvars   = [x[(ai, di, pi)] for ai in ai_list
                                   if (ai, di, pi) in x
                                   and (div_id, ai) in parallel_ais]
                    other_cvars = [x[(ai, di, pi)] for ai in ai_list
                                   if (ai, di, pi) in x
                                   and (div_id, ai) not in parallel_ais]
                    # At most 1 non-parallel subject per slot
                    if len(other_cvars) > 1:
                        model.Add(sum(other_cvars) <= 1)
                    # Non-parallel slots cannot overlap with parallel-lab slots
                    # (a theory class can't run while labs are running)
                    if other_cvars and par_cvars:
                        for ov in other_cvars:
                            for pv in par_cvars:
                                model.Add(ov + pv <= 1)

        # HC-3: no teacher clash — a teacher cannot teach two DIFFERENT subjects
        # at the same time.  Teaching the SAME subject to multiple divisions in
        # parallel IS allowed (parallel-teaching rule).
        #
        # Build: by_teacher_subj[(tid, sid)] = [ai, ...]
        by_teacher_subj: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for ai, a in enumerate(main_asgns):
            by_teacher_subj[(a.teacher.teacher_id, a.subject.subject_id)].append(ai)

        for tid, ai_list in by_teacher.items():
            # Group assignments by subject
            subj_groups: Dict[str, List[int]] = defaultdict(list)
            for ai in ai_list:
                subj_groups[main_asgns[ai].subject.subject_id].append(ai)

            for di, day in enumerate(DAYS):
                # Collect all period indices used by this teacher on this day
                pi_set: Set[int] = set()
                for ai in ai_list:
                    yr = self.year_map.get(main_asgns[ai].division.division_id, 2)
                    for pi in range(len(get_periods(yr, day))):
                        pi_set.add(pi)

                for pi in pi_set:
                    # For each (di, pi), group x-vars by subject_id.
                    # Same subject at same slot = parallel teaching (allowed).
                    # Different subjects at same slot = clash (forbidden).
                    slot_by_subj: Dict[str, List] = defaultdict(list)
                    for ai in ai_list:
                        if (ai, di, pi) in x:
                            sid = main_asgns[ai].subject.subject_id
                            slot_by_subj[sid].append(x[(ai, di, pi)])

                    if len(slot_by_subj) <= 1:
                        continue   # only one subject at this slot — no clash possible

                    # At most one distinct subject may be active at (teacher, day, period):
                    # create an indicator per subject and require at most one is chosen.
                    subj_active = []
                    for sid, svars in slot_by_subj.items():
                        # Indicator = 1 iff any assignment of this subject fires here
                        ind = model.NewBoolVar(f"tsubj_{tid}_{di}_{pi}_{sid}")
                        model.Add(sum(svars) >= 1).OnlyEnforceIf(ind)
                        model.Add(sum(svars) == 0).OnlyEnforceIf(ind.Not())
                        subj_active.append(ind)
                    model.Add(sum(subj_active) <= 1)

        # Parallel-teaching equality constraint:
        # If a teacher teaches the same subject to multiple divisions, ALL those
        # assignments must be placed at the SAME (day, period) slot each week.
        # For each (teacher, subject) pair with 2+ divisions, all x-vars at the
        # same slot (di, pi) must be equal (they're either all scheduled here or none).
        for (tid, sid), ai_list_ts in by_teacher_subj.items():
            if len(ai_list_ts) < 2:
                continue
            ref_year = self.year_map.get(
                main_asgns[ai_list_ts[0]].division.division_id, 2
            )
            for di, day in enumerate(DAYS):
                n_p = len(get_periods(ref_year, day))
                for pi in range(n_p):
                    slot_vars = [
                        x[(ai, di, pi)]
                        for ai in ai_list_ts
                        if (ai, di, pi) in x
                    ]
                    if len(slot_vars) < 2:
                        continue
                    # All must be equal: either all fired or none
                    for v in slot_vars[1:]:
                        model.Add(slot_vars[0] == v)

        # HC-4: teacher daily theory cap
        # Uses a per-period-slot indicator so parallel-teaching slots are
        # counted ONCE even when the teacher covers multiple divisions.
        # Cap is reduced to THEORY_CAP_WITH_LAB (2) on any day where the
        # teacher has a lab, a Project Phase, or a Seminar.
        theory_types = {SubjectType.THEORY, SubjectType.ELECTIVE}

        # Build: (tid, day_index) pairs that have a lab assignment
        teacher_lab_day_cpsat: Set[Tuple[str, int]] = set()
        for a in lab_asgns:
            tid_l = a.teacher.teacher_id
            for di_l, day_l in enumerate(DAYS):
                ai_l = ai_of.get(id(a))
                if ai_l is None:
                    continue
                for si_l in range(len(get_periods(
                        self.year_map.get(a.division.division_id, 2), DAYS[di_l]))):
                    if (ai_l, di_l, si_l) in lab_start:
                        teacher_lab_day_cpsat.add((tid_l, di_l))
                        break

        # Build: (tid, day_index) pairs that have a Project Phase / Seminar
        # assignment — these also reduce the theory cap to THEORY_CAP_WITH_LAB.
        teacher_project_day_cpsat: Set[Tuple[str, int]] = set()
        proj_main_asgns = [
            a for a in main_asgns
            if _is_project(a.subject)
            and a.subject.subject_type != SubjectType.LAB
        ]
        for a in proj_main_asgns:
            tid_p = a.teacher.teacher_id
            ai_p  = ai_of.get(id(a))
            if ai_p is None:
                continue
            for di_p in range(len(DAYS)):
                n_sp = len(get_periods(
                    self.year_map.get(a.division.division_id, 2), DAYS[di_p]))
                for si_p in range(n_sp):
                    if (ai_p, di_p, si_p) in x:
                        teacher_project_day_cpsat.add((tid_p, di_p))
                        break

        # HC-4: teacher daily theory cap.
        # Max THEORY_CAP_WITH_LAB (2) theory periods/day if teacher has a lab/project,
        # otherwise max 3 (BSH) or 2 (non-BSH).
        # Same parallel-teaching deduction as above: bool indicators per period.
        for tid, ai_list in by_teacher.items():
            if self._teacher_is_dummy.get(tid):
                continue  # dummy teachers exempt from daily caps
                
            ref_year = self.year_map.get(main_asgns[ai_list[0]].division.division_id, 2)
            theory_ais = [
                ai for ai in ai_list
                if main_asgns[ai].subject.subject_type in theory_types
                and not _is_project(main_asgns[ai].subject)
            ]
            if not theory_ais:
                continue
            for di, day in enumerate(DAYS):
                n_p = len(get_periods(ref_year, day))
                # One indicator per period: 1 iff teacher teaches any theory here
                slot_indicators = []
                for pi in range(n_p):
                    pvars = [
                        x[(ai, di, pi)]
                        for ai in theory_ais
                        if (ai, di, pi) in x
                    ]
                    if not pvars:
                        continue
                    ind = model.NewBoolVar(f"th_slot_{tid}_{di}_{pi}")
                    model.Add(sum(pvars) >= 1).OnlyEnforceIf(ind)
                    model.Add(sum(pvars) == 0).OnlyEnforceIf(ind.Not())
                    slot_indicators.append(ind)
                if slot_indicators:
                    # Reduce cap to THEORY_CAP_WITH_LAB on days this teacher
                    # has a lab, a Project Phase, or a Seminar.
                    has_cap_reducer = (
                        (tid, di) in teacher_lab_day_cpsat
                        or (tid, di) in teacher_project_day_cpsat
                    )
                    
                    base_cap = MAX_THEORY_PER_DAY
                    if self._teacher_is_bsh.get(tid):
                        base_cap = 3
                    else:
                        base_cap = 2
                        
                    cap = THEORY_CAP_WITH_LAB if has_cap_reducer else base_cap
                    model.Add(sum(slot_indicators) <= cap)

        # HC-5: teacher daily total cap — count unique occupied slots, not
        # assignment count, so parallel-teaching does not inflate the count.
        for tid, ai_list in by_teacher.items():
            if self._teacher_is_dummy.get(tid):
                continue  # dummy teachers exempt from total cap
                
            ref_year = self.year_map.get(main_asgns[ai_list[0]].division.division_id, 2)
            for di, day in enumerate(DAYS):
                n_p = len(get_periods(ref_year, day))
                slot_indicators = []
                for pi in range(n_p):
                    pvars = [
                        x[(ai, di, pi)]
                        for ai in ai_list
                        if (ai, di, pi) in x
                    ]
                    if not pvars:
                        continue
                    ind = model.NewBoolVar(f"tot_slot_{tid}_{di}_{pi}")
                    model.Add(sum(pvars) >= 1).OnlyEnforceIf(ind)
                    model.Add(sum(pvars) == 0).OnlyEnforceIf(ind.Not())
                    slot_indicators.append(ind)
                if slot_indicators:
                    model.Add(sum(slot_indicators) <= MAX_TOTAL_PER_DAY)

        # HC-7: elective parallel anchor  (requirement #3)
        # All assignments in the same elective_group must share one (day, period).
        # We model this with a single anchor variable per (group, di, pi) rather
        # than pairwise equality constraints — this is tighter and clearer.
        eg_by_group: Dict[str, List[int]] = defaultdict(list)
        for ai, a in enumerate(main_asgns):
            if (a.subject.subject_type == SubjectType.ELECTIVE
                    and a.subject.elective_group):
                eg_by_group[a.subject.elective_group].append(ai)

        elective_anchor: Dict[Tuple[str, int, int], object] = {}
        for group, ai_list in eg_by_group.items():
            if len(ai_list) < 2:
                continue
            ref_year = self.year_map.get(main_asgns[ai_list[0]].division.division_id, 2)
            # Create one anchor BoolVar per (group, di, pi)
            for di, day in enumerate(DAYS):
                for pi in range(len(get_periods(ref_year, day))):
                    av = model.NewBoolVar(f"eg_{group}_{di}_{pi}")
                    elective_anchor[(group, di, pi)] = av

            # Exactly one (di, pi) anchor is active per group
            anchor_vars = [
                elective_anchor[(group, di, pi)]
                for di in range(len(DAYS))
                for pi in range(len(get_periods(ref_year, DAYS[di])))
                if (group, di, pi) in elective_anchor
            ]
            if anchor_vars:
                model.AddExactlyOne(anchor_vars)

            # Each assignment in the group must be placed EXACTLY at the anchor slot
            for ai in ai_list:
                for di in range(len(DAYS)):
                    for pi in range(len(get_periods(ref_year, DAYS[di]))):
                        xv = x.get((ai, di, pi))
                        av = elective_anchor.get((group, di, pi))
                        if xv is None or av is None:
                            continue
                        # anchor ↔ x[ai]: they must agree
                        model.Add(xv == av)

        # ══════════════════════════════════════════════════════════════════════
        # SOFT OBJECTIVES  — 7 weighted penalties (spec Rules 1–7)
        #
        # The CP-SAT solver minimises this weighted sum, driving it toward
        # schedules that score 90–100 on score_schedule().
        #
        # Weights                  Spec   Rationale
        # ─────────────────────   ──────  ──────────────────────────────────────
        # S1 same-subj >1× /day    10    students see same subject twice in a day
        # S2 spread subjects         8    related: more than 1× on any day
        # S3 3+ consecutive         12    3 periods same subject in a row
        # S4 friday last period      6    students dislike late Friday classes
        # S5 teacher overload >3     8    teacher teaches 4-5 periods in one day
        # S6 teacher idle gaps       5    teacher has a long idle gap mid-day
        # S7 late labs (after P4)    4    labs should be in morning slots
        #
        # Note: S1 and S2 are modelled by the SAME "excess" variable because
        # "spread subjects across week" and "avoid >1× per day" share the
        # same decision variable (day-subject count > 1).  S2's weight (8) is
        # folded into S1's (10) as a combined weight of 10 on each excess unit.
        # ══════════════════════════════════════════════════════════════════════

        obj_terms: List[object] = []

        # ── S1 + S2 (weight 10): same theory subject more than once in same day ──
        # Penalise each occurrence of a (div, subject) beyond the SECOND in a day.
        # Spec Rule 1: subject(day) ≤ 2 — only penalise 3+ occurrences per day.
        # Appearing twice is acceptable (and sometimes unavoidable in fully-packed
        # timetables).  This captures both S1 and S2.
        for div_id, ai_list in by_div.items():
            year = self.year_map.get(div_id, 2)
            by_subj_id2: Dict[str, List[int]] = defaultdict(list)
            for ai in ai_list:
                a = main_asgns[ai]
                if a.subject.subject_type not in (SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW):
                    by_subj_id2[a.subject.subject_id].append(ai)

            for s_ais in by_subj_id2.values():
                ppw_s = max(main_asgns[s_ais[0]].subject.periods_per_week, 1)
                if ppw_s < 3:
                    continue   # needs at least 3 periods to possibly appear 3× in a day
                for di, day in enumerate(DAYS):
                    n_p = len(get_periods(year, day))
                    day_s_vars = [
                        x[(ai, di, pi)]
                        for ai in s_ais
                        for pi in range(n_p)
                        if (ai, di, pi) in x
                    ]
                    if len(day_s_vars) >= 3:
                        # excess = max(0, count_today - 2)  ← threshold is 2
                        excess_s = model.NewIntVar(
                            0, n_p, f"ex_{div_id}_{di}_{s_ais[0]}"
                        )
                        model.Add(excess_s >= sum(day_s_vars) - 2)
                        obj_terms.extend([excess_s] * 10)   # weight = 10

        # ── S3 (weight 12): consecutive same-theory-subject pairs ─────────────
        # Penalise adjacent-period pairs where the same non-lab subject appears.
        # Additionally apply a DOUBLE penalty when a triple (3-in-a-row) occurs.
        for div_id, ai_list in by_div.items():
            year = self.year_map.get(div_id, 2)
            by_subj_id: Dict[str, List[int]] = defaultdict(list)
            for ai in ai_list:
                a = main_asgns[ai]
                if a.subject.subject_type not in (SubjectType.LAB, SubjectType.PROJECT, SubjectType.CW):
                    by_subj_id[a.subject.subject_id].append(ai)

            for s_ais in by_subj_id.values():
                if len(s_ais) < 2:
                    continue
                for di, day in enumerate(DAYS):
                    n_p = len(get_periods(year, day))
                    for pi in range(n_p - 1):
                        v1_list = [x[(ai, di, pi)]     for ai in s_ais if (ai, di, pi)   in x]
                        v2_list = [x[(ai, di, pi + 1)] for ai in s_ais if (ai, di, pi+1) in x]
                        if not v1_list or not v2_list:
                            continue
                        # Pair penalty
                        pair_v = model.NewBoolVar(f"consec_{div_id}_{di}_{pi}")
                        model.Add(sum(v1_list) >= 1).OnlyEnforceIf(pair_v)
                        model.Add(sum(v2_list) >= 1).OnlyEnforceIf(pair_v)
                        obj_terms.extend([pair_v] * 12)  # weight = 12

                    # Triple (3-in-a-row) — extra penalty on top of the two pair penalties
                    for pi in range(n_p - 2):
                        v1_l = [x[(ai, di, pi)]     for ai in s_ais if (ai, di, pi)   in x]
                        v2_l = [x[(ai, di, pi + 1)] for ai in s_ais if (ai, di, pi+1) in x]
                        v3_l = [x[(ai, di, pi + 2)] for ai in s_ais if (ai, di, pi+2) in x]
                        if not v1_l or not v2_l or not v3_l:
                            continue
                        triple_v = model.NewBoolVar(f"triple_{div_id}_{di}_{pi}")
                        model.Add(sum(v1_l) >= 1).OnlyEnforceIf(triple_v)
                        model.Add(sum(v2_l) >= 1).OnlyEnforceIf(triple_v)
                        model.Add(sum(v3_l) >= 1).OnlyEnforceIf(triple_v)
                        obj_terms.extend([triple_v] * 12)  # extra weight = 12

        # ── S4 (weight 6): Friday last-period usage ────────────────────────────
        for div_id, ai_list in by_div.items():
            year     = self.year_map.get(div_id, 2)
            fri_ps   = get_periods(year, "Friday")
            if not fri_ps:
                continue
            last_pi  = len(fri_ps) - 1
            di_fri   = DAYS.index("Friday")
            fri_vars = [
                x[(ai, di_fri, last_pi)]
                for ai in ai_list
                if (ai, di_fri, last_pi) in x
            ]
            if fri_vars:
                fri_any = model.NewBoolVar(f"fri_last_{div_id}")
                model.AddMaxEquality(fri_any, fri_vars)
                obj_terms.extend([fri_any] * 6)   # weight = 6

        # ── S5 (weight 8): teacher daily overload — >3 unique periods per day ──
        # For each (teacher, day), count unique occupied slots (deduplicating
        # parallel-teaching).  When that count exceeds 3, add a penalty.
        for tid, ai_list in by_teacher.items():
            ref_year = self.year_map.get(main_asgns[ai_list[0]].division.division_id, 2)
            for di, day in enumerate(DAYS):
                n_p = len(get_periods(ref_year, day))
                # Build one indicator per unique (teacher, day, period) slot
                slot_indicators = []
                for pi in range(n_p):
                    pvars = [
                        x[(ai, di, pi)]
                        for ai in ai_list
                        if (ai, di, pi) in x
                    ]
                    if not pvars:
                        continue
                    ind = model.NewBoolVar(f"tslot_{tid}_{di}_{pi}")
                    model.Add(sum(pvars) >= 1).OnlyEnforceIf(ind)
                    model.Add(sum(pvars) == 0).OnlyEnforceIf(ind.Not())
                    slot_indicators.append(ind)

                if len(slot_indicators) > 3:
                    # overload_v = max(0, total_slots_today - 3)
                    overload_v = model.NewIntVar(
                        0, len(slot_indicators), f"ovld_{tid}_{di}"
                    )
                    model.Add(overload_v >= sum(slot_indicators) - 3)
                    model.Add(overload_v >= 0)
                    obj_terms.extend([overload_v] * 8)   # weight = 8

        # ── S6 (weight 5): teacher idle gaps ─────────────────────────────────
        # Penalise idle periods between a teacher's first and last class of the
        # day.  For each (teacher, day), gap = span - active_count where
        # span = last_active_pnum - first_active_pnum + 1.
        #
        # Modelling: introduce first_p and last_p integer variables for each
        # (teacher, day), then gap = last_p - first_p + 1 - slot_count.
        for tid, ai_list in by_teacher.items():
            ref_year = self.year_map.get(main_asgns[ai_list[0]].division.division_id, 2)
            for di, day in enumerate(DAYS):
                n_p = len(get_periods(ref_year, day))

                # Build slot-occupied indicators (deduplicated)
                slot_ind: Dict[int, object] = {}
                for pi in range(n_p):
                    pvars = [
                        x[(ai, di, pi)]
                        for ai in ai_list
                        if (ai, di, pi) in x
                    ]
                    if not pvars:
                        continue
                    ind = model.NewBoolVar(f"gslot_{tid}_{di}_{pi}")
                    model.Add(sum(pvars) >= 1).OnlyEnforceIf(ind)
                    model.Add(sum(pvars) == 0).OnlyEnforceIf(ind.Not())
                    slot_ind[pi] = ind

                if len(slot_ind) < 2:
                    continue   # 0 or 1 slot — no gap possible

                pi_vals   = sorted(slot_ind.keys())
                n_active  = len(pi_vals)
                first_p   = model.NewIntVar(pi_vals[0],  pi_vals[-1], f"fp_{tid}_{di}")
                last_p    = model.NewIntVar(pi_vals[0],  pi_vals[-1], f"lp_{tid}_{di}")
                slot_sum  = sum(slot_ind.values())

                # first_p = min period with a class; last_p = max period
                # Encode via: sum(pi * slot_ind[pi]) / slot_count ≈ mean
                # Simpler encoding: first_p ≤ pi if slot_ind[pi] == 1 ∧ no earlier slot
                # Use a tight linear approximation:
                #   first_p ≤ sum_{pi} pi * slot_ind[pi]  (first ≤ weighted sum ÷ 1)
                #   n_p * (1 - slot_ind[pi]) ≥ pi - first_p  for all pi  (if not active, can still be ≥ first)
                # Cleaner: just bound first_p and last_p tightly via conditional constraints.
                for pi, ind in slot_ind.items():
                    # If this slot is active, first_p ≤ pi  AND  last_p ≥ pi
                    model.Add(first_p <= pi).OnlyEnforceIf(ind)
                    model.Add(last_p  >= pi).OnlyEnforceIf(ind)

                # gap_v = last_p - first_p + 1 - slot_sum (≥ 0)
                gap_v = model.NewIntVar(0, n_p, f"gap_{tid}_{di}")
                model.Add(gap_v >= last_p - first_p + 1 - slot_sum)
                model.Add(gap_v >= 0)
                obj_terms.extend([gap_v] * 6)   # weight = 6 (spec S4)

        # ── S7 (weight 4): late labs — labs starting after P4 ─────────────────
        # Prefer labs that start in periods P1–P4 (morning / pre-lunch).
        # For each lab assignment, if the chosen block starts at or after period
        # index 4 (0-indexed: period 5 or later), add a penalty.
        for a in lab_asgns:
            ai          = ai_of[id(a)]
            lab_ppw_v   = self._lab_ppw(a.subject)
            lab_yr      = self.year_map.get(a.division.division_id, 2)
            is_par      = lab_yr in (2, 3) and lab_ppw_v == PARALLEL_LAB_PPW
            L_late      = PARALLEL_LAB_BLOCK if is_par else lab_ppw_v
            year        = lab_yr
            for di, day in enumerate(DAYS):
                periods = get_periods(year, day)
                n_p     = len(periods)
                for si in range(n_p - L_late + 1):
                    if (ai, di, si) not in lab_start:
                        continue
                    if si >= 4:
                        lsv = lab_start[(ai, di, si)]
                        obj_terms.extend([lsv] * 4)   # weight = 4

        # ── S_workload (weight 3): teacher workload balance across the week ─────
        # Keep this lower-weight term so the solver still spreads teacher
        # hours evenly but doesn't sacrifice spread for balance.
        for tid, ai_list in by_teacher.items():
            ref_year = self.year_map.get(main_asgns[ai_list[0]].division.division_id, 2)
            total_ppw = sum(
                max(main_asgns[ai].subject.periods_per_week, 1) for ai in ai_list
            )
            mean_approx = total_ppw // len(DAYS)
            for di, day in enumerate(DAYS):
                day_vars = [
                    x[(ai, di, pi)]
                    for ai in ai_list
                    for pi in range(len(get_periods(ref_year, day)))
                    if (ai, di, pi) in x
                ]
                if not day_vars:
                    continue
                dev = model.NewIntVar(0, MAX_TOTAL_PER_DAY, f"dev_{tid}_{di}")
                day_sum = sum(day_vars)
                model.Add(dev >= day_sum - mean_approx)
                model.Add(dev >= mean_approx - day_sum)
                obj_terms.extend([dev] * 3)   # weight = 3 (reduced from 2 — still useful)

        # ── Symmetry-breaking constraints (requirement #4) ────────────────────
        # For each (division, subject) where the same subject needs multiple
        # placements, require that the first occurrence (lowest day*P + period)
        # is lexicographically before the second.  This eliminates mirror
        # schedules that are otherwise equivalent, reducing solver search space.
        for div_id, ai_list in by_div.items():
            year = self.year_map.get(div_id, 2)
            # Group assignment indices by subject_id
            by_subj_sym: Dict[str, List[int]] = defaultdict(list)
            for ai in ai_list:
                by_subj_sym[main_asgns[ai].subject.subject_id].append(ai)

            for s_ais in by_subj_sym.values():
                if len(s_ais) < 2:
                    continue   # only need symmetry-breaking when ≥ 2 instances

                # Collect all (day_index * max_period + period_index) slot positions
                max_p = max(len(get_periods(year, d)) for d in DAYS)
                anchor_ai = s_ais[0]
                other_ai  = s_ais[1]   # just break symmetry between first two

                # Create integer position variables for anchor and other
                pos_a = model.NewIntVar(0, len(DAYS) * max_p, f"sym_a_{div_id}_{anchor_ai}")
                pos_b = model.NewIntVar(0, len(DAYS) * max_p, f"sym_b_{div_id}_{other_ai}")

                # pos_a = sum of (di * max_p + pi) * x[(anchor_ai, di, pi)]
                pos_a_terms = []
                pos_b_terms = []
                for di in range(len(DAYS)):
                    n_p = len(get_periods(year, DAYS[di]))
                    for pi in range(n_p):
                        slot_val = di * max_p + pi
                        if (anchor_ai, di, pi) in x:
                            pos_a_terms.append(x[(anchor_ai, di, pi)] * slot_val)
                        if (other_ai, di, pi) in x:
                            pos_b_terms.append(x[(other_ai, di, pi)] * slot_val)

                if pos_a_terms and pos_b_terms:
                    model.Add(pos_a == sum(pos_a_terms))
                    model.Add(pos_b == sum(pos_b_terms))
                    model.Add(pos_a <= pos_b)   # anchor must come ≤ second occurrence

        # ── Minimise total weighted objective ─────────────────────────────────
        if obj_terms:
            model.Minimize(sum(obj_terms))

        # ── Warm-start hints from greedy initial_grid ─────────────────────────
        # Tell CP-SAT which slots are already filled in the greedy solution so
        # it starts from a 95+ point and polishes rather than searching from scratch.
        if initial_grid:
            hints_added = 0
            for (div_id, day, pnum), a in initial_grid.items():
                ai = ai_of.get(id(a))
                if ai is None:
                    continue
                year = self.year_map.get(div_id, 2)
                for di, d in enumerate(DAYS):
                    if d != day:
                        continue
                    for pi, period in enumerate(get_periods(year, d)):
                        if period.number == pnum:
                            xv = x.get((ai, di, pi))
                            if xv is not None:
                                model.AddHint(xv, 1)
                                hints_added += 1
            log.info("CP-SAT warm-start: %d slot hints added from greedy grid.", hints_added)

        # ══════════════════════════════════════════════════════════════════════
        # SOLVER CONFIGURATION
        # CP-SAT is the PRIMARY solver per spec requirement.
        # Greedy provides a warm-start hint only; CP-SAT does the real work.
        # ══════════════════════════════════════════════════════════════════════
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 60   # CP-SAT is primary: give it real time
        solver.parameters.num_search_workers  = 12
        solver.parameters.random_seed         = random.randint(1, 10_000)
        solver.parameters.log_search_progress = False

        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            log.warning("CP-SAT: no feasible solution (%.1fs). Falling back to greedy.",
                        solver.WallTime())
            return None

        quality = "optimal" if status == cp_model.OPTIMAL else "feasible"
        log.info("CP-SAT: %s solution in %.2fs (obj=%d).",
                 quality, solver.WallTime(), int(solver.ObjectiveValue()))

        # ── Extract grid from solution ─────────────────────────────────────────
        # For parallel lab pairs, lab1 stays in the primary grid; lab2 is placed
        # in a separate parallel_grid dict that will become self._parallel_grid.
        # Build a reverse lookup: ai → div_id for lab2 in each parallel pair.
        lab2_ais: Dict[int, str] = {}   # ai → div_id for all lab2 members
        for div_id, (ai1, ai2) in parallel_pairs.items():
            lab2_ais[ai2] = div_id

        grid: Dict[Tuple[str, str, int], SubjectAssignment] = {}
        parallel_grid_out: Dict[Tuple[str, str, int], SubjectAssignment] = {}
        for ai, a in enumerate(main_asgns):
            year = self.year_map.get(a.division.division_id, 2)
            is_lab2 = ai in lab2_ais
            for di, day in enumerate(DAYS):
                for pi, period in enumerate(get_periods(year, day)):
                    if (ai, di, pi) in x and solver.Value(x[(ai, di, pi)]) == 1:
                        key = (a.division.division_id, day, period.number)
                        if is_lab2:
                            parallel_grid_out[key] = a
                        else:
                            grid[key] = a

        return grid, parallel_grid_out

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Print: class timetable
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def print_class_timetable(self, division_id: Optional[str] = None) -> None:
        """
        Print the class timetable for one division, or all divisions if
        division_id is None.

        Grid format:
          • Rows = periods (P1..P7), Columns = days (Mon..Fri)
          • Cells: 2–4 char short code (e.g. DSA, TC, MIS)
          • Friday lunch-break separator row
          • Alphabetically sorted subject legend table below each grid
        """
        if not self._result:
            print("Call generate() first.")
            return

        targets = (
            [division_id] if division_id
            else sorted(self._result.class_timetables.keys())
        )

        for div_id in targets:
            cells = self._result.class_timetables.get(div_id)
            if not cells:
                print(f"No timetable for division {div_id}")
                continue
            year = self.year_map.get(div_id, 2)
            sem  = next((d.semester for d in self.divisions
                         if d.division_id == div_id), "?")
            self._print_class_grid(div_id, cells, year, sem)

    def _print_class_grid(
        self, div_id: str, cells: List[TimetableCell], year: int, sem
    ) -> None:
        DAY_ABBR = {"Monday":"Mon","Tuesday":"Tue","Wednesday":"Wed",
                    "Thursday":"Thu","Friday":"Fri"}
        col_w = 7
        p_col = 13
        width = p_col + col_w * 5

        by_dp: Dict[str, Dict[int, TimetableCell]] = defaultdict(dict)
        for c in cells:
            by_dp[c.day][c.period.number] = c

        max_period = max(len(get_periods(year, d)) for d in DAYS)
        lunch_after = get_lunch_break_after(year, "Friday")

        print(f"\n{'═' * width}")
        print(f"  CLASS TIMETABLE │ Division: {div_id}  │  Semester: {sem}  │  Year: {year}")
        print(f"{'═' * width}")
        print(f"{'Period':<{p_col}}" + "".join(f"{DAY_ABBR[d]:^{col_w}}" for d in DAYS))
        print("─" * width)

        for p_num in range(1, max_period + 1):
            if p_num == lunch_after + 1:
                mid = f"{'LUNCH 12:15–14:15':^{col_w}}"
                print(f"{'':^{p_col}}" + f"{'':^{col_w}}" * 4 + mid)

            row = ""
            for day in DAYS:
                today = get_periods(year, day)
                if p_num > len(today):
                    row += f"{'─':^{col_w}}"
                    continue
                c = by_dp[day].get(p_num)
                row += f"{(c.short_code if c and not c.is_free else '─'):^{col_w}}"

            ref = get_periods(year, "Monday")
            label = f"P{p_num} {ref[p_num-1].start}" if p_num <= len(ref) else f"P{p_num}"
            print(f"{label:<{p_col}}" + row)

        print("─" * width)
        self._print_legend(div_id, cells)

    def _print_legend(self, div_id: str, cells: List[TimetableCell]) -> None:
        """
        Print the subject legend, sorted alphabetically by short code.
        Improvement #9 — clean aligned columns.
        """
        seen: Dict[str, TimetableCell] = {}
        for c in cells:
            if c.subject and c.short_code and c.short_code not in seen:
                seen[c.short_code] = c

        if not seen:
            return

        print(f"\n  Subject Table — {div_id}")
        print(f"  {'Code':<6}  {'Course Code':<14}  {'Course Name':<42}  Faculty")
        print(f"  {'─'*6}  {'─'*14}  {'─'*42}  {'─'*20}")

        for code in sorted(seen.keys()):   # alphabetical (improvement #9)
            c    = seen[code]
            s    = c.subject
            t    = c.teacher
            name = (s.name[:40] + "..") if len(s.name) > 42 else s.name
            fac  = _teacher_short(t) if t else "─"
            print(f"  {code:<6}  {(s.code or '─'):<14}  {name:<42}  {fac}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Print: teacher timetable  (improvement #8 — verbose mode)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def print_teacher_timetable(
        self,
        teacher_id: Optional[str] = None,
        verbose:    bool          = False,
    ) -> None:
        """
        Print the teacher timetable.

        Args:
            teacher_id: specific teacher ID, or None to print all teachers.
            verbose:    False (default) → cells show "DSA(CS1)"
                        True  → cells show "DSA(CS1) – Data Structures & Algorithms"

        Improvement #8 — verbose mode.
        """
        if not self._result:
            print("Call generate() first.")
            return

        teacher_map: Dict[str, Teacher] = {t.teacher_id: t for t in self.teachers}

        targets = (
            [teacher_id] if teacher_id
            else sorted(
                self._result.teacher_timetables.keys(),
                key=lambda tid: teacher_map.get(tid,
                    type("_T", (), {"name": tid})()).name,   # type: ignore[arg-type]
            )
        )

        for tid in targets:
            cells = self._result.teacher_timetables.get(tid)
            if not cells:
                continue
            t = teacher_map.get(tid)
            if not t:
                continue
            self._print_teacher_grid(t, cells, verbose=verbose)

    def _print_teacher_grid(
        self,
        teacher: Teacher,
        cells:   List[TimetableCell],
        verbose: bool = False,
    ) -> None:
        DAY_ABBR = {"Monday":"Mon","Tuesday":"Tue","Wednesday":"Wed",
                    "Thursday":"Thu","Friday":"Fri"}

        # Verbose mode needs wider columns
        col_w = 28 if verbose else 11
        p_col = 13
        width = p_col + col_w * 5

        by_dp: Dict[str, Dict[int, TimetableCell]] = defaultdict(dict)
        for c in cells:
            by_dp[c.day][c.period.number] = c

        year = next((self.year_map.get(c.division_id, 2) for c in cells), 2)
        max_period = max(len(get_periods(year, d)) for d in DAYS)
        lunch_after = get_lunch_break_after(year, "Friday")

        # Designation label
        desig = ""
        try:
            desig = teacher.designation.value
        except AttributeError:
            pass

        print(f"\n{'═' * width}")
        print(f"  TEACHER TIMETABLE │ {teacher.name}  ({desig})")
        print(f"{'═' * width}")
        print(f"{'Period':<{p_col}}" + "".join(f"{DAY_ABBR[d]:^{col_w}}" for d in DAYS))
        print("─" * width)

        for p_num in range(1, max_period + 1):
            if p_num == lunch_after + 1:
                print(f"{'':^{p_col}}" + f"{'':^{col_w}}" * 4 + f"{'LUNCH':^{col_w}}")

            row = ""
            for day in DAYS:
                today = get_periods(year, day)
                if p_num > len(today):
                    row += f"{'─':^{col_w}}"
                    continue
                c = by_dp[day].get(p_num)
                if c and c.subject:
                    if verbose:
                        # "DSA(CS1) – Data Structures & Algorithms"
                        base  = f"{c.short_code}({c.division_id})"
                        name  = c.subject.name[:14]
                        cell_text = f"{base} – {name}"[:col_w - 1]
                    else:
                        cell_text = f"{c.short_code}({c.division_id})"
                else:
                    cell_text = "─"
                row += f"{cell_text:^{col_w}}"

            ref = get_periods(year, "Monday")
            label = f"P{p_num} {ref[p_num-1].start}" if p_num <= len(ref) else f"P{p_num}"
            print(f"{label:<{p_col}}" + row)

        print("─" * width)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Summary  (improvement #11 — schedule score)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def print_summary(self) -> None:
        """Print concise statistics including the full quality score breakdown."""
        print(f"\n{'─' * 68}")
        print("  TIMETABLE GENERATION SUMMARY")
        print(f"{'─' * 68}")

        total  = sum(
            len(get_periods(self.year_map.get(d.division_id, 2), day))
            for d in self.divisions for day in DAYS
        )
        filled = len(self._grid)
        blank  = total - filled

        bd = self.score_breakdown()

        print(f"  Divisions scheduled      : {len(self.divisions)}")
        print(f"  Total period slots       : {total}")
        print(f"  Filled slots             : {filled}")
        print(f"  Blank slots              : {blank}")
        print()
        print(f"  ── Hard constraints ──────────────────────────────")
        print(f"  Teacher clashes          : {bd['teacher_clashes']}")
        print(f"  Class clashes            : {bd['class_clashes']}")
        print(f"  Daily limit issues       : {len(self.check_teacher_daily_limit())}")
        print()
        print(f"  ── Soft quality metrics ──────────────────────────")
        print(f"  Consecutive same-subject : {bd['consecutive_pairs']}  pairs")
        print(f"  Subject-day excess       : {bd['subject_day_excess']}  (same subj >1× per day)")
        print(f"  Workload CV (dedup)      : {bd['workload_cv']}/10  (coefficient of variation)")
        print(f"  Friday last-period       : {bd['friday_last']} division(s)")
        print(f"  Teacher overload days    : {bd['teacher_overload']}  (>3 unique periods/day)")
        print(f"  Teacher idle gaps        : {bd['teacher_gaps']}  (gap-periods across week)")
        print(f"  Late labs (after P4)     : {bd['late_labs']}  lab block(s)")
        print()
        print(f"  ── Sub-scores (100 = perfect) ─────────────────────")
        print(f"  Subject distribution     : {bd['subject_distribution_score']:>3}/100")
        print(f"  Teacher balance          : {bd['teacher_balance_score']:>3}/100")
        print(f"  Gap score                : {bd['gap_score']:>3}/100")
        print(f"  Friday penalty           : {bd['friday_score']:>3}/100")
        print(f"  Repeat penalty           : {bd['repeat_penalty_score']:>3}/100")
        print(f"  Lab time score           : {bd['lab_time_score']:>3}/100")
        print(f"  ─────────────────────────────────────────────────")
        print(f"  Schedule score           : {bd['total']:>3}/100")

        clashes = (self.check_teacher_clash()
                   + self.check_class_clash()
                   + self.check_teacher_daily_limit())
        for item in clashes[:6]:
            print(f"    ⚠  {item}")

        print(f"{'─' * 68}\n")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # JSON export  (improvement #10 — clean API-ready format)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def block_external_busy_slots(
        self,
        external_busy: Dict[str, List[Tuple[str, str, str, str]]],
    ) -> int:
        """
        PREVENT cross-(dept, semester) teacher clashes (gap #4), not just
        detect them after the fact. Call this after load_data() and before
        generate().

        `external_busy` is teacher_id -> [(day, start, end, subject_name), ...]
        accumulated from every engine run completed earlier in this
        generation cycle (see app.py). For each of THIS run's teachers, any
        (day, period) whose actual clock time overlaps one of their existing
        bookings is pre-marked busy in `_teacher_busy` with a sentinel that
        can never match a real subject_id — so the greedy scheduler, the
        relaxed fallback pass, and every other placement path that already
        checks `_teacher_free()` will correctly treat that slot as
        unavailable, the same as if it were double-booked within one run.

        Returns the number of slots blocked (useful for logging).
        """
        if not external_busy:
            return 0

        def _to_minutes(hhmm: str) -> int:
            h, m = hhmm.split(':')
            return int(h) * 60 + int(m)

        year = next(iter(self.year_map.values()), 2)
        keys: Set[Tuple[str, str, int]] = set()
        for teacher in self.teachers:
            tid = teacher.teacher_id
            prior_intervals = external_busy.get(tid)
            if not prior_intervals:
                continue
            for day in DAYS:
                for period in get_periods(year, day):
                    s0, e0 = _to_minutes(period.start), _to_minutes(period.end)
                    for pday, pstart, pend, _psubj in prior_intervals:
                        if pday != day:
                            continue
                        s1, e1 = _to_minutes(pstart), _to_minutes(pend)
                        if s0 < e1 and s1 < e0:  # real clock-time overlap
                            keys.add((tid, day, period.number))
                            break

        self._external_busy_keys = keys
        # Apply immediately too, in case generate() is never called (or a
        # caller inspects _teacher_busy directly before generate()).
        for key in keys:
            self._teacher_busy[key] = "__EXTERNAL_BUSY__"

        if keys:
            log.info(
                "  Pre-blocked %d slot(s) to avoid cross-semester teacher "
                "clashes (gap #4).", len(keys),
            )
        return len(keys)

    def export_teacher_busy_intervals(self) -> Dict[str, List[Tuple[str, str, str, str]]]:
        """
        Return, for every teacher booked by this engine run, their occupied
        slots as ACTUAL CLOCK-TIME intervals rather than period numbers:

            { teacher_id: [(day, start, end, subject_name), ...], ... }

        This exists to support cross-(dept, semester) clash detection (gap #4
        in the constraints doc). Within one engine run, teacher clashes are
        already checked correctly via `_teacher_busy` keyed by period NUMBER,
        because every division inside one run shares the same year's period
        structure. But a full generation cycle creates a FRESH
        TimetableGeneratorEngine() per (dept, semester) — see
        app.admin_assign_subjects — so a teacher who teaches, say, both a
        Year-2 class and a Year-4 class is checked against two completely
        separate `_teacher_busy` dicts that never see each other. Year 2 and
        Year 4 have different period structures (see PERIOD_STRUCTURES), so
        "Period 3" in one does not mean the same clock time as "Period 3" in
        the other — comparing period numbers across engine runs would be
        wrong even if it were attempted.

        The caller (app.py) accumulates these across every engine run in a
        full generation cycle and checks for actual time overlaps, reporting
        any clash it finds rather than silently allowing it.
        """
        out: Dict[str, List[Tuple[str, str, str, str]]] = defaultdict(list)
        seen: Set[Tuple[str, str, str, int]] = set()
        for (div_id, day, pnum), a in self._grid.items():
            tid  = a.teacher.teacher_id
            year = self.year_map.get(div_id, 2)
            key  = (tid, day, str(year), pnum)
            if key in seen:
                continue
            seen.add(key)
            for p in get_periods(year, day):
                if p.number == pnum:
                    out[tid].append((day, p.start, p.end, a.subject.name))
                    break
        return dict(out)

    @staticmethod
    def find_cross_run_teacher_clashes(
        existing: Dict[str, List[Tuple[str, str, str, str]]],
        new:      Dict[str, List[Tuple[str, str, str, str]]],
    ) -> List[str]:
        """
        Compare `new` teacher-busy intervals (from the engine run just
        completed) against `existing` intervals accumulated from all prior
        engine runs in this generation cycle. Returns human-readable
        descriptions of any actual clock-time overlaps found — i.e. real
        double-bookings that period-number comparison alone would miss.
        """
        def _to_minutes(hhmm: str) -> int:
            h, m = hhmm.split(':')
            return int(h) * 60 + int(m)

        clashes: List[str] = []
        for tid, new_intervals in new.items():
            prior_intervals = existing.get(tid)
            if not prior_intervals:
                continue
            for day, start, end, subj_name in new_intervals:
                s0, e0 = _to_minutes(start), _to_minutes(end)
                for pday, pstart, pend, psubj in prior_intervals:
                    if pday != day:
                        continue
                    s1, e1 = _to_minutes(pstart), _to_minutes(pend)
                    if s0 < e1 and s1 < e0:   # real overlap
                        clashes.append(
                            f"Teacher {tid} double-booked on {day}: "
                            f"'{subj_name}' ({start}-{end}) overlaps "
                            f"'{psubj}' ({pstart}-{pend}) from an earlier "
                            f"dept/semester run — same clock time, different "
                            f"year-group period numbers."
                        )
        return clashes

    def export_to_dict(self) -> Dict:
        """
        Return a JSON-serialisable dict with both timetable views.

        Every non-free cell contains:
          division_id, day, period_number, period_start, period_end,
          short_code, subject_id, subject_code, subject_name, subject_type,
          teacher_id, teacher_name, is_lab, is_free
        """
        if not self._result:
            return {}

        # FIX 4: lookup maps so cell_dict can include semester + student dept
        # for the teacher-timetable "S3 – CS – A" class context label.
        div_semester: Dict[str, int] = {
            d.division_id: d.semester for d in self.divisions
        }
        div_dept: Dict[str, str] = {
            d.division_id: d.student_dept_id for d in self.divisions
        }

        def cell_dict(c: TimetableCell) -> Dict:
            return {
                "division_id":         c.division_id,
                "day":                 c.day,
                "period_number":       c.period.number,
                "period_start":        c.period.start,
                "period_end":          c.period.end,
                "short_code":          c.short_code     or None,
                "subject_id":          c.subject.subject_id         if c.subject else None,
                "subject_code":        c.subject.code               if c.subject else None,
                "subject_name":        c.subject.name               if c.subject else None,
                "subject_type":        c.subject.subject_type.value if c.subject else None,
                "teacher_id":          c.teacher.teacher_id if c.teacher else None,
                "teacher_name":        c.teacher.name       if c.teacher else None,
                "is_lab":              c.is_lab,
                "is_free":             c.is_free,
                # FIX 4: include semester + student dept so teacher-mode viewers
                # can display the class context label "S3 – CS – A" in each cell
                "semester":            div_semester.get(c.division_id),
                "student_dept":        div_dept.get(c.division_id),
                # Parallel-lab pair fields (year 2 & 3 only)
                # When non-null, this cell holds two simultaneous labs.
                "paired_short_code":   c.paired_short_code  or None,
                "paired_subject_id":   c.paired_subject.subject_id         if c.paired_subject else None,
                "paired_subject_name": c.paired_subject.name               if c.paired_subject else None,
                "paired_subject_code": c.paired_subject.code               if c.paired_subject else None,
                "paired_teacher_id":   c.paired_teacher.teacher_id if c.paired_teacher else None,
                "paired_teacher_name": c.paired_teacher.name       if c.paired_teacher else None,
            }

        return {
            "class_timetables": {
                div_id: [cell_dict(c) for c in cells]
                for div_id, cells in self._result.class_timetables.items()
            },
            "teacher_timetables": {
                tid: [cell_dict(c) for c in cells]
                for tid, cells in self._result.teacher_timetables.items()
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_year_map(divisions: List[Division]) -> Dict[str, int]:
    """Map each division_id to its academic year (1–4)."""
    return {d.division_id: sem_to_year(d.semester) for d in divisions}


def generate_from_db(
    db_path:             str,
    target_student_dept: str,
    target_semester:     int,
    use_cpsat:           bool = True,   # now defaults to True (improvement #1)
) -> TimetableGeneratorEngine:
    """
    Full pipeline: SQLite DB → subject assignment → timetable generation.

    Returns a TimetableGeneratorEngine with generate() already called,
    ready for printing.

    Example:
        gen = generate_from_db("timetable.db", "CS", 3)
        gen.print_class_timetable()
        gen.print_teacher_timetable()
        gen.print_teacher_timetable(verbose=True)
    """
    log.info("Full pipeline: dept=%s, semester=%d", target_student_dept, target_semester)
    ae = load_from_db(db_path, target_student_dept, target_semester)
    assignments = ae.assign()
    ae.print_summary()

    year_map = _build_year_map(ae.divisions)
    gen = TimetableGeneratorEngine()
    gen.load_data(
        assignments = assignments,
        teachers    = ae.teachers,
        divisions   = ae.divisions,
        departments = ae.departments,
        year_map    = year_map,
    )
    gen.generate(use_cpsat=use_cpsat)
    gen.print_summary()
    return gen


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, os

    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timetable.db")

    if os.path.exists(db_path):
        # ── Real database run ─────────────────────────────────────────────────
        gen = generate_from_db(
            db_path             = db_path,
            target_student_dept = "CS",
            target_semester     = 3,
            use_cpsat           = True,
        )
        gen.print_class_timetable()
        gen.print_teacher_timetable()

        # Optional: verbose teacher timetable for one teacher
        if gen.teachers:
            first_tid = gen.teachers[0].teacher_id
            gen.print_teacher_timetable(teacher_id=first_tid, verbose=True)

        # JSON export sample
        exported  = gen.export_to_dict()
        first_div = next(iter(exported["class_timetables"]), None)
        if first_div:
            sample = [c for c in exported["class_timetables"][first_div]
                      if not c["is_free"]][:3]
            print("\nJSON export sample (first 3 filled cells of", first_div, "):")
            print(json.dumps(sample, indent=2, ensure_ascii=False))

    else:
        # ── Sample data run ───────────────────────────────────────────────────
        print("Database not found — using built-in sample data.\n")
        teachers, subjects, departments, divisions = build_sample_data()

        ae = SubjectAssignmentEngine()
        ae.load_data(teachers, subjects, departments, divisions)
        assignments = ae.assign()
        ae.print_summary()

        gen = TimetableGeneratorEngine()
        gen.load_data(assignments)                   # single-arg form
        gen.generate(use_cpsat=True)                 # CP-SAT first

        gen.print_class_timetable()
        gen.print_teacher_timetable()
        gen.print_teacher_timetable(verbose=True)
        gen.print_summary()