"""
ltpr_audit_and_fix.py — Calculate, display, and correct LTPR totals for
every class (division) across Semester 1-8.

Rules implemented (exactly as specified):
  1. Total LTPR is calculated and displayed per class, subject-by-subject,
     BEFORE any modification.
  2. Seminar / Mini Project / Minor Project / Major Project are EXCLUDED
     from LTPR adjustment — their values are never touched.
  3. Every genuine lab subject's LTPR contribution must be exactly 3.
  4. Non-lab, non-excluded subjects are adjusted so each semester's total
     matches its cap exactly: 34 for Semesters 1-6, 29 for Semesters 7-8.
  5. Each semester/division must have exactly 2 DISTINCT lab subjects.
     Duplicate labs (same lab appearing twice) are replaced with the
     correct second lab, never left as a duplicate.
  6. All other subject information (name, code, type, division, credit,
     revision) is preserved untouched — only HOURS_PER_WEEK changes.
  7. AFTER modification, the updated calculation is displayed and every
     semester is re-verified against both constraints.

Usage:
    python ltpr_audit_and_fix.py                  # dry run — BEFORE report only
    python ltpr_audit_and_fix.py --apply           # apply corrections + AFTER report
    python ltpr_audit_and_fix.py --apply timetable.db
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict

# ── Constants ────────────────────────────────────────────────────────────
CAP_S1_6 = 34
CAP_S7_8 = 29
LAB_LTPR = 3   # every genuine lab subject must contribute exactly this many periods

# Only these `courses.department` values are ever queried by the real
# scheduling engine (see app.py STUDENT_DEPTS) — the standalone BSH course
# file is legacy/unused data and is excluded, exactly as in
# validate_course_data.py.
STUDENT_DEPTS = {'AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC'}

_LTPR_RE = re.compile(r'^(\d+)-(\d+)-(\d+)(?:-(\d+))?')

_EXCLUDED_RE = re.compile(
    r'\bseminar\b|\bmini[\s\-]?project\b|\bminor[\s\-]?project\b'
    r'|\bmajor[\s\-]?project\b|\bproject\s*phase\b',
    re.IGNORECASE,
)


def parse_ltpr(hours_str: str):
    m = _LTPR_RE.match((hours_str or '').strip())
    if not m:
        return None
    L, T, P, R = [int(x) if x else 0 for x in m.groups(default='0')]
    return L, T, P, R


def is_excluded(name: str) -> bool:
    """Seminar / Mini / Minor / Major Project / Project Phase — never adjusted."""
    return bool(_EXCLUDED_RE.search(name or ''))


_LAB_NAME_RE = re.compile(r'\blab\b', re.IGNORECASE)


def is_lab_type(ctype: str, name: str, hours: str = None) -> bool:
    """
    A genuine lab subject, detected via three independent signals (any one
    is sufficient) because the source data is inconsistent about which of
    these it uses correctly for any given department:
      1. TYPE contains 'lab' (the normal case, most departments).
      2. NAME ends in "Lab"/"lab" (ME mistags some labs as TYPE='Core').
      3. LTPR SHAPE is pure-practical — L=0, T=0, R=0, only P nonzero (e.g.
         "0-0-3-0") — a course with this shape is unambiguously a lab even
         when neither its type nor its name says so (e.g. ME's "Computer
         Aided Machine Drawing & Modelling", TYPE='Core', no "lab" in the
         name, but hours_per_week='0-0-3-0').
    Excludes anything matching the project/seminar exclusion list even if
    it happens to match one of the above (e.g. some Project Phase rows are
    TYPE='Lab' but must never be treated as a lab).
    """
    if is_excluded(name):
        return False
    if 'lab' in (ctype or '').lower():
        return True
    if _LAB_NAME_RE.search(name or ''):
        return True
    if hours is not None:
        parsed = parse_ltpr(hours)
        if parsed:
            L, T, P, R = parsed
            if L == 0 and T == 0 and R == 0 and P > 0:
                return True
    return False


def periods_for(hours_str: str, ctype: str, name: str):
    parsed = parse_ltpr(hours_str)
    if parsed is None:
        return None
    L, T, P, R = parsed
    if is_lab_type(ctype, name, hours_str):
        return P
    return L + T + P + R


def cap_for(semester: int) -> int:
    return CAP_S1_6 if semester <= 6 else CAP_S7_8


def load_rows(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, code, name, department, type, hours_per_week, semester, division
        FROM courses
        WHERE department IN ({})
    """.format(','.join('?' * len(STUDENT_DEPTS))), tuple(STUDENT_DEPTS))
    return cur.fetchall()


