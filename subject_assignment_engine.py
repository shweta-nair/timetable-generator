"""
subject_assignment_engine.py  — CORRECTED v2
─────────────────────────────────────────────────────────────────────────────
College Timetable Generation System
Engine 1: Subject Assignment Engine

BUGS FIXED IN THIS VERSION
───────────────────────────────────────────────────────────────────────────
FIX 1 — Non-AP multi-division load double-counting  (_assign_regular)
    The normal (non-AP) assignment path incremented a teacher's theory load
    once PER DIVISION. If the same teacher was picked for CS1 and CS2 of the
    same subject, their load read 2 instead of 1, violating Rule 7 ("counts
    as one subject load").
    → Added `load_incremented_for` set; load is incremented only once per
      unique (teacher, subject) pair regardless of how many divisions they cover.

FIX 2 — Lab companion teacher gets lab load incremented per division
    (_assign_lab_with_pairing)
    When a theory teacher was assigned across all divisions via the AP
    multi-division rule, `_get_lab_companion_teacher` returned the SAME
    teacher for every division's lab. Their lab load was then incremented N
    times, silently violating the 1-lab-per-teacher limit.
    → Now checks `can_take_lab()` on the companion teacher before using them.
      If they are already at lab capacity, the code falls through to
      `_pick_teacher` to find an alternative lab instructor.
    → Added `lab_load_incremented_for` set (same Rule 7 fix for labs).

FIX 3 — `_pick_teacher` returns None on department mismatch  (_pick_teacher)
    The final department safety check returned None immediately when the
    top-scoring candidate had the wrong department. This silently abandoned
    the entire subject even when other valid candidates existed further down
    the sorted list.
    → Changed `return None` → `continue` so the loop moves on to the next
      best candidate instead of giving up.

FIX 4 — Theory-lab companion linking threshold too strict  (load_from_db)
    The SequenceMatcher threshold of 0.7 missed common pairs such as
    "Data Structures and Algorithms" / "Data Structures Lab" (score ≈ 0.69)
    and "Digital Electronics and Logic Design" / "Digital Lab" (score ≈ 0.43).
    → Threshold lowered to 0.55 AND added keyword-containment check:
      if the lab base name (lab name minus "Lab") is found inside the theory
      name, the pair is linked regardless of sequence similarity.

FIX 5 — Elective multi-division load double-counting  (_assign_elective_group)
    Same root cause as Fix 1: theory load for electives was incremented once
    per division, so teaching an elective to two divisions counted as 2 loads.
    → Added `load_incremented_for` set inside the division loop.

FIX 6 — Incorrect `can_take_theory()` guard in _assign_lab_assistant
    The assistant filter used `can_take_theory()` (checks theory load < 2),
    which is semantically wrong — the spec says assistant role does NOT count
    against any load. A junior teacher with 2 theory subjects was excluded
    from being an assistant even though they were eligible.
    → Guard removed. Assistants are now filtered only by designation
      (Assistant Professor or Lecturer) and department.
    → Added assistant-usage counter so duties are spread across junior staff
      rather than always falling on the least-loaded single teacher.

CONSTRAINT RULES ENFORCED
──────────────────────────────────────────────────────────────────────────────
1. TEACHER PREFERENCES
   • 3 preferences for odd semesters (1,3,5,7)
   • 3 preferences for even semesters (2,4,6,8)
   • Preferences loaded based on semester parity (odd vs even)

2. ASSIGNMENT PRIORITY (4 levels):
   Level 1: PREFERENCE MATCH
     - Teacher's ranked preference for this subject
     - Rank 1 > Rank 2 > Rank 3
     - Seniority is the tiebreaker when ranks are equal

   Level 2: SENIORITY
     - Professor > Associate Prof > Assistant Prof > Lecturer
     - Tiebreaker when multiple teachers have the same preference rank

   Level 3: AREA OF INTEREST
     - Subject name/keywords match teacher's specializations
     - Used when no preference match exists

   Level 4: REMAINING SUBJECTS (REVERSE SENIORITY)
     - Unassigned subjects go to junior teachers first
     - Lecturer > Assistant Prof > Associate Prof > Professor

3. TEACHER SUBJECT LOAD
   • Maximum 2 CORE THEORY subjects per teacher per semester
   • Maximum 1 lab OR 1 project OR 1 seminar per teacher (combined slot)
     → A teacher may hold at most 3 subjects total:
          2 Core Theory  +  1 (Lab OR Project OR Seminar)
   • (ELECTIVE counts as theory; the combined Lab/Project/Seminar slot is
     tracked by the lab_or_extra_assigned flag on Teacher)
   • Same subject taught across multiple divisions = ONE load unit (Rule 7/8)

3b. MAXIMUM WEEKLY TEACHING LOAD
   • HODs:                     max 16 periods/week
   • Senior/Associate teachers: max 17 periods/week
     (Principal, Deputy Dean, Associate Professor)
   • Regular teachers:          max 19 periods/week
     (Assistant Professor, Professor, Lecturer, and dummy teachers)
   → Enforced via _max_periods_for_teacher() and Teacher.weekly_load_ok()

4. THEORY-LAB PAIRING
   • If a subject has a companion lab, the same teacher is preferred for both
   • If the companion teacher is already at lab capacity, a different lab
     instructor is selected automatically

5. LAB STAFF REQUIREMENT
   • Every lab needs TWO staff members per division:
     - Main teacher (handles theory + lab)
     - Assistant teacher (must be junior: Assistant Prof or Lecturer only)
   • Assistant does NOT count against their load

6. ASSOCIATE PROFESSOR MULTI-DIVISION RULE
   • Associate Professors may teach same subject in multiple divisions
   • Used when AP wants the subject (preference or specialization match)
   • Counts as ONE subject load (load incremented once)

7. DEPARTMENT MATCHING
   • courses.dept_id determines which department's teachers can teach
   • Final safety check in _pick_teacher prevents cross-dept assignments
     (skips mis-matched candidates instead of aborting the whole pick)

8. ELECTIVE PARALLEL GROUPING
   • Electives with same elective_group are processed together
   • Assigned to different teachers when possible
   • Will be scheduled in parallel time slots by timetable generator
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import re
import time as _time
from datetime import datetime as _dt
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Per-designation weekly teaching load caps
# ─────────────────────────────────────────────────────────────────────────────
# HODs:                              max 16 periods/week
# Senior/Associate/Priority teachers: max 17 periods/week
#   (Principal, Deputy Dean, Associate Professor)
# Regular teachers:                  max 19 periods/week
#   (Assistant Professor, Professor, Lecturer, and dummy/placeholder teachers)
#
# MAX_WEEKLY_PERIODS is kept as the highest possible cap (used as a sentinel
# for dummy teachers and anywhere a single upper bound is needed).
MAX_WEEKLY_PERIODS = 19  # highest real-teacher cap; also used for dummies

# Designations that belong to the "senior" tier (17 periods/week)
_SENIOR_DESIGNATIONS: frozenset = frozenset({
    'Designation.PRINCIPAL',
    'Designation.DEPUTY_DEAN',
    'Designation.ASSOCIATE_PROF',
})


def _max_periods_for_teacher(teacher: "Teacher") -> int:
    """
    Return the weekly period cap for a teacher based on their designation.

      HOD (Head of Department) : 16 periods/week
      Senior tier              : 17 periods/week
        (Principal, Deputy Dean, Associate Professor)
      Regular tier             : 19 periods/week
        (Assistant Professor, Professor, Lecturer,
         or any dummy/placeholder teacher)
    """
    # Dummy teachers get the regular cap
    if getattr(teacher, 'is_dummy', False):
        return 19
    desig = getattr(teacher, 'designation', None)
    if desig is None:
        return 19
    # HOD
    if desig.value == 'HOD':
        return 16
    # Senior tier: Principal, Deputy Dean, Associate Professor
    if desig.value in ('Principal', 'Deputy Dean', 'Associate Professor'):
        return 17
    # Regular: Assistant Professor, Professor, Lecturer
    return 19


# ─────────────────────────────────────────────────────────────────────────────
# Additional-responsibility subject helpers
# ─────────────────────────────────────────────────────────────────────────────
# These subjects are EXCLUDED from the 2-theory + 1-lab main load limit.
# They are assigned AFTER the main subject allocation is complete.

_ADDITIONAL_KEYWORDS: Tuple[str, ...] = (
    'seminar', 'project phase', 'mini project', 'miniproject', 'ccw', 'course work', 'y2p',
)


def _is_additional_responsibility(subj: "Subject") -> bool:
    """
    Return True for subjects that must NOT count against the main
    2-theory + 1-lab load (Seminar, Project Phase 1/2, Mini Project, CCW, Y2P).
    """
    if getattr(subj, 'subject_type', None) == SubjectType.CW:
        return True
    name_lower = subj.name.lower()
    return any(kw in name_lower for kw in _ADDITIONAL_KEYWORDS)


def _multi_teacher_count(subj: "Subject") -> int:
    """
    Return the required number of teachers for a multi-supervised subject.
      Project Phase 1 / Phase 2  →  5  (max 5 teachers per phase)
      Seminar                     →  5  (1 main + up to 4 co-supervisors / assistants)
      Mini Project                →  3
      CCW                         →  2
      anything else               →  1

    NOTE: For Seminar, the first teacher in the returned list is the 'main'
    teacher; the remaining 4 are 'co_supervisor' (assistant) teachers.
    Not all co-supervisors need to be present at every seminar period —
    at least one assigned teacher must be present (same rule as Project Phase).
    """
    name_lower = subj.name.lower()
    if 'project phase' in name_lower:
        return 5
    if 'seminar' in name_lower:
        return 5   # 1 main + up to 4 co-supervisors
    if 'mini project' in name_lower or 'miniproject' in name_lower:
        return 3
    if getattr(subj, 'subject_type', None) == SubjectType.CW or 'ccw' in name_lower or 'course work' in name_lower:
        return 2
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Cross-engine additional-responsibility flag registry
# ─────────────────────────────────────────────────────────────────────────────
# FIX (Problems 2, 3, 7): admin_assign_subjects in app.py iterates over
# multiple (dept, sem) pairs, creating a FRESH engine for each.  The
# global_teacher_loads dict preserves theory/lab counts, but the boolean
# additional-responsibility flags on Teacher objects are always False in a
# freshly-built engine.  Without cross-engine persistence a teacher can
# receive:  Seminar(S7 AI) + Seminar(S7 DS) across two separate engines.
#
# This module-level dict accumulates flags across engine instances during a
# single generation run (preserve_loads=True calls).  The flags auto-expire
# after _CROSS_ENGINE_EXPIRE_SECONDS so stale state from a previous run on a
# long-lived server does not affect a new generation.
#
# All flag names are listed here so they can be looped over uniformly:
_ALL_ADDITIONAL_FLAGS: Tuple[str, ...] = (
    'project_phase_assigned',
    'seminar_assigned',
    'miniproject_assigned',
    'ccw_assigned',           # was missing, caused duplicate CCW assignments
    'y2p_assigned',           # was missing, caused duplicate Y2P assignments
    'lab_or_extra_assigned',  # NEW — combined Lab/Project/Seminar slot flag
)

# {teacher_id: set_of_flag_names_that_are_True}
_cross_engine_additional_flags: Dict[str, Set[str]] = {}
# Cross-engine NUMERIC count of additional duties each teacher has received
# so far across all engine calls in a single generation run.  Persisted the
# same way as _cross_engine_additional_flags so that when the second (dept2,
# sem) engine runs it can seed extra_load with already-accumulated counts and
# therefore not pile additional responsibilities on the same teacher again.
# Key: teacher_id (str); Value: cumulative additional-duty count (int).
_cross_engine_extra_load: Dict[str, int] = {}
# Cross-engine registry of subject IDs already assigned (as THEORY) to each
# non-BSH teacher.  Used to prevent the same teacher from teaching the same
# subject to more than one division.  Persisted across engine calls so that
# the second (dept2, sem) engine does not re-assign subjects that were locked
# in by a previous engine call.
# Key: teacher_id (str); Value: set of subject_ids already assigned as theory.
_cross_engine_teacher_subject_ids: Dict[str, Set[str]] = {}
# Monotonic timestamp of the last time cross-engine flags were seeded
# (either by reset or by a preserve_loads=True call).
_cross_engine_last_ts: float = 0.0
# Auto-expire after 2 hours of inactivity (guards against stale server state)
_CROSS_ENGINE_EXPIRE_SECONDS: float = 7_200.0


def reset_cross_engine_flags() -> None:
    """
    Clear the module-level cross-engine additional-responsibility flag registry.

    Called automatically when assign(preserve_loads=False) runs (standalone /
    test mode).  Can also be called explicitly before each full generation run
    to guarantee a clean slate.
    """
    global _cross_engine_additional_flags, _cross_engine_extra_load, \
           _cross_engine_teacher_subject_ids, _cross_engine_last_ts
    _cross_engine_additional_flags = {}
    _cross_engine_extra_load = {}
    _cross_engine_teacher_subject_ids = {}
    _cross_engine_last_ts = _time.monotonic()


# ─────────────────────────────────────────────────────────────────────────────
# Domain Enums & Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class Designation(Enum):
    PRINCIPAL      = "Principal"           # seniority 0 — most senior
    DEPUTY_DEAN    = "Deputy Dean"         # seniority 1
    HOD            = "HOD"                 # seniority 2 (Head of Department)
    ASSOCIATE_PROF = "Associate Professor" # seniority 3
    ASSISTANT_PROF = "Assistant Professor" # seniority 4
    PROFESSOR      = "Professor"           # seniority 5 (plain Prof below Assistant per spec)
    LECTURER       = "Lecturer"            # seniority 6 — least senior

# Lower number = more senior.
# Rule: Principal > Deputy Dean > HOD > Associate Prof > Assistant Prof > Professor > Lecturer
DESIGNATION_SENIORITY: Dict[Designation, int] = {
    Designation.PRINCIPAL:      0,
    Designation.DEPUTY_DEAN:    1,
    Designation.HOD:            2,
    Designation.ASSOCIATE_PROF: 3,
    Designation.ASSISTANT_PROF: 4,
    Designation.PROFESSOR:      5,
    Designation.LECTURER:       6,
}

# Designations that are INELIGIBLE to be lab assistants (too senior).
# Eligible: ASSISTANT_PROF, PROFESSOR, LECTURER
_LAB_ASSISTANT_INELIGIBLE: frozenset = frozenset({
    Designation.PRINCIPAL,
    Designation.DEPUTY_DEAN,
    Designation.HOD,
    Designation.ASSOCIATE_PROF,
})

class SubjectType(Enum):
    THEORY   = "theory"
    LAB      = "lab"
    ELECTIVE = "elective"
    PROJECT  = "project"
    CW       = "cw"          # Course Work — must be scheduled in 2-consecutive-period blocks


@dataclass
class Department:
    dept_id: str
    name: str


@dataclass
class Division:
    division_id:     str
    student_dept_id: str
    semester:        int


@dataclass
class Subject:
    subject_id:         str
    name:               str
    subject_type:       SubjectType
    student_dept_id:    str
    teacher_dept_id:    str
    semester:           int
    periods_per_week:   int = 3
    companion_theory_id: Optional[str] = None
    companion_lab_id:    Optional[str] = None
    elective_group:      Optional[str] = None
    code:               str = ""
    applicable_division_tokens: List[str] = field(default_factory=list)
    credit:             int = 0           # from CREDIT column in courses table
    # Explicit parallel-group tag. When set, the timetable engine allows the
    # same teacher to occupy the same slot across divisions in this group.
    parallel_group_id:  Optional[str] = None
    # Number of separate lab sessions derived from LTPR P-value.
    # P=3 → 1 session; P=11 → 4 sessions (3,3,3,2)
    lab_sessions:       int = 1


@dataclass
class Teacher:
    teacher_id:       str
    name:             str
    dept_id:          str
    designation:      Designation
    seniority_level:  int = 999
    priority_subjects: List[Tuple[str, int]] = field(default_factory=list)
    specializations:  List[str] = field(default_factory=list)
    assigned_load:    Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Tracks actual periods used this semester (LTPR-based).
    # Must stay <= _max_periods_for_teacher(self).
    weekly_periods_used: int = 0
    # True for BSH (Basic Sciences & Humanities) teachers — service dept.
    is_bsh:           bool = False
    # True for dummy (placeholder) teachers created for unassigned subjects.
    is_dummy:         bool = False
    # Teacher code for timetable display
    code:             str = ""

    # ── Additional-responsibility tracking (one-per-teacher limits) ──────────
    project_phase_assigned: bool = field(default=False)
    seminar_assigned:        bool = field(default=False)
    miniproject_assigned:    bool = field(default=False)
    ccw_assigned:            bool = field(default=False)
    y2p_assigned:            bool = field(default=False)

    # ── Combined Lab/Project/Seminar slot tracking ────────────────────────────
    # A teacher may hold at most 3 subjects in total:
    #   • 2 Core Theory subjects, AND
    #   • 1 Lab  OR  1 Project  OR  1 Seminar  (only ONE of these)
    # This flag is set to True the first time the teacher receives any one of
    # Lab / Project-type / Seminar / Mini-Project assignments.  When True the
    # teacher cannot receive any further Lab, Project or Seminar assignment.
    lab_or_extra_assigned: bool = field(default=False)

    # ── Non-BSH subject uniqueness tracking ──────────────────────────────────
    # Records the subject_ids already assigned as theory to this teacher.
    # For non-BSH teachers, the same subject_id must NOT appear in more than
    # one division (two theory slots must be two DIFFERENT subjects).
    # For BSH teachers, same subject across multiple divisions is allowed
    # (each class counts separately toward their 3-theory cap).
    assigned_theory_subject_ids: Set[str] = field(default_factory=set)

    def get_total_load(self) -> int:
        return self.assigned_load.get('theory', 0) + self.assigned_load.get('lab', 0)

    def max_weekly_periods(self) -> int:
        """Return the designation-specific weekly period cap for this teacher."""
        return _max_periods_for_teacher(self)

    def can_take_theory(self) -> bool:
        """
        Non-BSH: max 2 DISTINCT theory subjects.
          Each slot must be a different subject_id; same subject in a second
          division is NOT a new slot — it is simply disallowed.
          The check uses assigned_load['theory'] which counts unique subjects.

        BSH: max 3 theory assignments (same subject across classes counts per
          class, so assigned_load['theory'] increments per class assignment).
        """
        cap = 3 if self.is_bsh else 2
        return self.assigned_load.get('theory', 0) < cap

    def can_take_subject(self, subject_id: str) -> bool:
        """
        Return True if this teacher can be assigned the given theory subject.

        Non-BSH: rejects the subject if it is already in
          assigned_theory_subject_ids (same subject cannot go to two divisions).
        BSH:     always True from this perspective (multi-division allowed).
        """
        if self.is_bsh:
            return True   # BSH may teach same subject to multiple divisions
        return subject_id not in self.assigned_theory_subject_ids

    def can_take_lab(self) -> bool:
        """Check if this teacher can take a Lab assignment.
        Enforces the combined Lab/Project/Seminar slot limit (max 1 total).
        """
        if self.lab_or_extra_assigned:
            return False
        return self.assigned_load.get('lab', 0) < 1

    def can_take_extra_subject(self) -> bool:
        """Check if this teacher can take a Project/Seminar/MiniProject assignment.
        Enforces the combined Lab/Project/Seminar slot limit (max 1 total).
        """
        return not self.lab_or_extra_assigned

    def weekly_load_ok(self, extra_periods: int = 1) -> bool:
        """Check whether adding extra_periods keeps weekly total within the
        designation-specific cap (_max_periods_for_teacher)."""
        return self.weekly_periods_used + extra_periods <= _max_periods_for_teacher(self)


@dataclass
class SubjectAssignment:
    subject:   Subject
    division:  Division
    teacher:   Teacher
    role:      str = 'main'  # 'main' | 'assistant'


# ─────────────────────────────────────────────────────────────────────────────
# Subject Assignment Engine
# ─────────────────────────────────────────────────────────────────────────────

class SubjectAssignmentEngine:
    def __init__(self):
        self.teachers:    List[Teacher]    = []
        self.subjects:    List[Subject]    = []
        self.departments: List[Department] = []
        self.divisions:   List[Division]   = []
        self.assignments: List[SubjectAssignment] = []

    def load_data(self, teachers: List[Teacher], subjects: List[Subject],
                  departments: List[Department], divisions: List[Division]):
        self.teachers    = teachers
        self.subjects    = subjects
        self.departments = departments
        self.divisions   = divisions
        self.assignments = []

    def assign(self, preserve_loads: bool = False) -> List[SubjectAssignment]:
        """
        Main assignment algorithm with 4-level priority.

        preserve_loads=True: skip the internal load-reset so that loads
        pre-applied by the caller (e.g. cross-dept global tracking) are
        honoured.  Use False (default) for standalone / test runs.

        Process order:
        1. THEORY subjects
        2. ELECTIVE subjects (grouped by elective_group)
        3. PROJECT subjects
        4. LAB subjects (pairs with already-assigned theory)
        """
        # Declare all module-level globals this function may assign to.
        # Python requires these to appear before any use in the same function scope.
        global _cross_engine_last_ts
        global _cross_engine_extra_load
        global _cross_engine_teacher_subject_ids

        self.assignments = []

        # Reset teacher loads — skipped when preserve_loads=True so that
        # cross-dept/cross-sem accumulated loads injected by the caller
        # (admin_assign_subjects global_teacher_loads) are not wiped out.
        if not preserve_loads:
            # Standalone / test run: start from zero and clear the cross-engine
            # flag registry so a fresh generation is not poisoned by stale state.
            reset_cross_engine_flags()
            for t in self.teachers:
                t.assigned_load = defaultdict(int)
                t.assigned_theory_subject_ids = set()
                for fn in _ALL_ADDITIONAL_FLAGS:
                    setattr(t, fn, False)
        else:
            # Cross-engine run (preserve_loads=True): auto-expire the registry
            # if it was last touched more than _CROSS_ENGINE_EXPIRE_SECONDS ago
            # (guards against stale server state from a previous generation run).
            now = _time.monotonic()
            if (now - _cross_engine_last_ts) > _CROSS_ENGINE_EXPIRE_SECONDS:
                log.info(
                    "Cross-engine flags expired (%.0f s idle) — clearing.",
                    now - _cross_engine_last_ts,
                )
                reset_cross_engine_flags()
            _cross_engine_last_ts = now
            # Restore flags and subject-ID sets accumulated by earlier engines.
            for t in self.teachers:
                saved = _cross_engine_additional_flags.get(t.teacher_id, set())
                for fn in _ALL_ADDITIONAL_FLAGS:
                    if fn in saved:
                        setattr(t, fn, True)
                # Restore assigned subject IDs so the same non-BSH teacher
                # cannot receive the same subject in a different dept/sem engine.
                saved_sids = _cross_engine_teacher_subject_ids.get(t.teacher_id, set())
                t.assigned_theory_subject_ids = set(saved_sids)

        # Type order: theory first, then elective, project, lab
        type_order = {
            SubjectType.THEORY:   0,
            SubjectType.ELECTIVE: 1,
            SubjectType.PROJECT:  2,
            SubjectType.LAB:      3,
        }
        subjects_sorted = sorted(self.subjects, key=lambda s: type_order.get(s.subject_type, 99))

        # Extract elective groups
        elective_groups = defaultdict(list)
        standalone_subjects = []

        for subj in subjects_sorted:
            if subj.subject_type == SubjectType.ELECTIVE and subj.elective_group:
                elective_groups[subj.elective_group].append(subj)
            else:
                standalone_subjects.append(subj)

        # ── FIX: Pull additional-responsibility subjects out of the main pool ──
        # Seminar / Project Phase 1&2 / Mini Project / CCW / Y2P must be
        # assigned AFTER all regular theory/lab/elective subjects so they do
        # not consume teacher load slots prematurely.
        additional_subjects: List[Subject] = []
        main_standalone: List[Subject] = []
        for subj in standalone_subjects:
            if _is_additional_responsibility(subj):
                additional_subjects.append(subj)
            else:
                main_standalone.append(subj)
        standalone_subjects = main_standalone

        # Separate standalone subjects into phases
        priority_subjects = []
        specialization_subjects = []
        remaining_subjects = []
        
        for subj in standalone_subjects:
            has_preference = any(
                self._preference_rank(t, subj) > 0
                for t in self.teachers
                if t.dept_id == subj.teacher_dept_id or not subj.teacher_dept_id
            )
            
            if has_preference:
                priority_subjects.append(subj)
            else:
                has_specialization = any(
                    self._specialization_score(t, subj) > 0
                    for t in self.teachers
                    if t.dept_id == subj.teacher_dept_id or not subj.teacher_dept_id
                )
                if has_specialization:
                    specialization_subjects.append(subj)
                else:
                    remaining_subjects.append(subj)

        # Categorize elective groups into phases
        elective_group_phases = {}
        for group_name, electives in elective_groups.items():
            # Determine phase based on whether ANY teacher wants ANY elective in this group
            has_preference = any(
                self._preference_rank(t, elec) > 0
                for elec in electives
                for t in self.teachers
                if t.dept_id == elec.teacher_dept_id or not elec.teacher_dept_id
            )
            
            if has_preference:
                elective_group_phases[group_name] = 'priority'
            else:
                has_specialization = any(
                    self._specialization_score(t, elec) > 0
                    for elec in electives
                    for t in self.teachers
                    if t.dept_id == elec.teacher_dept_id or not elec.teacher_dept_id
                )
                if has_specialization:
                    elective_group_phases[group_name] = 'specialization'
                else:
                    elective_group_phases[group_name] = 'remaining'

        log.info(f"Assignment phases: {len(priority_subjects)} priority standalone, "
                f"{len(specialization_subjects)} specialization standalone, "
                f"{len(remaining_subjects)} remaining standalone, "
                f"{len(elective_groups)} elective groups")

        # PHASE 1: Assign priority subjects
        for subj in priority_subjects:
            self._assign_subject(subj, assignment_phase='priority')

        # PHASE 1: Assign priority elective groups
        for group_name, electives in elective_groups.items():
            if elective_group_phases[group_name] == 'priority':
                self._assign_elective_group(electives, 'priority')

        # PHASE 2: Assign specialization subjects
        for subj in specialization_subjects:
            self._assign_subject(subj, assignment_phase='specialization')

        # PHASE 2: Assign specialization elective groups
        for group_name, electives in elective_groups.items():
            if elective_group_phases[group_name] == 'specialization':
                self._assign_elective_group(electives, 'specialization')

        # PHASE 3: Assign remaining subjects
        for subj in remaining_subjects:
            self._assign_subject(subj, assignment_phase='remaining')

        # PHASE 3: Assign remaining elective groups
        for group_name, electives in elective_groups.items():
            if elective_group_phases[group_name] == 'remaining':
                self._assign_elective_group(electives, 'remaining')

        # PHASE 4: Assign additional responsibilities AFTER main load is complete.
        # These subjects (Seminar, Project Phase 1/2, Mini Project, CCW, Y2P)
        # do NOT count against the 2-theory + 1-lab limit and are distributed
        # across multiple teachers where required.
        if additional_subjects:
            log.info("PHASE 4: assigning %d additional-responsibility subjects",
                     len(additional_subjects))
            # LOAD-BALANCE FIX: Seed extra_load from the cross-engine registry so
            # that teachers who already received additional duties in an earlier
            # (dept, sem) engine call are ranked higher (busier) and not picked
            # again while other teachers still have zero additional duties.
            # Without this, extra_load was always a fresh dict(=0 for everyone),
            # letting a single teacher accumulate Seminar + CCW + Y2P across
            # three separate engine calls.
            extra_load_seed: Dict[str, int] = {}
            if preserve_loads:
                for t in self.teachers:
                    extra_load_seed[t.teacher_id] = _cross_engine_extra_load.get(
                        t.teacher_id, 0
                    )
            extra_load_out = self._assign_additional_responsibilities(
                additional_subjects, extra_load=extra_load_seed
            )
            # Persist updated counts back so the next engine call in this run
            # sees the accumulated totals.
            if preserve_loads:
                for tid, count in extra_load_out.items():
                    _cross_engine_extra_load[tid] = max(
                        _cross_engine_extra_load.get(tid, 0), count
                    )
        else:
            extra_load_out = {}

        # FIX (Problem 3): Persist any additional-responsibility flags that were
        # set during this engine run into the cross-engine registry so the NEXT
        # engine (next dept/sem pair in admin_assign_subjects) does not assign
        # the same type of duty to a teacher who already has one.
        if preserve_loads:
            for t in self.teachers:
                entry = _cross_engine_additional_flags.setdefault(
                    t.teacher_id, set()
                )
                for fn in _ALL_ADDITIONAL_FLAGS:
                    if getattr(t, fn, False):
                        entry.add(fn)
                # Persist assigned subject IDs so the next engine call for a
                # different dept/sem cannot re-assign the same subjects.
                saved = _cross_engine_teacher_subject_ids.setdefault(t.teacher_id, set())
                saved.update(t.assigned_theory_subject_ids)

        log.info(f"Total assignments before post-passes: {len(self.assignments)}")

        # PHASE 5: Guarantee preferred subjects for non-BSH teachers (R2/S2)
        self._guarantee_preferred_subjects()

        # PHASE 6: Bin-pack remaining unassigned slots into dummy teachers (S9)
        dummy_teachers = self._pack_dummies()
        if dummy_teachers:
            log.info(
                "PHASE 6: created %d dummy teacher(s) to cover %d unassigned slots",
                len(dummy_teachers),
                sum(1 for a in self.assignments if a.teacher.is_dummy and a.role == 'main'),
            )

        log.info(f"Total assignments (including dummies): {len(self.assignments)}")
        return self.assignments


    def _assign_elective_group(self, electives: List[Subject], assignment_phase: str):
        """
        Assign all electives in a group together.
        Try to assign to different teachers when possible.
        """
        if not electives:
            return

        log.info(f"Assigning elective group: {electives[0].elective_group} "
                f"({len(electives)} electives) [{assignment_phase}]")

        # Get divisions for the first elective (all should be same)
        divisions = self._get_relevant_divisions(electives[0])
        if not divisions:
            log.warning(f"No divisions for elective group {electives[0].elective_group}")
            return

        is_bsh_group = bool(
            electives[0].teacher_dept_id
            and 'BSH' in electives[0].teacher_dept_id.upper()
        )

        for div in divisions:
            assigned_teachers: Set[str] = set()
            load_incremented_for: Set[str] = set()

            for elective in electives:
                elig = self._get_eligible_teachers(elective)
                if not elig:
                    log.warning(f"No eligible teachers for {elective.name}")
                    continue

                available = [
                    t for t in elig
                    if t.teacher_id not in assigned_teachers
                    and t.can_take_theory()
                    and t.can_take_subject(elective.subject_id)   # unique-subject rule
                    and t.weekly_load_ok(elective.periods_per_week)
                ]
                if not available:
                    available = [
                        t for t in elig
                        if t.can_take_theory()
                        and t.can_take_subject(elective.subject_id)
                        and t.weekly_load_ok(elective.periods_per_week)
                    ]
                if not available:
                    log.warning(
                        f"  ✗ {elective.name} ({div.division_id}): No teachers with capacity"
                    )
                    continue

                teacher = self._pick_teacher(available, elective, assignment_phase)
                if teacher:
                    self.assignments.append(
                        SubjectAssignment(elective, div, teacher, 'main')
                    )
                    if teacher.teacher_id not in load_incremented_for:
                        if is_bsh_group:
                            # BSH: count every class assignment separately
                            teacher.assigned_load['theory'] = (
                                teacher.assigned_load.get('theory', 0) + 1
                            )
                            teacher.weekly_periods_used += elective.periods_per_week
                        else:
                            # Non-BSH: count only new unique subjects
                            if elective.subject_id not in teacher.assigned_theory_subject_ids:
                                teacher.assigned_load['theory'] = (
                                    teacher.assigned_load.get('theory', 0) + 1
                                )
                                teacher.weekly_periods_used += elective.periods_per_week
                                teacher.assigned_theory_subject_ids.add(elective.subject_id)
                        load_incremented_for.add(teacher.teacher_id)
                    assigned_teachers.add(teacher.teacher_id)
                    log.info(
                        f"  ✓ {elective.name} → {teacher.name} ({div.division_id})"
                    )
                else:
                    log.warning(
                        f"  ✗ {elective.name} ({div.division_id}): No available teacher"
                    )

    def _assign_subject(self, subj: Subject, assignment_phase: str):
        """Dispatch to lab or regular assignment."""
        if subj.subject_type == SubjectType.LAB:
            self._assign_lab_with_pairing(subj, assignment_phase)
        else:
            self._assign_regular(subj, assignment_phase)

    def _assign_regular(self, subj: Subject, assignment_phase: str):
        """
        Assign a regular (theory / elective / project) subject per-division.

        Rules enforced:
          • Each division is assigned independently (R10: load-balanced sort).
          • Weekly workload cap (MAX_WEEKLY_PERIODS periods, LTPR-based) checked before each assignment.
          • BSH teachers: Priority-1 first; same subject to multiple divisions allowed
            and each counts separately toward their 3-theory cap.
          • Non-BSH teachers: max 2 DISTINCT theory subjects.
              - Same subject MUST NOT go to two different divisions for the same teacher.
              - Two theory slots = two DIFFERENT subject_ids.
          • Additional-responsibility subjects excluded from load caps.
          • Divisions that exceed all available capacity get queued for dummy packing.
        """
        divisions = self._get_relevant_divisions(subj)
        if not divisions:
            log.warning(f"No divisions for {subj.name}")
            return

        eligible = self._get_eligible_teachers(subj)
        if not eligible:
            log.warning(
                f"No eligible teachers for {subj.name} (dept_id={subj.teacher_dept_id})"
            )
            return

        is_extra = _is_additional_responsibility(subj)
        is_bsh_subj = bool(
            subj.teacher_dept_id and 'BSH' in subj.teacher_dept_id.upper()
        )
        assigned_teachers_this_subj: Set[str] = set()

        for div in divisions:
            if is_extra:
                candidates = [
                    t for t in eligible
                    if t.weekly_load_ok(subj.periods_per_week)
                ]
            elif is_bsh_subj:
                # BSH: same subject across divisions allowed — only theory-cap + weekly-cap
                candidates = [
                    t for t in eligible
                    if t.can_take_theory()
                    and t.weekly_load_ok(subj.periods_per_week)
                ]
            else:
                # Non-BSH:
                #   • theory-cap (max 2 unique subjects)
                #   • unique-subject rule (no same subject_id in two divisions)
                #   • weekly-cap
                #   • one-division-per-subject: exclude teachers already assigned
                #     to another division for THIS same subject
                candidates = [
                    t for t in eligible
                    if t.can_take_theory()
                    and t.can_take_subject(subj.subject_id)
                    and t.weekly_load_ok(subj.periods_per_week)
                    and t.teacher_id not in assigned_teachers_this_subj
                ]

            if not candidates:
                log.warning(
                    f"  ✗ {subj.name} ({div.division_id}): "
                    f"no teacher with capacity — queued for dummy"
                )
                continue

            # BSH sorting: Priority-1 first, then P2/P3, then any eligible
            if is_bsh_subj:
                p1_cands  = [t for t in candidates if self._preference_rank(t, subj) == 1]
                p23_cands = [t for t in candidates if self._preference_rank(t, subj) in (2, 3)]
                if p1_cands:
                    candidates = sorted(p1_cands, key=lambda t: t.weekly_periods_used)
                elif p23_cands:
                    candidates = sorted(
                        p23_cands,
                        key=lambda t: (self._preference_rank(t, subj), t.weekly_periods_used),
                    )
                else:
                    candidates.sort(key=lambda t: t.weekly_periods_used)
                log.debug(
                    "BSH subject %s (%s): %d P1, %d P23, using %d candidate(s)",
                    subj.name, div.division_id, len(p1_cands), len(p23_cands), len(candidates),
                )
            else:
                # Non-BSH: load-balanced sort; _pick_teacher applies preference/seniority
                candidates.sort(
                    key=lambda t: (t.weekly_periods_used, t.get_total_load())
                )

            teacher = self._pick_teacher(candidates, subj, assignment_phase)
            if not teacher:
                log.warning(
                    f"  ✗ {subj.name} ({div.division_id}): _pick_teacher returned None"
                )
                continue

            self.assignments.append(SubjectAssignment(subj, div, teacher, 'main'))

            if not is_extra:
                # BSH: each class assignment counts separately → always increment
                # Non-BSH: each UNIQUE subject counts → increment only the first
                #   time this teacher is assigned this subject (subsequent divisions
                #   of the same subject are blocked by can_take_subject, so this
                #   path is only reached once per unique subject per teacher).
                if is_bsh_subj:
                    # BSH same subject → multiple divisions all count
                    teacher.assigned_load['theory'] = (
                        teacher.assigned_load.get('theory', 0) + 1
                    )
                    teacher.weekly_periods_used += subj.periods_per_week
                else:
                    # Non-BSH: increment theory load once per unique subject
                    if subj.subject_id not in teacher.assigned_theory_subject_ids:
                        teacher.assigned_load['theory'] = (
                            teacher.assigned_load.get('theory', 0) + 1
                        )
                        teacher.weekly_periods_used += subj.periods_per_week
                        teacher.assigned_theory_subject_ids.add(subj.subject_id)
                    # else: should not happen (can_take_subject blocks it), but guard

            assigned_teachers_this_subj.add(teacher.teacher_id)
            log.info(
                f"  ✓ {subj.name} → {teacher.name} ({div.division_id}) "
                f"theory={teacher.assigned_load.get('theory', 0)} "
                f"weekly={teacher.weekly_periods_used} [{assignment_phase}]"
            )


    def _assign_lab_with_pairing(self, lab: Subject, assignment_phase: str):
        """
        Assign a lab subject per-division.

        Rules:
          1. For each division, find the teacher who handles the matching theory
             subject in THAT division (by companion_theory_id OR by name match
             after stripping "lab").  That teacher is STRONGLY preferred as the
             main lab instructor.
          2. The companion teacher is used only if they have weekly capacity.
             If they would exceed the MAX_WEEKLY_PERIODS cap, fall back to another
             eligible teacher from the department.
          3. If no matching theory teacher is found or they are over cap, fall
             back to any eligible teacher who can take a lab.
          4. Non-BSH: each lab division is independent.
          5. BSH: same teacher may cover multiple divisions.
          6. Each lab requires a main teacher + an assistant teacher.
        """
        divisions = self._get_relevant_divisions(lab)
        if not divisions:
            return

        eligible = self._get_eligible_teachers(lab)
        if not eligible:
            log.warning("No eligible teachers for lab %s", lab.name)
            return

        is_bsh_lab = bool(
            lab.teacher_dept_id and 'BSH' in lab.teacher_dept_id.upper()
        )

        bsh_main: Optional[Teacher] = None

        for div in divisions:
            main_teacher: Optional[Teacher] = None
            load_already_counted = False  # True when load was incremented earlier

            if is_bsh_lab and bsh_main and bsh_main.weekly_load_ok(lab.periods_per_week):
                main_teacher = bsh_main
                load_already_counted = True
            else:
                # Step 1: preferred companion-theory pairing for this division.
                # Use the theory teacher for the matching subject in THIS division
                # if they have remaining weekly capacity.
                comp = self._get_lab_companion_teacher(lab, div)
                if comp:
                    # Check if they are already assigned THIS lab in this division
                    # (duplicate guard for multi-pass calls).
                    already_has_lab = any(
                        a.subject.subject_id == lab.subject_id
                        and a.division.division_id == div.division_id
                        and a.teacher.teacher_id == comp.teacher_id
                        and a.role == 'main'
                        for a in self.assignments
                    )
                    if already_has_lab:
                        main_teacher = comp
                        load_already_counted = True
                    elif comp.can_take_lab() and comp.weekly_load_ok(lab.periods_per_week):
                        main_teacher = comp
                        log.info(
                            "  Lab %s (%s): paired with theory teacher %s",
                            lab.name, div.division_id, comp.name,
                        )
                    elif not comp.can_take_lab():
                        log.warning(
                            "  Lab %s (%s): theory teacher %s already has a lab "
                            "assigned (cap=1) — falling back to another eligible "
                            "teacher instead of double-booking them.",
                            lab.name, div.division_id, comp.name,
                        )
                    else:
                        log.warning(
                            "  Lab %s (%s): theory teacher %s is at weekly cap "
                            "(%d periods) — falling back to another eligible teacher.",
                            lab.name, div.division_id, comp.name,
                            comp.weekly_periods_used,
                        )

                # Step 2: no matching theory teacher (or they are over cap) —
                # fall back to any eligible teacher who can take a lab.
                if not main_teacher:
                    fallback = [
                        t for t in eligible
                        if t.can_take_lab()
                        and t.weekly_load_ok(lab.periods_per_week)
                        and (is_bsh_lab or not any(
                            a.subject.subject_id == lab.subject_id
                            and a.teacher.teacher_id == t.teacher_id
                            and a.role == 'main'
                            for a in self.assignments
                        ))
                    ]
                    main_teacher = self._pick_teacher(fallback, lab, assignment_phase)

            if not main_teacher:
                log.warning(
                    "  Lab %s (%s): no available teacher — queued for dummy",
                    lab.name, div.division_id,
                )
                continue

            # Book the main assignment
            self.assignments.append(SubjectAssignment(lab, div, main_teacher, 'main'))

            # Update load counters (skip if already counted for this teacher/lab)
            if not load_already_counted:
                already_has_this_lab_load = any(
                    a.subject.subject_id == lab.subject_id
                    and a.teacher.teacher_id == main_teacher.teacher_id
                    and a.role == 'main'
                    and a.division.division_id != div.division_id  # another division
                    for a in self.assignments
                )
                if not already_has_this_lab_load:
                    # First time this teacher is assigned this lab — count it
                    main_teacher.assigned_load['lab'] = (
                        main_teacher.assigned_load.get('lab', 0) + 1
                    )
                    main_teacher.weekly_periods_used += lab.periods_per_week
                    # Consume the combined Lab/Project/Seminar slot so this
                    # teacher cannot also receive a project or seminar.
                    main_teacher.lab_or_extra_assigned = True
                    if is_bsh_lab and bsh_main is None:
                        bsh_main = main_teacher

            log.info(
                "  Lab %s main=%s (%s) weekly=%d [%s]",
                lab.name, main_teacher.name, div.division_id,
                main_teacher.weekly_periods_used, assignment_phase,
            )

            # Assign assistant
            assistant = self._assign_lab_assistant(lab, div, main_teacher)
            if assistant:
                log.info(
                    "  + Assistant %s for %s (%s)",
                    assistant.name, lab.name, div.division_id,
                )

    def _assign_lab_assistant(self, lab: Subject, div: Division, main_teacher: Teacher) -> Optional[Teacher]:
        """
        Assign an assistant teacher for a lab.

        Eligibility rules:
          - Must NOT be Principal, Deputy Dean, HOD, or Associate Professor
            (_LAB_ASSISTANT_INELIGIBLE).
          - Must NOT already have ANY lab main assignment (no lab duties at all).
          - Must NOT be the main teacher for this lab.
          - Weekly workload must have capacity for the lab block.
          - Assistant load does NOT count toward their theory/lab subject limits.
        """
        eligible = self._get_eligible_teachers(lab)

        # Build set of teacher IDs who already have a main lab assignment
        teachers_with_lab_duties: Set[str] = {
            a.teacher.teacher_id
            for a in self.assignments
            if a.role == 'main' and a.subject.subject_type == SubjectType.LAB
        }

        asst_eligible = [
            t for t in eligible
            if t.designation not in _LAB_ASSISTANT_INELIGIBLE
            and t.teacher_id != main_teacher.teacher_id
            and t.teacher_id not in teachers_with_lab_duties
            and t.weekly_load_ok(lab.periods_per_week)
        ]

        if not asst_eligible:
            log.warning(
                "  No eligible assistant for %s (%s)", lab.name, div.division_id
            )
            return None

        # Spread assistant duties evenly across eligible staff
        assistant_usage: Dict[str, int] = defaultdict(int)
        for a in self.assignments:
            if a.role in ('assistant', 'seminar_assistant'):
                assistant_usage[a.teacher.teacher_id] += 1

        assistant = min(
            asst_eligible,
            key=lambda t: (assistant_usage[t.teacher_id], t.weekly_periods_used),
        )
        self.assignments.append(SubjectAssignment(lab, div, assistant, 'assistant'))

        # Count toward assistant's weekly total but NOT toward subject limits
        assistant.weekly_periods_used += lab.periods_per_week
        return assistant


    # ─────────────────────────────────────────────────────────────────────────
    # Additional-responsibility subject assignment  (Phase 4)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Helper: determine which "one-per-teacher" flag a subject uses ──────────
    @staticmethod
    def _additional_flag(subj: "Subject") -> str:
        """
        Return the name of the Teacher flag that governs the one-per-teacher
        limit for this additional-responsibility subject.

        Returns:
          'project_phase_assigned'  — for Project Phase 1 / Project Phase 2
          'seminar_assigned'        — for Seminar
          'miniproject_assigned'    — for Mini Project
          'ccw_assigned'            — for CCW  (FIX: was returning '' → no limit enforced)
          'y2p_assigned'            — for Y2P  (FIX: was returning '' → no limit enforced)
          ''                        — for any other extra (genuinely no per-teacher limit)
        """
        name = (subj.name or '').lower()
        if 'project phase' in name:
            return 'project_phase_assigned'
        if 'seminar' in name:
            return 'seminar_assigned'
        if 'mini project' in name or 'miniproject' in name:
            return 'miniproject_assigned'
        if 'ccw' in name or 'course work' in name:
            return 'ccw_assigned'
        if 'y2p' in name:
            return 'y2p_assigned'
        return ''

    def _assign_additional_responsibilities(
        self,
        subjects: List[Subject],
        extra_load: Optional[Dict[str, int]] = None,
    ) -> Dict[str, int]:
        """
        Assign additional-responsibility subjects (Phase 4).

        Rules enforced:
          • These subjects do NOT count against the 2-theory + 1-lab limit.
          • WORKLOAD RULE: Only assign to teachers whose weekly_periods_used is
            below their designation-specific cap (_max_periods_for_teacher).
          • COMBINED EXTRA-SLOT CAP: A teacher may only receive ONE of the
            following across ALL subjects — Lab, Project Phase, Seminar, Mini
            Project.  Teachers whose lab_or_extra_assigned flag is already True
            are excluded from Phase/Seminar/MiniProject assignment.
          • Multi-supervised subjects receive the required number of teachers:
              Project Phase 1/2 → up to 5 teachers
              Seminar           → up to 5 (1 main + up to 4 co-supervisors)
              Mini Project      → 3 teachers
              CCW               → 2 teachers
          • Remaining subjects (Y2P, …) receive 1 teacher.
          • Teachers who already carry many additional subjects are deprioritised
            so the extra duties are spread across the department.
          • CONSTRAINT: Project Phase / Seminar / Mini Project may be assigned
            to a teacher AT MOST ONCE (across all divisions + semesters).

        Parameters:
          extra_load: optional pre-seeded cumulative additional-duty counts
                      accumulated from earlier engine calls in this generation
                      run.  If None a fresh counter is used (standalone mode).

        Returns:
          The updated extra_load dict (caller persists it back to the
          cross-engine registry when preserve_loads=True).
        """
        if extra_load is None:
            extra_load = defaultdict(int)
        else:
            extra_load = defaultdict(int, extra_load)

        for subj in subjects:
            count     = _multi_teacher_count(subj)
            divisions = self._get_relevant_divisions(subj)
            flag      = self._additional_flag(subj)

            # Determine if this subject counts as the combined Lab/Extra slot.
            # Project Phase, Seminar, and Mini Project each consume the single
            # Lab/Project/Seminar slot a teacher may hold.
            is_extra_slot_subject = bool(
                'project phase' in subj.name.lower()
                or 'seminar' in subj.name.lower()
                or 'mini project' in subj.name.lower()
                or 'miniproject' in subj.name.lower()
            )

            # Only teachers whose current weekly load is below their designation
            # cap should receive additional assignments.
            raw_eligible = self._get_eligible_teachers(subj)
            under_loaded = [
                t for t in raw_eligible
                if t.weekly_periods_used < _max_periods_for_teacher(t)
            ]

            # Filter out teachers who already hold this type of additional duty
            if flag:
                eligible = [t for t in under_loaded if not getattr(t, flag, False)]
            else:
                eligible = under_loaded

            # For subjects that consume the combined Lab/Extra slot, also
            # exclude teachers who already have a Lab or other extra-slot subject.
            if is_extra_slot_subject:
                eligible = [t for t in eligible if t.can_take_extra_subject()]

            # Fall back to all eligible (any load) if nobody is under cap — rare
            if not eligible:
                if flag:
                    eligible = [t for t in raw_eligible if not getattr(t, flag, False)]
                else:
                    eligible = list(raw_eligible)
                # Reapply extra-slot filter even on fallback pool
                if is_extra_slot_subject:
                    eligible = [t for t in eligible if t.can_take_extra_subject()]

            if not divisions:
                log.warning("Additional subject %s: no divisions found — skipping", subj.name)
                continue
            if not eligible:
                log.warning("Additional subject %s: no eligible teachers — skipping", subj.name)
                continue

            if count > 1:
                self._assign_multi_teacher_additional(
                    subj, count, divisions, eligible, extra_load, flag,
                    is_extra_slot_subject=is_extra_slot_subject)
            else:
                self._assign_single_additional(
                    subj, divisions, eligible, extra_load, flag,
                    is_extra_slot_subject=is_extra_slot_subject)

        return dict(extra_load)

    def _assign_multi_teacher_additional(
        self,
        subj:         Subject,
        num_teachers: int,
        divisions:    List[Division],
        eligible:     List[Teacher],
        extra_load:   Dict[str, int],
        flag:         str = '',
        is_extra_slot_subject: bool = False,
    ) -> None:
        """
        Assign *num_teachers* teachers to a multi-supervised subject.

        The FIRST teacher gets role='main' (the timetable generator uses this
        to schedule the continuous block).  The remaining teachers get
        role='co_supervisor' — they are blocked during project periods but do
        not independently produce a separate timetable block.

        WORKLOAD-FILLING RULE (Rule 8):
          Teachers whose weekly total is below their designation-specific cap
          are preferred (they need extra duties to reach their workload).
          Among teachers with the same workload situation, those with fewer
          additional duties are preferred so that the extra responsibilities
          are spread evenly.

        flag: if non-empty, the Teacher attribute to set True after assignment
              so the same teacher cannot be assigned this type again.
        is_extra_slot_subject: if True, set lab_or_extra_assigned=True on
              each selected teacher (consumes their combined Lab/Extra slot).
        """
        # Prefer teachers below their designation cap first.
        # Within that group, sort by fewest additional duties, then lightest load,
        # then seniority (senior first).
        ranked = sorted(
            eligible,
            key=lambda t: (
                0 if t.weekly_periods_used < _max_periods_for_teacher(t) else 1,  # under-loaded first
                extra_load[t.teacher_id],                  # fewest additional duties
                t.get_total_load(),                        # lighter main-subject load
                t.seniority_level if t.seniority_level is not None else 999,
            )
        )
        selected = ranked[:num_teachers]
        if not selected:
            log.warning("  ✗ %s: could not select teachers for multi-assignment", subj.name)
            return

        for div in divisions:
            for idx, teacher in enumerate(selected):
                role = 'main' if idx == 0 else 'co_supervisor'
                self.assignments.append(SubjectAssignment(subj, div, teacher, role))
                extra_load[teacher.teacher_id] += 1
                log.info("  ✓ %s → %s (%s) [additional, %s]",
                         subj.name, teacher.name, div.division_id, role)

        # Mark each selected teacher as "already has this type" so they cannot
        # be picked again for another project phase / seminar / mini project.
        if flag:
            for teacher in selected:
                setattr(teacher, flag, True)

        # Mark the combined Lab/Extra slot as consumed for each selected teacher.
        if is_extra_slot_subject:
            for teacher in selected:
                teacher.lab_or_extra_assigned = True

    def _assign_single_additional(
        self,
        subj:       Subject,
        divisions:  List[Division],
        eligible:   List[Teacher],
        extra_load: Dict[str, int],
        flag:       str = '',
        is_extra_slot_subject: bool = False,
    ) -> None:
        """
        Assign exactly one teacher to a single-supervised additional subject
        (Y2P, etc.).  Prefers teachers below their designation cap
        (workload-filling rule) and fewest existing additional duties to
        spread the load.

        flag: if non-empty, set this Teacher attribute to True after assignment
              so the teacher cannot be assigned this type again.
        is_extra_slot_subject: if True, set lab_or_extra_assigned=True on the
              selected teacher (consumes their combined Lab/Extra slot).
        """
        ranked = sorted(
            eligible,
            key=lambda t: (
                0 if t.weekly_periods_used < _max_periods_for_teacher(t) else 1,  # under-loaded first (Rule 8)
                extra_load[t.teacher_id],                  # fewest additional duties
                t.get_total_load(),                        # lighter main subject load
                -DESIGNATION_SENIORITY.get(t.designation, 99),  # junior first (tiebreaker)
            )
        )
        teacher = ranked[0]
        for div in divisions:
            self.assignments.append(SubjectAssignment(subj, div, teacher, 'main'))
        extra_load[teacher.teacher_id] += 1
        # Mark teacher so they cannot be assigned this type again
        if flag:
            setattr(teacher, flag, True)
        # Mark the combined Lab/Extra slot as consumed
        if is_extra_slot_subject:
            teacher.lab_or_extra_assigned = True
        log.info("  ✓ %s → %s [additional, single]", subj.name, teacher.name)

    def _get_lab_companion_teacher(self, lab: Subject, div: Division) -> Optional[Teacher]:
        """
        Find the teacher already assigned to the companion theory subject in
        this specific division.

        Strategy (in order):
          1. Exact companion_theory_id match (set during load_from_db linking).
          2. Name-based fallback: strip "lab" from the lab name and look for
             any theory/elective assignment in this division whose subject name
             contains the stripped keyword. This catches cases where the
             companion link was not established (e.g. similarity below threshold).
        """
        # Step 1 — companion_theory_id link (preferred, deterministic)
        if lab.companion_theory_id:
            for asgn in self.assignments:
                if (asgn.subject.subject_id == lab.companion_theory_id
                        and asgn.division.division_id == div.division_id
                        and asgn.role == 'main'):
                    return asgn.teacher

        # Step 2 — name-based fallback
        # Strip "lab" (and common suffixes like "laboratory") from lab name
        lab_base = re.sub(
            r'\b(lab(oratory)?)\b\.?', '', lab.name, flags=re.IGNORECASE
        ).strip().lower()

        if not lab_base:
            return None

        for asgn in self.assignments:
            if asgn.division.division_id != div.division_id:
                continue
            if asgn.role != 'main':
                continue
            if asgn.subject.subject_type not in (SubjectType.THEORY, SubjectType.ELECTIVE):
                continue
            theory_name = asgn.subject.name.lower()
            # Check if lab base keyword is contained in the theory name
            if lab_base in theory_name or theory_name.startswith(lab_base):
                log.info(
                    "  Lab companion (name match): '%s' -> '%s' for %s",
                    lab.name, asgn.subject.name, div.division_id,
                )
                return asgn.teacher

        return None

    def _pick_teacher(self, candidates: List[Teacher], subj: Subject,
                     assignment_phase: str, filter_designation: Optional[Designation] = None) -> Optional[Teacher]:
        """
        Pick best teacher using 4-level priority system.

        PREFERENCE + SENIORITY RULES (updated):
          • Priority level 1 — teacher explicitly ranked this subject (rank 1-3).
              Rank 1 score = 3, Rank 2 score = 2, Rank 3 score = 1.
              When two teachers share the SAME rank for the same subject,
              the MORE SENIOR teacher wins (seniority_level ASC — lower = senior).
              When ranks differ (e.g. senior P2 vs junior P1), the HIGHER-RANK
              holder wins: junior's P1 (score 3) beats senior's P2 (score 2).
          • Priority level 2 — specialization/interest match.
          • Priority level 3/4 — no match; remaining phase uses junior-first.

        SENIORITY SOURCE (updated):
          Uses teacher.seniority_level (loaded from DB, lower = more senior,
          combines designation + date-of-joining) instead of the old
          DESIGNATION_SENIORITY mapping which only considered designation.

        DEPARTMENT SAFETY CHECK (FIX 2):
          Skips mis-matched candidates instead of aborting the entire pick.
        """
        if filter_designation:
            candidates = [t for t in candidates if t.designation == filter_designation]

        # Filter by load capacity AND unique-subject rule
        if subj.subject_type == SubjectType.LAB:
            available = [t for t in candidates if t.can_take_lab()]
        else:
            available = [
                t for t in candidates
                if t.can_take_theory() and t.can_take_subject(subj.subject_id)
            ]

        if not available:
            return None

        # Calculate scores for each candidate
        scored_candidates = []
        for t in available:
            pref_rank  = self._preference_rank(t, subj)
            spec_score = self._specialization_score(t, subj)
            # Use seniority_level (DB field, lower = more senior).
            # Fallback to 999 when not set so unranked teachers sort last.
            sen_lvl = t.seniority_level if t.seniority_level is not None else 999

            # ── Priority level determination ─────────────────────────────────
            # Level 1: teacher has an explicit preference for this subject.
            #   priority_score = 4 - rank  →  rank1=3, rank2=2, rank3=1
            #
            #   CONFLICT RULE:
            #     Same rank      →  senior wins  (seniority_level ASC as tiebreaker)
            #     Senior P2/P3 vs Junior P1  →  Junior P1 wins
            #       (higher priority_score beats lower regardless of seniority)
            #
            # Level 2: specialization/interest match (no explicit preference).
            # Level 3/4: no match; remaining phase uses junior-first ordering.
            if pref_rank > 0:
                priority_level = 1
                priority_score = 4 - pref_rank   # rank1→3, rank2→2, rank3→1
            elif spec_score > 0:
                priority_level = 2
                priority_score = spec_score
            else:
                priority_level = 3 if assignment_phase != 'remaining' else 4
                priority_score = 0

            scored_candidates.append((t, priority_level, priority_score, sen_lvl))

        # Sort key:
        #
        # Level 1 (PREFERENCE MATCH):
        #   Primary:   -priority_score  (rank1 beats rank2 beats rank3)
        #   Secondary:  seniority_level (senior first when SAME rank)
        #   Tertiary:   total_load      (lighter load as final tiebreaker)
        #
        #   This gives the correct conflict resolution:
        #     Senior P2 (score=2) vs Junior P1 (score=3) → Junior P1 wins (-3 < -2)
        #     Senior P1 (score=3) vs Junior P1 (score=3) → Senior wins (lower seniority_level)
        #
        # Level 2+ (SPECIALIZATION / REMAINING — no preference match):
        #   Primary:   total_load       (lighter load first)
        #   Secondary: seniority_level  (senior first as tiebreaker)
        #   Tertiary:  -spec_score      (higher spec score as last tiebreaker)
        sorted_candidates = sorted(
            scored_candidates,
            key=lambda x: (
                x[1],                                              # priority_level ASC
                -x[2] if x[1] == 1 else x[0].get_total_load(),   # pref: -score; non-pref: load
                x[3]  if x[1] == 1 else x[3],                    # seniority ASC (both cases)
                x[0].get_total_load() if x[1] == 1 else -x[2],   # pref: load; non-pref: -spec_score
            )
        )

        for best in sorted_candidates:
            teacher = best[0]

            # Department safety check: _get_eligible_teachers should have already
            # filtered, but guard here as a final backstop.  Skip mis-matched
            # candidates instead of returning None (which would abandon the subject
            # even when valid teachers remain further down the list — FIX 2).
            if subj.teacher_dept_id:
                allowed_depts = {d.strip() for d in subj.teacher_dept_id.split(',')}
                if teacher.dept_id not in allowed_depts:
                    log.warning(
                        f"Department mismatch (skipping): {teacher.name} "
                        f"({teacher.dept_id}) not in {allowed_depts} for {subj.name}"
                    )
                    continue

            log.debug(
                f"Selected {teacher.name} for {subj.name} "
                f"(level={best[1]}, score={best[2]:.2f}, seniority_level={best[3]})"
            )
            return teacher

        return None

    def _preference_rank(self, teacher: Teacher, subj: Subject) -> int:
        """Return preference rank (1-3) if teacher has this subject as preference, else 0.

        Normalises spaces so that a preference stored as 'HUT300' matches a
        course code 'HUT 300', and vice versa — handles minor formatting
        inconsistencies between how teachers enter codes and how they are stored
        in the courses table.
        """
        subj_code_norm  = (subj.code or '').replace(' ', '').upper()
        subj_id_norm    = (subj.subject_id or '').replace(' ', '').upper()
        for code, rank in teacher.priority_subjects:
            pref_norm = (code or '').replace(' ', '').upper()
            if pref_norm == subj_code_norm or pref_norm == subj_id_norm:
                return rank
        return 0

    def _specialization_score(self, teacher: Teacher, subj: Subject) -> float:
        """Return 0-1 score for how well teacher's specializations match subject."""
        if not teacher.specializations:
            return 0.0
        
        subj_name_lower = subj.name.lower()
        max_score = 0.0
        
        for spec in teacher.specializations:
            spec_lower = spec.lower()
            if spec_lower in subj_name_lower or subj_name_lower in spec_lower:
                max_score = max(max_score, 1.0)
            else:
                similarity = SequenceMatcher(None, spec_lower, subj_name_lower).ratio()
                max_score = max(max_score, similarity)
        
        return max_score

    def _get_eligible_teachers(self, subj: Subject) -> List[Teacher]:
        """Get teachers eligible to teach this subject (dept_id match)."""
        if not subj.teacher_dept_id:
            return self.teachers
        
        # Handle multi-dept: dept_id can be comma-separated
        allowed_depts = {d.strip() for d in subj.teacher_dept_id.split(',')}
        return [t for t in self.teachers if t.dept_id in allowed_depts]

    def _get_relevant_divisions(self, subj: Subject) -> List[Division]:
        """Get divisions that should take this subject."""
        if not subj.applicable_division_tokens:
            return [d for d in self.divisions
                   if d.student_dept_id == subj.student_dept_id
                   and d.semester == subj.semester]
        
        # Safer division token matching with .strip()
        relevant = []
        for div in self.divisions:
            if (div.student_dept_id == subj.student_dept_id and
                div.semester == subj.semester):
                # Division ID is the token itself (e.g., "AI", "DS", "CS1")
                if div.division_id.strip() in [t.strip() for t in subj.applicable_division_tokens]:
                    relevant.append(div)
        return relevant

    def print_summary(self):
        """Print assignment summary."""
        print("\n" + "="*80)
        print("SUBJECT ASSIGNMENT SUMMARY")
        print("="*80)
        
        # Group by division
        by_division = defaultdict(list)
        for asgn in self.assignments:
            by_division[asgn.division.division_id].append(asgn)
        
        for div_id in sorted(by_division.keys()):
            print(f"\n{div_id}:")
            print("-" * 80)
            for asgn in by_division[div_id]:
                role_marker = " (asst)" if asgn.role == 'assistant' else ""
                print(f"  {asgn.subject.name:<40} → {asgn.teacher.name}{role_marker}")
        
        # Teacher load summary — count unique (teacher, subject_id) pairs so that
        # a subject taught across multiple divisions is counted only once.
        print("\n" + "="*80)
        print("TEACHER LOAD SUMMARY")
        print("="*80)

        # Step 1: collect unique subjects per teacher (ignore assistant roles)
        teacher_subjects: Dict[str, set] = defaultdict(set)
        teacher_divisions: Dict[str, set] = defaultdict(set)
        teacher_obj: Dict[str, Teacher] = {}
        for asgn in self.assignments:
            if asgn.role != 'main':
                continue
            teacher_subjects[asgn.teacher.name].add(
                (asgn.subject.subject_id, asgn.subject.subject_type)
            )
            teacher_divisions[asgn.teacher.name].add(asgn.division.division_id)
            teacher_obj[asgn.teacher.name] = asgn.teacher

        # Step 2: derive theory/lab counts from the deduplicated subject set
        teacher_loads = defaultdict(lambda: {'theory': 0, 'lab': 0, 'total': 0})
        for name, subj_set in teacher_subjects.items():
            for _sid, stype in subj_set:
                if stype == SubjectType.LAB:
                    teacher_loads[name]['lab'] += 1
                else:
                    teacher_loads[name]['theory'] += 1
            teacher_loads[name]['total'] = (
                teacher_loads[name]['theory'] + teacher_loads[name]['lab']
            )

        for name in sorted(teacher_loads.keys()):
            load  = teacher_loads[name]
            divs  = ", ".join(sorted(teacher_divisions[name]))
            t_obj = teacher_obj.get(name)
            weekly = t_obj.weekly_periods_used if t_obj else '?'
            cap    = 3 if (t_obj and t_obj.is_bsh) else 2
            print(f"{name:<30} Theory: {load['theory']}/{cap}  Lab: {load['lab']}/1  "
                  f"Weekly: {weekly}/{MAX_WEEKLY_PERIODS}   Divisions: {divs}")

        print("="*80 + "\n")

    # ─────────────────────────────────────────────────────────────────────────
    # Post-assignment passes
    # ─────────────────────────────────────────────────────────────────────────

    def _guarantee_preferred_subjects(self) -> None:
        """
        Ensure every non-BSH teacher with preferences gets at least one preferred
        subject assigned. Uses safe, atomic swaps with junior teachers (R2/S2).
        Called after the main 3-phase assignment loop.
        """
        # Build a map: subject_id -> list of main assignments
        subj_assignments: Dict[str, List[SubjectAssignment]] = defaultdict(list)
        for a in self.assignments:
            if a.role == 'main':
                subj_assignments[a.subject.subject_id].append(a)

        # Find non-BSH teachers with zero main assignments who have preferences
        deprived = [
            t for t in self.teachers
            if not t.is_bsh
            and not any(a.teacher.teacher_id == t.teacher_id and a.role == 'main'
                        for a in self.assignments)
            and t.priority_subjects
        ]

        for senior in deprived:
            # Try to find their best preferred subject
            for _, pref_subj_id in sorted(
                senior.priority_subjects, key=lambda x: x[1]
            ):
                asgns = subj_assignments.get(pref_subj_id, [])
                if not asgns:
                    continue

                # Check if a lower-seniority teacher holds this subject
                for asgn in asgns:
                    junior = asgn.teacher
                    if junior.teacher_id == senior.teacher_id:
                        break   # senior already has it somehow
                    # junior must be less senior (higher seniority_level number)
                    if junior.seniority_level <= senior.seniority_level:
                        continue  # junior is actually equally/more senior — skip
                    # Attempt safe swap
                    if self._safe_swap(senior, junior, asgn.subject, asgn.division):
                        log.info(
                            "Preferred-subject swap: %s → %s (%s)",
                            senior.name, asgn.subject.name, asgn.division.division_id,
                        )
                        # Rebuild subj_assignments map after swap
                        subj_assignments = defaultdict(list)
                        for a in self.assignments:
                            if a.role == 'main':
                                subj_assignments[a.subject.subject_id].append(a)
                        break

    def _safe_swap(
        self,
        senior: Teacher,
        junior: Teacher,
        subject: Subject,
        division: Division,
    ) -> bool:
        """
        Atomically swap `subject/division` from junior → senior, placing junior
        on an alternative subject. Aborts (returns False) if any constraint
        would be violated (S2/R2).
        """
        # Guard 1: senior must be able to take more theory/lab
        if subject.subject_type == SubjectType.LAB:
            if not senior.can_take_lab():
                return False
        else:
            if not senior.can_take_theory():
                return False
        # Guard 2: weekly cap
        if not senior.weekly_load_ok(subject.periods_per_week):
            return False
        # Guard 3: find a landing spot for the junior
        alt_subj, alt_div = self._find_alternative_for(junior, exclude_ids={subject.subject_id})
        if alt_subj is None:
            log.debug("Swap aborted: no alternative for junior %s", junior.name)
            return False
        # Guard 4: alt subject must not exceed junior's caps after reassignment
        if alt_subj.subject_type == SubjectType.LAB:
            if not junior.can_take_lab():
                return False
        else:
            if not junior.can_take_theory():
                return False
        if not junior.weekly_load_ok(alt_subj.periods_per_week):
            return False

        # Pre-swap snapshot for rollback
        snap_senior_load = dict(senior.assigned_load)
        snap_junior_load = dict(junior.assigned_load)
        snap_senior_wpu  = senior.weekly_periods_used
        snap_junior_wpu  = junior.weekly_periods_used
        snap_senior_sids = set(senior.assigned_theory_subject_ids)
        snap_junior_sids = set(junior.assigned_theory_subject_ids)

        # Guard: non-BSH teacher must not already have the new subject
        if not senior.is_bsh and subject.subject_id in senior.assigned_theory_subject_ids:
            return False
        if not junior.is_bsh and alt_subj.subject_id in junior.assigned_theory_subject_ids:
            return False

        # Execute swap atomically
        # 1. Reassign existing assignment to senior
        for a in self.assignments:
            if (a.subject.subject_id == subject.subject_id
                    and a.division.division_id == division.division_id
                    and a.teacher.teacher_id == junior.teacher_id
                    and a.role == 'main'):
                a.teacher = senior
                break
        # 2. Add alt assignment for junior
        self.assignments.append(SubjectAssignment(alt_subj, alt_div, junior, 'main'))

        # 3. Update loads and subject-ID sets
        load_key = 'lab' if subject.subject_type == SubjectType.LAB else 'theory'
        senior.assigned_load[load_key] = senior.assigned_load.get(load_key, 0) + 1
        senior.weekly_periods_used += subject.periods_per_week
        if load_key == 'theory' and not senior.is_bsh:
            senior.assigned_theory_subject_ids.add(subject.subject_id)

        alt_key = 'lab' if alt_subj.subject_type == SubjectType.LAB else 'theory'
        junior.assigned_load[alt_key] = junior.assigned_load.get(alt_key, 0) + 1
        junior.weekly_periods_used += alt_subj.periods_per_week
        if alt_key == 'theory' and not junior.is_bsh:
            junior.assigned_theory_subject_ids.add(alt_subj.subject_id)

        # 4. Validate — rollback if broken
        try:
            self._revalidate_after_change([senior, junior])
        except AssertionError as exc:
            log.warning("Swap rollback (%s): %s", subject.name, exc)
            # Rollback
            for a in self.assignments:
                if (a.subject.subject_id == subject.subject_id
                        and a.division.division_id == division.division_id
                        and a.teacher.teacher_id == senior.teacher_id
                        and a.role == 'main'):
                    a.teacher = junior
                    break
            self.assignments = [
                a for a in self.assignments
                if not (a.subject.subject_id == alt_subj.subject_id
                        and a.division.division_id == alt_div.division_id
                        and a.teacher.teacher_id == junior.teacher_id
                        and a.role == 'main')
            ]
            senior.assigned_load = snap_senior_load
            junior.assigned_load = snap_junior_load
            senior.weekly_periods_used = snap_senior_wpu
            junior.weekly_periods_used = snap_junior_wpu
            senior.assigned_theory_subject_ids = snap_senior_sids
            junior.assigned_theory_subject_ids = snap_junior_sids
            return False
        return True

    def _find_alternative_for(
        self,
        teacher: Teacher,
        exclude_ids: Optional[Set[str]] = None,
    ) -> Tuple[Optional[Subject], Optional[Division]]:
        """
        Find an unassigned preferred (or dept-matching) subject for `teacher`.
        Returns (subject, division) or (None, None).
        """
        exclude_ids = exclude_ids or set()
        assigned_subj_ids = {
            a.subject.subject_id
            for a in self.assignments
            if a.teacher.teacher_id == teacher.teacher_id and a.role == 'main'
        }
        # Preferred subjects first
        for _, pref_sid in sorted(teacher.priority_subjects, key=lambda x: x[1]):
            if pref_sid in exclude_ids or pref_sid in assigned_subj_ids:
                continue
            for subj in self.subjects:
                if subj.subject_id != pref_sid:
                    continue
                divs = self._get_relevant_divisions(subj)
                for div in divs:
                    already_assigned = any(
                        a.subject.subject_id == pref_sid
                        and a.division.division_id == div.division_id
                        and a.role == 'main'
                        for a in self.assignments
                    )
                    if not already_assigned:
                        return subj, div
        # Fallback: any unassigned dept subject
        for subj in self.subjects:
            if subj.subject_id in exclude_ids or subj.subject_id in assigned_subj_ids:
                continue
            if subj.teacher_dept_id and subj.teacher_dept_id != teacher.dept_id:
                continue
            if _is_additional_responsibility(subj):
                continue
            divs = self._get_relevant_divisions(subj)
            for div in divs:
                already_assigned = any(
                    a.subject.subject_id == subj.subject_id
                    and a.division.division_id == div.division_id
                    and a.role == 'main'
                    for a in self.assignments
                )
                if not already_assigned:
                    return subj, div
        return None, None

    def _pack_dummies(self) -> List[Teacher]:
        """
        Bin-pack every unassigned (subject, division) pair into dummy
        teachers (X1, X2, X3...), keeping their workloads as evenly
        distributed as possible rather than maxing out one dummy before
        starting the next.

        Dummy teachers are only created when no real teacher could be
        assigned due to reaching their teaching load limit (designation-based
        weekly cap or subject allocation limit).  Each dummy is capped at
        19 periods/week (the regular teacher cap) and the same
        2-theory + 1-lab load limit as real teachers, and is otherwise
        scheduled exactly like a real teacher (same downstream placement
        logic in the timetable generator).

        As many dummies as needed are created — there is no cap on count —
        so every remaining (subject, division) pair is guaranteed a teacher.
        This closes the "blank because no faculty was available" case: once
        this runs, the only reason a period can stay unplaced downstream is
        a genuine scheduling/time-slot conflict, never a missing teacher.
        """
        # Collect unassigned (subj, div) pairs
        assigned_set: Set[Tuple[str, str]] = {
            (a.subject.subject_id, a.division.division_id)
            for a in self.assignments
            if a.role == 'main'
        }
        unassigned: List[Tuple[Subject, Division]] = []
        for subj in self.subjects:
            if _is_additional_responsibility(subj):
                continue
            for div in self._get_relevant_divisions(subj):
                if (subj.subject_id, div.division_id) not in assigned_set:
                    unassigned.append((subj, div))

        if not unassigned:
            return []

        log.info(
            "_pack_dummies: %d unassigned (subject, division) pairs — "
            "creating dummy (temporary) teachers to ensure full timetable coverage.",
            len(unassigned),
        )

        # Sort: labs first (least flexible to place), then theory by period
        # count descending — placing the biggest chunks first gives the
        # load-balancer the most room to spread work evenly.
        unassigned.sort(
            key=lambda x: (0 if x[0].subject_type == SubjectType.LAB else 1,
                           -x[0].periods_per_week)
        )

        dummies: List[Teacher] = []
        dummy_counter: Dict[str, int] = defaultdict(int)
        dummies_by_dept: Dict[str, List[Teacher]] = defaultdict(list)

        # Dummy teachers use the regular teacher cap (19 periods/week).
        _DUMMY_WEEKLY_CAP = 19

        def _new_dummy(dept_id: str) -> Teacher:
            dummy_counter[dept_id] += 1
            n = dummy_counter[dept_id]
            d = Teacher(
                teacher_id=f"dummy-{dept_id}-{n}",
                name=f"TBD-{dept_id}-{n}",
                dept_id=dept_id,
                designation=Designation.LECTURER,
                is_dummy=True,
            )
            dummies.append(d)
            dummies_by_dept[dept_id].append(d)
            return d

        for subj, div in unassigned:
            dept = subj.teacher_dept_id or div.student_dept_id
            load_key = 'lab' if subj.subject_type == SubjectType.LAB else 'theory'
            cap = 1 if load_key == 'lab' else 2

            # Pick the LEAST-LOADED existing dummy in this dept that can
            # still absorb this subject — balances workload across X1, X2,
            # X3... instead of filling X1 to the brim before touching X2.
            candidates = [
                d for d in dummies_by_dept.get(dept, [])
                if d.assigned_load.get(load_key, 0) < cap
                and (d.weekly_periods_used + subj.periods_per_week) <= _DUMMY_WEEKLY_CAP
            ]
            target = (
                min(candidates, key=lambda d: d.weekly_periods_used)
                if candidates else _new_dummy(dept)
            )

            self.assignments.append(SubjectAssignment(subj, div, target, 'main'))
            target.assigned_load[load_key] = (
                target.assigned_load.get(load_key, 0) + 1
            )
            target.weekly_periods_used += subj.periods_per_week
            log.info(
                "  Dummy %s ← %s (%s)  [weekly=%d/%d]",
                target.teacher_id, subj.name, div.division_id,
                target.weekly_periods_used, _DUMMY_WEEKLY_CAP,
            )

        log.info("Dummy teachers created: %d", len(dummies))
        return dummies

    def _revalidate_after_change(self, changed_teachers: List[Teacher]) -> None:
        """
        Assert that no hard constraints are violated after any in-place
        modification of assignments. Raises AssertionError on violation (S13).
        """
        for t in changed_teachers:
            theory    = t.assigned_load.get('theory', 0)
            lab       = t.assigned_load.get('lab', 0)
            cap       = 3 if t.is_bsh else 2
            week_cap  = _max_periods_for_teacher(t)
            assert theory <= cap, \
                f"{t.name}: theory_load={theory} > {cap}"
            assert lab <= 1, \
                f"{t.name}: lab_load={lab} > 1"
            assert t.weekly_periods_used <= week_cap, \
                f"{t.name}: weekly_periods={t.weekly_periods_used} > {week_cap} (designation cap)"
            # Non-BSH: unique subjects count must match theory load
            if not t.is_bsh:
                assert len(t.assigned_theory_subject_ids) <= cap, \
                    f"{t.name}: unique theory subjects {len(t.assigned_theory_subject_ids)} > {cap}"


