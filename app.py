from flask import (Flask, render_template, redirect, url_for,
                   request, flash, jsonify)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from database import (db, Department, Teacher, Course, TeacherPreference,
                      TeacherAuth, Timetable, SubjectAssignment,
                      Notification, PreferenceWindow, SystemSettings)
import os

# ──────────────────────────────────────────────────────────────
# Load .env file for email credentials and other config
# ──────────────────────────────────────────────────────────────
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()
            # Only set if not already in environment
            if key and not os.environ.get(key):
                os.environ[key] = value

_load_dotenv()


# ──────────────────────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = 'scms_timetable_secret_2025'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'timetable.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ── Session security ─────────────────────────────────────────
from datetime import timedelta
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ── Remember-me / persistent login cookie ───────────────────
# remember=False (see login()) keeps the login cookie a session cookie
# rather than a persistent one, so it's cleared when the browser fully
# closes. This does NOT give each tab its own session — browser cookies are
# shared by every tab/window of the same browser profile, so only one
# account can be logged in per browser at a time. Two DIFFERENT accounts
# can be active simultaneously only in genuinely separate cookie jars: two
# different browsers, an incognito/private window, or separate browser
# profiles. Within one browser, logging into a second account now correctly
# replaces the first (see login()) instead of silently doing nothing.
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'


@login_manager.user_loader
def load_user(user_id):
    return TeacherAuth.query.get(int(user_id))


@app.before_request
def _enforce_single_session():
    """
    Single-session enforcement (session management #3/#4): each account may
    have only ONE active session at a time. Every login writes a fresh
    random token into both the DB (TeacherAuth.active_session_token) and
    this browser's session cookie. If a request arrives whose cookie token
    doesn't match what's currently in the DB for that account, a newer
    login happened somewhere else (different browser, device, or a fresh
    /login submission in the same browser — see the login() fix) and this
    older session is stale. Log it out here rather than letting it keep
    working silently alongside the newer one.

    Skipped for the login/logout routes themselves and static files, so the
    login flow (which is what actually rotates the token) isn't blocked by
    the check it's in the middle of satisfying.
    """
    if request.endpoint in (None, 'login', 'logout', 'static'):
        return
    if not current_user.is_authenticated:
        return
    from flask import session as flask_session
    cookie_token = flask_session.get('session_token')
    if cookie_token and cookie_token != current_user.active_session_token:
        logout_user()
        flask_session.clear()
        flash('You were logged out because this account was signed in elsewhere.', 'warning')


@login_manager.unauthorized_handler
def unauthorized():
    """Handle unauthorized access attempts."""
    flash('Please log in to access this page.', 'warning')
    return redirect(url_for('login'))



def is_admin():
    return current_user.is_authenticated and current_user.role == 'admin'


def require_admin():
    if not is_admin():
        flash('Admin access required.', 'error')
        return redirect(url_for('login'))
    return None


def get_teacher_for_user():
    """Return Teacher record for the currently logged-in teacher."""
    if current_user.teacher_id:
        return Teacher.query.get(current_user.teacher_id)
    return None

import re
@app.template_filter('first_name')
def get_first_name(full_name):
    if not full_name: return ''
    parts = full_name.split()
    if not parts: return ''
    
    titles = ['Dr.', 'Mr.', 'Ms.', 'Prof.', 'Dr', 'Mr', 'Ms', 'Prof']
    if len(parts) > 1 and parts[0] in titles:
        return f"{parts[0]} {parts[1]}"
    return parts[0]

@app.template_filter('last_name')
def get_last_name(full_name):
    if not full_name: return ''
    parts = full_name.split()
    if not parts: return ''
    
    titles = ['Dr.', 'Mr.', 'Ms.', 'Prof.', 'Dr', 'Mr', 'Ms', 'Prof']
    if len(parts) > 1 and parts[0] in titles:
        if len(parts) > 2:
            return ' '.join(parts[2:])
        return ''
    if len(parts) > 1:
        return ' '.join(parts[1:])
    return ''


# ──────────────────────────────────────────────────────────────
# AUTH ROUTES
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# LANDING PAGE
# ──────────────────────────────────────────────────────────────