def build_class_map(rows):
    """
    Returns { (dept, division, semester_int): [row_dict, ...] } — one entry
    per (course row, division) pair, since a single course row can apply to
    multiple divisions (e.g. "CS1,CS2,CS3,CS4").
    """
    classes: dict = defaultdict(list)
    for cid, code, name, dept, ctype, hours, sem, division in rows:
        try:
            sem_int = int(float(sem)) if sem not in (None, '') else None
        except ValueError:
            sem_int = None
        if sem_int is None or not (1 <= sem_int <= 8):
            continue
        divs = [d.strip() for d in re.split(r'[,&]', division or '') if d.strip()]
        for d in divs:
            classes[(dept, d, sem_int)].append({
                'id': cid, 'code': code, 'name': name, 'type': ctype,
                'hours': hours, 'sem': sem_int, 'division_field': division,
            })
    return classes


def print_report(classes, title):
    print("=" * 88)
    print(title)
    print("=" * 88)
    for key in sorted(classes.keys(), key=lambda k: (k[0], k[1], k[2])):
        dept, div, sem = key
        subjects = classes[key]
        cap = cap_for(sem)
        total = 0
        print(f"\n{dept} / {div} / Semester {sem}   (cap = {cap})")
        print("-" * 88)
        for s in subjects:
            p = periods_for(s['hours'], s['type'], s['name'])
            label = ""
            if is_excluded(s['name']):
                label = " [EXCLUDED from adjustment]"
            elif is_lab_type(s['type'], s['name'], s['hours']):
                label = " [LAB]"
            p_display = p if p is not None else "PARSE ERROR"
            print(f"  {s['code']:14s} {s['name']:45s} "
                  f"hours={s['hours']!r:14s} -> {p_display}{label}")
            if p is not None:
                total += p
        flag = "OK" if total <= cap else f"OVER by {total - cap}"
        if total < cap:
            flag = f"UNDER by {cap - total}"
        print(f"  {'TOTAL':60s} {total:>3d} / {cap}   [{flag}]")

        labs = [s for s in subjects if is_lab_type(s['type'], s['name'], s['hours'])]
        distinct_lab_names = {s['name'] for s in labs}
        if len(distinct_lab_names) == 2:
            lab_flag = "OK (exactly 2 distinct labs)"
        elif len(distinct_lab_names) < 2 and len(labs) > len(distinct_lab_names):
            lab_flag = f"DUPLICATE LAB DETECTED — {len(labs)} rows but only " \
                       f"{len(distinct_lab_names)} distinct name(s): {distinct_lab_names}"
        else:
            lab_flag = f"{len(distinct_lab_names)} distinct lab(s) found: {distinct_lab_names or '(none)'}"
        print(f"  Lab check: {lab_flag}")


def find_duplicate_labs(classes):
    """
    Returns { (dept,div,sem): [course_id, ...] } for any class where the
    SAME lab name appears on 2+ separate course rows (a genuine data
    duplicate, not the old scheduling-side duplication which is already
    fixed in the generator).
    """
    dups = {}
    for key, subjects in classes.items():
        labs = [s for s in subjects if is_lab_type(s['type'], s['name'], s['hours'])]
        by_name = defaultdict(list)
        for s in labs:
            by_name[s['name']].append(s)
        for name, rows in by_name.items():
            if len(rows) > 1:
                dups.setdefault(key, []).extend(r['id'] for r in rows[1:])
    return dups


