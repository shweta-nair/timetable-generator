import sqlite3

conn = sqlite3.connect('/Users/bhavyasasok/Downloads/demo-timetable-main-2/timetable.db')
cur = conn.cursor()

print("=" * 90)
print("SEMESTER 6 - CHECKING FOR PARALLEL SUBJECTS (ELECTIVES WITH SAME GROUP)")
print("=" * 90)

cur.execute("""
    SELECT DISTINCT department FROM courses WHERE semester=6 ORDER BY department
""")
depts = [row[0] for row in cur.fetchall()]

for dept in depts:
    print(f"\n{dept.upper()} SEMESTER 6:")
    print("-" * 90)
    
    # Find electives
    cur.execute("""
        SELECT code, name, type, hours_per_week
        FROM courses 
        WHERE department=? AND semester=6
        ORDER BY code
    """, (dept,))
    courses = cur.fetchall()
    
    electives = {}
    others = []
    
    for code, name, ctype, hours in courses:
        if 'elective' in name.lower():
            # Extract group number (e.g., "Elective 1", "Elective 2")
            group_key = None
            if 'elective' in name.lower():
                # Try to find "Elective N" or "Program Elective N"
                words = name.split()
                for i, w in enumerate(words):
                    if w.lower() == 'elective' and i > 0:
                        group_key = words[i-1] + ' ' + w
                    elif w.lower() == 'elective' and i+1 < len(words):
                        try:
                            num = int(words[i+1].split('-')[0])
                            group_key = f"Elective {num}"
                        except:
                            pass
            if group_key is None:
                group_key = name[:30]
            
            if group_key not in electives:
                electives[group_key] = []
            electives[group_key].append((code, name, hours))
        else:
            others.append((code, name, ctype, hours))
    
    if electives:
        print("\n  ELECTIVES (grouped by name pattern):")
        for group, items in sorted(electives.items()):
            if len(items) > 1:
                print(f"    [PARALLEL GROUP] {group}:")
                for code, name, hours in items:
                    print(f"      {code:14s} hours={hours:12s}  {name}")
            else:
                code, name, hours = items[0]
                print(f"    [single] {code:14s} hours={hours:12s}  {name}")
    
    if others:
        print("\n  NON-ELECTIVE SUBJECTS:")
        for code, name, ctype, hours in others:
            print(f"    {code:14s} hours={hours:12s}  {name}")

conn.close()
