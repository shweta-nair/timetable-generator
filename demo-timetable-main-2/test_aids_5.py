import traceback

from app import app, db
import logging

logging.basicConfig(level=logging.INFO)

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from subject_assignment_engine import load_from_db, validate_before_timetable

with app.app_context():
    try:
        engine = load_from_db('timetable.db', 'AIDS', 5)
        assignments = engine.assign()
        print("Assignments complete")
        validate_before_timetable(assignments, engine.teachers, engine.subjects, engine.divisions)
        print("Validation passed!")
    except Exception as e:
        print("CATCHED EXCEPTION:", str(e))
        traceback.print_exc()