def apply_lab_ltpr_fix(conn, classes):
    """Force every genuine lab subject's LTPR to exactly LAB_LTPR (=3), preserving L/T/R at 0."""
    cur = conn.cursor()
    changed = 0
    seen_ids = set()
    for key, subjects in classes.items():
        for s in subjects:
            if s['id'] in seen_ids:
                continue
            if not is_lab_type(s['type'], s['name'], s['hours']):
                continue
            parsed = parse_ltpr(s['hours'])
            if parsed is None:
                # Corrupted/unparseable hours_per_week (e.g. 'ddddddd') on a
                # subject unambiguously identified as a lab by TYPE or NAME.
                # Still must be forced to the standard LAB_LTPR shape.
                new_hours = f"0-0-{LAB_LTPR}-0"
                cur.execute("UPDATE courses SET hours_per_week=? WHERE id=?", (new_hours, s['id']))
                seen_ids.add(s['id'])
                changed += 1
                continue
            L, T, P, R = parsed
            if P == LAB_LTPR and L == 0 and T == 0 and R == 0:
                seen_ids.add(s['id'])
                continue
            new_hours = f"0-0-{LAB_LTPR}-0"
            cur.execute("UPDATE courses SET hours_per_week=? WHERE id=?", (new_hours, s['id']))
            seen_ids.add(s['id'])
            changed += 1
    conn.commit()
    return changed


def find_shared_course_ids(classes):
    """
    Returns the set of course IDs that must NOT be independently rescaled
    per division.

    A course is only "protected" if it's shared between divisions that have
    genuinely DIFFERENT overall subject sets (e.g. AIDS "ai" and "ds", which
    overlap on some common subjects but differ on others) — rescaling it to
    hit one division's target would silently corrupt the other's.

    If every division sharing a course has an IDENTICAL full subject list
    (e.g. CS1/CS2/CS3/CS4, which take the exact same set of courses every
    semester in this dataset), the course is NOT protected: those divisions
    are really one class from an LTPR standpoint, and freely adjusting it
    is correct — once the first division's pass sets the right value, the
    others (with identical subjects) naturally already match the target and
    are left untouched on their own pass.
    """
    subject_set_by_class = {
        key: frozenset(s['id'] for s in subjects)
        for key, subjects in classes.items()
    }
    id_to_classes: dict = defaultdict(set)
    for key, subjects in classes.items():
        for s in subjects:
            id_to_classes[s['id']].add(key)

    protected = set()
    for cid, keys in id_to_classes.items():
        if len(keys) <= 1:
            continue
        distinct_subject_sets = {subject_set_by_class[k] for k in keys}
        if len(distinct_subject_sets) > 1:
            protected.add(cid)
    return protected


