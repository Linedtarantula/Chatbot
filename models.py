"""
Database models for the WhatsApp appointment system.

Maps directly to the PostgreSQL tables created by Prisma in the NextJS panel.
Table/column names must match EXACTLY what Prisma generates.
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
import uuid

from zones import get_zone_for_location

db = SQLAlchemy()


def generate_cuid():
    """Generate a cuid-like ID compatible with Prisma's @default(cuid())."""
    return 'c' + uuid.uuid4().hex[:24]


# --- Source / Status constants -----------------------------------------------

class AppointmentSource:
    LEROY = 'leroy'
    PERSONAL = 'personal'
    ALL = [LEROY, PERSONAL]


class AppointmentStatus:
    PENDING = 'pending'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'
    # Active statuses that occupy time on the calendar.
    ACTIVE = [PENDING, COMPLETED]


class Appointment(db.Model):
    """Maps to the Prisma 'Appointment' table."""
    __tablename__ = 'Appointment'  # Prisma table name (PascalCase)

    id        = db.Column(db.String, primary_key=True, default=generate_cuid)
    date      = db.Column(db.String, nullable=False)          # 'YYYY-MM-DD'
    timeStart = db.Column('timeStart', db.String, nullable=True)  # 'HH:mm'
    duration  = db.Column(db.Integer, nullable=False, default=60) # minutes
    client    = db.Column(db.String, nullable=False)
    phone     = db.Column(db.String, nullable=False, default='')
    locality  = db.Column(db.String, nullable=False)
    workType  = db.Column('workType', db.String, nullable=False, default='')
    reference = db.Column(db.String, nullable=False, default='')
    source    = db.Column(db.String, nullable=False, default='leroy')
    status    = db.Column(db.String, nullable=False, default='pending')
    notes     = db.Column(db.String, nullable=False, default='')
    createdAt = db.Column('createdAt', db.DateTime, nullable=False, default=datetime.utcnow)
    updatedAt = db.Column('updatedAt', db.DateTime, nullable=False, default=datetime.utcnow,
                          onupdate=datetime.utcnow)

    # --- Helpers ---

    @property
    def zone(self):
        return get_zone_for_location(self.locality) or 'Sin zona'

    @property
    def duration_hours(self):
        return self.duration / 60.0

    @property
    def end_time_str(self):
        """Compute end time string from timeStart + duration."""
        if not self.timeStart:
            return None
        try:
            h, m = map(int, self.timeStart.split(':'))
            total_min = h * 60 + m + self.duration
            eh, em = divmod(total_min, 60)
            return f"{eh:02d}:{em:02d}"
        except Exception:
            return None

    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date,
            'timeStart': self.timeStart,
            'duration': self.duration,
            'client': self.client,
            'phone': self.phone,
            'locality': self.locality,
            'workType': self.workType,
            'reference': self.reference,
            'source': self.source,
            'status': self.status,
            'notes': self.notes,
            'createdAt': self.createdAt.isoformat() if self.createdAt else None,
            'updatedAt': self.updatedAt.isoformat() if self.updatedAt else None,
            # Computed fields (for backward compat with bot logic)
            'zone': self.zone,
            'end_time': self.end_time_str,
            'customer_name': self.client,
            'customer_phone': self.phone,
            'location': self.locality,
            'work_type': self.workType,
            'wo_reference': self.reference,
        }


class BlockedDay(db.Model):
    """Maps to the Prisma 'BlockedDay' table."""
    __tablename__ = 'BlockedDay'  # Prisma table name (PascalCase)

    id        = db.Column(db.String, primary_key=True, default=generate_cuid)
    date      = db.Column(db.String, unique=True, nullable=False)  # 'YYYY-MM-DD'
    reason    = db.Column(db.String, nullable=False, default='')
    createdAt = db.Column('createdAt', db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date,
            'reason': self.reason,
            'createdAt': self.createdAt.isoformat() if self.createdAt else None,
        }