def validate_before_timetable(
    assignments: List[SubjectAssignment],
    teachers: List[Teacher],
    subjects: List[Subject],
    divisions: List[Division],
) -> None:
    """
    Blocking gate between Engine 1 and Engine 2. Raises ValueError if any
    hard constraint is violated in the assignment output (S14).

    Triggered automatically in app.py between engine1.assign()
    and tt_engine.generate(). Division coverage gaps trigger automatic
    dummy creation; workload/limit errors surface to the admin UI.
    """
    errors: List[str] = []

    # 1. Weekly workload — checked against each teacher's designation-specific cap
    for t in teachers:
        if getattr(t, 'is_dummy', False):
            continue
        week_cap = _max_periods_for_teacher(t)
        if t.weekly_periods_used > week_cap:
            errors.append(
                f"{t.name}: weekly_periods={t.weekly_periods_used} > {week_cap} "
                f"(cap for {getattr(t.designation, 'value', '?')})"
            )

    # 2. Subject limits
    for t in teachers:
        if getattr(t, 'is_dummy', False):
            continue
        theory = t.assigned_load.get('theory', 0)
        lab    = t.assigned_load.get('lab', 0)
        cap    = 3 if getattr(t, 'is_bsh', False) else 2
        if theory > cap:
            errors.append(f"{t.name}: theory_load={theory} exceeds cap={cap}")
        if lab > 1:
            errors.append(f"{t.name}: lab_load={lab} exceeds cap=1")

    # 2b. Non-BSH unique-subject check: same subject must not appear in multiple
    #     divisions for the same teacher.
    for t in teachers:
        if getattr(t, 'is_dummy', False) or getattr(t, 'is_bsh', False):
            continue
        # Build subject_id → list of division_ids for this teacher's main assignments
        subj_divs: Dict[str, List[str]] = defaultdict(list)
        for a in assignments:
            if a.teacher.teacher_id == t.teacher_id and a.role == 'main':
                if a.subject.subject_type not in (SubjectType.LAB, SubjectType.PROJECT):
                    if not _is_additional_responsibility(a.subject):
                        subj_divs[a.subject.subject_id].append(a.division.division_id)
        for sid, divs in subj_divs.items():
            if len(divs) > 1:
                subj_name = next(
                    (a.subject.name for a in assignments
                     if a.subject.subject_id == sid), sid
                )
                errors.append(
                    f"{t.name}: same subject '{subj_name}' assigned to multiple "
                    f"divisions {divs} — non-BSH teachers must have 2 DISTINCT subjects"
                )

    # 3. Division coverage (missing = unassigned and no dummy created yet)
    assigned_set: Set[Tuple[str, str]] = {
        (a.subject.subject_id, a.division.division_id)
        for a in assignments
        if a.role == 'main'
    }
    for subj in subjects:
        if _is_additional_responsibility(subj):
            continue
        expected_divs = [
            d for d in divisions
            if d.student_dept_id == subj.student_dept_id
            and d.semester == subj.semester
            and (not getattr(subj, 'applicable_division_tokens', []) or d.division_id in getattr(subj, 'applicable_division_tokens', []))
        ]
        for div in expected_divs:
            if (subj.subject_id, div.division_id) not in assigned_set:
                errors.append(
                    f"{subj.name} ({div.division_id}): no main teacher assigned"
                )

    if errors:
        msg = "Assignment validation FAILED:\n" + "\n".join(f"  • {e}" for e in errors)
        log.error(msg)
        raise ValueError(msg)

    log.info(
        "validate_before_timetable: PASSED (%d assignments, %d teachers)",
        len(assignments), len(teachers),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Database loader
# ─────────────────────────────────────────────────────────────────────────────

def load_from_db(db_path: str, student_dept: str, semester: int) -> SubjectAssignmentEngine:
    """
    Load data from SQLite database for a specific department and semester.
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Load departments
    cur.execute('SELECT id, name FROM departments')
    departments = [Department(dept_id=r[0], name=r[1]) for r in cur.fetchall()]

    # Load subjects for this student department and semester.
    # FIX: semester values may be stored as floats ("1.0", "3.0") in SQLite,
    # especially for Mechanical department. Normalise by converting to int so
    # "1.0" == "1", "3.0" == "3", etc.  We query using both the plain integer
    # string AND the float string to cover all storage variants.
    _sem_int = int(float(semester))   # e.g. 3.0 → 3
    cur.execute("""
        SELECT id, code, name, department, dept_id, type, hours_per_week, 
               semester, division, revision
        FROM courses
        WHERE department = ?
          AND (semester = ? OR semester = ? OR CAST(CAST(semester AS REAL) AS INTEGER) = ?)
    """, (student_dept, str(_sem_int), str(float(_sem_int)), _sem_int))

    subjects: List[Subject] = []
    for row in cur.fetchall():
        sid, code, name, dept, teacher_dept, stype, hours, sem, division, revision = row
        
        # Elective detection using TYPE column
        # Parse subject type
        stype_lower = (stype or 'theory').lower()
        name_lower  = (name or '').lower()
        if 'course work' in name_lower or stype_lower == 'cw':
            # e.g. "Comprehensive Course Work" (CAT308, CST308, EET308, ...).
            # Real DB data tags these TYPE='CORE'/'PCC (Core)' like ordinary
            # theory subjects, so name-based detection is required — there is
            # no dedicated TYPE value for this in the source spreadsheets.
            subject_type = SubjectType.CW
        elif ('project phase' in name_lower or 'seminar' in name_lower
                or 'mini project' in name_lower or 'miniproject' in name_lower):
            # Name-based project/seminar detection MUST come before the type
            # check below: real source data tags "Project Phase I"/"Project
            # Phase II" as TYPE='LAB' for AU, CE, CS, EC, and EEE (and as
            # TYPE='PCC (Core)'/'PWS (Core)' elsewhere) — never a dedicated
            # "project" type. If the 'lab' check below ran first, an 11-period
            # Project Phase II would be swept into the generic LAB placement
            # path, which requires ONE single 11-consecutive-period block
            # (impossible in a single day) instead of the correct chunked
            # 3+3+3+2 block placement, resulting in 0 periods ever placed.
            subject_type = SubjectType.PROJECT
        elif 'lab' in stype_lower:
            subject_type = SubjectType.LAB
        elif 'elective' in stype_lower or 'pec' in stype_lower:
            subject_type = SubjectType.ELECTIVE
        else:
            subject_type = SubjectType.THEORY

        # Parse hours (LTPR format: "L-T-P-R", e.g. "3-1-2-1")
        #   Theory / Elective / Project → periods = L + T + R
        #   Lab                          → periods = P  (practical hours)
        _hours_str = (hours or '').strip()
        _ltpr = re.match(r'^(\d+)-(\d+)-(\d+)(?:-(\d+))?', _hours_str)
        if _ltpr:
            _L = int(_ltpr.group(1))
            _T = int(_ltpr.group(2))
            _P = int(_ltpr.group(3))
            _R = int(_ltpr.group(4) or 0)
            if subject_type == SubjectType.LAB:
                periods = _P if _P > 0 else 3   # fallback: 3-period lab
            else:
                # FIX: use L+T+P+R (all four LTPR components) not L+T+R.
                # The old formula skipped _P (practical hours), causing theory
                # subjects like "Chemistry  5-0-2-0" to report only 5 periods
                # instead of the correct 7 — producing blank timetable slots.
                periods = _L + _T + _P + _R if (_L + _T + _P + _R) > 0 else 3
        else:
            try:
                periods = int(_hours_str.split()[0])
            except (ValueError, IndexError):
                periods = 3

        # Robust division token parsing using regex
        div_tokens = []
        if division:
            raw_tokens = re.split(r'[,&]', division)
            raw_tokens = [t.strip().strip('"') for t in raw_tokens if t.strip()]
            div_tokens = raw_tokens

        # Detect elective group from name
        elective_group = None
        if subject_type == SubjectType.ELECTIVE:
            # Extract group name from patterns like "Program Elective III - A"
            match = re.search(r'(.*?Elective\s+[IVX]+)', name, re.IGNORECASE)
            if match:
                elective_group = match.group(1).strip()

        subjects.append(Subject(
            subject_id=str(sid),
            code=code or '',
            name=name,
            subject_type=subject_type,
            student_dept_id=dept,
            teacher_dept_id=teacher_dept or '',
            semester=int(float(sem)) if sem is not None else 0,  # int(float()) handles "1.0" stored by ME dept
            periods_per_week=periods,
            applicable_division_tokens=div_tokens,
            elective_group=elective_group,
        ))

    # Link theory-lab companions
    # Strategy: use either SequenceMatcher similarity OR keyword containment.
    # The 0.7 threshold was too strict — e.g. "Data Structures and Algorithms"
    # vs "Data Structures Lab" scores ~0.69 and was incorrectly rejected.
    # New approach: match if similarity > 0.55 OR if the lab base name (lab name
    # with "Lab" stripped) is a substring of the theory name, or vice versa.
    for subj in subjects:
        if subj.subject_type == SubjectType.THEORY:
            for lab in subjects:
                if lab.subject_type == SubjectType.LAB:
                    theory_lower = subj.name.lower()
                    lab_lower    = lab.name.lower()

                    # Strip the word "lab" (and trailing punctuation) from the lab
                    # name to get a bare keyword like "data structures"
                    lab_base = re.sub(r'\blab\b\.?', '', lab_lower).strip()

                    # Keyword containment: "data structures" in "data structures and algorithms"
                    keyword_match = (
                        bool(lab_base)
                        and (lab_base in theory_lower or theory_lower.startswith(lab_base))
                    )

                    similarity = SequenceMatcher(None, theory_lower, lab_lower).ratio()

                    if similarity > 0.55 or keyword_match:
                        subj.companion_lab_id   = lab.subject_id
                        lab.companion_theory_id = subj.subject_id

    def _parse_joining_date(doj: str) -> Optional[int]:
        """
        Parse teacher.date_of_joining into an ordinal day number.
        Returns smaller values for earlier dates (more senior).
        """
        s = (doj or "").strip()
        if not s:
            return None
        # Try common formats stored by HTML date input or manual entry.
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
            try:
                return _dt.strptime(s, fmt).date().toordinal()
            except Exception:
                continue
        # Last resort: try ISO-ish prefix
        try:
            return _dt.fromisoformat(s[:10]).date().toordinal()
        except Exception:
            return None

    def _designation_rank(desig_l: str) -> int:
        """
        Higher priority -> lower rank number.
        Requested order (high→low):
          principal > deputy dean > hod > associate > assistant > professor > lecturer
        """
        d = desig_l or ""
        if "principal" in d:
            return 0
        if "deputy dean" in d or ("deputy" in d and "dean" in d):
            return 1
        if "hod" in d or "head of department" in d:
            return 2
        if "associate" in d:
            return 3
        if "assistant" in d:
            return 4
        if "professor" in d:
            return 5
        return 6

    # Load teachers
    cur.execute("""
        SELECT id, name, dept_id, designation, area_of_specialization, seniority_level, date_of_joining, code
        FROM teachers
        WHERE dept_id IS NOT NULL
    """)
    
    teachers: List[Teacher] = []
    for row in cur.fetchall():
        tid, tname, dept_id, desig, spec, seniority_level, date_of_joining, tcode = row
        
        # Parse designation — extended to cover Principal, Deputy Dean, HOD
        desig_l = (desig or "").lower()
        if "principal" in desig_l:
            designation = Designation.PRINCIPAL
        elif "deputy" in desig_l and "dean" in desig_l:
            designation = Designation.DEPUTY_DEAN
        elif "hod" in desig_l or "head of department" in desig_l:
            designation = Designation.HOD
        elif "associate" in desig_l:
            designation = Designation.ASSOCIATE_PROF
        elif "assistant" in desig_l:
            designation = Designation.ASSISTANT_PROF
        elif "professor" in desig_l:
            designation = Designation.PROFESSOR
        else:
            designation = Designation.LECTURER

        # Seniority: prefer explicit integer seniority_level from DB.
        # Otherwise compute from designation priority + date_of_joining.
        computed_seniority = 999_999
        try:
            if seniority_level is not None and str(seniority_level).strip() != "":
                computed_seniority = int(str(seniority_level).strip())
            else:
                raise ValueError()
        except Exception:
            doj_ord = _parse_joining_date(str(date_of_joining or ""))
            doj_ord = doj_ord if doj_ord is not None else 999_999
            computed_seniority = _designation_rank(desig_l) * 1_000_000 + doj_ord

        # Load preferences based on odd/even semester groups.
        #
        # ROOT-CAUSE FIX: The old query filtered tp.semester IN (1,3,5,7) using
        # integer literals.  The preferences table stores semester as VARCHAR with
        # values 'odd', 'even', or '' (blank = applies to all semesters).
        # Integer IN-list comparisons against string values ALWAYS return FALSE in
        # SQLite — meaning ZERO preferences were ever loaded for ANY teacher.
        #
        # Fixed filter accepts:
        #   'odd'         — teacher explicitly chose odd semesters
        #   'even'        — teacher explicitly chose even semesters
        #   '' / NULL     — teacher left it blank → treat as applicable to all
        #   '1','3',…     — legacy numeric string format (kept for safety)
        cur2 = conn.cursor()
        cur2.execute("""
            SELECT tp.course_code, tp.rank, tp.created_at, tp.id
            FROM teacher_preferences tp
            WHERE (
                tp.teacher_id = ?
                OR (
                    (tp.teacher_id IS NULL OR tp.teacher_id = '')
                    AND tp.teacher_code = (SELECT code FROM teachers WHERE id = ? LIMIT 1)
                )
            )
            AND (
                (? % 2 = 1 AND (
                    tp.semester IN ('odd', '1', '3', '5', '7')
                    OR tp.semester = '' OR tp.semester IS NULL
                ))
                OR
                (? % 2 = 0 AND (
                    tp.semester IN ('even', '2', '4', '6', '8')
                    OR tp.semester = '' OR tp.semester IS NULL
                ))
            )
            ORDER BY tp.rank ASC, tp.created_at DESC, tp.id DESC
        """, (tid, tid, semester, semester))
        
        pref_rows = cur2.fetchall()
        # Take the newest unique course codes; keep at most 3.
        priorities: List[Tuple[str, int]] = []
        seen_codes: Set[str] = set()
        for course_code, rank, _created_at, _pid in pref_rows:
            if not course_code:
                continue
            code_clean = str(course_code).strip()
            if not code_clean or code_clean in seen_codes:
                continue
            seen_codes.add(code_clean)
            try:
                rank_i = int(rank)
            except Exception:
                rank_i = 999
            priorities.append((code_clean, rank_i))
            if len(priorities) >= 3:
                break
        
        # Parse specializations
        specializations = [s.strip() for s in (spec or '').split(',') if s.strip()]

        # is_bsh: teacher belongs to the Basic Sciences & Humanities service dept
        is_bsh = bool(dept_id and 'BSH' in str(dept_id).upper())

        teachers.append(Teacher(
            teacher_id=str(tid),
            name=tname or '',
            dept_id=dept_id or '',
            designation=designation,
            seniority_level=computed_seniority,
            priority_subjects=priorities,
            specializations=specializations,
            is_bsh=is_bsh,
            code=str(tcode) if tcode else '',
        ))

    # Build divisions using division tokens directly
    seen: Set[str] = set()
    divisions: List[Division] = []
    for subj in subjects:
        for tok in subj.applicable_division_tokens:
            if tok not in seen:
                seen.add(tok)
                divisions.append(Division(
                    division_id=tok,  # Use token directly: "AI", "DS", "CS1", "CS2"
                    student_dept_id=student_dept,
                    semester=semester,
                ))

    if not divisions:
        divisions.append(Division(
            division_id="A",
            student_dept_id=student_dept,
            semester=semester,
        ))

    conn.close()

    # Validate semester load
    try:
        validate_semester_load(subjects, semester, raise_on_mismatch=False)
    except ValueError as e:
        log.warning("Load validation failed: %s", e)

    engine = SubjectAssignmentEngine()
    engine.load_data(teachers, subjects, departments, divisions)
    return engine


def validate_semester_load(
    subjects: List[Subject], semester: int, raise_on_mismatch: bool = False
) -> bool:
    """Validate that semester period load matches target."""
    TARGET = 34 if semester <= 6 else 29
    elective_group_counted: set = set()
    total = 0
    for s in subjects:
        if s.elective_group:
            if s.elective_group not in elective_group_counted:
                elective_group_counted.add(s.elective_group)
                total += s.periods_per_week
        else:
            total += s.periods_per_week

    # When multiple divisions are represented by separate subject rows, the
    # total load may legitimately exceed TARGET. In that case, validate only
    # against a reasonable range rather than insisting on a single 34/29 value.
    division_sets = []
    all_tokens = set()
    for s in subjects:
        toks = tuple(sorted(s.applicable_division_tokens)) if s.applicable_division_tokens else None
        if toks:
            all_tokens.update(toks)
        division_sets.append(toks)

    if any(t is None for t in division_sets) and all_tokens:
        division_sets = [tuple(sorted(all_tokens)) if t is None else t for t in division_sets]

    expect_exact = bool(division_sets and len({tuple(t) for t in division_sets}) == 1)
    if expect_exact:
        expected_low = expected_high = TARGET
    else:
        expected_low = TARGET
        expected_high = TARGET * max(1, len(all_tokens))

    is_valid = (expected_low <= total <= expected_high)
    status = 'OK' if is_valid else f'MISMATCH (expected {expected_low}-{expected_high}, got {total})'
    log.info('Semester %d load: %d periods — %s', semester, total, status)

    if not is_valid:
        msg = (
            f'Load mismatch for semester {semester}: got {total}, expected {expected_low}-{expected_high}. '
            f'Check HOURS_PER_WEEK values.'
        )
        if raise_on_mismatch:
            raise ValueError(msg)
        log.warning(msg)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Sample data factory (for standalone testing)
# ─────────────────────────────────────────────────────────────────────────────

def build_sample_data():
    """Build sample test data."""
    departments = [
        Department("CS",  "Computer Science"),
        Department("EC",  "Electronics & Communication"),
        Department("BSH", "Basic Sciences & Humanities"),
    ]
    divisions = [
        Division("CS1", student_dept_id="CS", semester=3),
        Division("CS2", student_dept_id="CS", semester=3),
        Division("A",   student_dept_id="CS", semester=5),
        Division("B",   student_dept_id="CS", semester=5),
        Division("A",   student_dept_id="EC", semester=3),
    ]
    subjects = [
        Subject("CS301",  "Data Structures & Algorithms", SubjectType.THEORY,
                student_dept_id="CS", teacher_dept_id="CS",
                semester=3, periods_per_week=3, companion_lab_id="CS301L",
                code="CS301", applicable_division_tokens=["CS1", "CS2"]),
        Subject("CS301L", "Data Structures Lab",          SubjectType.LAB,
                student_dept_id="CS", teacher_dept_id="CS",
                semester=3, periods_per_week=2, companion_theory_id="CS301",
                code="CS301L", applicable_division_tokens=["CS1", "CS2"]),
        Subject("CS302",  "Operating Systems",            SubjectType.THEORY,
                student_dept_id="CS", teacher_dept_id="CS",
                semester=3, periods_per_week=3,
                code="CS302", applicable_division_tokens=["CS1", "CS2"]),
        Subject("BSH301", "Discrete Mathematics",         SubjectType.THEORY,
                student_dept_id="CS", teacher_dept_id="BSH",
                semester=3, periods_per_week=3,
                code="BSH301", applicable_division_tokens=["CS1", "CS2"]),
    ]
    teachers = [
        Teacher("T001", "Dr. Arun Kumar",    "CS",  Designation.ASSOCIATE_PROF,
                priority_subjects=[("CS301", 1), ("CS302", 2)],
                specializations=["algorithms", "networks"]),
        Teacher("T002", "Prof. Priya Nair",  "CS",  Designation.PROFESSOR,
                priority_subjects=[("CS302", 1)],
                specializations=["operating systems"]),
        Teacher("T003", "Ms. Divya Raj",     "CS",  Designation.ASSISTANT_PROF,
                priority_subjects=[("CS301L", 1)],
                specializations=["lab", "compiler"]),
        Teacher("T004", "Dr. Suresh Menon",  "BSH", Designation.ASSOCIATE_PROF,
                priority_subjects=[("BSH301", 1)],
                specializations=["mathematics"]),
    ]
    return teachers, subjects, departments, divisions


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    db_path = os.path.join(os.path.dirname(__file__), "timetable.db")

    if os.path.exists(db_path):
        print("Loading from database...\n")
        engine = load_from_db(db_path, "CS", 3)
        assignments = engine.assign()
        engine.print_summary()
    else:
        print("Database not found — using sample data.\n")
        teachers, subjects, departments, divisions = build_sample_data()
        engine = SubjectAssignmentEngine()
        engine.load_data(teachers, subjects, departments, divisions)
        assignments = engine.assign()
        engine.print_summary()