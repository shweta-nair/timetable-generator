import sqlite3
import re

DB = 'timetable.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()
rows = cur.execute(
    "SELECT code, name, type, hours_per_week, department, semester, division "
    "FROM courses WHERE department IN ('AU','CE','EEE','ME','CS','AIDS','EC')"
).fetchall()

cap = lambda sem: 34 if sem <= 6 else 29

def parse_ltpr(hours):
    m = re.match(r'^(\d+)-(\d+)-(\d+)(?:-(\d+))?', (hours or '').strip())
    if not m:
        return None
    return [int(x) if x else 0 for x in m.groups(default='0')]

def periods_for(hours, ctype, name):
    parsed = parse_ltpr(hours)
    if parsed is None:
        return None
    L, T, P, R = parsed
    if 'elective' in (ctype or '').lower() or 'pec' in (ctype or '').lower():
        return L + T + P + R
    if 'lab' in (ctype or '').lower() or (L == 0 and T == 0 and R == 0 and P > 0):
        return P
    return L + T + P + R

classes = {}
for code, name, ctype, hours, dept, sem, division in rows:
    try:
        sem_i = int(float(sem))
    except Exception:
        continue
    divs = [d.strip() for d in re.split(r'[,&]', division or '') if d.strip()]
    for div in divs:
        classes.setdefault((dept, div, sem_i), []).append((code, name, ctype, hours))

mismatches = []
for key in sorted(classes.keys(), key=lambda k: (k[0], k[1], k[2])):
    dept, div, sem = key
    total = 0
    details = []
    for code, name, ctype, hours in classes[key]:
        p = periods_for(hours, ctype, name)
        details.append((code, name, ctype, hours, p))
        if p is not None:
            total += p
    capv = cap(sem)
    if total != capv:
        mismatches.append((dept, div, sem, capv, total, details))

if not mismatches:
    print('All classes meet expected caps.')
else:
    for dept, div, sem, capv, total, details in mismatches:
        status = 'OVER' if total > capv else 'UNDER'
        diff = abs(total - capv)
        print('=' * 100)
        print(f'{dept}/{div}/S{sem}: total={total} cap={capv} {status} by {diff}')
        print('-' * 100)
        for code, name, ctype, hours, p in details:
            pstr = str(p) if p is not None else 'PARSE_ERR'
            print(f'{code:12s} type={ctype or "" :15s} hours={hours or "" :12s} -> {pstr:>3s}  {name}')
conn.close()
