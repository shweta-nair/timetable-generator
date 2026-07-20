return render_template(
    "hod_preference_window.html",
    dept=dept,
    sem_type=sem_type,
    pref_window=pref_win,
    pref_status=pref_status,
    odd_courses_by_sem=odd_courses_by_sem,
    even_courses_by_sem=even_courses_by_sem,
    teacher=get_teacher_for_user(),
    show_my_prefs=False,
)