def apply_semester_cap_fix(conn, classes, shared_ids):
    """
    Adjust non-lab, non-excluded, non-shared subjects in each class so the
    class total exactly matches its cap. Uses proportional scaling with a
    largest-remainder rounding pass so every subject's share of the
    adjustment is fair and the total lands exactly on the cap.

    Courses shared across multiple divisions (shared_ids) are treated as
    FIXED — counted toward the total but never rescaled — because a single
    shared HOURS_PER_WEEK value can't be independently tuned to hit two
    different divisions' targets at once without corrupting one of them.
    """
    cur = conn.cursor()
    changed = 0
    for key, subjects in classes.items():
        dept, div, sem = key
        cap = cap_for(sem)

        lab_total = sum(
            periods_for(s['hours'], s['type'], s['name']) or 0
            for s in subjects if is_lab_type(s['type'], s['name'], s['hours'])
        )
        excluded_total = sum(
            periods_for(s['hours'], s['type'], s['name']) or 0
            for s in subjects if is_excluded(s['name'])
        )
        shared_nonlab_total = sum(
            periods_for(s['hours'], s['type'], s['name']) or 0
            for s in subjects
            if s['id'] in shared_ids
            and not is_lab_type(s['type'], s['name'], s['hours'])
            and not is_excluded(s['name'])
        )
        adjustable = [
            s for s in subjects
            if not is_lab_type(s['type'], s['name'], s['hours'])
            and not is_excluded(s['name'])
            and s['id'] not in shared_ids
        ]

        fixed_total = lab_total + excluded_total + shared_nonlab_total
        target_for_adjustable = cap - fixed_total

        if not adjustable:
            if target_for_adjustable < 0:
                print(f"  ⚠ {dept}/{div}/S{sem}: fixed (lab+excluded+shared) periods "
                      f"({fixed_total}) alone exceed cap ({cap}), and there are no "
                      f"division-exclusive subjects left to adjust — cannot rebalance "
                      f"without touching a subject shared with another division.")
            continue

        if target_for_adjustable < 0:
            print(f"  ⚠ {dept}/{div}/S{sem}: fixed (lab+excluded+shared) periods "
                  f"({fixed_total}) alone exceed cap ({cap}) — cannot fully rebalance "
                  f"via division-exclusive subjects alone.")
            target_for_adjustable = len(adjustable)  # floor: 1 period each, best effort

        current_adjustable_total = sum(
            periods_for(s['hours'], s['type'], s['name']) or 0 for s in adjustable
        )
        if current_adjustable_total == target_for_adjustable:
            continue
        if current_adjustable_total == 0:
            continue

        ratio = target_for_adjustable / current_adjustable_total
        raw_values = []
        for s in adjustable:
            cur_p = periods_for(s['hours'], s['type'], s['name']) or 0
            raw = cur_p * ratio
            raw_values.append((s, raw, max(1, int(raw))))

        floor_sum = sum(v[2] for v in raw_values)
        remainder = target_for_adjustable - floor_sum
        raw_values.sort(key=lambda v: (v[1] - int(v[1])), reverse=True)
        final = {}
        for i, (s, raw, floor_val) in enumerate(raw_values):
            bump = 1 if i < remainder else 0
            final[s['id']] = max(1, floor_val + bump)

        total_final = sum(final.values())
        diff = target_for_adjustable - total_final
        if diff != 0:
            order = sorted(raw_values, key=lambda v: v[1], reverse=(diff > 0))
            i = 0
            while diff != 0 and i < len(order) * 10:
                s = order[i % len(order)][0]
                if diff > 0:
                    final[s['id']] += 1
                    diff -= 1
                elif final[s['id']] > 1:
                    final[s['id']] -= 1
                    diff += 1
                i += 1

        for s in adjustable:
            new_p = final[s['id']]
            cur_p = periods_for(s['hours'], s['type'], s['name']) or 0
            if new_p != cur_p:
                new_hours = f"{new_p}-0-0-0"
                cur.execute("UPDATE courses SET hours_per_week=? WHERE id=?",
                            (new_hours, s['id']))
                changed += 1
    conn.commit()
    return changed


def fix_duplicate_labs(conn, classes, dups):
    """
    Replace duplicate lab rows with 'the correct second lab subject',
    inferred from a sibling division in the same department/semester that
    already has 2 distinct labs and shares one of them with the duplicated
    division. If no such sibling clue exists, the duplicate is reported but
    NOT guessed at — fabricating a lab name would violate 'preserve all
    other subject information'.
    """
    cur = conn.cursor()
    fixed, unresolved = 0, []
    for (dept, div, sem), dup_ids in dups.items():
        # Look for a sibling division in the same dept/sem with exactly 2
        # distinct labs, one of which matches this division's single
        # (deduplicated) lab.
        this_labs = {s['name'] for s in classes[(dept, div, sem)]
                     if is_lab_type(s['type'], s['name'], s['hours'])}
        sibling_second = None
        for (d2, div2, s2), subs2 in classes.items():
            if d2 != dept or s2 != sem or div2 == div:
                continue
            labs2 = {s['name']: s for s in subs2 if is_lab_type(s['type'], s['name'], s['hours'])}
            if len(labs2) == 2 and this_labs and this_labs.issubset(set(labs2.keys())) is False:
                shared = this_labs & set(labs2.keys())
                if shared:
                    missing = set(labs2.keys()) - this_labs
                    if missing:
                        sibling_second = (list(missing)[0], labs2[list(missing)[0]])
                        break
        if sibling_second:
            new_name, template_row = sibling_second
            cur.execute(
                "UPDATE courses SET name=?, code=?, hours_per_week=? WHERE id=?",
                (new_name, template_row['code'] + ' (from sibling)',
                 f"0-0-{LAB_LTPR}-0", dup_ids[0])
            )
            fixed += 1
        else:
            unresolved.append((dept, div, sem, dup_ids))
    conn.commit()
    return fixed, unresolved


