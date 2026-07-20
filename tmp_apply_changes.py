import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), 'timetable.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Change 1: AIDS/ds/S4 PCCST403 '3-1-0-1' -> '3-1-0-0'
print("1. AIDS/ds/S4: PCCST403 '3-1-0-1' -> '3-1-0-0'")
cur.execute("""
    UPDATE courses 
    SET hours_per_week='3-1-0-0' 
    WHERE code='PCCST403' 
    AND department='AIDS'
    AND semester=4 
    AND division LIKE '%ds%'
""")
print(f"   Rows updated: {cur.rowcount}")

# Change 2: CS/CS4/S2 copy to CS/CS3/S2
print("\n2. CS/CS4/S2 -> copy all to CS/CS3/S2")
cur.execute("""
    SELECT id, code, name, type, hours_per_week, semester, credit, revision
    FROM courses 
    WHERE department='CS'
    AND semester=2 
    AND division LIKE '%CS4%'
""")
rows_to_copy = cur.fetchall()
print(f"   Found {len(rows_to_copy)} courses in CS division with CS4")
for row in rows_to_copy:
    cid, code, name, ctype, hours, sem, credit, revision = row
    # Insert new row with division updated: replace CS4 with CS3 or add CS3
    cur.execute("""
        INSERT INTO courses (code, name, department, type, hours_per_week, semester, division, credit, revision)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (code, name, 'CS', ctype, hours, sem, 'CS3', credit, revision))
print(f"   Rows inserted for CS3: {cur.rowcount}")

# Change 3: CS/CS4/S6 CST 302 '4-1-0-1' -> '5-1-0-1'
print("\n3. CS/CS4/S6: CST 302 '4-1-0-1' -> '5-1-0-1'")
cur.execute("""
    UPDATE courses 
    SET hours_per_week='5-1-0-1' 
    WHERE code='CST 302' 
    AND department='CS'
    AND semester=6 
    AND division LIKE '%CS4%'
""")
print(f"   Rows updated: {cur.rowcount}")

conn.commit()
print("\n✓ All changes applied. Database updated.")
conn.close()