@app.route('/')
def landing():
    try:
        if current_user.is_authenticated:
            if is_admin():
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('teacher_profile'))
    except Exception as e:
        app.logger.error(f"Error checking current_user: {e}")
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and current_user.is_authenticated:
        # Only auto-redirect on a plain page visit. A POST (actually
        # submitting credentials) must always be processed below, even if
        # someone else — or a stale version of this account — is currently
        # logged in in this browser. Previously this check ran before the
        # POST branch too, so submitting different credentials while already
        # logged in silently did nothing and bounced back to the old
        # dashboard without ever checking the new username/password.
        return redirect(url_for('admin_dashboard') if is_admin() else url_for('teacher_profile'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '').strip()
        auth = TeacherAuth.query.filter_by(username=username).first()
        if auth and auth.check_password(password):
            # Cleanly end whatever session (if any) was active in this
            # browser first — this may be a different account, or a stale
            # session of the SAME account from before a token existed.
            if current_user.is_authenticated:
                logout_user()

            from flask import session as flask_session
            flask_session.clear()
            flask_session.permanent = True

            # New session token: written to both the DB and this cookie.
            # Single-session enforcement (_enforce_single_session) uses this
            # to detect and log out any other session for this same account
            # the next time it makes a request — e.g. the same account open
            # in another browser/device gets signed out automatically.
            token = uuid.uuid4().hex
            auth.active_session_token = token
            db.session.commit()
            flask_session['session_token'] = token

            login_user(auth, remember=False)
            if auth.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            elif auth.role == 'hod':
                return redirect(url_for('hod_dashboard'))
            return redirect(url_for('teacher_profile'))
        flash('Invalid username or password.', 'error')

    return render_template('login.html')


def _no_cache_response(response):
    """Add cache-control headers so browser back button after logout shows login page."""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/logout')
@login_required
def logout():
    if current_user.is_authenticated:
        current_user.active_session_token = None
        db.session.commit()
    logout_user()
    from flask import session as flask_session
    flask_session.clear()
    response = redirect(url_for('login'))
    _no_cache_response(response)
    return response


@app.after_request
def apply_no_cache_for_authenticated(response):
    """Prevent browser from caching pages for authenticated users."""
    if current_user.is_authenticated:
        _no_cache_response(response)
    return response


# Registration removed — accounts are created by Admin/HOD only.
@app.route('/register')
def register():
    flash('Registration is not available. Please contact your administrator.', 'error')
    return redirect(url_for('login'))

import uuid
from datetime import datetime, timedelta
from database import PasswordResetToken

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip()
        if not username or not email:
            flash('Please enter both your username and email address.', 'error')
            return render_template('forgot_password.html')

        # Verify BOTH username AND email match in DB
        user = TeacherAuth.query.filter_by(username=username, email=email).first()
        if user:
            # Invalidate old tokens for this user
            PasswordResetToken.query.filter_by(auth_id=user.id, used=False).delete()
            db.session.commit()

            # Create a new DB token (expires in 1 hour)
            reset_token = uuid.uuid4().hex
            token_obj = PasswordResetToken(
                auth_id=user.id,
                token=reset_token,
                created_at=datetime.utcnow(),
            )
            db.session.add(token_obj)
            db.session.commit()

            reset_link = url_for('reset_password', token=reset_token, _external=True)

            html_body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 0;">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
        <tr><td style="background:linear-gradient(135deg,#2563eb,#1d4ed8);padding:32px 40px;text-align:center;">
          <h1 style="color:#fff;font-size:22px;margin:0;font-weight:700;">Password Reset Request</h1>
          <p style="color:rgba(255,255,255,0.8);margin:8px 0 0;font-size:14px;">Timetable Management System</p>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="color:#374151;font-size:15px;margin:0 0 16px;">Hello <strong>{username}</strong>,</p>
          <p style="color:#374151;font-size:15px;margin:0 0 24px;line-height:1.6;">We received a request to reset your password. Click the button below to set a new password. This link is valid for <strong>1 hour</strong>.</p>
          <div style="text-align:center;margin:28px 0;">
            <a href="{reset_link}" style="display:inline-block;background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:16px;font-weight:600;">Reset My Password</a>
          </div>
          <p style="color:#6b7280;font-size:13px;margin:0 0 8px;">If the button doesn't work, copy and paste this link:</p>
          <p style="color:#2563eb;font-size:12px;word-break:break-all;background:#eff6ff;border-radius:6px;padding:10px;">{reset_link}</p>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:28px 0;">
          <p style="color:#9ca3af;font-size:13px;margin:0;">If you did not request a password reset, please ignore this email.</p>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:20px 40px;text-align:center;border-top:1px solid #e5e7eb;">
          <p style="color:#9ca3af;font-size:12px;margin:0;">&copy; 2025 College Timetable System. Do not reply to this email.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""
            ok = send_email(user.email, 'Password Reset Request – Timetable System', html_body)
            if ok and os.environ.get('MAIL_USERNAME'):
                flash('A password reset link has been sent to your email.', 'success')
            elif not os.environ.get('MAIL_USERNAME'):
                flash('Email is not configured on this server — please contact your administrator for a password reset.', 'error')
            else:
                flash('Could not send email. Please contact the administrator.', 'error')
        else:
            flash('No account found matching that username and email. Please check and try again.', 'error')
            return render_template('forgot_password.html')

        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    token_obj = PasswordResetToken.query.filter_by(token=token, used=False).first()
    if not token_obj:
        flash('Invalid or expired reset link.', 'error')
        return redirect(url_for('login'))

    # Check expiry (1 hour)
    if datetime.utcnow() - token_obj.created_at > timedelta(hours=1):
        flash('This reset link has expired. Please request a new one.', 'error')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_pwd     = request.form.get('new_password', '')
        confirm_pwd = request.form.get('confirm_password', '')
        if len(new_pwd) < 4:
            flash('Password must be at least 4 characters.', 'error')
            return render_template('reset_password.html', token=token)
        if new_pwd != confirm_pwd:
            flash('Passwords do not match.', 'error')
            return render_template('reset_password.html', token=token)

        # Update password and mark token as used
        user = token_obj.auth
        user.set_password(new_pwd)
        token_obj.used = True
        db.session.commit()
        flash('Password reset successfully! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    teacher = get_teacher_for_user()
    if request.method == 'POST':
        current_pwd = request.form.get('current_password', '')
        new_pwd     = request.form.get('new_password', '')
        confirm_pwd = request.form.get('confirm_password', '')

        if not current_user.check_password(current_pwd):
            flash('Current password is incorrect.', 'error')
        elif new_pwd != confirm_pwd:
            flash('New passwords do not match.', 'error')
        elif len(new_pwd) < 4:
            flash('Password must be at least 4 characters.', 'error')
        else:
            current_user.set_password(new_pwd)
            db.session.commit()
            flash('Password updated successfully!', 'success')

    return render_template('settings.html', teacher=teacher)


@app.route('/teacher/settings', methods=['GET', 'POST'])
@login_required
def teacher_settings():
    if is_admin():
        return redirect(url_for('settings'))
    if is_hod():
        return redirect(url_for('hod_settings'))
        
    teacher = get_teacher_for_user()
    if request.method == 'POST':
        action = request.form.get('action', 'password')

        if action == 'username':
            new_username = request.form.get('new_username', '').strip().lower()
            if not new_username:
                flash('Username cannot be empty.', 'error')
            elif len(new_username) < 3:
                flash('Username must be at least 3 characters long.', 'error')
            elif new_username == current_user.username:
                flash('That is already your current username.', 'error')
            elif TeacherAuth.query.filter_by(username=new_username).first():
                flash('Username "{}" already exists. Please choose another.'.format(new_username), 'error')
            else:
                current_user.username = new_username
                db.session.commit()
                flash('Username updated to "{}".'.format(new_username), 'success')
        else:
            current_pwd = request.form.get('current_password', '')
            new_pwd     = request.form.get('new_password', '')
            confirm_pwd = request.form.get('confirm_password', '')

            if not current_user.check_password(current_pwd):
                flash('Current password is incorrect.', 'error')
            elif new_pwd != confirm_pwd:
                flash('New passwords do not match.', 'error')
            elif len(new_pwd) < 4:
                flash('Password must be at least 4 characters.', 'error')
            else:
                current_user.set_password(new_pwd)
                db.session.commit()
                flash('Password updated successfully!', 'success')

    return render_template('teacher_settings.html', teacher=teacher)


# ──────────────────────────────────────────────────────────────
# TEACHER ROUTES
# ──────────────────────────────────────────────────────────────

@app.route('/profile')
@login_required
def teacher_profile():
    if is_admin():
        return redirect(url_for('admin_dashboard'))
    if is_hod():
        return redirect(url_for('hod_profile'))
    teacher = get_teacher_for_user()
    prefs = []
    pref_window_open = False
    if teacher:
        prefs = TeacherPreference.query.filter_by(teacher_id=teacher.id).all()
        pref_window_open = pref_window_open_for_teacher(teacher)
    return render_template('teacher_profile.html', teacher=teacher, prefs=prefs,
                           pref_window_open=pref_window_open)


@app.route('/update-basic-details', methods=['POST'])
@login_required
def update_basic_details():
    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher not found.', 'error')
        return redirect(url_for('teacher_profile'))

    disp_name = request.form.get('display_name', '').strip()
    if disp_name:
        teacher.name = disp_name
    elif request.form.get('first_name'):
        # Fallback for HOD profile which might still use first/last name
        first = request.form.get('first_name', '').strip()
        last  = request.form.get('last_name', '').strip()
        new_name = (first + ' ' + last).strip()
        if new_name:
            teacher.name = new_name

    teacher.gender          = request.form.get('gender', '').strip()
    teacher.designation     = request.form.get('designation', '').strip()
    teacher.date_of_joining = request.form.get('date_of_joining', '').strip()

    teacher.experience      = request.form.get('experience', '').strip()
    teacher.qualification   = request.form.get('qualification', '').strip()
    teacher.area_of_specialization = request.form.get('area_specialization', '').strip()

    # Update email and username on the auth record
    new_email    = request.form.get('email', '').strip()
    new_username = request.form.get('username', '').strip()
    if new_email:
        current_user.email = new_email
    if new_username and new_username != current_user.username:
        existing = TeacherAuth.query.filter_by(username=new_username).first()
        if existing:
            flash('Username already exists. Please choose another.', 'error')
            return redirect(url_for('teacher_profile'))
        current_user.username = new_username

    db.session.commit()
    flash('Basic details updated.', 'success')
    return redirect(url_for('hod_profile') if is_hod() else url_for('teacher_profile'))


# ══════════════════════════════════════════════════════════════
# CONSOLIDATED TeacherPreference save/replace helper
# ──────────────────────────────────────────────────────────────
# Every route that lets a teacher (or an HOD editing on a teacher's
# behalf) set their 3 subject preferences funnels through this one
# function. It is the single place that:
#   1. validates the submitted course codes (exist, distinct
#      semesters, exactly 3),
#   2. deletes the teacher's previous rows for the CURRENT semester
#      group — matching legacy 'odd'/'even' string rows as well as
#      numeric-string rows, so old-format rows never survive a
#      resubmission, and
#   3. inserts the new rows using the numeric-string storage
#      standard (TeacherPreference.semester in {"1".."8"}).
#
# Previously five separate call sites each re-implemented this
# logic slightly differently: several deleted only numeric-string
# rows, leaving legacy 'odd'/'even' rows orphaned in the table —
# the root cause of the HOD dashboard showing counts like "6/3"
# instead of "3/3" after a teacher resubmitted. One call site
# (the old /update-additional-details form) didn't save `semester`
# at all. See migrate_teacher_preference_semester.py for the
# one-time cleanup of rows created before this fix.
# ══════════════════════════════════════════════════════════════
def save_teacher_preferences(teacher, course_codes, sem_type):
    """
    Replace `teacher`'s subject preferences for the given semester
    group ('odd' or 'even') with the (up to 3) course codes in
    `course_codes`.

    Returns (ok, message): ok=False + a user-facing flash string on
    validation failure, ok=True + a success flash string otherwise.
    Semester is ALWAYS stored numerically (str(course.semester)) —
    never as the literal 'odd'/'even'.
    """
    valid_sems = [1, 3, 5, 7] if sem_type == 'odd' else [2, 4, 6, 8]

    codes = [c.strip() for c in course_codes if c and c.strip()]
    if len(codes) < 3:
        return False, 'Please select all 3 preferences.'
    if len(set(codes)) != len(codes):
        return False, 'Duplicate subjects are not allowed.'

    courses = []
    sems_chosen = []
    for code in codes:
        course = Course.query.filter_by(code=code).first()
        if not course:
            return False, f'Course "{code}" not found.'
        courses.append(course)
        sems_chosen.append(course.semester)

    if len(set(sems_chosen)) < 3:
        return False, 'All 3 preferences must be from 3 different semesters.'

    # Delete every existing row for this teacher that belongs to the
    # CURRENT semester group, regardless of which format (legacy
    # literal or numeric string) it happens to be stored in. Matching
    # both teacher_id and teacher_code also catches legacy rows saved
    # with teacher_id NULL. This is what guarantees "exactly three
    # preferences remain" after every submission, with no duplicates
    # and no orphaned legacy rows left behind.
    numeric_strs = [str(s) for s in valid_sems]
    TeacherPreference.query.filter(
        (TeacherPreference.teacher_id == teacher.id) |
        (TeacherPreference.teacher_code == teacher.code),
        TeacherPreference.semester.in_(numeric_strs + [sem_type])
    ).delete(synchronize_session=False)
    db.session.flush()

    from datetime import datetime
    now = datetime.utcnow()
    for rank, course in enumerate(courses, start=1):
        db.session.add(TeacherPreference(
            teacher_id=teacher.id,
            teacher_code=teacher.code,
            course_code=course.code,
            semester=str(course.semester),
            rank=rank,
            created_at=now,
        ))
    db.session.commit()
    return True, 'Preferences saved successfully!'


@app.route('/update-additional-details', methods=['POST'])
@login_required
def update_additional_details():
    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher not found.', 'error')
        return redirect(url_for('teacher_profile'))

    # ENFORCE PREFERENCE WINDOW LOCK
    if not pref_window_open_for_teacher(teacher):
        flash('The preference submission window is currently closed. You cannot edit these details.', 'error')
        return redirect(url_for('teacher_profile'))

    teacher.experience = request.form.get('experience_current', '').strip()
    teacher.area_of_specialization = request.form.get('area_specialization', '').strip()
    db.session.commit()

    # Update subject preferences (delete old, insert new) — only if all
    # 3 fields were actually submitted from this form. This form predates
    # the semester-aware preference system; when used, it now goes
    # through the same save_teacher_preferences() helper as every other
    # preference-submission route so semester is always stored
    # numerically and no legacy/duplicate rows are left behind.
    pref_codes = [
        request.form.get('subject_pref_1', '').strip(),
        request.form.get('subject_pref_2', '').strip(),
        request.form.get('subject_pref_3', '').strip(),
    ]
    if any(pref_codes):
        sem_type = SystemSettings.get('active_semester_type', 'odd')
        ok, message = save_teacher_preferences(teacher, pref_codes, sem_type)
        flash(message, 'success' if ok else 'error')
    else:
        flash('Additional details updated.', 'success')

    return redirect(url_for('hod_profile') if is_hod() else url_for('teacher_profile'))


@app.route('/upload-photo', methods=['POST'])
@login_required
def upload_photo():
    """Teacher uploads a profile photo from their dashboard."""
    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher not found.', 'error')
        return redirect(url_for('teacher_profile'))

    file = request.files.get('photo')
    if not file or file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('teacher_profile'))

    import os, uuid
    from werkzeug.utils import secure_filename
    ALLOWED = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED:
        flash('Only image files are allowed (png, jpg, jpeg, gif, webp).', 'error')
        return redirect(url_for('teacher_profile'))

    upload_dir = os.path.join(BASE_DIR, 'static', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    filename = secure_filename(f'{teacher.id}_{uuid.uuid4().hex[:8]}.{ext}')
    file.save(os.path.join(upload_dir, filename))

    # Remove old photo if exists
    if teacher.photo_path:
        old = os.path.join(upload_dir, teacher.photo_path)
        if os.path.exists(old):
            os.remove(old)

    teacher.photo_path = filename
    db.session.commit()
    flash('Profile photo updated.', 'success')
    return redirect(url_for('hod_profile') if is_hod() else url_for('teacher_profile'))


@app.route('/remove-photo', methods=['POST'])
@login_required
def remove_photo():
    """Teacher removes their profile photo."""
    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher not found.', 'error')
        return redirect(url_for('teacher_profile'))

    if teacher.photo_path:
        upload_dir = os.path.join(BASE_DIR, 'static', 'uploads')
        old = os.path.join(upload_dir, teacher.photo_path)
        if os.path.exists(old):
            os.remove(old)
        teacher.photo_path = None
        db.session.commit()
        flash('Profile photo removed.', 'success')
    else:
        flash('No photo to remove.', 'error')
    return redirect(url_for('hod_profile') if is_hod() else url_for('teacher_profile'))


@app.route('/departments')
@login_required
def departments():
    dept_list = Department.query.order_by(Department.name).all()
    return render_template('departments.html', departments=dept_list)


@app.route('/subjects')
@login_required
def subjects():
    STUDENT_DEPTS = ['AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC']

    # Get only student departments (not BSH)
    dept_list = Department.query.filter(
        Department.id.in_(STUDENT_DEPTS)
    ).order_by(Department.name).all()
    dept_map = {d.id: d for d in dept_list}

    # Group courses by their student department column
    courses_by_dept = {dept: [] for dept in dept_list}

    all_courses = Course.query.filter(
        Course.department.in_(STUDENT_DEPTS)
    ).order_by(Course.semester, Course.name).all()

    for course in all_courses:
        dept_code = course.department
        if dept_code in dept_map:
            courses_by_dept[dept_map[dept_code]].append(course)

    # Remove empty departments
    courses_by_dept = {d: c for d, c in courses_by_dept.items() if c}

    return render_template('subjects.html', courses_by_dept=courses_by_dept)




@app.route('/timetable')
@login_required
def timetable():
    teacher = get_teacher_for_user()
    if is_admin():
        return redirect(url_for('admin_dashboard'))

    import json as _json

    dept_id  = request.args.get('dept_id', '')
    sem_type = request.args.get('sem_type', 'odd')
    tab      = request.args.get('tab', 'class')   # class | teacher | dept
    key      = request.args.get('key', '')         # division_id | teacher_id | dept_id

    # Exclude BSH from dropdown if desired, or allow all. Like admin:
    STUDENT_DEPTS = ['AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC']
    depts = Department.query.filter(Department.id.in_(STUDENT_DEPTS)).order_by(Department.id).all()
    teachers_all = Teacher.query.order_by(Teacher.name).all()

    all_keys = []
    timetable_cells = []

    # For class / dept tabs
    if dept_id and tab in ['class', 'dept']:
        from database import Timetable
        rows = Timetable.query.filter_by(
            dept_id=dept_id,
            semester_type=sem_type,
            timetable_type=tab
        ).order_by(Timetable.semester, Timetable.key).all()
        all_keys = list({r.key: r for r in rows}.keys())

        if key:
            tt = Timetable.query.filter_by(
                dept_id=dept_id,
                semester_type=sem_type,
                timetable_type=tab,
                key=key
            ).order_by(Timetable.id.desc()).first()
            if tt:
                try:
                    timetable_cells = _json.loads(tt.data_json)
                except Exception:
                    timetable_cells = []

    # For teacher tab
    if tab == 'teacher':
        from database import Timetable
        # FIX: only show canonical semester='all' rows to avoid duplicates
        teacher_tt_rows = Timetable.query.filter_by(
            timetable_type='teacher',
            semester_type=sem_type,
            semester='all',
        ).order_by(Timetable.key).all()
        all_keys = list({r.key: True for r in teacher_tt_rows}.keys())
        if key:
            key_rows = Timetable.query.filter_by(
                timetable_type='teacher',
                semester_type=sem_type,
                semester='all',
                key=key
            ).order_by(Timetable.id.asc()).all()
            if not key_rows:
                # Fall back to any row (pre-migration data)
                key_rows = Timetable.query.filter_by(
                    timetable_type='teacher',
                    semester_type=sem_type,
                    key=key
                ).order_by(Timetable.id.asc()).all()
            if key_rows:
                timetable_cells = []
                for row in key_rows:
                    try:
                        timetable_cells.extend(_json.loads(row.data_json))
                    except Exception:
                        pass

    teacher_map = {t.id: t.name for t in teachers_all}

    return render_template(
        'timetable.html',
        teacher=teacher,
        depts=depts,
        teachers_all=teachers_all,
        teacher_map=teacher_map,
        selected_dept=dept_id,
        sem_type=sem_type,
        tab=tab,
        selected_key=key,
        all_keys=all_keys,
        timetable_cells=timetable_cells,
        DAYS=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    )

# ── Teacher: Dedicated Timetable Page ──────────────────────────

@app.route('/teacher/timetable')
@login_required
def teacher_timetable():
    if is_admin() or is_hod():
        return redirect(url_for('teacher_profile'))
    teacher = get_teacher_for_user()
    sem_type = 'odd'
    try:
        from database import SystemSettings, Department
        s = SystemSettings.query.filter_by(key='active_semester_type').first()
        if s:
            sem_type = s.value
        STUDENT_DEPTS = ['AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC']
        depts = Department.query.filter(Department.id.in_(STUDENT_DEPTS)).order_by(Department.id).all()
    except Exception:
        depts = []

    teacher_dept = teacher.dept_id if teacher else ''
    return render_template('teacher_timetable.html', teacher=teacher, sem_type=sem_type, depts=depts, teacher_dept=teacher_dept)


# ── Teacher: Preference Tab ─────────────────────────────────────

@app.route('/teacher/preferences', methods=['GET', 'POST'])
@login_required
def teacher_preferences():
    if is_admin() or is_hod():
        return redirect(url_for('teacher_profile'))
    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher record not found.', 'error')
        return redirect(url_for('teacher_profile'))

    pref_open = pref_window_open_for_teacher(teacher)

    # Get active semester type
    sem_type = 'odd'
    try:
        from database import SystemSettings
        s = SystemSettings.query.filter_by(key='active_semester_type').first()
        if s:
            sem_type = s.value
    except Exception:
        pass

    if sem_type == 'odd':
        valid_sems = [1, 3, 5, 7]
    else:
        valid_sems = [2, 4, 6, 8]

    # Get THEORY-ONLY courses for the teacher's dept filtered by sem_type.
    # Exclude: lab subjects, seminar, project phase, mini project, ccw, y2p
    # Teachers should select only theory subject preferences (max 3).
    _EXCLUDED_PREF_KEYWORDS = [
        'seminar', 'project phase', 'mini project', 'miniproject', 'ccw', 'y2p',
    ]
    courses_raw = Course.query.filter(
        Course.semester.in_([str(s) for s in valid_sems]),
        Course.department == teacher.dept_id,
        ~Course.type.ilike('%lab%'),
        ~Course.type.ilike('%project%'),
    ).order_by(Course.semester, Course.name).all()

    def _is_theory_pref(c):
        name_lower = (c.name or '').lower()
        return not any(kw in name_lower for kw in _EXCLUDED_PREF_KEYWORDS)

    courses = [c for c in courses_raw if _is_theory_pref(c)]

    # Group by semester for display
    courses_by_sem = {}
    for c in courses:
        courses_by_sem.setdefault(c.semester, []).append(c)

    existing_prefs = TeacherPreference.query.filter_by(teacher_id=teacher.id).order_by(TeacherPreference.rank).all()

    if request.method == 'POST':
        if not pref_open:
            flash('Preference window is currently closed.', 'error')
            return redirect(url_for('teacher_preferences'))

        codes = [
            request.form.get('pref_1', '').strip(),
            request.form.get('pref_2', '').strip(),
            request.form.get('pref_3', '').strip(),
        ]
        ok, message = save_teacher_preferences(teacher, codes, sem_type)
        flash(message, 'success' if ok else 'error')
        return redirect(url_for('teacher_preferences'))

    return render_template('teacher_preferences.html',
                           teacher=teacher,
                           pref_open=pref_open,
                           sem_type=sem_type,
                           valid_sems=valid_sems,
                           courses_by_sem=courses_by_sem,
                           existing_prefs=existing_prefs)


@app.route('/timetable/pdf/<tt_type>')
@login_required
def timetable_pdf(tt_type):
    """
    Generate a PDF for class / teacher / dept timetable (Teacher access).
    """
    if is_admin():
        return redirect(url_for('admin_dashboard'))

    import json as _json
    from flask import make_response

    dept_id  = request.args.get('dept_id', '')
    sem_type = request.args.get('sem_type', 'odd')
    key      = request.args.get('key', '')
    # ISSUE 1 FIX: read semester so PDF loads the correct row
    semester = request.args.get('semester', '')

    from database import Timetable
    if tt_type == 'teacher':
        tt_rows = Timetable.query.filter_by(
            timetable_type='teacher', semester_type=sem_type, semester='all', key=key
        ).order_by(Timetable.id.asc()).all()
        if not tt_rows:
            tt_rows = Timetable.query.filter_by(
                timetable_type='teacher', semester_type=sem_type, key=key
            ).order_by(Timetable.id.asc()).all()
        if not tt_rows:
            flash(f'No timetable found for {key}', 'error')
            return redirect(url_for('timetable', dept_id=dept_id, sem_type=sem_type, tab=tt_type))
        cells = []
        for row in tt_rows:
            try:
                cells.extend(_json.loads(row.data_json))
            except Exception:
                pass
        sem_label = 'All Semesters'
    else:
        # ISSUE 1 FIX: filter by semester when provided so the correct semester row
        # is returned — without this, .order_by(id desc).first() always returned
        # the highest-id row (semester 7 or 8) regardless of selection.
        q = Timetable.query.filter_by(
            dept_id=dept_id, semester_type=sem_type, timetable_type=tt_type, key=key
        )
        if semester:
            q = q.filter_by(semester=str(semester))
        tt = q.order_by(Timetable.id.desc()).first()
        if not tt:
            flash(f'No timetable found for {key}', 'error')
            return redirect(url_for('timetable', dept_id=dept_id, sem_type=sem_type, tab=tt_type))
        try:
            cells = _json.loads(tt.data_json)
        except Exception:
            cells = []
        sem_label = f'Semester {tt.semester}'

    teacher_name = key
    if tt_type == 'teacher':
        t = Teacher.query.get(key)
        if t:
            teacher_name = t.name

    # Enrich cells with teacher_dept (not stored in JSON, looked up from DB)
    _tdept_map = {str(t.id): (t.dept_id or '') for t in Teacher.query.all()}
    for _c in cells:
        if not _c.get('teacher_dept') and _c.get('teacher_id'):
            _c['teacher_dept'] = _tdept_map.get(str(_c['teacher_id']), '')
        pdf_bytes = _render_timetable_pdf(
        cells      = cells,
        title      = f"{tt_type.title()} Timetable — {teacher_name if tt_type == 'teacher' else key}",
        subtitle   = f"{dept_id} | {sem_type.title()} {sem_label}",
        is_teacher = (tt_type == 'teacher'),
    )
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = (
        f'attachment; filename="timetable_{tt_type}_{key}.pdf"'
    )
    return response

# ──────────────────────────────────────────────────────────────
# ADMIN ROUTES
# ──────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_dashboard():
    if not is_admin():
        flash('Admin access required.', 'error')
        return redirect(url_for('teacher_profile'))

    stats = {
        'departments': Department.query.count(),
        'teachers':    Teacher.query.count(),
        'courses':     Course.query.count(),
    }
    # Pass active semester setting so the label is always correct
    active_sem = SystemSettings.query.filter_by(key='active_semester_type').first()
    settings_obj = active_sem  # has .value attribute
    # Departments whose timetable needs regeneration (e.g. new teacher added
    # since the last "Assign Subjects" run) — see _flag_timetable_stale.
    stale_rows = SystemSettings.query.filter(
        SystemSettings.key.like('timetable_stale_%')
    ).all()
    stale_depts = [
        {'dept_id': r.key.replace('timetable_stale_', ''), 'reason': r.value}
        for r in stale_rows
    ]

    return render_template('admin_dashboard.html', stats=stats, settings=settings_obj,
                            stale_depts=stale_depts)


# ─── Departments ───────────────────────────────────────────────

def _handle_hod_creation(dept_id, dept_name, hod_name):
    """
    Helper: Auto-create HOD teacher/login from the HOD name.
    Strips titles, grabs first name for username/password.
    Returns a success string with credentials.
    """
    if not hod_name:
        return ""

    import re
    from database import TeacherAuth
    
    # 1. Extract first name
    # e.g. "Dr. Sonal Ayyappan" -> "sonal"
    first = re.sub(r'^(Dr\.?|Mr\.?|Ms\.?|Prof\.?)\s*', '', hod_name, flags=re.IGNORECASE).split()[0].lower()
    first = re.sub(r'[^a-z]', '', first)
    if not first:
        return ""

    base_username = first
    password = first

    # 2. Check if a teacher with this EXACT name already exists in this dept
    existing_t = Teacher.query.filter_by(name=hod_name, dept_id=dept_id).first()
    if existing_t:
        # Teacher exists. Ensure they have an auth account and it's set to 'hod'
        if not existing_t.auth:
            username = base_username
            suffix = 1
            while TeacherAuth.query.filter_by(username=username).first():
                username = f'{base_username}{suffix}'
                suffix += 1
            auth = TeacherAuth(teacher_id=existing_t.id, username=username, role='hod')
            auth.set_password(password)
            db.session.add(auth)
            return f' | HOD Login created: username="{username}", password="{password}"'
        else:
            msg_part = 'promoted to HOD' if existing_t.auth.role != 'hod' else 'already exists'
            if existing_t.auth.role != 'hod':
                existing_t.auth.role = 'hod'
            return f' | HOD account {msg_part}: username="{existing_t.auth.username}"'

    # 3. Teacher does not exist, create them
    teacher_id = f'HOD_{dept_id}_{base_username}'
    count = 1
    base_id = teacher_id
    while Teacher.query.get(teacher_id):
        teacher_id = f'{base_id}_{count}'
        count += 1

    t = Teacher(id=teacher_id, name=hod_name, designation='HOD', dept_id=dept_id)
    db.session.add(t)

    # 4. Create Auth
    username = base_username
    suffix = 1
    while TeacherAuth.query.filter_by(username=username).first():
        username = f'{base_username}{suffix}'
        suffix += 1
    
    auth = TeacherAuth(teacher_id=teacher_id, username=username, role='hod')
    auth.set_password(password)
    db.session.add(auth)

    return f' | HOD Login created: username="{username}", password="{password}"'


@app.route('/admin/departments')
@login_required
def admin_departments():
    if not is_admin():
        return redirect(url_for('login'))
    departments = Department.query.order_by(Department.name).all()
    return render_template('admin_departments.html', departments=departments)


@app.route('/admin/department/add', methods=['POST'])
@login_required
def admin_department_add():
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    dept_id   = request.form.get('id', '').strip().upper()
    dept_name = request.form.get('name', '').strip()
    hod_name  = request.form.get('hod_name', '').strip()
    if not dept_id or not dept_name:
        flash('Department ID and Name are required.', 'error')
    elif Department.query.get(dept_id):
        flash('Department ID already exists.', 'error')
    else:
        db.session.add(Department(id=dept_id, name=dept_name, hod_name=hod_name))
        credentials_msg = _handle_hod_creation(dept_id, dept_name, hod_name)
        db.session.commit()
        flash(f'Department "{dept_name}" added.{credentials_msg}', 'success')
    return redirect(url_for('admin_departments'))


@app.route('/admin/department/edit/<dept_id>', methods=['GET', 'POST'])
@login_required
def admin_department_edit(dept_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    dept = Department.query.get_or_404(dept_id)
    if request.method == 'GET':
        return jsonify({'id': dept.id, 'name': dept.name, 'hod_name': dept.hod_name,
                         'lab_room_capacity': dept.lab_room_capacity})
    dept.name     = request.form.get('name', dept.name).strip()
    new_hod       = request.form.get('hod_name', dept.hod_name).strip()

    cap_raw = request.form.get('lab_room_capacity', '').strip()
    if cap_raw:
        try:
            dept.lab_room_capacity = max(1, int(cap_raw))
        except ValueError:
            pass

    credentials_msg = ""
    if new_hod:
        credentials_msg = _handle_hod_creation(dept.id, dept.name, new_hod)
    
    dept.hod_name = new_hod
    db.session.commit()
    flash(f'Department updated.{credentials_msg}', 'success')
    return redirect(url_for('admin_departments'))


@app.route('/admin/department/delete/<dept_id>', methods=['POST'])
@login_required
def admin_department_delete(dept_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    dept = Department.query.get_or_404(dept_id)
    db.session.delete(dept)
    db.session.commit()
    flash(f'Department "{dept.name}" deleted.', 'success')
    return redirect(url_for('admin_departments'))


# ─── Teachers ──────────────────────────────────────────────────

@app.route('/admin/teachers')
@login_required
def admin_teachers():
    if not is_admin():
        return redirect(url_for('login'))

    dept_filter = request.args.get('dept', '')
    desig_filter = request.args.get('designation', '')
    search = request.args.get('search', '').strip()

    query = Teacher.query
    if dept_filter:
        query = query.filter_by(dept_id=dept_filter)
    if desig_filter:
        query = query.filter(Teacher.designation.ilike(f'%{desig_filter}%'))
    if search:
        query = query.filter(Teacher.name.ilike(f'%{search}%'))

    teachers = query.order_by(Teacher.name).all()
    departments = Department.query.order_by(Department.name).all()
    designations = ['Professor', 'Associate Professor', 'Assistant Professor', 'Lecturer']

    return render_template('admin_teachers.html',
                           teachers=teachers,
                           departments=departments,
                           designations=designations,
                           dept_filter=dept_filter,
                           desig_filter=desig_filter,
                           search=search)


@app.route('/admin/teacher/get/<teacher_id>', methods=['GET'])
@login_required
def admin_teacher_get(teacher_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    t = Teacher.query.get_or_404(teacher_id)
    return jsonify({
        'id': t.id, 'name': t.name, 'code': t.code or '',
        'designation': t.designation, 'gender': t.gender,
        'experience': t.experience, 'date_of_joining': t.date_of_joining,
        'seniority_level': t.seniority_level,
        'area_of_specialization': t.area_of_specialization,
        'dept_id': t.dept_id or '',
    })

def _generate_teacher_id(dept_id):
    """
    Auto-generate dept-based teacher ID: e.g. 'aidst1', 'cst2'.

    Uses a persistent monotonic counter (SystemSettings), NOT max(existing)+1.
    max(existing)+1 reuses IDs after deletion — e.g. delete the only teacher
    'aidst1' in a department, add a new one, and it becomes 'aidst1' again,
    silently able to inherit any historical data a cascade-delete missed
    (embedded JSON blobs, future features that store teacher_id loosely,
    partial-failure edge cases, etc). The counter only ever increases, so a
    deleted teacher's ID is never handed to anyone else.
    """
    prefix = (dept_id or 'gen').lower().replace(' ', '') + 't'
    seq_key = f'teacher_id_seq_{prefix}'

    try:
        last_n = int(SystemSettings.get(seq_key, '0') or '0')
    except ValueError:
        last_n = 0

    # Guard for existing databases where teachers already exist under this
    # prefix but the counter hasn't been initialized yet.
    existing = Teacher.query.filter(Teacher.id.like(f'{prefix}%')).all()
    existing_max = 0
    for t in existing:
        suffix = t.id[len(prefix):]
        if suffix.isdigit():
            existing_max = max(existing_max, int(suffix))

    next_n = max(last_n, existing_max) + 1
    SystemSettings.set(seq_key, str(next_n))
    return f'{prefix}{next_n}'


_DESIGNATION_SENIORITY_WEIGHT = {
    'principal': 0,
    'vice principal': 1,
    'deputy dean': 2,
    'associate professor': 3,
    'assistant professor': 4,
    'professor': 5,
    'lecturer': 6,
    'hod': 3,
}


def _recalculate_seniority(dept_id):
    """Recalculate seniority_level for all teachers in a department.

    Sort by: experience (date_of_joining DESC, i.e. older = more senior) then
    by position weight (Principal first). Assigns rank 1 = most senior.
    """
    from datetime import date
    import re as _re
    teachers = Teacher.query.filter_by(dept_id=dept_id).all()

    def _parse_doj(doj_str):
        """Parse date_of_joining string; returns date or None."""
        if not doj_str:
            return None
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d',
                    '%d %b %Y', '%d-%b-%Y', '%B %d, %Y'):
            try:
                from datetime import datetime as _dt
                return _dt.strptime(doj_str.strip(), fmt).date()
            except ValueError:
                pass
        return None

    today = date.today()
    rows = []
    for t in teachers:
        doj = _parse_doj(t.date_of_joining)
        exp_days = (today - doj).days if doj else 0
        desig_key = (t.designation or '').lower().strip()
        pos_weight = _DESIGNATION_SENIORITY_WEIGHT.get(desig_key, 99)
        rows.append((t, exp_days, pos_weight))

    # Sort: primary = pos_weight ASC, secondary = exp_days DESC
    rows.sort(key=lambda x: (x[2], -x[1]))

    for rank, (t, _, _) in enumerate(rows, start=1):
        t.seniority_level = str(rank)
    db.session.commit()



@app.route('/admin/teacher/add', methods=['POST'])
@login_required
def admin_teacher_add():
    if not is_admin():
        return redirect(url_for('login'))

    name        = request.form.get('name', '').strip()
    code        = request.form.get('code', '').strip().upper()
    designation = request.form.get('designation', '').strip()
    gender      = request.form.get('gender', '').strip()
    experience  = request.form.get('experience', '').strip()
    doj         = request.form.get('date_of_joining', '').strip()
    area        = request.form.get('area_of_specialization', '').strip()
    dept_id     = request.form.get('dept_id', '').strip()

    if not name:
        flash('Teacher name is required.', 'error')
        return redirect(url_for('admin_teachers'))

    teacher_id = _generate_teacher_id(dept_id or 'gen')

    teacher = Teacher(id=teacher_id, name=name, code=code,
                      designation=designation, gender=gender,
                      experience=experience, date_of_joining=doj,
                      area_of_specialization=area,
                      dept_id=dept_id or None)
    db.session.add(teacher)

    # Create login — enforce min 3 chars
    from database import TeacherAuth
    import re
    first = re.sub(r'^(Dr\.?|Mr\.?|Ms\.?|Prof\.?)\s*', '', name, flags=re.IGNORECASE).split()[0].lower()
    first = re.sub(r'[^a-z]', '', first)
    if len(first) < 3:
        first = (first + name.replace(' ', '').lower())[:3]
    username = first
    suffix = 1
    while TeacherAuth.query.filter_by(username=username).first():
        username = f'{first}{suffix}'
        suffix += 1
    auth = TeacherAuth(teacher_id=teacher_id, username=username, role='teacher')
    auth.set_password(first)
    db.session.add(auth)
    db.session.commit()
    if dept_id:
        _recalculate_seniority(dept_id)
        _flag_timetable_stale(dept_id, f'New teacher "{name}" was added to {dept_id}.')
        db.session.commit()

    flash(f'Teacher "{name}" added. Login: {username}/{first}', 'success')
    return redirect(url_for('admin_teachers'))


@app.route('/admin/teacher/edit/<teacher_id>', methods=['POST'])
@login_required
def admin_teacher_edit(teacher_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    t = Teacher.query.get_or_404(teacher_id)

    # Capture old values to detect what changed
    old_name        = t.name or ''
    old_designation = t.designation or ''
    old_gender      = t.gender or ''
    old_experience  = t.experience or ''
    old_doj         = t.date_of_joining or ''
    old_area        = t.area_of_specialization or ''
    old_dept        = t.dept_id or ''

    t.name        = request.form.get('name', t.name or '').strip()
    t.code        = request.form.get('code', t.code or '').strip().upper()
    t.designation = request.form.get('designation', t.designation or '').strip()
    t.gender      = request.form.get('gender', t.gender or '').strip()
    t.experience  = request.form.get('experience', t.experience or '').strip()
    t.date_of_joining = request.form.get('date_of_joining', t.date_of_joining or '').strip()
    t.area_of_specialization = request.form.get('area_of_specialization', t.area_of_specialization or '').strip()
    t.dept_id     = request.form.get('dept_id', t.dept_id or '').strip() or None
    db.session.commit()
    if t.dept_id:
        _recalculate_seniority(t.dept_id)

    # Build change log
    from datetime import datetime as _dt
    changes = []
    if t.name        != old_name:        changes.append(f'Name: "{old_name}" → "{t.name}"')
    if t.designation != old_designation: changes.append(f'Designation: "{old_designation}" → "{t.designation}"')
    if t.gender      != old_gender:      changes.append(f'Gender: "{old_gender}" → "{t.gender}"')
    if t.experience  != old_experience:  changes.append(f'Experience: "{old_experience}" → "{t.experience}"')
    if t.date_of_joining != old_doj:     changes.append(f'Date of Joining: "{old_doj}" → "{t.date_of_joining}"')
    if t.area_of_specialization != old_area: changes.append(f'Specialization: "{old_area}" → "{t.area_of_specialization}"')
    if (t.dept_id or '') != old_dept:    changes.append(f'Department: "{old_dept}" → "{t.dept_id or ""}"')

    change_summary = '; '.join(changes) if changes else 'Minor details updated'
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')

    # Notify the teacher themselves about what was changed
    _notify_teacher(
        teacher_id=teacher_id,
        title='Your Profile Was Updated',
        message=(
            f'Your profile was updated by Admin on {now_str}. '
            f'Changes: {change_summary}.'
        ),
        sender_id=None,
    )

    flash(f'Teacher "{t.name}" updated.', 'success')
    return redirect(url_for('admin_teachers'))


@app.route('/admin/teacher/delete/<teacher_id>', methods=['POST'])
@login_required
def admin_teacher_delete(teacher_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    t = Teacher.query.get_or_404(teacher_id)
    teacher_name = t.name
    dept_id_for_flag = t.dept_id
    from database import PasswordResetToken
    # Remove auth first
    if t.auth:
        PasswordResetToken.query.filter_by(auth_id=t.auth.id).delete()
        db.session.delete(t.auth)
    # Remove preferences
    TeacherPreference.query.filter_by(teacher_id=teacher_id).delete()
    # Remove assigned subjects to satisfy foreign key constraints
    SubjectAssignment.query.filter_by(teacher_id=teacher_id).delete()
    # Remove notifications sent to or by this teacher
    Notification.query.filter(
        (Notification.recipient_id == teacher_id) |
        (Notification.sender_id == teacher_id)
    ).delete(synchronize_session=False)
    db.session.delete(t)
    db.session.commit()
    if dept_id_for_flag:
        _flag_timetable_stale(
            dept_id_for_flag,
            f'Teacher "{teacher_name}" was deleted from {dept_id_for_flag}.',
        )
        db.session.commit()
    flash(f'Teacher "{teacher_name}" and all associated records deleted.', 'success')
    return redirect(url_for('admin_teachers'))


# ─── Courses ───────────────────────────────────────────────────

@app.route('/admin/subjects')
@login_required
def admin_subjects():
    if not is_admin():
        return redirect(url_for('login'))

    dept_filter = request.args.get('dept', '')
    search = request.args.get('search', '').strip()

    # Valid student departments (BSH excluded from student dept display)
    STUDENT_DEPTS = ['AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC']

    query = Course.query
    if dept_filter:
        query = query.filter(Course.department == dept_filter)
    elif not dept_filter:
        # By default, show only student-dept courses (exclude BSH/unknown)
        query = query.filter(Course.department.in_(STUDENT_DEPTS))
    if search:
        query = query.filter(
            (Course.name.ilike(f'%{search}%')) | (Course.code.ilike(f'%{search}%'))
        )

    courses = query.order_by(Course.department, Course.semester, Course.name).all()
    departments = Department.query.filter(
        Department.id.in_(STUDENT_DEPTS)
    ).order_by(Department.name).all()

    return render_template('admin_subjects.html',
                           courses=courses,
                           departments=departments,
                           dept_filter=dept_filter,
                           search=search)



@app.route('/admin/course/get/<path:code>', methods=['GET'])
@login_required
def admin_course_get(code):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    dept = request.args.get('dept', '')
    if dept:
        c = Course.query.filter_by(code=code, department=dept).first_or_404()
    else:
        c = Course.query.filter_by(code=code).first_or_404()
    return jsonify({
        'id': c.id, 'code': c.code, 'name': c.name,
        'department': c.department, 'dept_id': c.dept_id or '',
        'type': c.type, 'hours_per_week': c.hours_per_week,
        'semester': c.semester, 'division': c.division, 'revision': c.revision,
    })


@app.route('/admin/course/add', methods=['POST'])
@login_required
def admin_course_add():
    if not is_admin():
        return redirect(url_for('login'))

    code       = request.form.get('code', '').strip()
    name       = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    dept_id    = request.form.get('dept_id', '').strip()

    if not code or not name:
        flash('Course code and name are required.', 'error')
    elif Course.query.filter_by(code=code, department=department).first():
        flash(f'Course code "{code}" already exists for department "{department}".', 'error')
    else:
        db.session.add(Course(
            code=code, name=name,
            department=department,
            dept_id=dept_id,
            type=request.form.get('type', '').strip(),
            hours_per_week=request.form.get('hours_per_week', '').strip(),
            semester=request.form.get('semester', '').strip(),
            division=request.form.get('division', '').strip(),
            revision=request.form.get('revision', '').strip(),
        ))
        db.session.commit()
        flash(f'Course "{name}" added.', 'success')

    return redirect(url_for('admin_subjects'))


@app.route('/admin/course/edit/<int:course_id>', methods=['POST'])
@login_required
def admin_course_edit(course_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    c = db.session.get(Course, course_id)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    c.name           = request.form.get('name', c.name).strip()
    c.department     = request.form.get('department', c.department).strip()
    c.dept_id        = request.form.get('dept_id', c.dept_id or '').strip()
    c.type           = request.form.get('type', c.type).strip()
    c.hours_per_week = request.form.get('hours_per_week', c.hours_per_week).strip()
    c.semester       = request.form.get('semester', c.semester).strip()
    c.division       = request.form.get('division', c.division).strip()
    c.revision       = request.form.get('revision', c.revision).strip()
    db.session.commit()
    flash(f'Course "{c.name}" updated.', 'success')
    return redirect(url_for('admin_subjects'))


@app.route('/admin/course/delete/<int:course_id>', methods=['POST'])
@login_required
def admin_course_delete(course_id):
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    c = db.session.get(Course, course_id)
    if not c:
        flash('Course not found.', 'error')
        return redirect(url_for('admin_subjects'))
    TeacherPreference.query.filter_by(course_code=c.code).delete()
    db.session.delete(c)
    db.session.commit()
    flash(f'Course "{c.name}" deleted.', 'success')
    return redirect(url_for('admin_subjects'))


# ─── Admin Timetable View ──────────────────────────────────────

@app.route('/admin/timetable')
@login_required
def admin_timetable():
    import json
    if not is_admin():
        return redirect(url_for('login'))

    departments = Department.query.order_by(Department.name).all()
    teachers_all = Teacher.query.order_by(Teacher.name).all()

    dept_id  = request.args.get('dept_id', '')
    division = request.args.get('division', '')

    classes = []
    if dept_id:
        courses = Course.query.filter_by(department=dept_id).all()
        for c in courses:
            for div in (c.division or '').split(','):
                div = div.strip().strip('"')
                if div and div not in classes:
                    classes.append(div)
        classes.sort()

    from database import Timetable
    timetable_data = None
    timetable_meta = None
    if dept_id and division:
        tt = Timetable.query.filter_by(dept_id=dept_id, division=division).order_by(Timetable.id.desc()).first()
        if tt:
            try:
                timetable_data = json.loads(tt.data_json)
                timetable_meta = tt
            except Exception:
                timetable_data = None

    return render_template('admin_timetable.html',
                           departments=departments,
                           teachers_all=teachers_all,
                           selected_dept=dept_id,
                           selected_division=division,
                           classes=classes,
                           timetable_data=timetable_data,
                           timetable_meta=timetable_meta)


# ─── Subjects / Courses ────────────────────────────────────────

# ─── API for dynamic dropdowns ─────────────────────────────────

@app.route('/api/classes')
@login_required
def api_classes():
    """Return list of divisions for a given department."""
    dept_id = request.args.get('dept_id', '')
    classes = []
    if dept_id:
        courses = Course.query.filter_by(department=dept_id).all()
        for c in courses:
            for div in (c.division or '').split(','):
                div = div.strip().strip('"')
                if div and div not in classes:
                    classes.append(div)
        classes.sort()
    return jsonify(classes)


@app.route('/api/divisions-by-sem')
@login_required
def api_divisions_by_sem():
    """Return list of division keys for a given department AND semester (from stored timetables)."""
    dept_id  = request.args.get('dept_id', '')
    semester = request.args.get('semester', '')
    if dept_id and semester:
        rows = Timetable.query.filter_by(
            dept_id=dept_id,
            semester=str(semester),
            timetable_type='class'
        ).order_by(Timetable.key).all()
        keys = sorted({r.key for r in rows})
    else:
        keys = []
    return jsonify(keys)


@app.route('/api/timetable')
@login_required
def api_timetable():
    """Return stored timetable JSON for a given dept+division."""
    import json
    from database import Timetable
    dept_id  = request.args.get('dept_id', '')
    division = request.args.get('division', '')
    if not dept_id or not division:
        return jsonify({'error': 'dept_id and division required'}), 400
    tt = Timetable.query.filter_by(dept_id=dept_id, division=division).order_by(Timetable.id.desc()).first()
    if not tt:
        return jsonify({'found': False})
    try:
        data = json.loads(tt.data_json)
    except Exception:
        data = {}
    return jsonify({'found': True, 'data': data,
                    'dept_id': tt.dept_id, 'semester': tt.semester,
                    'division': tt.division, 'effective_date': tt.effective_date})


# ──────────────────────────────────────────────────────────────
# Timetable Generation (Subject Assignment + Scheduling)
# ──────────────────────────────────────────────────────────────

STUDENT_DEPTS = ['AU', 'CE', 'EEE', 'ME', 'CS', 'AIDS', 'EC']

SEMESTER_MAP = {
    'odd':  [1, 3, 5, 7],
    'even': [2, 4, 6, 8],
}


def _semester_type(semester: int) -> str:
    return 'odd' if semester % 2 == 1 else 'even'


@app.route('/admin/fill-teacher-shortages', methods=['POST'])
@login_required
def admin_fill_teacher_shortages():
    """
    Detect subjects that exist in the course data but currently have no
    available real teacher (the allocation engine falls back to a dummy
    placeholder for these — see subject_assignment_engine.py PHASE 6), and
    auto-create real faculty to cover them.

    This only helps the subset of blank/placeholder timetable cells caused
    by a genuine TEACHER shortage. It cannot help cells that are blank
    because a division is missing course/subject data entirely — no new
    teacher can be assigned to a subject that was never scheduled for that
    division in the first place. See validate_course_data.py for that
    category; it's a data-completeness problem, not a staffing one.

    New teachers get a generic entry-level designation and NO subject
    assignments of their own yet — those are decided by re-running the
    normal "Assign Subjects" pipeline afterwards, so they're evaluated by
    the same priority/seniority/specialization rules as everyone else and
    end up with a realistic weekly load, not a special-cased one.
    """
    if not is_admin():
        return redirect(url_for('login'))

    sem_type = SystemSettings.get('active_semester_type', 'odd')
    target_sems = SEMESTER_MAP.get(sem_type, [1, 3, 5, 7])
    depts = Department.query.filter(Department.id.in_(STUDENT_DEPTS)).all()

    from subject_assignment_engine import load_from_db
    db_path = os.path.join(os.path.dirname(__file__), 'timetable.db')

    # ── Detection pass: dry-run assignment, nothing persisted ──────────────
    # Track the MAX number of dummy teachers any single semester needed per
    # department — a teacher created once is available to every semester
    # in future runs, so we only need enough to cover the worst case.
    shortage_by_dept: dict = {}
    shortage_subjects: dict = {}   # dept -> set of subject names, for the summary message

    valid_dept_ids = {d.id for d in depts} | {'BSH'}

    for dept_obj in depts:
        dept = dept_obj.id
        for sem in target_sems:
            try:
                engine = load_from_db(db_path, dept, sem)
                assignments = engine.assign()
            except Exception:
                continue
            # Group by the dummy's OWN dept_id — the allocation engine sets
            # this to the actual required TEACHER department (subj.teacher_
            # dept_id, e.g. 'CE' for a CS-curriculum subject taught by a CE
            # instructor), which can differ from `dept` (the student-facing
            # department whose course list we loaded). Using `dept` here
            # would create new faculty in the wrong department entirely.
            per_dummy_dept: dict = {}
            for a in assignments:
                if getattr(a.teacher, 'is_dummy', False):
                    raw = (a.teacher.dept_id or dept).strip()
                    # Source data has inconsistent casing ('ce' vs 'CE') and
                    # occasional malformed multi-dept values ('EEE, EC') —
                    # normalize and only accept a value that actually exists
                    # as a Department; otherwise fall back to the student
                    # dept rather than creating faculty under a bogus ID.
                    teacher_dept = raw.upper() if raw.upper() in valid_dept_ids else dept
                    per_dummy_dept.setdefault(teacher_dept, set()).add(a.teacher.teacher_id)
                    shortage_subjects.setdefault(teacher_dept, set()).add(a.subject.name)
            for teacher_dept, dummy_ids in per_dummy_dept.items():
                shortage_by_dept[teacher_dept] = max(
                    shortage_by_dept.get(teacher_dept, 0), len(dummy_ids)
                )

    if not shortage_by_dept:
        flash('No teacher shortages detected — every scheduled subject '
              'already has a real teacher available. (Blank cells, if any, '
              'are caused by missing course data, not a staffing gap — see '
              'validate_course_data.py.)', 'success')
        return redirect(url_for('admin_dashboard'))

    # ── Create real faculty to cover each shortage ──────────────────────────
    created = []
    for dept, n_needed in shortage_by_dept.items():
        for _ in range(n_needed):
            teacher_id = _generate_teacher_id(dept)
            name = f'New Faculty ({teacher_id.upper()})'
            teacher = Teacher(
                id=teacher_id, name=name, designation='Assistant Professor',
                dept_id=dept,
            )
            db.session.add(teacher)

            first = teacher_id.lower()
            username = first
            suffix = 1
            while TeacherAuth.query.filter_by(username=username).first():
                username = f'{first}{suffix}'
                suffix += 1
            auth = TeacherAuth(teacher_id=teacher_id, username=username, role='teacher')
            auth.set_password(first)
            db.session.add(auth)
            created.append((dept, teacher_id, username))

        _recalculate_seniority(dept)
        _flag_timetable_stale(
            dept,
            f'{n_needed} new faculty added to cover a teacher shortage for: '
            f'{", ".join(sorted(shortage_subjects.get(dept, [])))}.'
        )
    db.session.commit()

    # ── Regenerate so the new teachers are evaluated normally and pick up
    #    the previously-uncovered subjects with a realistic workload ────────
    admin_assign_subjects()

    summary = '; '.join(f'{dept}: +{n}' for dept, n in shortage_by_dept.items())
    creds = '; '.join(f'{tid} (login: {u}/{u})' for _, tid, u in created)
    flash(
        f'Created {len(created)} new faculty to cover teacher shortages ({summary}). '
        f'Timetables regenerated. New logins — {creds}',
        'success',
    )
    return redirect(url_for('admin_teachers'))


@app.route('/admin/assign-subjects', methods=['POST'])
@login_required
def admin_assign_subjects():
    """
    One-click: Assign subjects for all depts/sems, then generate full timetables.
    Maintains global teacher load tracking across all dept+sem iterations so that
    cross-department teachers (e.g. BSH) are never over-assigned.
    Rule: max 2 theory + 1 lab per teacher across the active semester mode.
    """
    if not is_admin():
        return redirect(url_for('login'))

    sem_type = 'odd'
    s = SystemSettings.query.filter_by(key='active_semester_type').first()
    if s:
        sem_type = s.value

    target_sems = SEMESTER_MAP.get(sem_type, [1, 3, 5, 7])
    depts = Department.query.filter(Department.id.in_(STUDENT_DEPTS)).all()

    import sys as _sys, json as _json
    from collections import defaultdict as _dd
    from datetime import datetime as _dt
    _sys.path.insert(0, os.path.dirname(__file__))
    from subject_assignment_engine import load_from_db, validate_before_timetable, Teacher as _EngTeacher
    from timetable_generator_engine import TimetableGeneratorEngine

    db_path   = os.path.join(os.path.dirname(__file__), 'timetable.db')
    now_str   = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
    today_str = _dt.now().strftime('%Y-%m-%d')

    total_assigns = 0
    total_classes = 0
    errors = []

    # ─── Global teacher load tracking ────────────────────────────────────────
    # Tracks accumulated theory/lab loads for every teacher across ALL
    # dept+sem iterations.  Keyed by teacher_id (string).
    # Format: {teacher_id: {'theory': int, 'lab': int}}
    global_teacher_loads: dict = {}

    def _apply_global_loads(engine):
        """Inject previously accumulated loads into a freshly-built engine."""
        for t in engine.teachers:
            prev = global_teacher_loads.get(t.teacher_id, {})
            t.assigned_load['theory']      = prev.get('theory', 0)
            t.assigned_load['lab']         = prev.get('lab', 0)
            t.weekly_periods_used          = prev.get('weekly_periods', 0)
            # Restore the set of already-assigned subject IDs so the
            # unique-subject rule (non-BSH: no same subject in two divisions)
            # is enforced across engine calls.
            saved_sids = prev.get('theory_subject_ids', set())
            t.assigned_theory_subject_ids  = set(saved_sids)

    def _record_global_loads(engine):
        """After assignment, save each teacher's net load back to the global dict."""
        for t in engine.teachers:
            if t.teacher_id not in global_teacher_loads:
                global_teacher_loads[t.teacher_id] = {
                    'theory': 0, 'lab': 0,
                    'weekly_periods': 0, 'theory_subject_ids': set(),
                }
            gl = global_teacher_loads[t.teacher_id]
            gl['theory']             = t.assigned_load.get('theory', 0)
            gl['lab']                = t.assigned_load.get('lab', 0)
            gl['weekly_periods']     = t.weekly_periods_used
            # Merge subject IDs (accumulate across engine calls)
            gl.setdefault('theory_subject_ids', set()).update(
                getattr(t, 'assigned_theory_subject_ids', set())
            )
    # ─────────────────────────────────────────────────────────────────────────

    # ── Step 1: Wipe old assignments & reassign ───────────────────────────────
    for dept_obj in depts:
        dept = dept_obj.id
        SubjectAssignment.query.filter_by(dept_id=dept, semester_type=sem_type).delete()
    db.session.commit()

    for dept_obj in depts:
        dept = dept_obj.id
        for sem in target_sems:
            try:
                engine = load_from_db(db_path, dept, sem)
                _apply_global_loads(engine)          # restore accumulated loads
                # FIX: reset_loads=False preserves the injected global loads
                # (the old engine.assign() unconditionally reset them, destroying
                # the cross-dept overload protection entirely)
                assignments = engine.assign(preserve_loads=True)
                _record_global_loads(engine)         # save updated loads

                # FIX S14: Critical validation gate between Engine 1 and Engine 2
                try:
                    validate_before_timetable(assignments, engine.teachers, engine.subjects, engine.divisions)
                except (ValueError, AssertionError) as e:
                    errors.append(f'Validation failed for {dept} Sem {sem}: {e}')
                    continue # Skip saving assignments if validation fails

                for rec in assignments:
                    db.session.add(SubjectAssignment(
                        dept_id       = dept,
                        semester      = sem,
                        semester_type = sem_type,
                        division_id   = rec.division.division_id,
                        subject_code  = rec.subject.subject_id,
                        subject_name  = rec.subject.name,
                        subject_type  = rec.subject.subject_type.value,
                        teacher_id    = rec.teacher.teacher_id,
                        teacher_name  = rec.teacher.name,
                        role          = rec.role,
                        created_at    = now_str,
                    ))
                    total_assigns += 1
            except Exception as exc:
                errors.append(f'Assign {dept} Sem {sem}: {exc}')

    db.session.commit()

    # ── Step 2: Timetable Generation ─────────────────────────────────────────
    # FIX: Do NOT call assign() again here.  The old code cleared global loads
    # and re-ran assignment from scratch, which caused teacher overload because
    # every iteration started with zero loads (BSH/cross-dept teachers could
    # accumulate unlimited subjects across departments).
    #
    # Instead, rebuild engine assignments directly from the SubjectAssignment
    # rows that Step 1 already saved.  This guarantees the timetable generator
    # sees exactly the same subject→teacher mapping that was carefully computed
    # (with global load tracking) in Step 1.

    from subject_assignment_engine import (
        SubjectAssignment as _EngSA,
    )

    _depts_generated_ok: set = set()
    # Accumulates every teacher's booked clock-time intervals across ALL
    # (dept, semester) engine runs in this generation cycle — see gap #4.
    cross_run_teacher_busy: dict = {}

    for dept_obj in depts:
        dept = dept_obj.id
        for sem in target_sems:
            try:
                # Load engine only for its period/division/subject structures
                engine = load_from_db(db_path, dept, sem)

                # Rebuild assignments from DB (Step 1 output) -----------
                db_asgns = SubjectAssignment.query.filter_by(
                    dept_id=dept, semester=sem, semester_type=sem_type
                ).all()

                if not db_asgns:
                    continue   # nothing was assigned for this dept+sem

                teacher_map_e  = {t.teacher_id: t for t in engine.teachers}
                subject_map_e  = {s.subject_id: s for s in engine.subjects}
                division_map_e = {d.division_id: d for d in engine.divisions}

                # ── Import engine types needed for on-the-fly construction ──
                from subject_assignment_engine import (
                    Designation as _EngDesig,
                    Division    as _EngDiv,
                )

                assignments = []
                skipped_rows = []
                for dba in db_asgns:
                    tid  = str(dba.teacher_id)
                    sc   = str(dba.subject_code)
                    diid = str(dba.division_id)

                    t = teacher_map_e.get(tid)

                    # ── Dummy teacher: create a lightweight Teacher object ──
                    # Dummy teachers are never stored in the teachers table, so
                    # teacher_map_e won't contain them.  Recreate from the
                    # saved teacher_name / teacher_id.
                    if t is None and tid.startswith('dummy-'):
                        t = _EngTeacher(
                            teacher_id  = tid,
                            name        = str(dba.teacher_name or tid),
                            dept_id     = dept,
                            designation = _EngDesig.LECTURER,
                            is_dummy    = True,
                        )
                        teacher_map_e[tid] = t   # cache for subsequent rows

                    s = subject_map_e.get(sc)

                    # ── Division fallback ──
                    # If the saved division_id isn't in the engine (can happen
                    # when the course table lacks explicit division tokens and
                    # the engine defaulted to "A" but the DB stores the dept
                    # code as the division), create a minimal Division object.
                    div = division_map_e.get(diid)
                    if div is None and diid:
                        div = _EngDiv(
                            division_id     = diid,
                            student_dept_id = dept,
                            semester        = sem,
                        )
                        division_map_e[diid] = div   # cache

                    if t and s and div:
                        assignments.append(
                            _EngSA(subject=s, division=div, teacher=t, role=dba.role)
                        )
                    else:
                        skipped_rows.append(
                            f"teacher={tid}(found={t is not None}) "
                            f"subject={sc}(found={s is not None}) "
                            f"div={diid}(found={div is not None})"
                        )

                if not assignments:
                    detail = '; '.join(skipped_rows[:5])
                    errors.append(
                        f'No engine-reconstructable assignments for {dept} Sem {sem}. '
                        f'Skipped {len(skipped_rows)} rows. First failures: {detail}'
                    )
                    continue

                if skipped_rows:
                    errors.append(
                        f'{dept} Sem {sem}: {len(skipped_rows)} assignment row(s) could not '
                        f'be reconstructed (partial timetable). First: {skipped_rows[0]}'
                    )
                # --------------------------------------------------------

                tt_eng = TimetableGeneratorEngine()
                lab_capacity_map = {d.id: (d.lab_room_capacity or 2) for d in depts}
                tt_eng.load_data(assignments, lab_capacity_map=lab_capacity_map)
                tt_eng.block_external_busy_slots(cross_run_teacher_busy)
                # guarantee_fill=True runs the Phase-3 guaranteed-fill pass that
                # eliminates zero-slack blank periods (S7/S8 fix). Every division
                # is filled to min(required, 29) with hard clash rules preserved.
                tt_eng.generate(use_cpsat=False, guarantee_fill=True)
                exported = tt_eng.export_to_dict()

                # ── Cross-run clash detection (gap #4) ─────────────────
                # Each TimetableGeneratorEngine() instance only knows about
                # the (dept, semester) it was built for, so a teacher shared
                # across two year-groups (e.g. a BSH teacher on both a Year 2
                # and a Year 4 class) is never checked against their OTHER
                # bookings by the engine itself. Compare this run's bookings,
                # converted to actual clock time, against everything booked
                # so far in this generation cycle.
                new_busy = tt_eng.export_teacher_busy_intervals()
                clashes = TimetableGeneratorEngine.find_cross_run_teacher_clashes(
                    cross_run_teacher_busy, new_busy
                )
                if clashes:
                    errors.append(
                        f'{dept} Sem {sem}: {len(clashes)} cross-semester teacher '
                        f'clash(es) detected. ' + '; '.join(clashes[:3])
                    )
                for tid, intervals in new_busy.items():
                    cross_run_teacher_busy.setdefault(tid, []).extend(intervals)

                # Wipe previous timetable data for this dept+semester
                Timetable.query.filter_by(dept_id=dept, semester=str(sem)).delete()
                db.session.commit()

                # ── Persist class timetables ──────────────────────────
                for div_id, cells in exported.get('class_timetables', {}).items():
                    db.session.add(Timetable(
                        dept_id        = dept,
                        semester       = str(sem),
                        semester_type  = sem_type,
                        division       = div_id,
                        timetable_type = 'class',
                        key            = div_id,
                        effective_date = today_str,
                        data_json      = _json.dumps(cells),
                        created_at     = now_str,
                    ))
                    total_classes += 1

                # ── Persist teacher timetables (per-semester rows) ────
                for tid, cells in exported.get('teacher_timetables', {}).items():
                    Timetable.query.filter_by(
                        dept_id=dept, semester=str(sem),
                        timetable_type='teacher', key=tid
                    ).delete()
                    db.session.add(Timetable(
                        dept_id        = dept,
                        semester       = str(sem),
                        semester_type  = sem_type,
                        division       = tid,
                        timetable_type = 'teacher',
                        key            = tid,
                        effective_date = today_str,
                        data_json      = _json.dumps(cells),
                        created_at     = now_str,
                    ))

                db.session.commit()
                _depts_generated_ok.add(dept)

            except Exception as exc:
                errors.append(f'Generate {dept} Sem {sem}: {exc}')
                _depts_generated_ok.discard(dept)

    # ── Clear "new teacher, needs regeneration" flags for depts that just
    #    successfully regenerated (closes gap #5 from the constraints doc) ──
    for dept in _depts_generated_ok:
        _clear_timetable_stale(dept)
    db.session.commit()

    # ── Post-processing: create merged teacher timetables under teacher's own dept ─
    # FIX: Teacher timetables are stored per-semester under the *student* dept
    # (e.g. a BSH teacher's timetable lands under dept_id='CS' when generating CS).
    # HOD and admin teacher-dept-filter views query by the teacher's own dept_id,
    # so BSH HOD would see nothing.
    #
    # Solution: after all per-semester rows are committed, collect all cells for
    # each teacher across all student depts/sems, then store a single merged row
    # with semester='all' under the teacher's OWN dept_id.  All display queries
    # use this 'all' row; the per-semester rows remain for audit/debugging.
    try:
        all_teachers_db = Teacher.query.all()
        teacher_own_dept = {str(t.id): t.dept_id for t in all_teachers_db if t.dept_id}

        # Gather per-semester rows (exclude existing 'all' rows to avoid double-merge)
        sem_rows = Timetable.query.filter_by(
            timetable_type='teacher',
            semester_type=sem_type
        ).filter(Timetable.semester != 'all').all()

        teacher_merged: dict = _dd(list)
        for row in sem_rows:
            try:
                teacher_merged[row.key].extend(_json.loads(row.data_json))
            except Exception:
                pass

        for tid, cells in teacher_merged.items():
            own_dept = teacher_own_dept.get(str(tid))
            if not own_dept or not cells:
                continue
            # Delete any stale 'all' row, then insert fresh
            Timetable.query.filter_by(
                dept_id=own_dept,
                timetable_type='teacher',
                semester_type=sem_type,
                semester='all',
                key=tid,
            ).delete()
            db.session.add(Timetable(
                dept_id        = own_dept,
                semester       = 'all',
                semester_type  = sem_type,
                division       = tid,
                timetable_type = 'teacher',
                key            = tid,
                effective_date = today_str,
                data_json      = _json.dumps(cells),
                created_at     = now_str,
            ))
        db.session.commit()
    except Exception as exc:
        errors.append(f'Teacher timetable post-processing: {exc}')

    if errors:
        flash('Completed with errors: ' + '; '.join(errors[:5]), 'error')
    else:
        flash(
            f'Subject Assignment + Timetable Generation complete! '
            f'{total_assigns} assignments, {total_classes} class timetables generated '
            f'for {sem_type.title()} semesters.',
            'success'
        )
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/generate-timetable', methods=['GET', 'POST'])
@login_required
def admin_generate_timetable():
    """Redirect legacy route to dashboard (generation is now done via Assign Subjects)."""
    if not is_admin():
        return redirect(url_for('login'))
    flash('Timetable generation is now automatic when you click "Assign Subjects" from the Dashboard.', 'info')
    return redirect(url_for('admin_dashboard'))


# ──────────────────────────────────────────────────────────────
# Timetable Views (DB-driven, 3 types)
# ──────────────────────────────────────────────────────────────

@app.route('/admin/timetable/view')
@login_required
def admin_timetable_view():
    """Unified 3-tab timetable viewer. Reads only from the database."""
    if not is_admin():
        return redirect(url_for('login'))

    import json as _json

    # Active semester type (set from dashboard — not a user filter here)
    sem_type = 'odd'
    s = SystemSettings.query.filter_by(key='active_semester_type').first()
    if s:
        sem_type = s.value

    valid_sems = SEMESTER_MAP.get(sem_type, [1, 3, 5, 7])

    dept_id    = request.args.get('dept_id', '')
    semester   = request.args.get('semester', '')      # specific semester number e.g. "3"
    tab        = request.args.get('tab', 'class')      # class | teacher
    key        = request.args.get('key', '')            # division_id or teacher_id
    # For teacher tab: optional dept filter to restrict teacher list
    teacher_dept = request.args.get('teacher_dept', '')

    depts        = Department.query.filter(Department.id.in_(STUDENT_DEPTS)).order_by(Department.id).all()
    teachers_all = Teacher.query.order_by(Teacher.name).all()

    timetable_cells = []
    all_keys = []

    if tab == 'class':
        # Populate division/class list filtered by dept + semester
        if dept_id and semester:
            rows = Timetable.query.filter_by(
                dept_id=dept_id,
                semester=semester,
                timetable_type='class'
            ).order_by(Timetable.key).all()
            all_keys = list({r.key: r for r in rows}.keys())
        elif dept_id:
            rows = Timetable.query.filter_by(
                dept_id=dept_id,
                semester_type=sem_type,
                timetable_type='class'
            ).order_by(Timetable.semester, Timetable.key).all()
            all_keys = list({r.key: r for r in rows}.keys())

        if key:
            query = Timetable.query.filter_by(timetable_type='class', key=key)
            if semester:
                query = query.filter_by(semester=semester)
            tt = query.order_by(Timetable.id.desc()).first()
            if tt:
                try:
                    timetable_cells = _json.loads(tt.data_json)
                except Exception:
                    timetable_cells = []

    elif tab == 'teacher':
        # FIX: query only semester='all' rows (the canonical merged teacher view).
        # The old code queried ALL rows (per-semester + merged), causing duplication.
        if teacher_dept:
            teacher_tt_rows = Timetable.query.filter_by(
                dept_id=teacher_dept,
                timetable_type='teacher',
                semester_type=sem_type,
                semester='all',
            ).order_by(Timetable.key).all()
        else:
            teacher_tt_rows = Timetable.query.filter_by(
                timetable_type='teacher',
                semester_type=sem_type,
                semester='all',
            ).order_by(Timetable.key).all()
        all_keys = list({r.key: True for r in teacher_tt_rows}.keys())

        if key:
            key_rows = Timetable.query.filter_by(
                timetable_type='teacher',
                semester_type=sem_type,
                semester='all',
                key=key
            ).order_by(Timetable.id.asc()).all()
            if not key_rows:
                # Fall back to any available rows (pre-migration data)
                key_rows = Timetable.query.filter_by(
                    timetable_type='teacher',
                    semester_type=sem_type,
                    key=key
                ).order_by(Timetable.id.asc()).all()
            if key_rows:
                timetable_cells = []
                for row in key_rows:
                    try:
                        timetable_cells.extend(_json.loads(row.data_json))
                    except Exception:
                        pass

    # Build teacher ID → name map
    teacher_map = {t.id: t.name for t in teachers_all}

    return render_template(
        'admin_timetable_view.html',
        depts           = depts,
        teachers_all    = teachers_all,
        teacher_map     = teacher_map,
        selected_dept   = dept_id,
        teacher_dept    = teacher_dept,
        sem_type        = sem_type,
        valid_sems      = valid_sems,
        semester        = semester,
        tab             = tab,
        selected_key    = key,
        all_keys        = all_keys,
        timetable_cells = timetable_cells,
        DAYS            = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    )


# ── API Endpoints for dynamic dropdowns ────────────────────────

@app.route('/api/timetable/keys')
@login_required
def api_timetable_keys():
    """Return available keys (divisions/teachers) for a dept+sem_type+type."""
    dept_id  = request.args.get('dept_id', '')
    sem_type = request.args.get('sem_type', 'odd')
    tt_type  = request.args.get('type', 'class')

    if tt_type == 'teacher':
        # BUG FIX: Teacher model uses dept_id, NOT department_id.
        # filter_by(department_id=...) silently matched nothing, so the dropdown
        # always returned all teachers regardless of the selected department.
        if dept_id:
            teachers = Teacher.query.filter_by(dept_id=dept_id).order_by(Teacher.name).all()
        else:
            teachers = Teacher.query.order_by(Teacher.name).all()
        keys = [{'key': str(t.id), 'label': t.name} for t in teachers]
        return jsonify(keys)

    # For 'class' or 'dept' type, use the generated Timetable rows
    rows = Timetable.query.filter_by(
        dept_id=dept_id, semester_type=sem_type, timetable_type=tt_type
    ).order_by(Timetable.key).all()

    seen = {}
    keys = []
    for r in rows:
        if r.key not in seen:
            seen[r.key] = True
            keys.append({'key': r.key, 'label': r.key})
    return jsonify(keys)


@app.route('/api/timetable/data')
@login_required
def api_timetable_data():
    """Return timetable cell JSON for a specific key."""
    import json as _json
    dept_id  = request.args.get('dept_id', '')
    sem_type = request.args.get('sem_type', 'odd')
    tt_type  = request.args.get('type', 'class')
    key      = request.args.get('key', '')
    # ISSUE 1 FIX: Accept explicit semester so we load the correct row, not .first()
    semester = request.args.get('semester', '')

    if not key:
        return jsonify({'found': False})

    if tt_type == 'teacher':
        # Only load the canonical semester='all' merged row.
        tt_rows = Timetable.query.filter_by(
            timetable_type='teacher',
            semester_type=sem_type,
            semester='all',
            key=key
        ).order_by(Timetable.id.asc()).all()
        if not tt_rows:
            tt_rows = Timetable.query.filter_by(
                timetable_type='teacher', semester_type=sem_type, key=key
            ).order_by(Timetable.id.asc()).all()
        if not tt_rows:
            return jsonify({'found': False})
        merged = []
        for row in tt_rows:
            try:
                merged.extend(_json.loads(row.data_json))
            except Exception:
                pass

        # ISSUE 4 FIX: Enrich each teacher cell with semester_num + student_dept
        # so the legend "Class" column can display "S5 AI" / "S3 CS1" etc.
        # Build a lookup: division_id → {semester, dept_id} from class timetable rows.
        div_meta = {}
        for tt_row in Timetable.query.filter_by(timetable_type='class').all():
            if tt_row.key and tt_row.key not in div_meta:
                div_meta[tt_row.key] = {
                    'semester_num': tt_row.semester,
                    'student_dept': tt_row.dept_id,
                }
        for c in merged:
            div_id = c.get('division_id', '')
            if div_id and div_id in div_meta:
                c['semester_num'] = div_meta[div_id]['semester_num']
                c['student_dept'] = div_meta[div_id]['student_dept']

        return jsonify({
            'found': True,
            'cells': merged,
            'semester': 'all',
            'created_at': tt_rows[-1].created_at,
        })
    else:
        # ISSUE 1 FIX: Filter by explicit semester when provided so we never
        # return the wrong semester's timetable (.order_by.first() used to
        # always return the latest/highest row — i.e. sem 7 or 8).
        query = Timetable.query.filter_by(
            dept_id=dept_id, semester_type=sem_type, timetable_type=tt_type, key=key
        )
        if semester:
            query = query.filter_by(semester=str(semester))
        tt = query.order_by(Timetable.id.desc()).first()

        if not tt:
            return jsonify({'found': False})

        try:
            data = _json.loads(tt.data_json)
        except Exception:
            data = []

        # ISSUE 4 FIX: enrich class/dept cells with the teaching teacher's dept_id
        # so the "Department" column in the class-wise Subject Code Mapping table
        # can be populated without touching the timetable engine.
        teacher_dept_map = {t.id: (t.dept_id or '') for t in Teacher.query.all()}
        for c in data:
            tid = c.get('teacher_id')
            if tid and not c.get('teacher_dept'):
                c['teacher_dept'] = teacher_dept_map.get(tid, '')

        return jsonify({
            'found': True,
            'cells': data,
            'semester': tt.semester,
            'created_at': tt.created_at,
        })


@app.route('/admin/timetable/pdf/<tt_type>')
@login_required
def admin_timetable_pdf(tt_type):
    """
    Generate a PDF for class / teacher / dept timetable.
    Query params: dept_id, sem_type, semester, key
    """
    if not is_admin():
        return redirect(url_for('login'))

    import json as _json
    from flask import make_response

    dept_id  = request.args.get('dept_id', '')
    sem_type = request.args.get('sem_type', 'odd')
    key      = request.args.get('key', '')
    # ISSUE 1 FIX: read semester so PDF loads the correct row
    semester = request.args.get('semester', '')

    if tt_type == 'teacher':
        tt_rows = Timetable.query.filter_by(
            timetable_type='teacher', semester_type=sem_type, semester='all', key=key
        ).order_by(Timetable.id.asc()).all()
        if not tt_rows:
            tt_rows = Timetable.query.filter_by(
                timetable_type='teacher', semester_type=sem_type, key=key
            ).order_by(Timetable.id.asc()).all()
        if not tt_rows:
            flash(f'No timetable found for {key}', 'error')
            return redirect(url_for('admin_timetable_view',
                                    dept_id=dept_id, sem_type=sem_type, tab=tt_type))
        cells = []
        for row in tt_rows:
            try:
                cells.extend(_json.loads(row.data_json))
            except Exception:
                pass
        sem_label = 'All Semesters'
        created_at = tt_rows[-1].created_at
    else:
        # ISSUE 1 FIX: filter by semester so we get the correct semester row,
        # not the highest-id row (which was always semester 7 or 8).
        q = Timetable.query.filter_by(
            dept_id=dept_id, semester_type=sem_type, timetable_type=tt_type, key=key
        )
        if semester:
            q = q.filter_by(semester=str(semester))
        tt = q.order_by(Timetable.id.desc()).first()
        if not tt:
            flash(f'No timetable found for {key}', 'error')
            return redirect(url_for('admin_timetable_view',
                                    dept_id=dept_id, sem_type=sem_type, tab=tt_type))
        try:
            cells = _json.loads(tt.data_json)
        except Exception:
            cells = []
        sem_label = f'Semester {tt.semester}'
        created_at = tt.created_at

    teacher_name = key
    if tt_type == 'teacher':
        t = Teacher.query.get(key)
        if t:
            teacher_name = t.name

    # Enrich cells with teacher_dept (not stored in JSON, looked up from DB)
    _tdept_map = {str(t.id): (t.dept_id or '') for t in Teacher.query.all()}
    for _c in cells:
        if not _c.get('teacher_dept') and _c.get('teacher_id'):
            _c['teacher_dept'] = _tdept_map.get(str(_c['teacher_id']), '')
        pdf_bytes = _render_timetable_pdf(
        cells      = cells,
        title      = f"{tt_type.title()} Timetable — {teacher_name if tt_type == 'teacher' else key}",
        subtitle   = f"{dept_id} | {sem_type.title()} {sem_label}",
        is_teacher = (tt_type == 'teacher'),
    )
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = (
        f'attachment; filename="timetable_{tt_type}_{key}.pdf"'
    )
    return response



# ── PDF renderer ───────────────────────────────────────────────

def _render_timetable_pdf(cells: list, title: str, subtitle: str,
                           is_teacher: bool = False) -> bytes:
    """
    Render timetable PDF matching the website layout exactly.
    Columns: Day | P1..Pn (pre-lunch) | LUNCH | P(n+1).. (post-lunch)
    Friday: first post-lunch column = extended lunch, subjects shifted right.
    is_teacher=True disables the Friday extended lunch shift.
    """
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from collections import defaultdict

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1*cm, rightMargin=1*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    story  = []
    story.append(Paragraph(f"<b>{title}</b>", styles['Title']))
    story.append(Paragraph(subtitle, styles['Normal']))
    story.append(Spacer(1, 0.4*cm))

    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    # ── Build day × period lookup ──────────────────────────────────────────
    by_day: dict = defaultdict(list)
    for c in cells:
        by_day[c['day']].append(c)
    for d in DAYS:
        by_day[d].sort(key=lambda c: c['period_number'])

    # ── Period metadata ────────────────────────────────────────────────────
    meta_wd, meta_fri = {}, {}
    for c in cells:
        pn = c['period_number']
        if c['day'] != 'Friday' and pn not in meta_wd:
            meta_wd[pn] = {'start': c.get('period_start',''), 'end': c.get('period_end','')}
        if c['day'] == 'Friday' and pn not in meta_fri:
            meta_fri[pn] = {'start': c.get('period_start',''), 'end': c.get('period_end','')}

    raw_pnums = sorted({c['period_number'] for c in cells})

    # ── Teacher timetable: always show all 7 periods so free slots are visible ──
    if is_teacher and raw_pnums:
        max_pn = max(max(raw_pnums), 7)
        min_pn = min(min(raw_pnums), 1)
        all_pnums = list(range(min_pn, max_pn + 1))
    else:
        all_pnums = raw_pnums

    # Standard period timings used to fill in columns where a teacher has no class
    STD_WD = {
        1:('08:45','09:35'), 2:('09:35','10:25'), 3:('10:35','11:30'),
        4:('12:15','13:05'), 5:('13:05','13:55'), 6:('14:05','14:55'),
        7:('14:55','15:45'),
    }
    STD_FRI = {
        1:('08:45','09:35'), 2:('09:35','10:25'), 3:('10:35','11:30'),
        4:('11:30','12:15'), 5:('14:15','14:55'), 6:('14:55','15:45'),
    }
    if is_teacher:
        for pn in all_pnums:
            if pn not in meta_wd and pn in STD_WD:
                meta_wd[pn] = {'start': STD_WD[pn][0], 'end': STD_WD[pn][1]}
            if pn not in meta_fri and pn in STD_FRI:
                meta_fri[pn] = {'start': STD_FRI[pn][0], 'end': STD_FRI[pn][1]}

    period_meta = {pn: meta_wd.get(pn) or meta_fri.get(pn) or {} for pn in all_pnums}

    # ── Find weekday lunch gap (largest gap) ──────────────────────────────
    wd_pnums  = [pn for pn in all_pnums if pn in meta_wd] or all_pnums
    max_gap, lunch_after_pnum = 0, wd_pnums[len(wd_pnums)//2 - 1]
    for i in range(len(wd_pnums)-1):
        m0 = meta_wd.get(wd_pnums[i]) or meta_fri.get(wd_pnums[i]) or {}
        m1 = meta_wd.get(wd_pnums[i+1]) or meta_fri.get(wd_pnums[i+1]) or {}
        if not m0.get('end') or not m1.get('start'):
            continue
        try:
            eh,em = int(m0['end'][:2]), int(m0['end'][3:5])
            sh,sm = int(m1['start'][:2]), int(m1['start'][3:5])
            gap = (sh*60+sm) - (eh*60+em)
            if gap > max_gap:
                max_gap = gap
                lunch_after_pnum = wd_pnums[i]
        except (ValueError, IndexError):
            pass

    lunch_idx = all_pnums.index(lunch_after_pnum)
    pre  = all_pnums[:lunch_idx+1]
    post = all_pnums[lunch_idx+1:]

    # Weekday lunch label
    wd_ls = (meta_wd.get(pre[-1]) or meta_fri.get(pre[-1]) or {}).get('end','')
    wd_le = (meta_wd.get(post[0]) or meta_fri.get(post[0]) or {}).get('start','') if post else ''
    wd_lunch_label = f"{wd_ls}\u2013{wd_le}" if wd_ls and wd_le else "12:00\u201312:45"

    # Friday lunch label
    fri_pnums = [pn for pn in all_pnums if pn in meta_fri]
    fri_max_gap, fri_lunch_after = 0, lunch_after_pnum
    for i in range(len(fri_pnums)-1):
        m0 = meta_fri.get(fri_pnums[i]) or {}
        m1 = meta_fri.get(fri_pnums[i+1]) or {}
        if not m0.get('end') or not m1.get('start'):
            continue
        try:
            eh,em = int(m0['end'][:2]), int(m0['end'][3:5])
            sh,sm = int(m1['start'][:2]), int(m1['start'][3:5])
            gap = (sh*60+sm)-(eh*60+em)
            if gap > fri_max_gap:
                fri_max_gap = gap
                fri_lunch_after = fri_pnums[i]
        except (ValueError, IndexError):
            pass
    fri_lunch_after_idx = fri_pnums.index(fri_lunch_after) if fri_lunch_after in fri_pnums else -1
    fri_next_pn = fri_pnums[fri_lunch_after_idx+1] if fri_lunch_after_idx < len(fri_pnums)-1 else None
    fri_ls = (meta_fri.get(fri_lunch_after) or {}).get('end','') or wd_ls
    fri_le = (meta_fri.get(fri_next_pn) or {}).get('start','') if fri_next_pn else ''
    fri_lunch_label = f"{fri_ls}\u2013{fri_le}" if fri_ls and fri_le else "12:15\u201314:15"

    # ── Colors ─────────────────────────────────────────────────────────────
    C_HEADER   = colors.HexColor('#e8edf5')   # period header bg
    C_DAY      = colors.HexColor('#f0f3f8')   # day label bg
    C_DAY_FRI  = colors.HexColor('#e2f4e8')   # Friday day label
    C_LUNCH    = colors.HexColor('#fef6dc')   # lunch col
    C_ALT_ROW  = colors.HexColor('#f7f9fc')   # alternating row
    C_LAB      = colors.HexColor('#f0ebff')   # lab cell
    C_REMEDIAL = colors.HexColor('#fef9c3')   # remedial cell
    C_BORDER   = colors.HexColor('#b0bec5')   # table border
    C_HEADER_TXT = colors.HexColor('#1a202c')
    C_LUNCH_TXT  = colors.HexColor('#7a5000')
    C_DAY_FRI_TXT= colors.HexColor('#1b5e20')
    C_BODY_TXT   = colors.HexColor('#1a202c')
    C_TEACHER_TXT= colors.HexColor('#546e7a')

    # ── Build table data ────────────────────────────────────────────────────
    # Columns: [Day] + [pre periods] + [LUNCH] + [post periods]
    n_pre  = len(pre)
    n_post = len(post)
    n_cols = 1 + n_pre + 1 + n_post  # Day + pre + lunch + post

    def period_hdr(pn):
        m = period_meta.get(pn, {})
        s, e = m.get('start',''), m.get('end','')
        return f"P{pn}\n{s}\u2013{e}" if s and e else f"P{pn}"

    # Header row
    # LUNCH header — display "LUNCH BREAK" on first line, time on second
    lunch_hdr_text = f"LUNCH\nBREAK\n{wd_lunch_label}"
    header = ['Day'] + [period_hdr(pn) for pn in pre] + [lunch_hdr_text] + [period_hdr(pn) for pn in post]
    rows = [header]

    day_styles = []  # per-cell color styles

    for row_idx, day in enumerate(DAYS, start=1):
        day_cells_map = {c['period_number']: c for c in by_day[day]}
        fri = (day == 'Friday')

        def cell_text(c):
            if not c:
                return ''
            if c.get('is_remedial'):
                return 'REMEDIAL'
            code = c.get('short_code') or c.get('subject_code') or c.get('subject_name') or ''
            # Parallel-lab cell: show "Lab1/Lab2"
            paired_code = c.get('paired_short_code') or c.get('paired_subject_code') or ''
            if paired_code and not is_teacher:
                return f"{code[:8]}/{paired_code[:8]}"
            teacher = c.get('teacher_name') or ''
            # For teacher mode, show division; for class mode show teacher
            extra = ''
            if is_teacher and c.get('division_id'):
                extra = c['division_id']
            elif not is_teacher and teacher:
                # shorten teacher name
                parts = teacher.split()
                titles = ['Dr.','Mr.','Ms.','Prof.','Dr','Mr','Ms','Prof']
                if len(parts) > 1 and parts[0] in titles:
                    extra = parts[0] + ' ' + parts[1]
                elif parts:
                    extra = parts[0]
            return f"{code[:10]}\n{extra}" if extra else code[:12]

        # Pre-lunch cols
        pre_cells = [cell_text(day_cells_map.get(pn)) for pn in pre]

        # LUNCH col + post cols
        # Friday (class timetable): post[0] = extended lunch cell, post[k>=1] = shifted subject data
        # This exactly mirrors the JS buildGrid: post.forEach (colIdx=0→lunch, colIdx>0→post[colIdx-1] data)
        lunch_col = ''  # LUNCH DIVIDER column stays empty for ALL rows (header covers it via rowspan)
        if fri and not is_teacher:
            post_cells = []
            for i, pn in enumerate(post):
                if i == 0:
                    # First post column = Friday extended lunch break cell
                    post_cells.append(f"Lunch\nBreak\n{fri_lunch_label}")
                else:
                    # Shift: use data from post[i-1] period (mirrors JS: dataPn = post[colIdx-1])
                    src_pn = post[i - 1]
                    post_cells.append(cell_text(day_cells_map.get(src_pn)))
        else:
            post_cells = [cell_text(day_cells_map.get(pn)) for pn in post]

        row_data = [day] + pre_cells + [lunch_col] + post_cells
        rows.append(row_data)

        # Color styles for this row
        lunch_col_idx = 1 + n_pre  # 0-based column index of lunch col
        # Alternating row background — skip lunch divider col and Friday extended-lunch cell
        if row_idx % 2 == 0:
            day_styles.append(('BACKGROUND', (1, row_idx), (lunch_col_idx-1, row_idx), C_ALT_ROW))
            # For Friday class timetable: post[0] = extended lunch cell → don't alt-color it
            alt_post_start = lunch_col_idx + 2 if (fri and not is_teacher) else lunch_col_idx + 1
            day_styles.append(('BACKGROUND', (alt_post_start, row_idx), (-1, row_idx), C_ALT_ROW))

        # Lab cells — pre-lunch
        for col_i, pn in enumerate(pre):
            c = day_cells_map.get(pn)
            if c and (c.get('is_lab') or (c.get('subject_type','').lower()=='lab')):
                day_styles.append(('BACKGROUND', (1+col_i, row_idx), (1+col_i, row_idx), C_LAB))
        # Lab cells — post-lunch (for Friday: source period is post[col_i-1] due to shift)
        for col_i, pn in enumerate(post):
            src_pn = post[col_i-1] if (fri and not is_teacher and col_i > 0) else pn
            c = day_cells_map.get(src_pn)
            if c and (c.get('is_lab') or (c.get('subject_type','').lower()=='lab')):
                day_styles.append(('BACKGROUND', (lunch_col_idx+1+col_i, row_idx), (lunch_col_idx+1+col_i, row_idx), C_LAB))

        # Friday extended lunch cell — styled to match the LUNCH header column
        if fri and not is_teacher:
            day_styles.append(('BACKGROUND', (lunch_col_idx+1, row_idx), (lunch_col_idx+1, row_idx), C_LUNCH))
            day_styles.append(('TEXTCOLOR',  (lunch_col_idx+1, row_idx), (lunch_col_idx+1, row_idx), C_LUNCH_TXT))
            day_styles.append(('FONTNAME',   (lunch_col_idx+1, row_idx), (lunch_col_idx+1, row_idx), 'Helvetica-Bold'))
            day_styles.append(('FONTSIZE',   (lunch_col_idx+1, row_idx), (lunch_col_idx+1, row_idx), 6.5))
            day_styles.append(('LEADING',    (lunch_col_idx+1, row_idx), (lunch_col_idx+1, row_idx), 8))
            day_styles.append(('BOX',        (lunch_col_idx+1, row_idx), (lunch_col_idx+1, row_idx), 0.8, colors.HexColor('#d4a800')))

    # ── Base table styles ──────────────────────────────────────────────────
    lunch_ci = 1 + n_pre   # 0-based lunch column index
    base_styles = [
        # All cells baseline
        ('FONTNAME',      (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE',      (0,0), (-1,-1), 8),
        ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('GRID',          (0,0), (-1,-1), 0.5, C_BORDER),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING',   (0,0), (-1,-1), 4),
        ('RIGHTPADDING',  (0,0), (-1,-1), 4),
        # Header row — matches .tt-hdr-period on website
        ('BACKGROUND', (0,0), (-1,0), C_HEADER),
        ('TEXTCOLOR',  (0,0), (-1,0), C_HEADER_TXT),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0,0), (-1,0), 8),
        # Lunch column (entire column) — matches .tt-hdr-lunch / .tt-lunch-cell
        ('BACKGROUND', (lunch_ci,0), (lunch_ci,-1), C_LUNCH),
        ('TEXTCOLOR',  (lunch_ci,0), (lunch_ci,-1), C_LUNCH_TXT),
        ('FONTNAME',   (lunch_ci,0), (lunch_ci,-1), 'Helvetica-Bold'),
        ('FONTSIZE',   (lunch_ci,0), (lunch_ci,-1), 6.5),
        ('LEADING',    (lunch_ci,0), (lunch_ci,-1), 8),
        ('GRID',       (lunch_ci,0), (lunch_ci,-1), 0.8, colors.HexColor('#d4a800')),
        # Day label column — matches .tt-day-cell
        ('BACKGROUND', (0,1), (0,-1), C_DAY),
        ('FONTNAME',   (0,1), (0,-1), 'Helvetica-Bold'),
        # Friday day label — matches .tt-row-fri .tt-day-cell
        ('BACKGROUND', (0, len(DAYS)), (0, len(DAYS)), C_DAY_FRI),
        ('TEXTCOLOR',  (0, len(DAYS)), (0, len(DAYS)), C_DAY_FRI_TXT),
        # Body text color
        ('TEXTCOLOR',  (1,1), (-1,-1), C_BODY_TXT),
        ('FONTSIZE',   (1,1), (-1,-1), 8),
    ]
    # Alternating row background — matches .tt-row-alt on website
    for r in range(2, len(DAYS)+1, 2):   # rows 2,4 (0-indexed; 1=Mon,2=Tue,...)
        base_styles += [
            ('BACKGROUND', (1,r), (lunch_ci-1,r), C_ALT_ROW),
            ('BACKGROUND', (lunch_ci+1,r), (-1,r), C_ALT_ROW),
        ]
    # Friday full row tint — matches .tt-row-fri td
    fri_row = len(DAYS)   # 5 (Friday is last)
    base_styles += [
        ('BACKGROUND', (1,fri_row), (lunch_ci-1,fri_row), colors.HexColor('#f3faf4')),
        ('BACKGROUND', (lunch_ci+1,fri_row), (-1,fri_row), colors.HexColor('#f3faf4')),
    ]
    # ── ONE unified lunch column — remove internal horizontal lines inside it ─
    # Without this, each weekday row shows a SEPARATE yellow box in the lunch column.
    # Setting LINEBELOW=0 for rows 1..4 hides the dividers between Mon-Thu cells,
    # making the entire lunch column appear as one continuous vertical stripe.
    for r in range(1, len(DAYS)):  # rows 1-4 (Mon-Thu): remove bottom border of each
        base_styles.append(('LINEBELOW', (lunch_ci,r), (lunch_ci,r), 0, C_LUNCH))
    # Ensure outer borders of the lunch column remain solid gold
    base_styles += [
        ('LINEBEFORE', (lunch_ci,1), (lunch_ci,-1), 0.8, colors.HexColor('#d4a800')),
        ('LINEAFTER',  (lunch_ci,1), (lunch_ci,-1), 0.8, colors.HexColor('#d4a800')),
        ('LINEABOVE',  (lunch_ci,1), (lunch_ci,1),  0.8, colors.HexColor('#d4a800')),
        ('LINEBELOW',  (lunch_ci,fri_row), (lunch_ci,fri_row), 0.8, colors.HexColor('#d4a800')),
    ]

    # ── Column widths ──────────────────────────────────────────────────────
    # landscape A4 ≈ 27.7cm usable after margins
    page_w  = landscape(A4)[0] - 2*cm
    day_w   = 1.5*cm    # Day label
    lunch_w = 1.8*cm    # Lunch column — wider so text "LUNCH BREAK\n11:30–12:15" is readable
    period_w_total = page_w - day_w - lunch_w
    # Period cols: shrink slightly if many periods, otherwise use comfortable 2.6cm
    n_period_cols = n_pre + n_post
    rem_w = min(2.8*cm, period_w_total / n_period_cols) if n_period_cols > 0 else 2.6*cm
    # If rem_w * n_period_cols < period_w_total, expand to fill
    if rem_w * n_period_cols < period_w_total:
        rem_w = period_w_total / n_period_cols
    col_widths = [day_w] + [rem_w]*n_pre + [lunch_w] + [rem_w]*n_post

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(base_styles + day_styles))
    story.append(t)

    # ── Subject Code Mapping ────────────────────────────────────────────────
    unique_subjects = {}

    def _register_subject(c, code, subject_code, subject_name, teacher_name, teacher_dept):
        if not subject_name:
            return
        if is_teacher:
            map_key = f"{code}|{c.get('division_id','')}"
        else:
            map_key = code
        if map_key not in unique_subjects:
            div_id   = c.get('division_id','')
            sem_num  = c.get('semester_num') or c.get('semester','')
            stud_dep = c.get('student_dept','')
            if sem_num:
                class_lbl = f"S{sem_num}"
                if stud_dep:
                    class_lbl += f" {stud_dep}"
                if div_id and div_id != stud_dep:
                    class_lbl += f" – {div_id}"
            else:
                class_lbl = div_id
            unique_subjects[map_key] = {
                'short': code, 'code': subject_code,
                'name': subject_name, 'teacher': teacher_name,
                'dept': teacher_dept, 'class_label': class_lbl,
            }

    for c in cells:
        # Primary subject
        _register_subject(
            c,
            c.get('short_code') or c.get('subject_code') or c.get('subject_name') or '',
            c.get('subject_code',''),
            c.get('subject_name',''),
            c.get('teacher_name',''),
            c.get('teacher_dept',''),
        )
        # Paired lab subject (parallel-lab cells, class timetable only)
        if c.get('paired_short_code') and not is_teacher:
            _register_subject(
                c,
                c.get('paired_short_code') or c.get('paired_subject_code') or c.get('paired_subject_name') or '',
                c.get('paired_subject_code',''),
                c.get('paired_subject_name',''),
                c.get('paired_teacher_name',''),
                c.get('teacher_dept',''),
            )

    if unique_subjects:
        story.append(Spacer(1, 0.8*cm))
        story.append(Paragraph('<b>Subject Code Mapping</b>', styles['Normal']))
        story.append(Spacer(1, 0.2*cm))

        # Column choice: teacher timetable → show Class column; class → show Faculty+Department
        if is_teacher:
            map_headers = ['Code', 'Course Code', 'Course Name', 'Class']
            map_data = [map_headers]
            for sc in sorted(unique_subjects.keys()):
                info = unique_subjects[sc]
                map_data.append([info['short'], info['code'], info['name'][:45],
                                 info.get('division','') or info.get('class_label','')])
            col_ws = [1.8*cm, 2.2*cm, 10*cm, 3.5*cm]
        else:
            map_headers = ['Code', 'Course Code', 'Course Name', 'Faculty', 'Department']
            map_data = [map_headers]
            for sc in sorted(unique_subjects.keys()):
                info = unique_subjects[sc]
                map_data.append([info['short'], info['code'], info['name'][:45],
                                 info['teacher'], info['dept']])
            col_ws = [1.8*cm, 2.2*cm, 9*cm, 5.5*cm, 2.0*cm]

        n_map_rows = len(map_data)
        map_sty = [
            # Header row
            ('BACKGROUND',    (0,0), (-1,0), colors.HexColor('#e8edf5')),
            ('TEXTCOLOR',     (0,0), (-1,0), colors.HexColor('#1a202c')),
            ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',      (0,0), (-1,0), 8),
            ('BOTTOMPADDING', (0,0), (-1,0), 6),
            # All cells
            ('FONTSIZE',      (0,1), (-1,-1), 7.5),
            ('FONTNAME',      (0,1), (-1,-1), 'Helvetica'),
            ('FONTNAME',      (0,1), (0,-1),  'Helvetica-Bold'),   # Code col bold
            ('TEXTCOLOR',     (0,1), (0,-1),  colors.HexColor('#1a202c')),
            ('GRID',          (0,0), (-1,-1), 0.5, C_BORDER),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
            ('ALIGN',         (0,0), (1,-1),  'CENTER'),   # Code & Course Code centred
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,1), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 5),
            ('RIGHTPADDING',  (0,0), (-1,-1), 5),
        ]
        # Alternating row shading (matches website #f7f9fc on even rows)
        for r in range(2, n_map_rows, 2):
            map_sty.append(('BACKGROUND', (0,r), (-1,r), colors.HexColor('#f7f9fc')))

        map_t = Table(map_data, colWidths=col_ws)
        map_t.setStyle(TableStyle(map_sty))
        story.append(map_t)

    doc.build(story)
    return buf.getvalue()

def is_hod():
    return current_user.is_authenticated and current_user.role == 'hod'


def get_hod_dept():
    """Return the Department for the current HOD user."""
    if not is_hod():
        return None
    teacher = get_teacher_for_user()
    if teacher and teacher.dept_id:
        return Department.query.get(teacher.dept_id)
    return None


def send_email(to_email, subject, html_body):
    """Send an HTML email; prints to console if SMTP not configured."""
    smtp_server     = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    smtp_port       = int(os.environ.get('MAIL_PORT', 587))
    sender_email    = os.environ.get('MAIL_USERNAME', '')
    sender_password = os.environ.get('MAIL_PASSWORD', '')
    if sender_email and sender_password and 'your_16_char' not in sender_password:
        try:
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            import smtplib
            msg = MIMEMultipart('alternative')
            msg['From']    = sender_email
            msg['To']      = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(html_body, 'html'))
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
            server.quit()
            return True
        except Exception as e:
            print(f'Email send failed: {e}')
            return False
    else:
        print(f'\n--- EMAIL (console fallback) ---\nTo: {to_email}\nSubject: {subject}\n{html_body[:200]}\n---\n')
        return True


# ── Preference window helpers ─────────────────────────────────

def get_pref_window(dept_id, semester_type):
    """Return the active PreferenceWindow row for dept+sem_type, or None."""
    return PreferenceWindow.query.filter_by(
        dept_id=dept_id, semester_type=semester_type
    ).order_by(PreferenceWindow.id.desc()).first()


def pref_window_open_for_teacher(teacher):
    """Return True if the preference window is open for a teacher's department."""
    if not teacher or not teacher.dept_id:
        return False
    sem_type = SystemSettings.get('active_semester_type', 'odd')
    w = get_pref_window(teacher.dept_id, sem_type)
    return w.is_currently_open() if w else False


# ── Notification helpers ──────────────────────────────────────

def unread_notifications_count():
    """Return unread notification count for current teacher."""
    if not current_user.is_authenticated or not current_user.teacher_id:
        return 0
    teacher = Teacher.query.get(current_user.teacher_id)
    if not teacher:
        return 0
    return Notification.query.filter(
        db.or_(
            Notification.recipient_id == teacher.id,
            db.and_(
                Notification.dept_id == teacher.dept_id,
                Notification.recipient_id == None,
                db.or_(Notification.sender_id != teacher.id, Notification.sender_id == None)
            )
        ),
        Notification.is_read == False,
        ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{teacher.id},%')
    ).count()


@app.context_processor
def inject_globals():
    """Inject common variables into all templates."""
    notif_count = 0
    admin_notif_count = 0
    if current_user.is_authenticated:
        if is_admin():
            # Count unread notifications directed at this admin
            t_id = _get_notif_identity()
            if t_id:
                admin_notif_count = Notification.query.filter(
                    Notification.recipient_id == t_id,
                    Notification.is_read == False,
                    ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{t_id},%')
                ).count()
        else:
            notif_count = unread_notifications_count()
    return dict(
        is_hod_user=is_hod(),
        is_admin_user=is_admin(),
        notif_count=notif_count,
        admin_notif_count=admin_notif_count,
    )


# ═══════════════════════════════════════════════════════════════
# HOD ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/hod')
@login_required
def hod_dashboard():
    if not is_hod():
        flash('HOD access required.', 'error')
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    if not dept:
        flash('No department assigned to this HOD.', 'error')
        return redirect(url_for('teacher_profile'))

    teachers = Teacher.query.filter_by(dept_id=dept.id).order_by(Teacher.name).all()
    courses   = Course.query.filter_by(department=dept.id).order_by(Course.semester, Course.name).all()
    sem_type  = SystemSettings.get('active_semester_type', 'odd')
    pref_win  = get_pref_window(dept.id, sem_type)
    notifs    = Notification.query.filter_by(dept_id=dept.id).order_by(
        Notification.created_at.desc()
    ).limit(10).all()

    return render_template('hod_dashboard.html',
        dept=dept,
        teachers=teachers,
        courses=courses,
        sem_type=sem_type,
        pref_window=pref_win,
        recent_notifs=notifs,
        teacher=get_teacher_for_user(),
    )


# ── HOD: Subject (Course) Management ─────────────────────────

@app.route('/hod/subjects')
@login_required
def hod_subjects():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    if not dept:
        flash('No department assigned.', 'error')
        return redirect(url_for('hod_dashboard'))
    courses = Course.query.filter_by(department=dept.id).order_by(Course.semester, Course.name).all()
    return render_template('hod_subjects.html',
        dept=dept, courses=courses, teacher=get_teacher_for_user())


@app.route('/hod/course/edit/<int:course_id>', methods=['POST'])
@login_required
def hod_course_edit(course_id):
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    c = db.session.get(Course, course_id)
    if not c or c.department != dept.id:
        flash('Course not found or not in your department.', 'error')
        return redirect(url_for('hod_subjects'))
    # HOD can change name, LTPR, type
    old_name = c.name
    old_hours = c.hours_per_week
    old_type  = c.type
    c.name           = request.form.get('name', c.name).strip()
    c.hours_per_week = request.form.get('hours_per_week', c.hours_per_week).strip()
    c.type           = request.form.get('type', c.type).strip()
    c.dept_id        = request.form.get('dept_id', c.dept_id or '').strip()
    db.session.commit()

    from datetime import datetime as _dt
    changes = []
    if c.name           != old_name:  changes.append(f'Name: "{old_name}" → "{c.name}"')
    if c.hours_per_week != old_hours: changes.append(f'Hours/Week: "{old_hours}" → "{c.hours_per_week}"')
    if c.type           != old_type:  changes.append(f'Type: "{old_type}" → "{c.type}"')
    change_summary = '; '.join(changes) if changes else 'Minor details updated'

    hod_teacher = get_teacher_for_user()
    hod_name = hod_teacher.name if hod_teacher else 'HOD'
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')
    _notify_admin(
        title=f'Subject Updated — {dept.id}',
        message=(
            f'HOD {hod_name} ({dept.id} dept) updated subject "{c.name}" (code: {c.code}) on {now_str}. '
            f'Changes: {change_summary}.'
        )
    )
    flash(f'Course "{c.name}" updated.', 'success')
    return redirect(url_for('hod_subjects'))


@app.route('/hod/course/add', methods=['POST'])
@login_required
def hod_course_add():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    code = request.form.get('code', '').strip()
    name = request.form.get('name', '').strip()
    if not code or not name:
        flash('Code and name required.', 'error')
        return redirect(url_for('hod_subjects'))
    if Course.query.filter_by(code=code, department=dept.id).first():
        flash(f'Course code "{code}" already exists.', 'error')
        return redirect(url_for('hod_subjects'))
    db.session.add(Course(
        code=code, name=name, department=dept.id,
        dept_id=request.form.get('dept_id', dept.id).strip(),
        type=request.form.get('type', '').strip(),
        hours_per_week=request.form.get('hours_per_week', '').strip(),
        semester=request.form.get('semester', '').strip(),
        division=request.form.get('division', '').strip(),
    ))
    db.session.commit()
    hod_teacher = get_teacher_for_user()
    from datetime import datetime as _dt
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')
    _notify_admin(
        title=f'New Subject Added — {dept.id}',
        message=(
            f'HOD {hod_teacher.name if hod_teacher else "HOD"} ({dept.id} dept) added new subject '
            f'"{name}" (code: {code}) on {now_str}.'
        )
    )
    flash(f'Course "{name}" added.', 'success')
    return redirect(url_for('hod_subjects'))


@app.route('/hod/course/delete/<int:course_id>', methods=['POST'])
@login_required
def hod_course_delete(course_id):
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    c = db.session.get(Course, course_id)
    if not c or c.department != dept.id:
        flash('Not found or not in your department.', 'error')
        return redirect(url_for('hod_subjects'))
    c_name = c.name
    db.session.delete(c)
    db.session.commit()
    hod_teacher = get_teacher_for_user()
    from datetime import datetime as _dt
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')
    _notify_admin(
        title=f'Subject Deleted — {dept.id}',
        message=(
            f'HOD {hod_teacher.name if hod_teacher else "HOD"} ({dept.id} dept) deleted subject '
            f'"{c_name}" on {now_str}.'
        )
    )
    flash(f'Course "{c_name}" deleted.', 'success')
    return redirect(url_for('hod_subjects'))


# ── HOD: Teacher Management ───────────────────────────────────

@app.route('/hod/teachers')
@login_required
def hod_teachers():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    teachers = Teacher.query.filter_by(dept_id=dept.id).order_by(Teacher.name).all()
    return render_template('hod_teachers.html',
        dept=dept, teachers=teachers, teacher=get_teacher_for_user())


@app.route('/hod/teacher/edit/<teacher_id>', methods=['POST'])
@login_required
def hod_teacher_edit(teacher_id):
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    t = Teacher.query.get_or_404(teacher_id)
    if t.dept_id != dept.id:
        flash('Teacher not in your department.', 'error')
        return redirect(url_for('hod_teachers'))

    # Capture old values before update to build a meaningful change log
    old_name        = t.name or ''
    old_designation = t.designation or ''
    old_seniority   = t.seniority_level or ''
    old_area        = t.area_of_specialization or ''

    t.name        = request.form.get('name', t.name).strip()
    t.designation = request.form.get('designation', t.designation or '').strip()
    t.seniority_level = request.form.get('seniority_level', t.seniority_level or '').strip()
    t.area_of_specialization = request.form.get('area_of_specialization', t.area_of_specialization or '').strip()
    db.session.commit()

    # Build change summary
    from datetime import datetime as _dt
    changes = []
    if t.name            != old_name:        changes.append(f'Name: "{old_name}" → "{t.name}"')
    if t.designation     != old_designation: changes.append(f'Designation: "{old_designation}" → "{t.designation}"')
    if t.seniority_level != old_seniority:   changes.append(f'Seniority Level: "{old_seniority}" → "{t.seniority_level}"')
    if t.area_of_specialization != old_area: changes.append(f'Specialization: "{old_area}" → "{t.area_of_specialization}"')
    change_summary = '; '.join(changes) if changes else 'Minor details updated'

    hod_teacher = get_teacher_for_user()
    hod_name = hod_teacher.name if hod_teacher else 'HOD'
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')

    # Notify admin with full details
    _notify_admin(
        title=f'Teacher Record Updated — {dept.id}',
        message=(
            f'HOD {hod_name} ({dept.id} dept) updated teacher "{t.name}" on {now_str}. '
            f'Changes: {change_summary}.'
        )
    )

    # Notify the affected teacher personally
    _notify_teacher(
        teacher_id=teacher_id,
        title='Your Profile Was Updated',
        message=(
            f'Your profile was updated by HOD {hod_name} ({dept.id} dept) on {now_str}. '
            f'Changes: {change_summary}.'
        ),
        sender_id=hod_teacher.id if hod_teacher else None,
    )

    flash(f'Teacher "{t.name}" updated.', 'success')
    return redirect(url_for('hod_teachers'))


@app.route('/hod/teacher/delete/<teacher_id>', methods=['POST'])
@login_required
def hod_teacher_delete(teacher_id):
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    t = Teacher.query.get_or_404(teacher_id)
    if t.dept_id != dept.id:
        flash('Teacher not in your department.', 'error')
        return redirect(url_for('hod_teachers'))
    teacher_name = t.name
    from database import PasswordResetToken
    if t.auth:
        PasswordResetToken.query.filter_by(auth_id=t.auth.id).delete()
        db.session.delete(t.auth)
    TeacherPreference.query.filter_by(teacher_id=teacher_id).delete()
    SubjectAssignment.query.filter_by(teacher_id=teacher_id).delete()
    Notification.query.filter(
        (Notification.recipient_id == teacher_id) |
        (Notification.sender_id == teacher_id)
    ).delete(synchronize_session=False)
    db.session.delete(t)
    db.session.commit()
    # Notify admin with timestamp
    hod_teacher = get_teacher_for_user()
    from datetime import datetime as _dt
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')
    _notify_admin(
        title=f'Teacher Deleted — {dept.id}',
        message=(
            f'HOD {hod_teacher.name if hod_teacher else "HOD"} ({dept.id} dept) deleted teacher '
            f'"{teacher_name}" on {now_str}. All associated records removed.'
        )
    )
    flash(f'Teacher "{teacher_name}" and all records deleted.', 'success')
    return redirect(url_for('hod_teachers'))


# ── HOD: Preference Window ────────────────────────────────────

@app.route('/hod/preference-window', methods=['GET', 'POST'])
@login_required
def hod_preference_window():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    sem_type = SystemSettings.get('active_semester_type', 'odd')

    if request.method == 'POST':
        action = request.form.get('action', '')
        from datetime import datetime, timedelta

        if action == 'open':
            hours_str = request.form.get('duration_hours', '48')
            try:
                hours = int(hours_str)
            except ValueError:
                hours = 48
            hours = max(1, min(168, hours))  # 1h – 7 days

            # Close any existing open window for this dept+sem_type
            PreferenceWindow.query.filter_by(
                dept_id=dept.id, semester_type=sem_type, is_open=True
            ).update({'is_open': False})

            now = datetime.utcnow()
            teacher = get_teacher_for_user()
            w = PreferenceWindow(
                dept_id=dept.id,
                semester_type=sem_type,
                is_open=True,
                open_from=now,
                open_until=now + timedelta(hours=hours),
                opened_by=teacher.id if teacher else None,
                created_at=now,
            )
            db.session.add(w)
            db.session.commit()

            # Notify all teachers in dept
            hod_t = get_teacher_for_user()
            _notify_teachers_dept(
                dept_id=dept.id,
                title='Preference Submission Window Open',
                message=(
                    f'The {sem_type.title()} semester subject preference window is now open. '
                    f'Please log in and submit your 3 subject preferences before '
                    f'{(now + timedelta(hours=hours)).strftime("%d %b %Y %H:%M")} UTC.'
                ),
                sender_id=hod_t.id if hod_t else None,
            )
            # Notify admin
            from datetime import datetime as _now_dt
            _notify_admin(
                title=f'HOD Opened Preference Window — {dept.id}',
                message=(
                    f'HOD {hod_t.name if hod_t else "(unknown)"} opened the {sem_type.title()} semester '
                    f'preference window for {dept.name} ({dept.id}) department. '
                    f'Window duration: {hours}h. Opened at: {now.strftime("%d %b %Y %H:%M")} UTC.'
                )
            )
            flash(f'Preference window opened for {hours} hours. Teachers have been notified.', 'success')

        elif action == 'close':
            PreferenceWindow.query.filter_by(
                dept_id=dept.id, semester_type=sem_type, is_open=True
            ).update({'is_open': False})
            db.session.commit()
            flash('Preference window closed.', 'success')

        return redirect(url_for('hod_preference_window'))

    # Courses grouped by semester
    if sem_type == "odd":
        valid_semesters = ["1", "3", "5", "7"]
    else:
        valid_semesters = ["2", "4", "6", "8"]

    courses = Course.query.filter(
        Course.department == dept.id,
        Course.semester.in_(valid_semesters)
    ).order_by(Course.semester, Course.name).all()

    courses_by_sem = {}
    for c in courses:
        courses_by_sem.setdefault(c.semester, []).append(c)

    odd_courses_by_sem = {}
    even_courses_by_sem = {}

    for sem, clist in courses_by_sem.items():
        try:
            if int(sem) % 2:
                odd_courses_by_sem[sem] = clist
            else:
                even_courses_by_sem[sem] = clist
        except:
            pass

    pref_win = get_pref_window(dept.id, sem_type)
    teachers = Teacher.query.filter_by(dept_id=dept.id).order_by(Teacher.name).all()

    def _sem_is_odd(sem_val):
        """Return True if sem_val represents an odd semester group."""
        if sem_val is None:
            return False
        s = str(sem_val).strip().lower()
        if s == 'odd':
            return True
        if s == 'even':
            return False
        # Legacy numeric strings: '1','3','5','7' → odd
        try:
            return int(float(s)) % 2 == 1
        except (ValueError, TypeError):
            return False

    def _sem_is_even(sem_val):
        """Return True if sem_val represents an even semester group."""
        if sem_val is None:
            return False
        s = str(sem_val).strip().lower()
        if s == 'even':
            return True
        if s == 'odd':
            return False
        # Legacy numeric strings: '2','4','6','8' → even
        try:
            return int(float(s)) % 2 == 0
        except (ValueError, TypeError):
            return False

    # Build preference status per teacher
    pref_status = []

    for t in teachers:

        prefs = (
            TeacherPreference.query
            .filter_by(teacher_id=t.id)
            .order_by(TeacherPreference.rank)
            .all()
        )

        odd_prefs = [p for p in prefs if _sem_is_odd(p.semester)]
        even_prefs = [p for p in prefs if _sem_is_even(p.semester)]

        pref_status.append({
            "teacher": t,
            "odd_count": len(odd_prefs),
            "even_count": len(even_prefs),
            "odd_prefs": odd_prefs,
            "even_prefs": even_prefs,
            "prefs": prefs
        })

    return render_template(
        "hod_preference_window.html",
        dept=dept,
        sem_type=sem_type,
        pref_window=pref_win,
        pref_status=pref_status,
        odd_courses_by_sem=odd_courses_by_sem,
        even_courses_by_sem=even_courses_by_sem,
        teacher=get_teacher_for_user(),
        show_my_prefs=False,)


@app.route('/hod/update_teacher_preference/<teacher_id>', methods=['POST'])
@login_required
def hod_update_teacher_preference(teacher_id):

    if not is_hod():
        return redirect(url_for('teacher_profile'))

    teacher = Teacher.query.get_or_404(teacher_id)
    dept = get_hod_dept()

    # Security: HOD may edit only teachers in their department
    if teacher.dept_id != dept.id:
        flash("You cannot edit another department's teacher preferences.", "error")
        return redirect(url_for("hod_preference_window"))

    # Which semester group is being edited. The HOD edit panel has
    # separate Odd/Even tabs, each posting its own hidden `sem_type`
    # field — that must be honored rather than always falling back to
    # the globally-active semester type, or editing a teacher's Even
    # preferences while Odd is the globally-active window would
    # delete/insert against the wrong semester group.
    sem_type = request.form.get("sem_type") or SystemSettings.get("active_semester_type", "odd")
    if sem_type not in ("odd", "even"):
        sem_type = "odd"

    # Read submitted preferences
    pref_codes = [
        request.form.get("pref_1", "").strip(),
        request.form.get("pref_2", "").strip(),
        request.form.get("pref_3", "").strip(),
    ]

    # No duplicate subjects
    non_empty = [p for p in pref_codes if p]
    if len(non_empty) != len(set(non_empty)):
        flash("Duplicate subjects are not allowed.", "error")
        return redirect(url_for("hod_preference_window"))

    ok, message = save_teacher_preferences(teacher, pref_codes, sem_type)
    if ok:
        flash(f"Preferences updated successfully for {teacher.name}.", "success")
    else:
        flash(message, "error")

    return redirect(url_for("hod_preference_window"))


def _get_notif_identity():
    """Returns the teacher.id or a synthetic admin identity for notification tracking."""
    if not current_user.is_authenticated:
        return None
    t = get_teacher_for_user()
    if t: return t.id
    if is_admin(): return f"admin_{current_user.id}"
    return None

def _flag_timetable_stale(dept_id, reason):
    """
    Mark dept_id's timetable as needing regeneration (e.g. a new teacher was
    just added and isn't reflected in the current timetable/21-period loads
    yet). Surfaced as a dashboard banner and an admin notification; cleared
    automatically the next time 'Assign Subjects' successfully regenerates
    that department (see admin_assign_subjects).
    """
    if not dept_id:
        return
    from datetime import datetime
    SystemSettings.set(f'timetable_stale_{dept_id}', reason)
    _notify_admin(
        title=f'Timetable regeneration needed — {dept_id}',
        message=f'{reason} Re-run "Assign Subjects" from the dashboard to include them.'
    )


def _clear_timetable_stale(dept_id):
    """Clear the stale flag for dept_id after a successful regeneration."""
    row = SystemSettings.query.get(f'timetable_stale_{dept_id}')
    if row:
        db.session.delete(row)


def _notify_admin(title, message):
    """Create a notification for all admin users about an HOD action."""
    from datetime import datetime
    admin_auths = TeacherAuth.query.filter_by(role='admin').all()
    for adm in admin_auths:
        rid = adm.teacher_id if adm.teacher_id else f"admin_{adm.id}"
        db.session.add(Notification(
                dept_id=None,
                recipient_id=rid,
                sender_id=None,
                title=title,
                message=message,
                is_read=False,
                cleared_by='',
                created_at=datetime.utcnow(),
            ))
    db.session.commit()


def _notify_teacher(teacher_id, title, message, sender_id=None):
    """Create a personal notification for a specific teacher."""
    from datetime import datetime
    t = Teacher.query.get(teacher_id)
    if not t:
        return
    db.session.add(Notification(
        dept_id=t.dept_id,
        recipient_id=str(teacher_id),
        sender_id=str(sender_id) if sender_id else None,
        title=title,
        message=message,
        is_read=False,
        cleared_by='',
        created_at=datetime.utcnow(),
    ))
    db.session.commit()


def _notify_teachers_dept(dept_id, title, message, sender_id=None):
    """Create in-app notifications for all teachers in a department, excluding the sender."""
    from datetime import datetime
    # dept-wide notification (recipient_id=None means all in dept).
    # We store sender_id so the sender can be excluded from their own view.
    db.session.add(Notification(
        dept_id=dept_id,
        recipient_id=None,
        sender_id=sender_id,
        title=title,
        message=message,
        is_read=False,
        cleared_by='',
        created_at=datetime.utcnow(),
    ))
    db.session.commit()

    # Optionally send email to each teacher (skip sender)
    teachers = Teacher.query.filter_by(dept_id=dept_id).all()
    for t in teachers:
        if t.id == sender_id:
            continue  # don't email the HOD their own message
        if t.auth and t.auth.email:
            html = f"""
            <div style="font-family:Arial,sans-serif;padding:20px;">
              <h2 style="color:#1e3a5f;">{title}</h2>
              <p>{message}</p>
              <hr>
              <p style="color:#888;font-size:12px;">Timetable Management System — {dept_id} Department</p>
            </div>
            """
            send_email(t.auth.email, f'[TMS] {title}', html)


# ── HOD: Notifications ────────────────────────────────────────

@app.route('/hod/notifications', methods=['GET', 'POST'])
@login_required
def hod_notifications():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    teacher_self = get_teacher_for_user()
    teachers = Teacher.query.filter_by(dept_id=dept.id).order_by(Teacher.name).all()

    if request.method == 'POST':
        from datetime import datetime
        title   = request.form.get('title', '').strip()
        message = request.form.get('message', '').strip()
        to      = request.form.get('recipient', 'all')  # 'all' or teacher_id

        if not title or not message:
            flash('Title and message are required.', 'error')
        else:
            if to == 'all':
                _notify_teachers_dept(
                    dept_id=dept.id, title=title, message=message,
                    sender_id=teacher_self.id if teacher_self else None
                )
            else:
                db.session.add(Notification(
                    dept_id=dept.id,
                    recipient_id=to,
                    sender_id=teacher_self.id if teacher_self else None,
                    title=title, message=message,
                    is_read=False,
                    created_at=datetime.utcnow(),
                ))
                db.session.commit()
                # email single teacher
                t = Teacher.query.get(to)
                if t and t.auth and t.auth.email:
                    send_email(t.auth.email, f'[TMS] {title}',
                               f'<p>{message}</p>')
            flash('Notification sent.', 'success')
        return redirect(url_for('hod_notifications'))

    sent = Notification.query.filter_by(dept_id=dept.id).order_by(
        Notification.created_at.desc()
    ).limit(30).all()
    return render_template('hod_notifications.html',
        dept=dept, teachers=teachers, sent_notifications=sent,
        teacher=teacher_self)


# ── HOD: Delete sent notification log entry ─────────────────────────────────

@app.route('/hod/notifications/delete/<int:notif_id>', methods=['POST'])
@login_required
def hod_notification_delete_sent(notif_id):
    """HOD deletes a single sent notification entry from their log."""
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    n = Notification.query.get_or_404(notif_id)
    # Only allow deleting notifications from this dept
    if n.dept_id == dept.id:
        db.session.delete(n)
        db.session.commit()
        flash('Notification log entry removed.', 'success')
    return redirect(url_for('hod_notifications'))


@app.route('/hod/notifications/clear-sent', methods=['POST'])
@login_required
def hod_notifications_clear_sent():
    """HOD clears all sent notification logs for their department."""
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    Notification.query.filter_by(dept_id=dept.id).delete()
    db.session.commit()
    flash('Sent notification log cleared.', 'success')
    return redirect(url_for('hod_notifications'))


# ── HOD: Profile ────────────────────────────────────────────────

@app.route('/hod/profile')
@login_required
def hod_profile():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    teacher = get_teacher_for_user()
    prefs = []
    if teacher:
        prefs = TeacherPreference.query.filter_by(teacher_id=teacher.id).all()
    return render_template('hod_profile.html', dept=dept, teacher=teacher, prefs=prefs)


# ── HOD: Inbox (Personal Notifications) ─────────────────────────

@app.route('/hod/inbox')
@login_required
def hod_inbox():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    teacher = get_teacher_for_user()

    notifs = Notification.query.filter(
        ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{teacher.id},%'),
        db.or_(
            Notification.recipient_id == teacher.id,
            db.and_(
                Notification.dept_id == dept.id,
                Notification.recipient_id == None,
                # Exclude notifications the HOD sent themselves
                db.or_(Notification.sender_id != teacher.id, Notification.sender_id == None)
            )
        )
    ).order_by(Notification.created_at.desc()).all()

    # Mark all as read
    for n in notifs:
        if not n.is_read:
            n.is_read = True
    db.session.commit()

    return render_template('hod_inbox.html', dept=dept, teacher=teacher, notifications=notifs)


# ── HOD: Add Teacher ─────────────────────────────────────────────

@app.route('/hod/teacher/add', methods=['POST'])
@login_required
def hod_teacher_add():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()

    name        = request.form.get('name', '').strip()
    designation = request.form.get('designation', '').strip()
    seniority   = request.form.get('seniority_level', '').strip()
    area        = request.form.get('area_of_specialization', '').strip()

    if not name:
        flash('Teacher name is required.', 'error')
        return redirect(url_for('hod_teachers'))

    # Auto-generate dept-based teacher ID
    teacher_id = _generate_teacher_id(dept.id)

    # Auto-generate username (min 3 chars)
    username_base = name.split()[0].lower()
    username_base = ''.join(c for c in username_base if c.isalpha())
    if len(username_base) < 3:
        username_base = (username_base + name.replace(' ', '').lower())[:3]
    username = username_base
    suffix = 1
    while TeacherAuth.query.filter_by(username=username).first():
        username = f'{username_base}{suffix}'
        suffix += 1

    new_teacher = Teacher(
        id=teacher_id, name=name,
        designation=designation,
        area_of_specialization=area,
        dept_id=dept.id
    )
    db.session.add(new_teacher)
    db.session.flush()

    new_auth = TeacherAuth(username=username, teacher_id=teacher_id, role='teacher')
    new_auth.set_password(username)
    db.session.add(new_auth)
    db.session.commit()
    _recalculate_seniority(dept.id)

    hod_teacher = get_teacher_for_user()
    from datetime import datetime as _dt
    now_str = _dt.utcnow().strftime('%d %b %Y %H:%M UTC')
    _notify_admin(
        title=f'New Teacher Added — {dept.id}',
        message=(
            f'HOD {hod_teacher.name if hod_teacher else "HOD"} ({dept.id}) added new teacher '
            f'"{name}" (ID: {teacher_id}, designation: {designation or "—"}) on {now_str}. '
            f'Login credentials: {username} / {username}.'
        )
    )
    _flag_timetable_stale(dept.id, f'New teacher "{name}" was added to {dept.id}.')
    db.session.commit()
    flash(f'Teacher "{name}" added. Login: {username} / {username}', 'success')
    return redirect(url_for('hod_teachers'))


# ── HOD: Timetable ──────────────────────────────────────────────

@app.route('/hod/timetable')
@login_required
def hod_timetable():
    if not is_hod():
        return redirect(url_for('teacher_profile'))

    import json as _json

    dept = get_hod_dept()
    teacher_self = get_teacher_for_user()

    sem_type = 'odd'
    s = SystemSettings.query.filter_by(key='active_semester_type').first()
    if s:
        sem_type = s.value

    valid_sems = SEMESTER_MAP.get(sem_type, [1, 3, 5, 7])

    # Only teachers in this HOD's department
    teachers_in_dept = Teacher.query.filter_by(dept_id=dept.id).order_by(Teacher.name).all()

    tab     = request.args.get('tab', 'own')       # own | teacher | class
    key     = request.args.get('key', '')           # teacher_id or division_id
    sem     = request.args.get('sem', '')           # semester number (for class tab)
    teacher_id_own = teacher_self.id if teacher_self else None

    timetable_cells = []
    all_keys = []

    if tab == 'own' and teacher_id_own:
        # FIX: filter semester='all' to get the single merged row for this teacher.
        # Without this filter the query returns both the per-semester rows AND the
        # merged 'all' row, causing every cell to appear twice in the display.
        rows = Timetable.query.filter_by(
            timetable_type='teacher',
            semester_type=sem_type,
            semester='all',
            key=str(teacher_id_own)
        ).order_by(Timetable.id.asc()).all()
        for row in rows:
            try:
                timetable_cells.extend(_json.loads(row.data_json))
            except Exception:
                pass

    elif tab == 'teacher':
        # List all teachers in dept as available keys
        teacher_ids = [str(t.id) for t in teachers_in_dept]
        # FIX: query semester='all' rows (post-processed, stored under teacher's own dept).
        # The old query used dept_id=dept.id which broke for teachers whose timetable
        # rows were stored under a student dept (e.g. BSH teachers stored under 'CS').
        all_keys = teacher_ids
        if key and key in teacher_ids:
            rows = Timetable.query.filter_by(
                timetable_type='teacher',
                semester_type=sem_type,
                semester='all',
                key=key
            ).order_by(Timetable.id.asc()).all()
            for row in rows:
                try:
                    timetable_cells.extend(_json.loads(row.data_json))
                except Exception:
                    pass
        elif not key and teacher_ids:
            pass  # no selection yet

    elif tab == 'class':
        # Class-wise timetables for this dept — show ALL semesters and ALL divisions.
        # Dedup key: (semester, division_key) so "S1 AI" and "S3 AI" are separate entries.
        rows = Timetable.query.filter_by(
            dept_id=dept.id,
            timetable_type='class',
            semester_type=sem_type
        ).order_by(Timetable.semester, Timetable.key).all()

        seen_combo = set()
        all_keys_meta = []
        for r in rows:
            sem_str  = str(r.semester) if r.semester else ''
            combo_id = f"{sem_str}_{r.key}"     # unique per (semester, division)
            if combo_id not in seen_combo:
                seen_combo.add(combo_id)
                label = (f"S{sem_str} — {r.key}") if sem_str else r.key
                all_keys_meta.append({
                    'key':     r.key,
                    'label':   label,
                    'semester': sem_str,
                    'combo':   combo_id,    # used as <option value>
                })
        all_keys = [m['key'] for m in all_keys_meta]

        # Load cells when a specific (semester, division) is selected
        # URL carries ?key=AI&sem=1 so both are available
        if key and sem:
            q = Timetable.query.filter_by(
                dept_id=dept.id,
                timetable_type='class',
                semester_type=sem_type,
                key=key,
                semester=str(sem)
            )
            tt = q.order_by(Timetable.id.desc()).first()
            if tt:
                try:
                    timetable_cells = _json.loads(tt.data_json)
                except Exception:
                    timetable_cells = []
        elif key and not sem:
            # Fallback: no semester specified — load the first matching row
            tt = Timetable.query.filter_by(
                dept_id=dept.id,
                timetable_type='class',
                semester_type=sem_type,
                key=key
            ).order_by(Timetable.semester, Timetable.id.desc()).first()
            if tt:
                try:
                    timetable_cells = _json.loads(tt.data_json)
                    sem = str(tt.semester)   # fill sem for PDF link
                except Exception:
                    timetable_cells = []
    else:
        all_keys_meta = []

    if tab != 'class':
        all_keys_meta = [{'key': k, 'label': k, 'semester': ''} for k in all_keys]

    teacher_map = {str(t.id): t.name for t in teachers_in_dept}

    # Enrich timetable_cells with teacher_dept (not stored in JSON, looked up from DB).
    # This populates the Department column in the Subject Code Mapping table for all tabs.
    if timetable_cells:
        _tdept_map = {str(t.id): (t.dept_id or '') for t in Teacher.query.all()}
        for _c in timetable_cells:
            if not _c.get('teacher_dept') and _c.get('teacher_id'):
                _c['teacher_dept'] = _tdept_map.get(str(_c['teacher_id']), '')

    return render_template('hod_timetable.html',
        dept=dept,
        teacher=teacher_self,
        teachers_in_dept=teachers_in_dept,
        teacher_map=teacher_map,
        sem_type=sem_type,
        valid_sems=valid_sems,
        tab=tab,
        selected_key=key,
        selected_sem=sem,       # active semester (for class tab PDF link)
        all_keys=all_keys,
        all_keys_meta=all_keys_meta,
        timetable_cells=timetable_cells,
        DAYS=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    )

# ── HOD: Settings ───────────────────────────────────────────────

@app.route('/hod/settings', methods=['GET', 'POST'])
@login_required
def hod_settings():
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    dept = get_hod_dept()
    teacher_self = get_teacher_for_user()
    user = TeacherAuth.query.get(current_user.id)

    if request.method == 'POST':
        from werkzeug.security import check_password_hash, generate_password_hash
        old_pw = request.form.get('old_password')
        new_pw = request.form.get('new_password')
        confirm_pw = request.form.get('confirm_password')

        if not check_password_hash(user.password_hash, old_pw):
            flash('Incorrect old password.', 'error')
        elif new_pw != confirm_pw:
            flash('New passwords do not match.', 'error')
        elif len(new_pw) < 4:
            flash('Password must be at least 4 characters long.', 'error')
        else:
            user.password_hash = generate_password_hash(new_pw)
            db.session.commit()
            flash('Password updated successfully.', 'success')
            return redirect(url_for('hod_settings'))

    return render_template('hod_settings.html', dept=dept, teacher=teacher_self)

# ══════════════════════════════════════════════════════════════
# TEACHER: Notifications view
# ══════════════════════════════════════════════════════════════

@app.route('/notifications')
@login_required
def teacher_notifications():
    if is_admin():
        return redirect(url_for('admin_dashboard'))
    teacher = get_teacher_for_user()
    if not teacher:
        return redirect(url_for('teacher_profile'))

    # All notifications for this teacher: personal + dept-wide
    # Exclude dept-wide notifications where the viewer is the sender
    notifs = Notification.query.filter(
        ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{teacher.id},%'),
        db.or_(
            Notification.recipient_id == teacher.id,
            db.and_(
                Notification.dept_id == teacher.dept_id,
                Notification.recipient_id == None,
                db.or_(Notification.sender_id != teacher.id, Notification.sender_id == None)
            )
        )
    ).order_by(Notification.created_at.desc()).all()

    # Mark all as read
    for n in notifs:
        if not n.is_read:
            n.is_read = True
    db.session.commit()

    return render_template('teacher_notifications.html',
        teacher=teacher, notifications=notifs)


@app.route('/notifications/clear', methods=['POST'])
@login_required
def clear_notifications():
    """Clear all (mark as read + delete) notifications for the current teacher."""
    teacher = get_teacher_for_user()
    if not teacher:
        return jsonify({'ok': False}), 400
    # Instead of deleting, we mark them as cleared by this user.
    notifs_to_clear = Notification.query.filter(
        ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{teacher.id},%'),
        db.or_(
            Notification.recipient_id == teacher.id,
            db.and_(
                Notification.dept_id == teacher.dept_id,
                Notification.recipient_id == None
            )
        )
    ).all()
    
    for n in notifs_to_clear:
        if not n.cleared_by:
            n.cleared_by = ','
        if f',{teacher.id},' not in n.cleared_by:
            n.cleared_by += f'{teacher.id},'
            
    db.session.commit()
    if is_hod():
        return redirect(url_for('hod_inbox'))
    return redirect(url_for('teacher_notifications'))


@app.route('/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    """Mark a single notification as read for the current user."""
    teacher = get_teacher_for_user()
    n = Notification.query.get_or_404(notif_id)
    if not n.is_read:
        n.is_read = True
        db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    if is_hod():
        return redirect(url_for('hod_inbox'))
    return redirect(url_for('teacher_notifications'))


@app.route('/notifications/delete/<int:notif_id>', methods=['POST'])
@login_required
def delete_notification(notif_id):
    """Delete (hide) a single notification for the current user only."""
    t_id = _get_notif_identity()
    if not t_id:
        return jsonify({'ok': False}), 400
    n = Notification.query.get_or_404(notif_id)
    # Use the per-user cleared_by pattern so it only hides for this user
    if not n.cleared_by:
        n.cleared_by = ','
    if f',{t_id},' not in n.cleared_by:
        n.cleared_by += f'{t_id},'
    n.is_read = True
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    if is_admin():
        return redirect(url_for('admin_notifications_page'))
    if is_hod():
        return redirect(url_for('hod_inbox'))
    return redirect(url_for('teacher_notifications'))


@app.route('/admin/notifications/clear', methods=['POST'])
@login_required
def admin_clear_notifications():
    """Clear all notifications from the ADMIN's view only."""
    if not is_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    t_id = _get_notif_identity()
    if t_id:
        notifs = Notification.query.filter(
            Notification.recipient_id == t_id,
            ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{t_id},%')
        ).all()
        for n in notifs:
            if not n.cleared_by:
                n.cleared_by = ','
            if f',{t_id},' not in n.cleared_by:
                n.cleared_by += f'{t_id},'
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/notify-hods', methods=['POST'])
@login_required
def admin_notify_hods():
    """Admin sends an in-app notification to all HOD accounts."""
    if not is_admin():
        flash('Unauthorized.', 'error')
        return redirect(url_for('admin_dashboard'))
    title   = request.form.get('title', '').strip()
    message = request.form.get('message', '').strip()
    if not title or not message:
        flash('Title and message are required.', 'error')
        return redirect(url_for('admin_dashboard'))
    from datetime import datetime as _dt
    hod_auths = TeacherAuth.query.filter_by(role='hod').all()
    count = 0
    for ha in hod_auths:
        if ha.teacher_id:
            db.session.add(Notification(
                dept_id=None,
                recipient_id=ha.teacher_id,
                sender_id=None,
                title=title,
                message=message,
                is_read=False,
                cleared_by='',
                created_at=_dt.utcnow(),
            ))
            count += 1
    db.session.commit()
    flash(f'Notification sent to {count} HOD(s).', 'success')
    return redirect(url_for('admin_dashboard'))


# ── HOD: My Own Preferences ───────────────────────────────────────

@app.route('/hod/my-preferences', methods=['GET', 'POST'])
@login_required
def hod_my_preferences():
    """HOD can submit/view their own subject preferences (window not required)."""
    if not is_hod():
        return redirect(url_for('teacher_profile'))
    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher record not found.', 'error')
        return redirect(url_for('hod_dashboard'))
    dept = get_hod_dept()

    sem_type = SystemSettings.get('active_semester_type', 'odd')
    valid_sems = [1, 3, 5, 7] if sem_type == 'odd' else [2, 4, 6, 8]

    _EXCLUDED_PREF_KEYWORDS = ['seminar', 'project phase', 'mini project', 'miniproject', 'ccw', 'y2p']
    courses_raw = Course.query.filter(
        Course.semester.in_([str(s) for s in valid_sems]),
        Course.department == teacher.dept_id,
        ~Course.type.ilike('%lab%'),
        ~Course.type.ilike('%project%'),
    ).order_by(Course.semester, Course.name).all()

    def _is_theory(c):
        return not any(kw in (c.name or '').lower() for kw in _EXCLUDED_PREF_KEYWORDS)

    courses = [c for c in courses_raw if _is_theory(c)]
    courses_by_sem = {}
    for c in courses:
        courses_by_sem.setdefault(c.semester, []).append(c)

    existing_prefs = TeacherPreference.query.filter_by(teacher_id=teacher.id).order_by(TeacherPreference.rank).all()

    if request.method == 'POST':
        codes = [
            request.form.get('pref_1', '').strip(),
            request.form.get('pref_2', '').strip(),
            request.form.get('pref_3', '').strip(),
        ]
        ok, message = save_teacher_preferences(teacher, codes, sem_type)
        flash(message, 'success' if ok else 'error')
        return redirect(url_for('hod_my_preferences'))

    return render_template('hod_preference_window.html',
        dept=dept,
        sem_type=sem_type,
        pref_window=get_pref_window(dept.id, sem_type),
        pref_status=[],
        teacher=teacher,
        hod_prefs=existing_prefs,
        hod_courses_by_sem=courses_by_sem,
        show_my_prefs=True,
    )


@app.route('/api/notifications/unread-count')
@login_required
def notif_unread_count():
    """Returns unread notification count for the current user (teacher/hod/admin)."""
    teacher = get_teacher_for_user()
    if not teacher:
        return jsonify({'count': 0})
    count = Notification.query.filter(
        ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{teacher.id},%'),
        db.or_(
            Notification.recipient_id == teacher.id,
            db.and_(
                Notification.dept_id == teacher.dept_id,
                Notification.recipient_id == None
            )
        ),
        Notification.is_read == False
    ).count()
    return jsonify({'count': count})


@app.route('/api/notifications/popup')
@login_required
def notif_popup():
    """Returns latest unread notifications for the login popup."""
    teacher = get_teacher_for_user()
    if not teacher:
        return jsonify({'notifications': []})
    items = Notification.query.filter(
        ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{teacher.id},%'),
        db.or_(
            Notification.recipient_id == teacher.id,
            db.and_(
                Notification.dept_id == teacher.dept_id,
                Notification.recipient_id == None
            )
        ),
        Notification.is_read == False
    ).order_by(Notification.created_at.desc()).limit(5).all()
    return jsonify({'notifications': [
        {'id': n.id, 'title': n.title, 'message': n.message,
         'at': n.created_at.strftime('%d %b %H:%M') if n.created_at else ''}
        for n in items
    ]})


@app.route('/admin/notifications')
@login_required
def admin_notifications():
    """Admin sees notifications directed at admins (filtered by cleared_by)."""
    if not is_admin():
        return redirect(url_for('login'))
    t_id = _get_notif_identity()
    q = Notification.query.filter(Notification.recipient_id == t_id)
    if t_id:
        q = q.filter(~db.func.coalesce(Notification.cleared_by, '').like(f'%,{t_id},%'))
    notifs = q.order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify({'notifications': [
        {'id': n.id, 'title': n.title, 'message': n.message,
         'dept': n.dept_id, 'sender': n.sender.name if n.sender else 'System',
         'read': n.is_read,
         'at': n.created_at.strftime('%d %b %H:%M') if n.created_at else ''}
        for n in notifs
    ]})


@app.route('/admin/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
def admin_mark_notification_read(notif_id):
    """Mark a single admin notification as read."""
    if not is_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    n = Notification.query.get_or_404(notif_id)
    if not n.is_read:
        n.is_read = True
        db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('admin_notifications_page'))


@app.route('/admin/notifications/mark-all-read', methods=['POST'])
@login_required
def admin_mark_all_notifications_read():
    """Mark all admin notifications as read."""
    if not is_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    t_id = _get_notif_identity()
    if t_id:
        notifs = Notification.query.filter(
            Notification.recipient_id == t_id,
            Notification.is_read == False,
            ~db.func.coalesce(Notification.cleared_by, '').like(f'%,{t_id},%')
        ).all()
        for n in notifs:
            n.is_read = True
        db.session.commit()
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('admin_notifications_page'))


@app.route('/admin/notifications/page')
@login_required
def admin_notifications_page():
    """Admin full-page notifications view."""
    if not is_admin():
        return redirect(url_for('login'))
    t_id = _get_notif_identity()
    q = Notification.query.filter(Notification.recipient_id == t_id)
    if t_id:
        q = q.filter(~db.func.coalesce(Notification.cleared_by, '').like(f'%,{t_id},%'))
    notifs = q.order_by(Notification.created_at.desc()).limit(100).all()
    return render_template('admin_notifications.html', notifications=notifs)



@login_required
def pref_window_status():
    teacher = get_teacher_for_user()
    if not teacher: return jsonify({'open': False})
    is_open = pref_window_open_for_teacher(teacher)
    return jsonify({'open': is_open})

# ══════════════════════════════════════════════════════════════
# UPDATED: Semester-aware preference submission for teachers
# ══════════════════════════════════════════════════════════════

@app.route('/update-preferences', methods=['POST'])
@login_required
def update_preferences():
    """
    Teacher submits 3 preferences per semester group (odd or even).
    Each preference must come from a DIFFERENT semester.
    Only allowed when preference window is open.
    """
    if is_admin():
        return redirect(url_for('admin_dashboard'))

    teacher = get_teacher_for_user()
    if not teacher:
        flash('Teacher record not found.', 'error')
        return redirect(url_for('teacher_profile'))

    # Check window open
    if not pref_window_open_for_teacher(teacher):
        flash('The preference submission window is currently closed.', 'error')
        return redirect(url_for('teacher_profile'))

    sem_type = SystemSettings.get('active_semester_type', 'odd')
    sem_list  = [1, 3, 5, 7] if sem_type == 'odd' else [2, 4, 6, 8]

    # Collect the submitted course codes. The form also posts a
    # pref_sem_i hint per row, but the semester actually stored always
    # comes from the course record itself (via save_teacher_preferences),
    # so a stale/mismatched hint can't desync what's saved from what the
    # course actually is.
    codes = []
    for i in range(1, 4):
        code = request.form.get(f'pref_code_{i}', '').strip()
        if code:
            codes.append(code)

    ok, message = save_teacher_preferences(teacher, codes, sem_type)
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('teacher_profile'))


# ══════════════════════════════════════════════════════════════
# ADMIN: HOD Role Management
# ══════════════════════════════════════════════════════════════

@app.route('/admin/teacher/set-hod/<teacher_id>', methods=['POST'])
@login_required
def admin_set_hod(teacher_id):
    """Promote a teacher to HOD (or demote back to teacher)."""
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 403
    t = Teacher.query.get_or_404(teacher_id)
    if not t.auth:
        flash('This teacher has no login account.', 'error')
        return redirect(url_for('admin_teachers'))
    new_role = request.form.get('role', 'teacher')
    if new_role not in ('teacher', 'hod', 'admin'):
        new_role = 'teacher'
    t.auth.role = new_role
    db.session.commit()
    flash(f'{t.name} role set to {new_role}.', 'success')
    return redirect(url_for('admin_teachers'))


# ══════════════════════════════════════════════════════════════
# ADMIN: System Settings
# ══════════════════════════════════════════════════════════════

@app.route('/admin/set-semester', methods=['POST'])
@login_required
def admin_set_semester():
    """Toggle the global semester mode (Odd/Even)."""
    if not is_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    mode = request.form.get('semester_type', '').lower()
    if mode in ['odd', 'even']:
        SystemSettings.set('active_semester_type', mode)
        flash(f'System is now in {mode.capitalize()} Semester mode.', 'success')
    return redirect(url_for('admin_dashboard'))


# ══════════════════════════════════════════════════════════════
# API: Teacher preference window status
# ══════════════════════════════════════════════════════════════

@app.route('/api/pref-window-status')
@login_required
def api_pref_window_status():
    teacher = get_teacher_for_user()
    if not teacher or not teacher.dept_id:
        return jsonify({'open': False})
    sem_type = SystemSettings.get('active_semester_type', 'odd')
    w = get_pref_window(teacher.dept_id, sem_type)
    open_ = w.is_currently_open() if w else False
    until = w.open_until.strftime('%d %b %Y %H:%M UTC') if (w and w.open_until) else ''
    return jsonify({'open': open_, 'until': until, 'sem_type': sem_type})


# ══════════════════════════════════════════════════════════════
# API: Subjects for teacher preference selection
# ══════════════════════════════════════════════════════════════

@app.route('/api/subjects-for-pref')
@login_required
def api_subjects_for_pref():
    """Return courses for a dept+semester that teacher can select as preference."""
    dept_id    = request.args.get('dept_id', '')
    semester   = request.args.get('semester', '')
    if not dept_id or not semester:
        return jsonify([])
    courses = Course.query.filter_by(
        department=dept_id, semester=semester
    ).order_by(Course.name).all()
    return jsonify([
        {'code': c.code, 'name': c.name, 'type': c.type}
        for c in courses
        if 'lab' not in (c.type or '').lower()  # exclude standalone labs from preferences
    ])

# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5001)