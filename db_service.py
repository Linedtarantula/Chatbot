"""
Database-backed availability & appointment service.

Replaces the previous Google Calendar integration. All availability detection,
zone grouping and day summaries are now computed from the local SQLite
database via SQLAlchemy.

Working window: Monday-Friday, 08:00-16:00. Standard job block: 1.5h.
"""

from datetime import datetime, timedelta, time as dtime, date as ddate

from models import db, Appointment, AppointmentStatus, AppointmentSource
from zones import get_zone_for_location

# --- Working window / defaults ---------------------------------------------
WORK_START_HOUR = 8            # 08:00
WORK_END_HOUR = 16            # 16:00
DEFAULT_DURATION_HOURS = 1.5  # standard job block

# --- Spanish date formatting -----------------------------------------------
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
    """Format a datetime like 'martes 7 de julio a las 08:00' (Spanish)."""
    return (f"{_DAYS_ES[dt.weekday()]} {dt.day} de "
            f"{_MONTHS_ES[dt.month]} a las {dt.strftime('%H:%M')}")


def format_date_es(d):
    """Format a date like 'Martes 7 de julio' (Spanish, capitalised)."""
    return f"{_DAYS_ES[d.weekday()].capitalize()} {d.day} de {_MONTHS_ES[d.month]}"


# --- Query helpers ----------------------------------------------------------

def get_appointments_for_day(day):
    """
    Return active appointments (occupying time) for a given date, ordered by
    start time. Cancelled appointments are excluded.
    """
    if isinstance(day, datetime):
        day = day.date()
    return (Appointment.query
            .filter(Appointment.appointment_date == day)
            .filter(Appointment.status.in_(AppointmentStatus.ACTIVE))
            .order_by(Appointment.start_time.asc())
            .all())


def _active_events_in_range(start_day, end_day):
    """Active appointments whose date falls within [start_day, end_day]."""
    return (Appointment.query
            .filter(Appointment.appointment_date >= start_day)
            .filter(Appointment.appointment_date <= end_day)
            .filter(Appointment.status.in_(AppointmentStatus.ACTIVE))
            .all())


# --- Availability -----------------------------------------------------------

def _overlaps_any(start_dt, end_dt, day_events):
    """True if [start_dt, end_dt) overlaps any event (or a full-day block)."""
    for ev in day_events:
        if ev.is_full_day_block:
            return True
        ev_start = ev.start_datetime()
        ev_end = ev.end_datetime()
        if start_dt < ev_end and end_dt > ev_start:
            return True
    return False


def _round_up_to_half(dt):
    if dt.minute in (0, 30):
        return dt.replace(second=0, microsecond=0)
    if dt.minute < 30:
        return dt.replace(minute=30, second=0, microsecond=0)
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _free_slots_for_day(day, duration_hours, day_events):
    """Return valid start datetimes for a given day (no overlaps)."""
    duration = timedelta(hours=duration_hours)
    day_start = datetime.combine(day, dtime(WORK_START_HOUR, 0))
    day_end = datetime.combine(day, dtime(WORK_END_HOUR, 0))

    # If a full-day block exists, no slots at all.
    if any(ev.is_full_day_block for ev in day_events):
        return []

    now = datetime.now()
    cursor = max(day_start, now) if day == now.date() else day_start
    cursor = _round_up_to_half(cursor)

    free = []
    while cursor + duration <= day_end:
        slot_end = cursor + duration
        if not _overlaps_any(cursor, slot_end, day_events):
            free.append(cursor)
            cursor = slot_end
        else:
            cursor += timedelta(minutes=30)
    return free


def _build_slot(slot_start, duration_hours, in_zone=False, zone_note=None):
    slot_end = slot_start + timedelta(hours=duration_hours)
    return {
        'datetime': slot_start,
        'iso': slot_start.isoformat(),
        'date': slot_start.date().isoformat(),
        'start_time': slot_start.strftime('%H:%M'),
        'end_time': slot_end.strftime('%H:%M'),
        'formatted': format_es(slot_start),
        'in_zone': in_zone,
        'zone_note': zone_note,
    }


