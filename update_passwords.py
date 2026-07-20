import sqlite3
from werkzeug.security import generate_password_hash

# Connect to database
conn = sqlite3.connect('timetable.db')
cursor = conn.cursor()

try:
    # Get hash for new password
    new_hash = generate_password_hash('Password@123')
    
    # Update teacher_auth username to match teacher's code
    cursor.execute("""
        UPDATE teacher_auth 
        SET username = (
            SELECT code 
            FROM teachers 
            WHERE teachers.id = teacher_auth.teacher_id
        ),
        password_hash = ?
        WHERE role = 'teacher' OR role = 'HOD' OR role = 'HOD '
    """, (new_hash,))
    
    # Log changes
    cursor.execute("""
        SELECT username, role FROM teacher_auth WHERE role = 'teacher' OR role = 'HOD' LIMIT 5
    """)
    print("Sample updated records:", cursor.fetchall())
    
    conn.commit()
    print(f"Successfully updated {cursor.rowcount} teacher/HOD records.")
except Exception as e:
    print(f"Error: {e}")
    conn.rollback()
finally:
    conn.close()
