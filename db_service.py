"""
Database-backed availability & appointment service.

Reads/writes from the shared PostgreSQL database (same as the NextJS panel).
All availability detection, zone grouping and day summaries use SQLAlchemy
against the Prisma-created tables.

Working window: Monday-Friday, 08:00-16:00. Standard job block: 1.5h (90 min).
"""

from datetime import datetime, timedelta, time as dtime, date as ddate

from models import db, Appointment, BlockedDay, AppointmentStatus, AppointmentSource
from zones import get_zone_for_location

# --- Working window / defaults ----------------------------------------------
WORK_START_HOUR = 8
WORK_END_HOUR = 16
DEFAULT_DURATION_HOURS = 1.5
DEFAULT_DURATION_MIN = 90

# --- Spanish date formatting ------------------------------------------------
_MONTHS_ES = {
    1: 'enero', 2: 'febrero', 3: 'marzo', 4: 'abril', 5: 'mayo', 6: 'junio',
    7: 'julio', 8: 'agosto', 9: 'septiembre', 10: 'octubre', 11: 'noviembre',
    12: 'diciembre'
}
_DAYS_ES = {
    0: 'lunes', 1: 'martes', 2: 'miércoles', 3: 'jueves', 4: 'viernes',
    5: 'sábado', 6: 'domingo'
}


def format_es(dt):
    return (f"{_DAYS_ES[dt.weekday()]} {dt.day} de "
            f"{_MONTHS_ES[dt.month]} a las {dt.strftime('%H:%M')}")


def format_date_es(d):
    return f"{_DAYS_ES[d.weekday()].capitalize()} {d.day} de {_MONTHS_ES[d.month]}"


def _date_str(d):
    """Convert a date/datetime to 'YYYY-MM-DD' string."""
    if isinstance(d, datetime):
        d = d.date()
    return d.isoformat()


def _parse_time(ts):
    """Parse 'HH:MM' string to (hour, minute) tuple."""
    h, m = map(int, ts.split(':'))
    return h, m


def _time_to_minutes(ts):
    """Convert 'HH:MM' to minutes since midnight."""
    h, m = _parse_time(ts)
    return h * 60 + m


def _minutes_to_time(total_min):
    """Convert minutes since midnight to 'HH:MM'."""
    h, m = divmod(total_min, 60)
    return f"{h:02d}:{m:02d}"


# --- Query helpers ----------------------------------------------------------

def _is_day_blocked(day_str):
    """Check if a day has a BlockedDay entry."""
    return BlockedDay.query.filter(BlockedDay.date == day_str).first() is not None


def _get_appointments_for_day_str(day_str):
    """Return active appointments for a day (string YYYY-MM-DD)."""
    return (Appointment.query
            .filter(Appointment.date == day_str)
            .filter(Appointment.status.in_(AppointmentStatus.ACTIVE))
            .order_by(Appointment.timeStart.asc())
            .all())


def _active_events_in_range(start_str, end_str):
    """Active appointments in [start_str, end_str] (both YYYY-MM-DD)."""
    return (Appointment.query
            .filter(Appointment.date >= start_str)
            .filter(Appointment.date <= end_str)
            .filter(Appointment.status.in_(AppointmentStatus.ACTIVE))
            .all())


# --- Availability -----------------------------------------------------------

def _overlaps_any(start_min, end_min, day_appts):
    """Check if [start_min, end_min) overlaps any existing appointment."""
    for appt in day_appts:
        if not appt.timeStart:
            continue
        appt_start = _time_to_minutes(appt.timeStart)
        appt_end = appt_start + appt.duration
        if start_min < appt_end and end_min > appt_start:
            return True
    return False


