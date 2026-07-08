"""
Database models for the WhatsApp appointment system.

Uses Flask-SQLAlchemy with a SQLite backend. A single `appointments` table
stores every kind of calendar entry: Leroy Merlin jobs, personal jobs and
full-day blocks. This replaces the previous Google Calendar integration and
gives the installer full control over their own data.
"""

from datetime import datetime, time as dtime
from flask_sqlalchemy import SQLAlchemy

from zones import get_zone_for_location

db = SQLAlchemy()


# --- Enumerations (kept as plain string constants for SQLite portability) ---

class AppointmentStatus:
    PENDING = 'pending'
    CONFIRMED = 'confirmed'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'
    BLOCKED = 'blocked'

    ALL = [PENDING, CONFIRMED, COMPLETED, CANCELLED, BLOCKED]
    # Statuses that occupy time on the calendar (used for availability checks).
    ACTIVE = [PENDING, CONFIRMED, COMPLETED, BLOCKED]


class AppointmentSource:
    LEROY_MERLIN = 'leroy_merlin'
    PERSONAL = 'personal'
    BLOCKED_DAY = 'blocked_day'

    ALL = [LEROY_MERLIN, PERSONAL, BLOCKED_DAY]


class Appointment(db.Model):
    __tablename__ = 'appointments'

    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(200), nullable=True)
    customer_phone = db.Column(db.String(40), nullable=True)
    location = db.Column(db.String(200), nullable=True)      # locality / zone
    work_type = db.Column(db.String(200), nullable=True)
    duration_hours = db.Column(db.Float, nullable=True, default=1.5)

    appointment_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)

    status = db.Column(db.String(20), nullable=False, default=AppointmentStatus.PENDING)
    source = db.Column(db.String(20), nullable=False, default=AppointmentSource.LEROY_MERLIN)

    notes = db.Column(db.Text, nullable=True)
    wo_reference = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    # -- Helpers -------------------------------------------------------------

    @property
    def zone(self):
        return get_zone_for_location(self.location) or 'Sin zona'

    @property
    def is_full_day_block(self):
        return self.source == AppointmentSource.BLOCKED_DAY or self.start_time is None

    def start_datetime(self):
        if self.start_time is None:
            return datetime.combine(self.appointment_date, dtime(0, 0))
        return datetime.combine(self.appointment_date, self.start_time)

    def end_datetime(self):
        if self.end_time is None:
            # Full-day block covers the whole day.
            return datetime.combine(self.appointment_date, dtime(23, 59, 59))
        return datetime.combine(self.appointment_date, self.end_time)

    def to_dict(self):
        return {
            'id': self.id,
            'customer_name': self.customer_name,
            'customer_phone': self.customer_phone,
            'location': self.location,
            'zone': self.zone,
            'work_type': self.work_type,
            'duration_hours': self.duration_hours,
            'appointment_date': self.appointment_date.isoformat() if self.appointment_date else None,
            'start_time': self.start_time.strftime('%H:%M') if self.start_time else None,
            'end_time': self.end_time.strftime('%H:%M') if self.end_time else None,
            'status': self.status,
            'source': self.source,
            'notes': self.notes,
            'wo_reference': self.wo_reference,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
