from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class Department(db.Model):
    __tablename__ = 'departments'
    id       = db.Column(db.String(20), primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    hod_name = db.Column(db.String(100), default='')
    # Number of lab rooms this department can run SIMULTANEOUSLY at any given
    # period. Gates whether two batches of a division can be scheduled as a
    # true parallel lab (same day+period, two different labs) vs falling
    # back to sequential single-lab sessions. Defaults to 2 — the capacity
    # implicitly assumed everywhere in the codebase before this field existed
    # — so nothing changes for existing departments until an admin sets it.
    lab_room_capacity = db.Column(db.Integer, default=2, nullable=False)
    teachers = db.relationship('Teacher', backref='dept', lazy=True)
    def __repr__(self): return f'<Department {self.id}>'


class Teacher(db.Model):
    __tablename__ = 'teachers'
    id                     = db.Column(db.String(30), primary_key=True)
    name                   = db.Column(db.String(100), nullable=False)
    code                   = db.Column(db.String(10), nullable=True)
    designation            = db.Column(db.String(60), default='')
    gender                 = db.Column(db.String(10), default='')
    experience             = db.Column(db.String(20), default='')
    date_of_joining        = db.Column(db.String(30), default='')
    seniority_level        = db.Column(db.String(10), default='')
    area_of_specialization = db.Column(db.String(200), default='')
    qualification          = db.Column(db.String(200), default='')
    dept_id                = db.Column(db.String(20), db.ForeignKey('departments.id'), nullable=True)
    photo_path             = db.Column(db.String(200), nullable=True)
    display_name           = db.Column(db.String(100), nullable=True)
    auth        = db.relationship('TeacherAuth', backref='teacher', uselist=False, lazy=True)
    preferences = db.relationship('TeacherPreference', backref='teacher', lazy=True)
    
    def get_display_name(self):
        """Return display name if set, otherwise return full name"""
        return self.display_name if self.display_name else self.name
    
    def __repr__(self): return f'<Teacher {self.name}>'


class Course(db.Model):
    __tablename__ = 'courses'
    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code           = db.Column(db.String(30), index=True, nullable=False)
    name           = db.Column(db.String(200), nullable=False)
    department     = db.Column(db.String(20), default='', index=True)
    dept_id        = db.Column(db.String(60), default='')
    type           = db.Column(db.String(30), default='')
    credit         = db.Column(db.Integer, default=0)
    hours_per_week = db.Column(db.String(30), default='')
    semester       = db.Column(db.String(10), default='')
    division       = db.Column(db.String(100), default='')
    revision       = db.Column(db.String(20), default='')
    __table_args__ = (db.UniqueConstraint('code', 'department', name='uq_course_dept'),)
    def __repr__(self): return f'<Course {self.code} [{self.department}]>'


class TeacherPreference(db.Model):
    __tablename__ = 'teacher_preferences'
    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    teacher_id   = db.Column(db.String(30), db.ForeignKey('teachers.id'), nullable=True)
    teacher_code = db.Column(db.String(10), nullable=True)
    course_code  = db.Column(db.String(30), nullable=True)
    rank         = db.Column(db.Integer, default=1)
    semester     = db.Column(db.String(10), default='')
    created_at   = db.Column(db.DateTime, nullable=True)
    def __repr__(self): return f'<Pref {self.teacher_id} -> {self.course_code}>'


class TeacherAuth(db.Model, UserMixin):
    __tablename__ = 'teacher_auth'
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    teacher_id    = db.Column(db.String(30), db.ForeignKey('teachers.id'), nullable=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    email         = db.Column(db.String(120), nullable=True)
    role          = db.Column(db.String(10), default='teacher')  # 'teacher' | 'admin' | 'hod'
    # Single-session enforcement: holds the token of the ONE currently-valid
    # login for this account. Every login generates a fresh token here and in
    # the Flask session cookie; any older session (different browser/device/
    # tab) whose cookie carries a stale token gets logged out on its next
    # request. NULL means no active session (never logged in, or logged out).
    active_session_token = db.Column(db.String(64), nullable=True)
    def set_password(self, p):
        self.password_hash = generate_password_hash(p, method='pbkdf2:sha256')
    def check_password(self, p):
        return check_password_hash(self.password_hash, p)
    def __repr__(self): return f'<Auth {self.username} [{self.role}]>'


class SubjectAssignment(db.Model):
    __tablename__ = 'subject_assignments'
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    dept_id       = db.Column(db.String(20), index=True)
    semester      = db.Column(db.Integer, default=0)
    semester_type = db.Column(db.String(10), default='')
    division_id   = db.Column(db.String(50), default='')
    subject_code  = db.Column(db.String(30), default='')
    subject_name  = db.Column(db.String(200), default='')
    subject_type  = db.Column(db.String(20), default='')
    teacher_id    = db.Column(db.String(30), db.ForeignKey('teachers.id'), nullable=True)
    teacher_name  = db.Column(db.String(100), default='')
    role          = db.Column(db.String(20), default='main')
    created_at    = db.Column(db.String(30), default='')
    def __repr__(self): return f'<SubjectAssignment {self.subject_code}>'


class Timetable(db.Model):
    __tablename__ = 'timetables'
    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    dept_id        = db.Column(db.String(20), db.ForeignKey('departments.id'), nullable=True, index=True)
    semester       = db.Column(db.String(10), default='')
    semester_type  = db.Column(db.String(10), default='')
    division       = db.Column(db.String(50), default='')
    timetable_type = db.Column(db.String(20), default='class')
    key            = db.Column(db.String(100), default='')
    effective_date = db.Column(db.String(30), default='')
    data_json      = db.Column(db.Text, default='{}')
    created_at     = db.Column(db.String(30), default='')
    def __repr__(self): return f'<Timetable {self.timetable_type}:{self.key}>'


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    auth_id    = db.Column(db.Integer, db.ForeignKey('teacher_auth.id'), nullable=False)
    token      = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)
    auth = db.relationship('TeacherAuth', backref='reset_tokens')
    def __repr__(self): return f'<PasswordResetToken {self.token[:8]}>'


# ─────────────────────────────────────────────────────────────────
# NEW MODELS — required by HOD features in app.py
# ─────────────────────────────────────────────────────────────────

class PreferenceWindow(db.Model):
    """HOD opens a time-limited window for teachers to edit preferences."""
    __tablename__ = 'preference_windows'
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    dept_id       = db.Column(db.String(20), db.ForeignKey('departments.id'), nullable=False, index=True)
    semester_type = db.Column(db.String(10), nullable=False)   # 'odd' | 'even'
    is_open       = db.Column(db.Boolean, default=False)
    open_from     = db.Column(db.DateTime, nullable=True)
    open_until    = db.Column(db.DateTime, nullable=True)
    opened_by     = db.Column(db.String(30), nullable=True)
    created_at    = db.Column(db.DateTime, nullable=True)
    department    = db.relationship('Department', backref='preference_windows')

    def is_currently_open(self):
        from datetime import datetime
        if not self.is_open:
            return False
        now = datetime.utcnow()
        if self.open_from and now < self.open_from:
            return False
        if self.open_until and now > self.open_until:
            return False
        return True

    def __repr__(self):
        return f'<PreferenceWindow {self.dept_id} {self.semester_type} open={self.is_open}>'


class Notification(db.Model):
    """HOD or Admin sends in-app notifications to teachers."""
    __tablename__ = 'notifications'
    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    dept_id      = db.Column(db.String(20), db.ForeignKey('departments.id'), nullable=True, index=True)
    recipient_id = db.Column(db.String(30), db.ForeignKey('teachers.id'), nullable=True)
    sender_id    = db.Column(db.String(30), nullable=True)
    title        = db.Column(db.String(200), default='')
    message      = db.Column(db.Text, default='')
    is_read      = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, nullable=True)
    cleared_by   = db.Column(db.Text, default='')
    department   = db.relationship('Department', backref='notifications')
    recipient    = db.relationship('Teacher', backref='notifications', foreign_keys=[recipient_id])
    sender       = db.relationship('Teacher', foreign_keys=[sender_id], primaryjoin="Notification.sender_id == Teacher.id")
    def __repr__(self): return f'<Notification to={self.recipient_id} "{self.title[:20]}">'


class SystemSettings(db.Model):
    """Key-value store for configurable settings (active semester, college name, etc.)."""
    __tablename__ = 'system_settings'
    key        = db.Column(db.String(100), primary_key=True)
    value      = db.Column(db.Text, default='')
    updated_at = db.Column(db.String(30), default='')

    @staticmethod
    def get(key, default=''):
        row = SystemSettings.query.get(key)
        return row.value if row else default

    @staticmethod
    def set(key, value):
        from datetime import datetime
        row = SystemSettings.query.get(key)
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        if row:
            row.value = str(value)
            row.updated_at = now
        else:
            db.session.add(SystemSettings(key=key, value=str(value), updated_at=now))
        db.session.commit()

    def __repr__(self): return f'<Setting {self.key}>'