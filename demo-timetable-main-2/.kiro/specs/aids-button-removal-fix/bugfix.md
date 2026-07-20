# Bugfix Requirements Document

## Introduction

The AIDS department timetable incorrectly displays an "AIDS" button/tab in the admin timetable view. This occurs because the system is using the department code "AIDS" as a class name instead of recognizing that AIDS has two separate divisions: "AI" (Artificial Intelligence) and "DS" (Data Science). The timetable should display separate tabs for "AI" and "DS" classes, not a single "AIDS" tab.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the AIDS department has courses without explicit division assignments in the database THEN the system uses "AIDS" as the division/class name

1.2 WHEN timetable entries are created for AIDS department courses THEN the system sets `class_name = "AIDS"` instead of "AI" or "DS"

1.3 WHEN the admin timetable view loads AIDS department timetables THEN an "AIDS" button appears in the class tabs

1.4 WHEN the buildClassTabs function processes class names THEN it filters out "AIDS" as a workaround, hiding the incorrect button

### Expected Behavior (Correct)

2.1 WHEN the AIDS department has courses THEN the system SHALL recognize that AIDS contains two divisions: "AI" and "DS"

2.2 WHEN timetable entries are created for AIDS department courses THEN the system SHALL assign `class_name` as either "AI" or "DS" based on the course's division

2.3 WHEN the admin timetable view loads AIDS department timetables THEN separate "AI" and "DS" buttons SHALL appear in the class tabs

2.4 WHEN the buildClassTabs function processes class names THEN it SHALL NOT need to filter out "AIDS" because it will not appear as a class name

### Unchanged Behavior (Regression Prevention)

3.1 WHEN other departments (non-AIDS) have courses with explicit divisions THEN the system SHALL CONTINUE TO use those division names as class names

3.2 WHEN other departments have courses without explicit divisions THEN the system SHALL CONTINUE TO use the department code as the default division (this behavior is correct for single-division departments)

3.3 WHEN the admin timetable view displays timetables for non-AIDS departments THEN the system SHALL CONTINUE TO show the correct class tabs without filtering

3.4 WHEN timetable entries are saved to the database THEN the system SHALL CONTINUE TO store all required fields (dept_id, class_name, division, year, semester, etc.) correctly