def _format_zone_note(zone_appts):
    if not zone_appts:
        return None
    parts = []
    for ev in zone_appts:
        hhmm = ev.start_time.strftime('%H:%M') if ev.start_time else '--:--'
        loc = ev.location or ev.customer_name or 'cita'
        parts.append(f"{hhmm} ({loc})")
    return "Ya hay citas en la zona ese día: " + ", ".join(parts)


def find_available_slots(duration_hours=DEFAULT_DURATION_HOURS, num_slots=3,
                         days_ahead=14, preferred_zone=None, start_from=None):
    """
    Find available slots inside the working window that do not overlap with any
    active appointment in the database.

    Zone grouping: if preferred_zone is given, days that already have an
    appointment in that zone are proposed first.
    """
    if not duration_hours or duration_hours <= 0:
        duration_hours = DEFAULT_DURATION_HOURS

    base = start_from or (datetime.now() + timedelta(days=1))
    if isinstance(base, datetime):
        base_date = base.date()
    else:
        base_date = base
    end_date = base_date + timedelta(days=days_ahead)

    events = _active_events_in_range(base_date, end_date)

    # Bucket events by date.
    events_by_day = {}
    for ev in events:
        events_by_day.setdefault(ev.appointment_date, []).append(ev)

    # Same-zone appointments per day.
    zone_days = {}
    if preferred_zone:
        for ev in events:
            if ev.is_full_day_block:
                continue
            if get_zone_for_location(ev.location) == preferred_zone:
                zone_days.setdefault(ev.appointment_date, []).append(ev)

    candidate_days = []
    for offset in range(days_ahead + 1):
        day = base_date + timedelta(days=offset)
        if day.weekday() >= 5:  # skip weekends
            continue
        day_events = events_by_day.get(day, [])
        day_slots = _free_slots_for_day(day, duration_hours, day_events)
        if not day_slots:
            continue
        in_zone = day in zone_days
        candidate_days.append({
            'date': day,
            'slots': day_slots,
            'in_zone': in_zone,
            'zone_appointments': zone_days.get(day, []),
        })

    # Prioritise days that already have same-zone appointments.
    candidate_days.sort(key=lambda d: (not d['in_zone'], d['date']))

    slots = []
    for day in candidate_days:
        zone_note = _format_zone_note(day['zone_appointments']) if day['in_zone'] else None
        for slot_start in day['slots']:
            slots.append(_build_slot(slot_start, duration_hours,
                                     in_zone=day['in_zone'], zone_note=zone_note))
            if len(slots) >= num_slots:
                return slots
    return slots


# --- Daily reminder ---------------------------------------------------------

def get_day_appointments(day):
    """
    Return the list of real jobs (not full-day blocks) for a given day as
    dictionaries, sorted by start time. Used by the daily reminder.
    """
    if isinstance(day, datetime):
        day = day.date()
    appts = (Appointment.query
             .filter(Appointment.appointment_date == day)
             .filter(Appointment.status.in_(AppointmentStatus.ACTIVE))
             .filter(Appointment.source != AppointmentSource.BLOCKED_DAY)
             .all())
    result = []
    for a in appts:
        if a.is_full_day_block:
            continue
        d = a.to_dict()
        d['time'] = a.start_time.strftime('%H:%M') if a.start_time else '--:--'
        result.append(d)
    result.sort(key=lambda x: x['time'])
    return result


# --- Slot / appointment creation -------------------------------------------

def create_appointment_from_slot(conversation, slot):
    """
    Persist a confirmed appointment (from the WhatsApp flow) into the database.
    `slot` is a dict produced by find_available_slots / fallback generator.
    Returns the created Appointment.
    """
    start_dt = slot['datetime']
    duration = conversation.get('duration_hours') or DEFAULT_DURATION_HOURS
    end_dt = start_dt + timedelta(hours=duration)

    appt = Appointment(
        customer_name=conversation.get('customer_name'),
        customer_phone=conversation.get('customer_phone'),
        location=conversation.get('location'),
        work_type=conversation.get('work_type'),
        duration_hours=duration,
        appointment_date=start_dt.date(),
        start_time=start_dt.time(),
        end_time=end_dt.time(),
        status=AppointmentStatus.CONFIRMED,
        source=AppointmentSource.LEROY_MERLIN,
        wo_reference=conversation.get('reference') or None,
        notes=conversation.get('address') or None,
    )
    db.session.add(appt)
    db.session.commit()
    return appt