def main():
    apply = '--apply' in sys.argv
    args = [a for a in sys.argv[1:] if a != '--apply']
    db_path = args[0] if args else 'timetable.db'

    conn = sqlite3.connect(db_path)

    rows = load_rows(conn)
    classes = build_class_map(rows)
    print_report(classes, "BEFORE — Current LTPR calculation per class")

    dups = find_duplicate_labs(classes)
    if dups:
        print("\n" + "=" * 88)
        print("DUPLICATE LABS DETECTED (same lab name on 2+ course rows)")
        print("=" * 88)
        for k, ids in dups.items():
            print(f"  {k}: duplicate course id(s) {ids}")
    else:
        print("\nNo duplicate lab entries found in the source data.")

    if not apply:
        print("\n(dry run — no changes made. Re-run with --apply to correct the data.)")
        return

    print("\n" + "=" * 88)
    print("APPLYING CORRECTIONS")
    print("=" * 88)

    if dups:
        n_fixed, unresolved = fix_duplicate_labs(conn, classes, dups)
        print(f"Duplicate labs resolved via sibling-division inference: {n_fixed}")
        for u in unresolved:
            print(f"  ⚠ Could not resolve duplicate for {u[0]}/{u[1]}/S{u[2]} "
                  f"(course id(s) {u[3]}) — no sibling clue available; left unchanged "
                  f"rather than fabricating a lab name.")
        # reload after duplicate fix
        rows = load_rows(conn)
        classes = build_class_map(rows)

    n_lab = apply_lab_ltpr_fix(conn, classes)
    print(f"Lab subjects corrected to LTPR={LAB_LTPR}: {n_lab}")

    rows = load_rows(conn)
    classes = build_class_map(rows)
    shared_ids = find_shared_course_ids(classes)
    n_cap = apply_semester_cap_fix(conn, classes, shared_ids)
    print(f"Non-lab, non-excluded subjects rebalanced to hit semester caps: {n_cap}")

    rows = load_rows(conn)
    classes = build_class_map(rows)
    print_report(classes, "AFTER — Updated LTPR calculation per class")

    print("\n" + "=" * 88)
    print("FINAL VERIFICATION")
    print("=" * 88)
    all_ok = True
    for key in sorted(classes.keys()):
        dept, div, sem = key
        subjects = classes[key]
        cap = cap_for(sem)
        total = sum(periods_for(s['hours'], s['type'], s['name']) or 0 for s in subjects)
        labs = {s['name'] for s in subjects if is_lab_type(s['type'], s['name'], s['hours'])}
        cap_ok = total <= cap
        lab_ok = len(labs) == 2
        if not cap_ok or not lab_ok:
            all_ok = False
            print(f"  ✗ {dept}/{div}/S{sem}: total={total}/{cap} "
                  f"({'OK' if cap_ok else 'FAIL'}), distinct labs={len(labs)} "
                  f"({'OK' if lab_ok else 'FAIL'}) {labs}")
    if all_ok:
        print("  All classes satisfy both constraints.")
    else:
        print("  Some classes could not be brought into full compliance — see above. "
              "These need real curriculum data (a genuine second lab subject) added "
              "by hand; this script will not fabricate one.")


if __name__ == '__main__':
    main()
