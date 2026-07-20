from subject_assignment_engine import load_from_db

try:
    engine = load_from_db('timetable.db', 'CS', 3)
    found_prefs = False
    for t in engine.teachers:
        if t.preferences:
            print(f"Teacher {t.name} ({t.teacher_id}) prefs:", t.preferences)
            found_prefs = True
    if not found_prefs:
        print("No preferences loaded from DB for CS/Sem3 teachers.")
except Exception as e:
    print("Error:", e)