def _free_slots_for_day(day_date, duration_min, day_appts):
    """Return valid start datetimes for a given day (no overlaps)."""
    day_start = WORK_START_HOUR * 60  # 480
    day_end = WORK_END_HOUR * 60      # 960

    now = datetime.now()
    if day_date == now.date():
        current_min = now.hour * 60 + now.minute
        # Round up to next 30-min block
        if current_min % 30 != 0:
            current_min = current_min + (30 - current_min % 30)
        cursor = max(day_start, current_min)
    else:
        cursor = day_start

    free = []
    while cursor + duration_min <= day_end:
        if not _overlaps_any(cursor, cursor + duration_min, day_appts):
            slot_dt = datetime.combine(day_date, dtime(cursor // 60, cursor % 60))
            free.append(slot_dt)
            cursor += duration_min  # Jump past this slot
        else:
            cursor += 30
    return free


def _build_slot(slot_start, duration_min, in_zone=False, zone_note=None):
    duration_hours = duration_min / 60.0
    slot_end = slot_start + timedelta(minutes=duration_min)
    return {
        'datetime': slot_start,
        'iso': slot_start.isoformat(),
        'date': slot_start.date().isoformat(),
        'start_time': slot_start.strftime('%H:%M'),
        'end_time': slot_end.strftime('%H:%M'),
        'formatted': format_es(slot_start),
        'in_zone': in_zone,
        'zone_note': zone_note,
        'duration_min': duration_min,
    }


def _format_zone_note(zone_appts):
    if not zone_appts:
        return None
    parts = []
    for ev in zone_appts:
        hhmm = ev.timeStart or '--:--'
        loc = ev.locality or ev.client or 'cita'
        parts.append(f"{hhmm} ({loc})")
    return "Ya hay citas en la zona ese día: " + ", ".join(parts)


def find_available_slots(duration_hours=DEFAULT_DURATION_HOURS, num_slots=3,
                         days_ahead=14, preferred_zone=None, start_from=None):
    if not duration_hours or duration_hours <= 0:
        duration_hours = DEFAULT_DURATION_HOURS
    duration_min = int(duration_hours * 60)

    base = start_from or (datetime.now() + timedelta(days=1))
    if isinstance(base, datetime):
        base_date = base.date()
    else:
        base_date = base
    end_date = base_date + timedelta(days=days_ahead)

    start_str = _date_str(base_date)
    end_str = _date_str(end_date)

    events = _active_events_in_range(start_str, end_str)

    # Bucket events by date string
    events_by_day = {}
    for ev in events:
        events_by_day.setdefault(ev.date, []).append(ev)

    # Blocked days
    blocked_days_q = (BlockedDay.query
                      .filter(BlockedDay.date >= start_str)
                      .filter(BlockedDay.date <= end_str)
                      .all())
    blocked_set = {bd.date for bd in blocked_days_q}

    # Same-zone appointments per day
    zone_days = {}
    if preferred_zone:
        for ev in events:
            if get_zone_for_location(ev.locality) == preferred_zone:
                zone_days.setdefault(ev.date, []).append(ev)

    candidate_days = []
    for offset in range(days_ahead + 1):
        day = base_date + timedelta(days=offset)
        if day.weekday() >= 5:  # skip weekends
            continue
        day_str = _date_str(day)
        if day_str in blocked_set:
            continue
        day_appts = events_by_day.get(day_str, [])
        day_slots = _free_slots_for_day(day, duration_min, day_appts)
        if not day_slots:
            continue
        in_zone = day_str in zone_days
        candidate_days.append({
            'date': day,
            'date_str': day_str,
            'slots': day_slots,
            'in_zone': in_zone,
            'zone_appointments': zone_days.get(day_str, []),
        })

    # Prioritise same-zone days
    candidate_days.sort(key=lambda d: (not d['in_zone'], d['date']))

    slots = []
    for day in candidate_days:
        zone_note = _format_zone_note(day['zone_appointments']) if day['in_zone'] else None
        for slot_start in day['slots']:
            slots.append(_build_slot(slot_start, duration_min,
                                     in_zone=day['in_zone'], zone_note=zone_note))
            if len(slots) >= num_slots:
                return slots
    return slots


# --- Daily reminder ---------------------------------------------------------

def get_day_appointments(day):
    if isinstance(day, datetime):
        day = day.date()
    day_str = _date_str(day)

    appts = (Appointment.query
             .filter(Appointment.date == day_str)
             .filter(Appointment.status.in_(AppointmentStatus.ACTIVE))
             .order_by(Appointment.timeStart.asc())
             .all())

    result = []
    for a in appts:
        d = a.to_dict()
        d['time'] = a.timeStart or '--:--'
        result.append(d)
    return result


# --- Create appointment from WhatsApp flow ----------------------------------

def create_appointment_from_slot(conversation, slot):
    """Persist a confirmed appointment from the WhatsApp scheduling flow."""
    duration_hours = conversation.get('duration_hours') or DEFAULT_DURATION_HOURS
    duration_min = int(duration_hours * 60)

    appt = Appointment(
        date=slot['date'],
        timeStart=slot['start_time'],
        duration=duration_min,
        client=conversation.get('customer_name') or 'Cliente',
        phone=conversation.get('customer_phone') or '',
        locality=conversation.get('location') or '',
        workType=conversation.get('work_type') or '',
        reference=conversation.get('reference') or '',
        source=AppointmentSource.LEROY,
        status=AppointmentStatus.PENDING,
        notes=conversation.get('address') or '',
    )
    db.session.add(appt)
    db.session.commit()
    return appt
