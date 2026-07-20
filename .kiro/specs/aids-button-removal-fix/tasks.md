# Implementation Plan

- [-] 1. Write bug condition exploration test
  - **Property 1: Fault Condition** - AIDS Department Shows Incorrect "AIDS" Button
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: Scope the property to AIDS department courses without explicit division assignments
  - Test that when AIDS department courses lack explicit divisions, the system incorrectly uses "AIDS" as class_name
  - Verify that timetable entries for AIDS courses have class_name = "AIDS" instead of "AI" or "DS"
  - Verify that the admin timetable view displays an "AIDS" button in the class tabs
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g., "AIDS course XYZ creates timetable entry with class_name='AIDS' instead of 'AI' or 'DS'")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3_

- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-AIDS Department Behavior Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe behavior on UNFIXED code for non-AIDS departments
  - Test that non-AIDS departments with explicit divisions continue to use those division names as class names
  - Test that non-AIDS departments without explicit divisions continue to use department code as default division
  - Test that admin timetable view displays correct class tabs for non-AIDS departments without filtering
  - Test that timetable entries store all required fields correctly for non-AIDS departments
  - Property-based testing generates many test cases for stronger guarantees
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 3. Fix for AIDS button removal

  - [ ] 3.1 Update timetable generation logic to recognize AIDS department
    - Modify timetable_scheduler_v2.py to detect AIDS department courses
    - When AIDS department is detected, assign class_name as either "AI" or "DS" based on course division
    - If division is not explicitly set, implement logic to determine appropriate division (AI or DS)
    - Ensure the logic handles both explicit and implicit division assignments
    - _Bug_Condition: isBugCondition(course) where course.dept_code = "AIDS" AND course.division is NULL_
    - _Expected_Behavior: class_name IN {"AI", "DS"} for all AIDS courses_
    - _Preservation: Non-AIDS departments continue using existing division logic_
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2_

  - [ ] 3.2 Remove the workaround filter from buildClassTabs function
    - Open templates/admin_timetable.html
    - Locate the buildClassTabs function
    - Remove the filter that excludes "AIDS" from class tabs
    - Verify that AI and DS tabs will now display correctly
    - _Bug_Condition: buildClassTabs filters out "AIDS" as workaround_
    - _Expected_Behavior: buildClassTabs processes all class names without filtering "AIDS"_
    - _Preservation: Other class tabs continue to display correctly_
    - _Requirements: 1.4, 2.4, 3.3_

  - [ ] 3.3 Verify database assignments for AIDS courses
    - Check that AIDS courses in the database have proper division assignments
    - If needed, update courses table to assign AI or DS divisions to AIDS courses
    - Ensure consistency between course definitions and timetable entries
    - _Bug_Condition: AIDS courses lack explicit division assignments_
    - _Expected_Behavior: All AIDS courses have division set to "AI" or "DS"_
    - _Preservation: Non-AIDS course divisions remain unchanged_
    - _Requirements: 2.1, 2.2, 3.1_

  - [ ] 3.4 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - AIDS Department Shows AI and DS Buttons
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - Verify that AIDS courses now have class_name as "AI" or "DS"
    - Verify that admin timetable view shows separate "AI" and "DS" buttons
    - Verify that no "AIDS" button appears in the class tabs
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [ ] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-AIDS Department Behavior Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)
    - Verify non-AIDS departments continue to work correctly
    - Verify timetable entries for all departments store data correctly

- [ ] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Verify that the AIDS button no longer appears in admin timetable view
  - Verify that AI and DS buttons appear correctly for AIDS department
  - Verify that all other department timetables continue to work as expected
