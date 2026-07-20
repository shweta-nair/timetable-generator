import sqlite3
import re
import csv
import sys
from collections import defaultdict

DB = 'timetable.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()

# Regex
_LTPR_RE = re.compile(r'^(\d+)-(\d+)-(\d+)(?:-(\d+))?')
_PROJECT_RE = re.compile(r'\b(project(\s*phase\s*[\divx]+)?|seminar|mini[\s\-]?project|miniproject)\b', re.IGNORECASE)

STUDENT_DEPTS = ('AU','CE','EEE','ME','CS','AIDS','EC')

def parse_ltpr(s):
    if not s:
        return None
    m = _LTPR_RE.match(s.strip())
    if not m:
        return None
    L = int(m.group(1)); T = int(m.group(2)); P = int(m.group(3)); R = int(m.group(4) or 0)
    return L,T,P,R

def periods_from_row(hours, ctype, name):
    parsed = parse_ltpr(hours)
    if parsed is None:
        return None
    L,T,P,R = parsed
    # Projects/seminars counted as full L+T+P+R
    if 'project' in (ctype or '').lower() or _PROJECT_RE.search(name or ''):
        return L+T+P+R
    # lab types use P
    if 'lab' in (ctype or '').lower():
        return P
    return L+T+P+R

# Build list of S7/S8 divisions that are student-facing
cur.execute("SELECT DISTINCT department, division FROM courses WHERE department IN ({})".format(','.join('?'*len(STUDENT_DEPTS))), STUDENT_DEPTS)
rows = cur.fetchall()
# Gather divisions by department and semester later
# We'll compute required LTPR per (department, division, semester)

# Query all courses for student depts
cur.execute("SELECT id, code, name, department, type, hours_per_week, semester, division FROM courses WHERE department IN ({})".format(','.join('?'*len(STUDENT_DEPTS))), STUDENT_DEPTS)
all_rows = cur.fetchall()

# Build per-division-sem class mapping
class_map = defaultdict(list)  # key -> list of rows
for cid, code, name, dept, ctype, hours, sem, division in all_rows:
    try:
        sem_i = int(float(sem))
    except Exception:
        continue
    if sem_i not in (7,8):
        continue
    divs = [d.strip() for d in re.split(r'[,&]', division or '') if d.strip()]
    for d in divs:
        class_map[(dept, d, sem_i)].append({'id':cid,'code':code,'name':name,'type':ctype,'hours':hours})

# Compute required totals and build subject lists
report_rows = []
for key in sorted(class_map.keys()):
    dept, div, sem = key
    subjects = class_map[key]
    total_required = 0
    subj_details = []
    for s in subjects:
        p = periods_from_row(s['hours'], s['type'], s['name'])
        subj_details.append((s['code'], s['name'], s['type'], s['hours'], p))
        if p is not None:
            total_required += p
    report_rows.append({'dept':dept,'division':div,'semester':sem,'required':total_required,'subjects':subj_details})

# Now load timetable generator to produce schedules and count scheduled periods per division+subject.
# Use existing generate_from_db helper if available.
try:
    from timetable_generator_engine import generate_from_db
except Exception:
    generate_from_db = None

scheduled_counts = defaultdict(lambda: defaultdict(int))  # (dept,div,sem) -> code -> count
missing_generator = False
if generate_from_db is None:
    missing_generator = True
else:
    # For each dept/semester, generate full assignments and count per-division
    depts_sem = sorted(set((r['dept'], r['semester']) for r in report_rows))
    for dept, sem in depts_sem:
        try:
            gen = generate_from_db(DB, dept, sem)
        except Exception as e:
            print('Generator failed for',dept,sem,'error',e,file=sys.stderr)
            continue
        # gen has class_timetable or class assignments; inspect its class_timetable if present
        # The engine in this project exposes generate(). After generate, engine.class_timetable[division][day][period] maps to assignment
        try:
            tt = gen
            # If gen is a TimetableGeneratorEngine, it may have .class_timetable
            if hasattr(tt, 'class_timetable'):
                # class_timetable: div -> day -> list of periods with subject codes
                for div in list(tt.class_timetable.keys()):
                    # div might be like 'CS1' or 'CS1/..' — we match exact tokens
                    if not any(r['dept']==dept and r['division']==div and r['semester']==sem for r in report_rows):
                        # However, our report_rows divisions might be tokens; proceed when division matches token
                        pass
                # Count periods per subject per division
                for div, daymap in tt.class_timetable.items():
                    # find matching division tokens in report_rows for this dept and sem
                    for rec in [rr for rr in report_rows if rr['dept']==dept and rr['semester']==sem and rr['division']==div]:
                        # count scheduled occurrences for this division
                        subjcount = defaultdict(int)
                        for day, periods in daymap.items():
                            for cell in periods:
                                if not cell:
                                    continue
                                # cell may have 'code' or similar
                                subj = None
                                if isinstance(cell, dict):
                                    subj = cell.get('code') or cell.get('subject_code') or cell.get('subject')
                                else:
                                    subj = str(cell)
                                if subj:
                                    subjcount[subj] += 1
                        for code, cnt in subjcount.items():
                            scheduled_counts[(dept,div,sem)][code] += cnt
            else:
                # try alternative interface: gen.generate() returns engine with .class_timetable
                try:
                    gen.generate()
                    tt = gen
                    if hasattr(tt, 'class_timetable'):
                        for div, daymap in tt.class_timetable.items():
                            for rec in [rr for rr in report_rows if rr['dept']==dept and rr['semester']==sem and rr['division']==div]:
                                subjcount = defaultdict(int)
                                for day, periods in daymap.items():
                                    for cell in periods:
                                        if not cell:
                                            continue
                                        subj = None
                                        if isinstance(cell, dict):
                                            subj = cell.get('code') or cell.get('subject_code') or cell.get('subject')
                                        else:
                                            subj = str(cell)
                                        if subj:
                                            subjcount[subj] += 1
                                for code, cnt in subjcount.items():
                                    scheduled_counts[(dept,div,sem)][code] += cnt
                except Exception as e:
                    print('Generator run failed for',dept,sem,'error',e,file=sys.stderr)
        except Exception as e:
            print('Error processing generated timetable for',dept,sem,e,file=sys.stderr)

# Produce CSV report
outf = 's7_s8_audit_report.csv'
with open(outf, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['department','division','semester','subject_code','subject_name','type','hours_db','required_periods','scheduled_periods','missing_periods'])
    for rec in report_rows:
        dept = rec['dept']; div = rec['division']; sem = rec['semester']; req_total = rec['required']
        counts = scheduled_counts.get((dept,div,sem), {})
        for code, name, ctype, hours, p in rec['subjects']:
            scheduled = counts.get(code, 0)
            missing = (p or 0) - scheduled if p is not None else ''
            w.writerow([dept,div,sem,code,name,ctype,hours,(p or ''),scheduled,missing])

print('Report written to',outf)
if missing_generator:
    print('Note: timetable generator helper not found; scheduled counts may be empty.')
conn.close()
