"""
WhatsApp appointment system — Flask server.

Uses the NextJS panel REST API for appointment data instead of direct
database access (the hosted DB is only reachable from Abacus infra).

Endpoints
---------
WhatsApp flow:
  POST /initiate-appointment        Start the WhatsApp scheduling flow.
  POST /webhook/whatsapp            Twilio inbound webhook.
  POST /send-daily-reminder         Send tomorrow's summary to the installer.
  GET  /appointment-status/<id>     Conversation / appointment status.
  GET  /health                      Health check.
"""

import os
import time
import json
import requests as http_requests
from datetime import datetime, timedelta, time as dtime

from flask import Flask, request, jsonify
from flask_cors import CORS
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from zones import get_zone_for_location, get_travel_time

# --- Config -----------------------------------------------------------------
PANEL_API_URL = os.environ.get('PANEL_API_URL', 'https://agendainstalacionesventura.abacusai.app')

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+34613491811')
INSTALLER_PHONE_NUMBER = os.environ.get('INSTALLER_PHONE_NUMBER', '+34694288242')

# WhatsApp Template SIDs (approved by Meta)
TEMPLATE_GREETING = os.environ.get('TEMPLATE_GREETING', 'HX0f24fd9eaf3fd49fc64a5f8e2bcdb9ee')
TEMPLATE_NEW = os.environ.get('TEMPLATE_NEW', 'HXb9370496f85f7a99f9290f236b91358d')

# --- Working window / defaults -----------------------------------------------
WORK_START_HOUR = 8
WORK_END_HOUR = 16
DEFAULT_DURATION_HOURS = 1.5
DEFAULT_DURATION_MIN = 90
TRAVEL_BUFFER_MIN = 30  # 30 min de desplazamiento entre citas

app = Flask(__name__)
CORS(app)

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# In-memory conversation state for the WhatsApp flow.
conversations = {}

# --- Temporary slot reservations (anti-overlap) ----------------------------
# key: "YYYY-MM-DD_HH:MM" -> {phone, expires_at}
slot_reservations = {}
RESERVATION_MINUTES = 15


def _cleanup_expired_reservations():
    """Remove expired slot reservations."""
    now = datetime.now()
    expired = [k for k, v in slot_reservations.items() if v['expires_at'] <= now]
    for k in expired:
        del slot_reservations[k]


def _reserve_slots(slots, customer_phone):
    """Temporarily reserve slots (15 min) to prevent double-booking."""
    _cleanup_expired_reservations()
    expires_at = datetime.now() + timedelta(minutes=RESERVATION_MINUTES)
    for slot in slots:
        key = f"{slot['date']}_{slot['start_time']}"
        slot_reservations[key] = {'phone': customer_phone, 'expires_at': expires_at}
    print(f'Reserved {len(slots)} slots for {customer_phone} until {expires_at.strftime("%H:%M")}')


def _release_slots(customer_phone):
    """Release all slots reserved by a specific customer."""
    keys_to_remove = [k for k, v in slot_reservations.items() if v['phone'] == customer_phone]
    for k in keys_to_remove:
        del slot_reservations[k]
    if keys_to_remove:
        print(f'Released {len(keys_to_remove)} reserved slots for {customer_phone}')


def _is_slot_reserved(date_str, time_str, exclude_phone=None):
    """Check if a slot is reserved by someone else."""
    _cleanup_expired_reservations()
    key = f"{date_str}_{time_str}"
    reservation = slot_reservations.get(key)
    if not reservation:
        return False
    if exclude_phone and reservation['phone'] == exclude_phone:
        return False
    return True


class ConversationState:
    GREETING = 'greeting'
    PROPOSING_SLOTS = 'proposing_slots'
    WAITING_CHOICE = 'waiting_choice'
    CONFIRMING_ADDRESS = 'confirming_address'  # Confirm/correct address before final
    CONFIRMING = 'confirming'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'
    NEEDS_MANUAL = 'needs_manual'  # Client rejected dates, installer must intervene


def _notify_installer(message):
    """Send a notification to the installer's WhatsApp."""
    installer_number = INSTALLER_PHONE_NUMBER or TWILIO_WHATSAPP_NUMBER.replace('whatsapp:', '')
    if installer_number:
        send_whatsapp_message(installer_number, message)


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


# --- Panel API helpers -------------------------------------------------------

def _api_get(path, params=None):
    """GET from the NextJS panel API."""
    url = f"{PANEL_API_URL}{path}"
    try:
        r = http_requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'API GET {url} error: {e}')
        return None


def _api_post(path, data):
    """POST to the NextJS panel API."""
    url = f"{PANEL_API_URL}{path}"
    try:
        r = http_requests.post(url, json=data, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'API POST {url} error: {e}')
        return None


def _fetch_appointments(from_date, to_date):
    """Fetch appointments from the panel API."""
    data = _api_get('/api/appointments', {
        'from': from_date,
        'to': to_date,
    })
    return data if isinstance(data, list) else []


def _fetch_blocked_days(from_date, to_date):
    """Fetch blocked days from the panel API."""
    data = _api_get('/api/blocked-days', {
        'from': from_date,
        'to': to_date,
    })
    return data if isinstance(data, list) else []


# --- Twilio helpers ---------------------------------------------------------

def send_whatsapp_message(to_number, message):
    """Send a free-form WhatsApp message (only works within 24h window)."""
    try:
        if client is None:
            print('Twilio client not configured; message not sent.')
            return None
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'
        sent = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER, body=message, to=to_number)
        return sent.sid
    except Exception as e:
        print(f'Error sending message: {e}')
        return None


def send_whatsapp_template(to_number, template_sid, variables):
    """Send a template-based WhatsApp message (required for first contact)."""
    global _last_send_error
    _last_send_error = None
    try:
        if client is None:
            _last_send_error = 'Twilio client not configured (TWILIO_ACCOUNT_SID missing?)'
            print(_last_send_error)
            return None
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'
        # Remove non-Twilio keys from variables
        twilio_vars = {k: v for k, v in variables.items() if k != 'fallback_text'}
        print(f'Sending template {template_sid} to {to_number} from {TWILIO_WHATSAPP_NUMBER}')
        sent = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number,
            content_sid=template_sid,
            content_variables=json.dumps(twilio_vars),
        )
        print(f'Template sent OK: {sent.sid}')
        return sent.sid
    except Exception as e:
        _last_send_error = str(e)
        print(f'Error sending template: {e}')
        fallback_msg = variables.get('fallback_text', '')
        if fallback_msg:
            print('Attempting fallback with free-form message...')
            return send_whatsapp_message(to_number, fallback_msg)
        return None

_last_send_error = None


# --- Quick Reply Buttons (Twilio Content API) --------------------------------
# Cache for content template SIDs (created once, reused)
_content_sids = {}


def _ensure_content_templates():
    """Create or find reusable quick-reply templates for in-session messages."""
    global _content_sids
    if _content_sids:
        return _content_sids
    if not client:
        return {}

    templates_to_create = {
        'slots_3': {
            'friendly_name': 'ventura_slots_3opt_v2',
            'body': 'Seleccione una opción:',
            'actions': [
                {'title': 'Opción 1', 'id': '1'},
                {'title': 'Opción 2', 'id': '2'},
                {'title': 'Opción 3', 'id': '3'},
            ]
        },
        'slots_2': {
            'friendly_name': 'ventura_slots_2opt_v2',
            'body': 'Seleccione una opción:',
            'actions': [
                {'title': 'Opción 1', 'id': '1'},
                {'title': 'Opción 2', 'id': '2'},
            ]
        },
        'slots_1': {
            'friendly_name': 'ventura_slots_1opt_v2',
            'body': '¿Le viene bien esta fecha?',
            'actions': [
                {'title': 'Sí, me viene bien', 'id': '1'},
                {'title': 'Prefiero otra fecha', 'id': 'no'},
            ]
        },
        'confirm_yesno': {
            'friendly_name': 'ventura_confirm_yn_v2',
            'body': '¿Confirma la cita?',
            'actions': [
                {'title': 'Sí, confirmo', 'id': 'si'},
                {'title': 'Cambiar algo', 'id': 'no'},
            ]
        },
        'address_check': {
            'friendly_name': 'ventura_address_v2',
            'body': '¿Es correcta la dirección?',
            'actions': [
                {'title': 'Sí, es correcta', 'id': 'si'},
                {'title': 'No, es otra', 'id': 'no'},
            ]
        },
    }

    # List existing templates
    try:
        existing = client.content.v1.contents.list()
        existing_map = {c.friendly_name: c.sid for c in existing}
    except Exception as e:
        print(f'Could not list content templates: {e}')
        existing_map = {}

    # Auto-cleanup: delete old v1 templates that no longer match
    old_prefixes = ['ventura_slots_3opt_v1', 'ventura_slots_2opt_v1',
                    'ventura_slots_1opt_v1', 'ventura_confirm_yn_v1',
                    'ventura_address_v1']
    for old_name in old_prefixes:
        if old_name in existing_map:
            try:
                client.content.v1.contents(existing_map[old_name]).delete()
                print(f'Deleted old template: {old_name} ({existing_map[old_name]})')
                del existing_map[old_name]
            except Exception as e:
                print(f'Could not delete old template {old_name}: {e}')

    for key, tmpl in templates_to_create.items():
        if tmpl['friendly_name'] in existing_map:
            _content_sids[key] = existing_map[tmpl['friendly_name']]
            print(f'Content template "{key}" found: {_content_sids[key]}')
            continue
        try:
            content = client.content.v1.contents.create(
                friendly_name=tmpl['friendly_name'],
                language='es',
                types={
                    'twilio/quick-reply': {
                        'body': tmpl['body'],
                        'actions': tmpl['actions'],
                    }
                },
            )
            _content_sids[key] = content.sid
            print(f'Content template "{key}" created: {content.sid}')
        except Exception as e:
            print(f'Error creating content template "{key}": {e}')

    return _content_sids


def send_whatsapp_buttons(to_number, body_text, template_key):
    """Send a WhatsApp message. Tries quick-reply buttons first;
    if Content API is not available, sends plain text (which always works)."""
    # Try buttons via Content API (only if enabled on the Twilio account)
    sids = _ensure_content_templates()
    content_sid = sids.get(template_key)

    if content_sid:
        try:
            btn_to = to_number
            if not btn_to.startswith('whatsapp:'):
                btn_to = f'whatsapp:{btn_to}'
            sent = client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=btn_to,
                content_sid=content_sid,
                content_variables=json.dumps({'1': body_text}),
            )
            print(f'Buttons ({template_key}) sent OK: {sent.sid}')
            return sent.sid
        except Exception as e:
            print(f'Buttons failed ({template_key}), sending plain text: {e}')

    # Fallback: always works — plain text message
    return send_whatsapp_message(to_number, body_text)


# --- Availability logic (uses panel API) ------------------------------------

def _time_to_minutes(ts):
    h, m = map(int, ts.split(':'))
    return h * 60 + m


def _minutes_to_time(total_min):
    h, m = divmod(total_min, 60)
    return f"{h:02d}:{m:02d}"


def _overlaps_any(start_min, end_min, day_appts, new_zone=None):
    """Check if [start_min, end_min) overlaps any existing appointment.
    Uses zone-aware travel buffer: if the new appointment is in a different
    zone from an existing one, uses the real inter-zone travel time.
    Also checks temporary slot reservations."""
    for appt in day_appts:
        appt_start_str = appt.get('timeStart', '')
        if not appt_start_str:
            continue
        appt_start = _time_to_minutes(appt_start_str)
        appt_duration = appt.get('duration', 90) or 90

        # Calculate zone-aware travel buffer
        appt_zone = get_zone_for_location(appt.get('locality', ''))
        if new_zone and appt_zone:
            travel = get_travel_time(new_zone, appt_zone)
        else:
            travel = TRAVEL_BUFFER_MIN  # default 30 min

        # Buffer after existing appointment (travel TO new)
        appt_end_with_travel = appt_start + appt_duration + travel
        # Buffer before existing appointment (travel FROM new)
        new_end_with_travel = end_min + travel

        if start_min < appt_end_with_travel and new_end_with_travel > appt_start:
            return True
    return False


def _free_slots_for_day(day_date, duration_min, day_appts,
                        new_zone=None, exclude_phone=None):
    """Return valid start datetimes for a given day (no overlaps, no reservations)."""
    day_start = WORK_START_HOUR * 60
    day_end = WORK_END_HOUR * 60
    day_str = day_date.isoformat()

    now = datetime.now()
    if day_date == now.date():
        current_min = now.hour * 60 + now.minute
        if current_min % 30 != 0:
            current_min = current_min + (30 - current_min % 30)
        cursor = max(day_start, current_min)
    else:
        cursor = day_start

    free = []
    while cursor + duration_min <= day_end:
        slot_time = _minutes_to_time(cursor)
        # Check reservation by another client
        if _is_slot_reserved(day_str, slot_time, exclude_phone=exclude_phone):
            cursor += 30
            continue
        if not _overlaps_any(cursor, cursor + duration_min, day_appts, new_zone=new_zone):
            slot_dt = datetime.combine(day_date, dtime(cursor // 60, cursor % 60))
            free.append(slot_dt)
            cursor += duration_min
        else:
            cursor += 30
    return free


def _build_slot(slot_start, duration_min, in_zone=False, zone_note=None):
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


def find_available_slots(duration_hours=DEFAULT_DURATION_HOURS, num_slots=3,
                         days_ahead=14, preferred_zone=None,
                         exclude_phone=None):
    """Find available time slots using the panel API data.
    - Prioritises days that already have appointments in the same zone.
    - Uses zone-aware travel buffers.
    - Excludes slots reserved by other clients."""
    if not duration_hours or duration_hours <= 0:
        duration_hours = DEFAULT_DURATION_HOURS
    duration_min = int(duration_hours * 60)

    base_date = (datetime.now() + timedelta(days=1)).date()
    end_date = base_date + timedelta(days=days_ahead)

    start_str = base_date.isoformat()
    end_str = end_date.isoformat()

    # Fetch data from panel API
    all_appointments = _fetch_appointments(start_str, end_str)
    all_blocked = _fetch_blocked_days(start_str, end_str)

    # All statuses except 'cancelled' count as occupied
    cancelled_statuses = {'cancelled', 'cancelada'}

    # Group appointments by date (all non-cancelled)
    appts_by_day = {}
    for appt in all_appointments:
        if appt.get('status', 'pending') not in cancelled_statuses:
            appts_by_day.setdefault(appt['date'], []).append(appt)

    # Blocked days set
    blocked_set = {bd['date'] for bd in all_blocked}

    # Zone appointments per day — which days already have work in the same zone
    zone_days = {}
    if preferred_zone:
        for appt in all_appointments:
            if appt.get('status', 'pending') not in cancelled_statuses:
                if get_zone_for_location(appt.get('locality', '')) == preferred_zone:
                    zone_days.setdefault(appt['date'], []).append(appt)

    candidate_days = []
    for offset in range(days_ahead + 1):
        day = base_date + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        day_str = day.isoformat()
        if day_str in blocked_set:
            continue
        day_appts = appts_by_day.get(day_str, [])
        day_slots = _free_slots_for_day(day, duration_min, day_appts,
                                        new_zone=preferred_zone,
                                        exclude_phone=exclude_phone)
        if not day_slots:
            continue
        in_zone = day_str in zone_days
        candidate_days.append({
            'date': day,
            'date_str': day_str,
            'slots': day_slots,
            'in_zone': in_zone,
            'zone_appts': zone_days.get(day_str, []),
        })

    # Prioritise same-zone days (less travel for the installer)
    candidate_days.sort(key=lambda d: (not d['in_zone'], d['date']))

    slots = []
    for day in candidate_days:
        zone_note = None
        if day['in_zone'] and day['zone_appts']:
            parts = []
            for ev in day['zone_appts']:
                parts.append(f"{ev.get('timeStart', '--:--')} ({ev.get('locality', 'cita')})")
            zone_note = "Ya hay citas en la zona ese día: " + ", ".join(parts)

        for slot_start in day['slots']:
            slots.append(_build_slot(slot_start, duration_min,
                                     in_zone=day['in_zone'], zone_note=zone_note))
            if len(slots) >= num_slots:
                return slots
    return slots


def generate_time_slots(duration_hours=DEFAULT_DURATION_HOURS, num_slots=3):
    """Fallback: simple slots without checking the database."""
    slots = []
    slot_date = (datetime.now() + timedelta(days=1)).replace(
        hour=8, minute=0, second=0, microsecond=0)
    while len(slots) < num_slots:
        while slot_date.weekday() >= 5:
            slot_date += timedelta(days=1)
        end = slot_date + timedelta(hours=duration_hours)
        slots.append({
            'datetime': slot_date,
            'iso': slot_date.isoformat(),
            'date': slot_date.date().isoformat(),
            'start_time': slot_date.strftime('%H:%M'),
            'end_time': end.strftime('%H:%M'),
            'formatted': format_es(slot_date),
            'in_zone': False,
            'zone_note': None,
            'duration_min': int(duration_hours * 60),
        })
        slot_date += timedelta(days=1)
    return slots


def check_specific_slot(preferred_date, preferred_time, duration_hours=DEFAULT_DURATION_HOURS):
    """Check if a specific date/time slot is available.
    Returns the slot dict if available, or None with a reason if not."""
    duration_min = int(duration_hours * 60)
    try:
        day = datetime.strptime(preferred_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None, 'Fecha no válida'

    # Check if it's a weekend
    if day.weekday() >= 5:
        return None, f'El {format_date_es(day)} es fin de semana'

    # Check if day is in the past
    if day <= datetime.now().date():
        return None, 'La fecha debe ser posterior a hoy'

    # Parse the requested time
    if not preferred_time:
        preferred_time = f'{WORK_START_HOUR:02d}:00'
    start_min = _time_to_minutes(preferred_time)
    end_min = start_min + duration_min

    # Check working hours
    if start_min < WORK_START_HOUR * 60 or end_min > WORK_END_HOUR * 60:
        return None, f'El horario {preferred_time} está fuera del horario laboral ({WORK_START_HOUR:02d}:00 - {WORK_END_HOUR:02d}:00)'

    day_str = day.isoformat()

    # Check blocked day
    blocked = _fetch_blocked_days(day_str, day_str)
    if blocked:
        return None, f'El {format_date_es(day)} está bloqueado'

    # Check overlaps with existing appointments
    appointments = _fetch_appointments(day_str, day_str)
    cancelled_statuses = {'cancelled', 'cancelada'}
    active_appts = [a for a in appointments if a.get('status', 'pending') not in cancelled_statuses]

    if _overlaps_any(start_min, end_min, active_appts):
        return None, f'El hueco el {format_date_es(day)} a las {preferred_time} ya está ocupado'

    # Slot is available — build it
    slot_dt = datetime.combine(day, dtime(start_min // 60, start_min % 60))
    slot = _build_slot(slot_dt, duration_min)
    return slot, None


def build_time_slots(duration_hours, location, num_slots=3,
                     preferred_date=None, preferred_time=None,
                     exclude_phone=None):
    """Build time slots. If preferred_date is given, try that specific slot first."""
    # If a specific date/time is requested, check that first
    if preferred_date:
        slot, reason = check_specific_slot(preferred_date, preferred_time, duration_hours)
        if slot:
            return [slot]
        else:
            print(f'Requested slot {preferred_date} {preferred_time} not available: {reason}')

    preferred_zone = get_zone_for_location(location) if location else None
    slots = find_available_slots(
        duration_hours=duration_hours, num_slots=num_slots,
        preferred_zone=preferred_zone, exclude_phone=exclude_phone)
    if slots:
        return slots
    return generate_time_slots(duration_hours=duration_hours, num_slots=num_slots)


# --- Create appointment via panel API ----------------------------------------

def create_appointment_via_api(conversation, slot):
    """Save a confirmed appointment through the panel REST API."""
    duration_hours = conversation.get('duration_hours') or DEFAULT_DURATION_HOURS
    duration_min = int(duration_hours * 60)

    payload = {
        'date': slot['date'],
        'timeStart': slot['start_time'],
        'duration': duration_min,
        'client': conversation.get('customer_name') or 'Cliente',
        'phone': conversation.get('customer_phone') or '',
        'locality': conversation.get('location') or '',
        'workType': conversation.get('work_type') or '',
        'reference': conversation.get('reference') or '',
        'source': 'leroy',
        'status': 'pending',
        'notes': conversation.get('address') or '',
    }
    result = _api_post('/api/appointments', payload)
    if result:
        print(f"Appointment created via API: {result.get('id', 'unknown')}")
    else:
        print('Failed to create appointment via API')
    return result


# --- Get daily appointments via panel API ------------------------------------

def get_day_appointments(target_date):
    """Fetch appointments for a specific day via the panel API."""
    if isinstance(target_date, datetime):
        day_str = target_date.date().isoformat()
    else:
        day_str = target_date.isoformat()

    appointments = _fetch_appointments(day_str, day_str)
    cancelled_statuses = {'cancelled', 'cancelada'}
    active = [a for a in appointments if a.get('status', 'pending') not in cancelled_statuses]
    active.sort(key=lambda a: a.get('timeStart', '99:99'))

    # Enrich with zone info
    for a in active:
        a['zone'] = get_zone_for_location(a.get('locality', '')) or 'Sin zona'
        a['time'] = a.get('timeStart', '--:--')
    return active


# ===========================================================================
# WhatsApp flow endpoints
# ===========================================================================

@app.route('/initiate-appointment', methods=['POST'])
def initiate_appointment():
    try:
        data = request.json or {}
        customer_phone = data.get('customer_phone')
        if not customer_phone:
            return jsonify({'success': False, 'error': 'customer_phone is required'}), 400
        if not customer_phone.startswith('+'):
            customer_phone = f'+34{customer_phone}'

        duration = float(data.get('duration_hours') or DEFAULT_DURATION_HOURS)
        location = (data.get('location') or '').strip()
        preferred_date = (data.get('preferred_date') or '').strip() or None
        preferred_time = (data.get('preferred_time') or '').strip() or None
        time_slots = build_time_slots(duration, location, num_slots=3,
                                       preferred_date=preferred_date,
                                       preferred_time=preferred_time,
                                       exclude_phone=customer_phone)
        # Reserve these slots temporarily (15 min) to prevent double-booking
        _reserve_slots(time_slots, customer_phone)

        conversation_id = f"{customer_phone}_{int(time.time())}"
        conversations[customer_phone] = {
            'id': conversation_id,
            'customer_name': data.get('customer_name'),
            'customer_phone': customer_phone,
            'work_type': data.get('work_type'),
            'duration_hours': duration,
            'reference': data.get('reference', ''),
            'address': data.get('address', ''),
            'location': location,
            'state': ConversationState.GREETING,
            'time_slots': time_slots,
            'selected_slot': None,
            'created_at': datetime.now().isoformat(),
        }

        customer_name = data.get('customer_name', 'cliente')
        work_type = data.get('work_type', 'la instalación')
        message_type = data.get('message_type', 'greeting')

        if message_type == 'new':
            template_sid = TEMPLATE_NEW
        else:
            template_sid = TEMPLATE_GREETING

        fallback_text = (
            f"Buenos días, {customer_name}. Le escribo como instalador de Leroy Merlin "
            f"para concretar una fecha para {work_type}. "
            f"¿Le parece bien si le propongo algunas fechas disponibles?"
        )

        template_vars = {
            '1': customer_name,
            '2': work_type,
            'fallback_text': fallback_text,
        }
        message_sid = send_whatsapp_template(customer_phone, template_sid, template_vars)

        # Track if a specific slot was requested
        slot_info = {}
        if preferred_date:
            slot_info['preferred_date'] = preferred_date
            slot_info['preferred_time'] = preferred_time
            if len(time_slots) == 1:
                slot_info['specific_slot_found'] = True
            else:
                slot_info['specific_slot_found'] = False
                slot_info['fallback_reason'] = 'Slot not available, showing alternatives'

        if message_sid:
            conversations[customer_phone]['state'] = ConversationState.PROPOSING_SLOTS
            # Init message log
            conversations[customer_phone]['message_log'] = [{
                'from': 'bot',
                'text': f'Plantilla de saludo enviada ({message_type})',
                'time': datetime.now().strftime('%H:%M'),
            }]
            return jsonify({
                'success': True,
                'conversation_id': conversation_id,
                'message': 'Appointment scheduling initiated',
                'message_sid': message_sid,
                'zone': get_zone_for_location(location) if location else None,
                'slot_info': slot_info,
                'slots_proposed': len(time_slots),
            })
        return jsonify({
            'success': False,
            'error': 'Failed to send WhatsApp message',
            'detail': _last_send_error,
            'debug': {
                'twilio_configured': client is not None,
                'from_number': TWILIO_WHATSAPP_NUMBER,
                'to_number': customer_phone,
                'template_sid': template_sid,
            }
        }), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _build_full_address(conversation):
    """Build a full address string from address + location fields."""
    address = (conversation.get('address') or '').strip()
    location = (conversation.get('location') or '').strip()
    if address and location:
        if location.lower() in address.lower():
            return address
        return f'{address}, {location}'
    return address or location or ''


def _format_slots_message(conversation):
    slots = conversation['time_slots']
    lines = []
    for i, slot in enumerate(slots):
        lines.append(f"*{i + 1}.* {slot['formatted']} (aprox. hasta las {slot['end_time']})")
    slots_text = '\n'.join(lines)

    if len(slots) == 1:
        return (
            f"Le propongo la siguiente fecha:\n\n"
            f"{slots_text}\n\n"
            f"Responda *1* si le viene bien o *no* si prefiere otra fecha."
        )
    else:
        n = len(slots)
        opts = ', '.join(str(i+1) for i in range(n))
        return (
            f"Estas son las fechas que tengo disponibles:\n\n"
            f"{slots_text}\n\n"
            f"Responda con el número de la opción ({opts}).\n"
            f"Si ninguna le encaja, escriba *no*."
        )


def _get_slots_button_key(num_slots):
    """Return the content template key for the number of slot options."""
    if num_slots == 1:
        return 'slots_1'
    elif num_slots == 2:
        return 'slots_2'
    else:
        return 'slots_3'


def _send_and_log(conversation, to_number, text, button_key=None):
    """Send a message (with buttons if possible) and log it."""
    if button_key:
        sid = send_whatsapp_buttons(to_number, text, button_key)
    else:
        sid = send_whatsapp_message(to_number, text)
    if 'message_log' not in conversation:
        conversation['message_log'] = []
    conversation['message_log'].append({
        'from': 'bot', 'text': text, 'time': datetime.now().strftime('%H:%M')
    })
    return sid


@app.route('/webhook/whatsapp', methods=['POST'])
def whatsapp_webhook():
    try:
        incoming_msg = request.values.get('Body', '').strip().lower()
        button_payload = request.values.get('ButtonPayload', '').strip()
        # Use button payload if available (more reliable than free text)
        user_choice = button_payload or incoming_msg

        from_number = request.values.get('From', '')
        customer_phone = from_number.replace('whatsapp:', '')
        response = MessagingResponse()

        if customer_phone not in conversations:
            response.message(
                'Disculpe, no tengo ninguna cita pendiente de coordinar con usted en este '
                'momento. Si necesita ayuda, puede ponerse en contacto con su tienda '
                'Leroy Merlin. Muchas gracias.')
            return str(response)

        conversation = conversations[customer_phone]
        state = conversation['state']

        # Log incoming message
        if 'message_log' not in conversation:
            conversation['message_log'] = []
        conversation['message_log'].append({
            'from': 'cliente',
            'text': request.values.get('Body', '').strip(),
            'time': datetime.now().strftime('%H:%M'),
        })

        if state == ConversationState.NEEDS_MANUAL:
            _notify_installer(
                f"💬 *Mensaje de cliente en espera*\n\n"
                f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                f"📞 {conversation.get('customer_phone', '')}\n"
                f"💬 \"{request.values.get('Body', '').strip()}\"\n\n"
                f"Este cliente está esperando que le propongas otras fechas."
            )
            response.message(
                'Gracias por su mensaje. El instalador está revisando su disponibilidad '
                'y se pondrá en contacto con usted en breve.')
            return str(response)

        # --- PROPOSING SLOTS (first response from client after template) ---
        if state == ConversationState.PROPOSING_SLOTS:
            slots = conversation['time_slots']
            slots_msg = _format_slots_message(conversation)
            btn_key = _get_slots_button_key(len(slots))
            _send_and_log(conversation, customer_phone, slots_msg, button_key=btn_key)
            conversation['state'] = ConversationState.WAITING_CHOICE
            # Return empty TwiML since we sent via REST API
            return str(MessagingResponse())

        # --- WAITING FOR SLOT CHOICE ---
        elif state == ConversationState.WAITING_CHOICE:
            slots = conversation['time_slots']
            num_options = len(slots)
            valid_options = [str(i) for i in range(1, num_options + 1)]

            # Accept button payload, number, or affirmative text
            is_affirmative = num_options == 1 and any(
                w in user_choice for w in ['si', 'sí', 'vale', 'ok', 'bien', 'perfecto', 'correcto']
            )
            is_rejection = user_choice == 'no' or any(
                w in incoming_msg for w in ['no', 'ninguna', 'otra']
            )

            if user_choice in valid_options or is_affirmative:
                idx = 0 if is_affirmative else int(user_choice) - 1
                if idx >= len(slots):
                    response.message('Esa opción no está disponible.')
                    return str(response)

                selected_slot = slots[idx]
                conversation['selected_slot'] = selected_slot
                # Release un-selected reserved slots
                _release_slots(customer_phone)
                # Re-reserve only the selected slot
                _reserve_slots([selected_slot], customer_phone)

                # --- Ask to confirm/correct the address ---
                address_line = _build_full_address(conversation)

                if address_line:
                    addr_msg = (
                        f"Perfecto, {selected_slot['formatted']}.\n\n"
                        f"La dirección que tengo para la instalación es:\n"
                        f"📍 {address_line}\n\n"
                        f"\nResponda *sí* o *no*."
                    )
                    _send_and_log(conversation, customer_phone, addr_msg, button_key='address_check')
                    conversation['state'] = ConversationState.CONFIRMING_ADDRESS
                else:
                    # No address on file, ask for it
                    addr_msg = (
                        f"Perfecto, {selected_slot['formatted']}.\n\n"
                        f"¿Podría indicarme la dirección exacta donde se realizará "
                        f"la instalación?"
                    )
                    _send_and_log(conversation, customer_phone, addr_msg)
                    conversation['state'] = ConversationState.CONFIRMING_ADDRESS
                return str(MessagingResponse())

            elif is_rejection:
                response.message(
                    'Entendido, no se preocupe. Le paso su solicitud al instalador '
                    'para que le proponga otras fechas. Se pondrá en contacto con usted '
                    'a la mayor brevedad. Gracias por su paciencia.')
                conversation['state'] = ConversationState.NEEDS_MANUAL
                _release_slots(customer_phone)
                _notify_installer(
                    f"⚠️ *Cliente necesita otras fechas*\n\n"
                    f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                    f"📞 {conversation.get('customer_phone', '')}\n"
                    f"🔧 {conversation.get('work_type', '')}\n"
                    f"📍 {_build_full_address(conversation)}\n"
                    f"💬 El cliente ha respondido: \"{request.values.get('Body', '').strip()}\"\n\n"
                    f"Las fechas propuestas no le venían bien. "
                    f"Revisa la agenda y contacta al cliente directamente."
                )
            else:
                # Count misunderstandings — after 2 unclear messages, escalate
                misses = conversation.get('_misunderstand_count', 0) + 1
                conversation['_misunderstand_count'] = misses

                if misses >= 2:
                    # Escalate to installer
                    raw_msg = request.values.get('Body', '').strip()
                    response.message(
                        'Disculpe, no consigo entender su respuesta. '
                        'Le paso con el instalador para que le atienda directamente.')
                    _notify_installer(
                        f"⚠️ *Bot no entiende al cliente*\n\n"
                        f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                        f"📞 {conversation.get('customer_phone', '')}\n"
                        f"🔧 {conversation.get('work_type', '')}\n"
                        f"📍 {_build_full_address(conversation)}\n"
                        f"💬 \"{raw_msg}\"\n\n"
                        f"El bot no ha podido entender al cliente después de varios intentos. "
                        f"Aténdelo manualmente."
                    )
                    conversation['state'] = ConversationState.NEEDS_MANUAL
                else:
                    slots_msg = _format_slots_message(conversation)
                    hint = (
                        f"Disculpe, no le he entendido.\n\n{slots_msg}\n\n"
                        f"Pulse un botón o escriba el número de la opción."
                    )
                    _send_and_log(conversation, customer_phone, hint,
                                  button_key=_get_slots_button_key(len(slots)))
                    return str(MessagingResponse())

        # --- CONFIRMING ADDRESS ---
        elif state == ConversationState.CONFIRMING_ADDRESS:
            is_address_ok = user_choice in ['si'] or any(
                w in user_choice for w in ['si', 'sí', 'vale', 'ok', 'correcta', 'correcto', 'bien']
            )

            if is_address_ok:
                # Address confirmed, show full summary
                pass  # fall through to send confirmation below
            else:
                # Client provided a new/corrected address
                new_address = request.values.get('Body', '').strip()
                if new_address and user_choice != 'no':
                    conversation['address'] = new_address
                    addr_confirm = (
                        f"Gracias, he actualizado la dirección a:\n"
                        f"📍 {new_address}\n\n"
                        f"\nResponda *sí* si es correcta o escríbame la dirección correcta."
                    )
                    _send_and_log(conversation, customer_phone, addr_confirm, button_key='address_check')
                    return str(MessagingResponse())
                elif user_choice == 'no':
                    ask_msg = '¿Cuál es la dirección correcta? Escríbamela, por favor.'
                    _send_and_log(conversation, customer_phone, ask_msg)
                    return str(MessagingResponse())

            # Show full confirmation summary
            selected_slot = conversation['selected_slot']
            work_type = conversation.get('work_type') or 'la instalación'
            duration = conversation.get('duration_hours') or DEFAULT_DURATION_HOURS
            address_line = _build_full_address(conversation) or 'la dirección indicada'

            confirm_msg = (
                f'Perfecto. Le confirmo los datos de la cita:\n\n'
                f"📅 {selected_slot['formatted']}\n"
                f'⏱️ Duración aproximada: {duration} horas\n'
                f'📍 Dirección: {address_line}\n'
                f'🔧 Trabajo: {work_type}\n\n'
                f'\nResponda *sí* para confirmar o *no* si necesita cambiar algo.'
            )
            _send_and_log(conversation, customer_phone, confirm_msg, button_key='confirm_yesno')
            conversation['state'] = ConversationState.CONFIRMING
            return str(MessagingResponse())

        # --- FINAL CONFIRMATION ---
        elif state == ConversationState.CONFIRMING:
            is_confirmed = user_choice == 'si' or any(
                w in user_choice for w in ['si', 'sí', 'vale', 'ok', 'correcto', 'confirmo']
            )
            is_rejection = user_choice == 'no'
            if is_confirmed:
                selected_slot = conversation['selected_slot']
                try:
                    create_appointment_via_api(conversation, selected_slot)
                except Exception as e:
                    print(f'Error saving appointment: {e}')
                # Release reservations
                _release_slots(customer_phone)
                full_addr = _build_full_address(conversation)
                addr_line = f'\n📍 {full_addr}' if full_addr else ''
                response.message(
                    f"Estupendo. Su cita ha quedado confirmada. ✅\n\n"
                    f"📅 {selected_slot['formatted']}{addr_line}\n\n"
                    f'Si le surgiera cualquier imprevisto, le agradecería que me '
                    f'avisara con antelación.\n\n'
                    f'Muchas gracias por su tiempo. Un saludo.')
                conversation['state'] = ConversationState.COMPLETED
                work_type = conversation.get('work_type') or 'Instalación'
                _notify_installer(
                    f"✅ *Cita confirmada*\n\n"
                    f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                    f"📅 {selected_slot['formatted']}\n"
                    f"📍 {_build_full_address(conversation)}\n"
                    f"🔧 {work_type}\n"
                    f"📞 {conversation.get('customer_phone', '')}\n\n"
                    f"La cita se ha guardado en tu agenda automáticamente."
                )
            elif is_rejection:
                response.message(
                    'Por supuesto. ¿Qué necesita que modifiquemos? Dígame y '
                    'lo ajustamos enseguida.')
                # Notify installer that client wants changes
                _notify_installer(
                    f"💬 *Cliente quiere cambiar algo de la cita*\n\n"
                    f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                    f"📞 {conversation.get('customer_phone', '')}\n"
                    f"🔧 {conversation.get('work_type', '')}\n"
                    f"📍 {_build_full_address(conversation)}\n\n"
                    f"El cliente no ha confirmado y quiere hacer cambios."
                )
                conversation['state'] = ConversationState.NEEDS_MANUAL
            else:
                # Client said something unexpected (imprevisto, pregunta, etc.)
                raw_msg = request.values.get('Body', '').strip()
                response.message(
                    'Gracias por avisarme. Le paso su mensaje al instalador '
                    'para que lo gestione lo antes posible.')
                _notify_installer(
                    f"💬 *Mensaje del cliente durante confirmación*\n\n"
                    f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                    f"📞 {conversation.get('customer_phone', '')}\n"
                    f"🔧 {conversation.get('work_type', '')}\n"
                    f"📍 {_build_full_address(conversation)}\n"
                    f"💬 \"{raw_msg}\"\n\n"
                    f"El cliente ha escrito esto en la fase de confirmación. "
                    f"Puede ser un imprevisto o una duda. Revísalo."
                )
                conversation['state'] = ConversationState.NEEDS_MANUAL

        # --- COMPLETED: client writes after confirmation ---
        elif state == ConversationState.COMPLETED:
            raw_msg = request.values.get('Body', '').strip()
            response.message(
                'Gracias por su mensaje. Se lo paso al instalador '
                'para que le atienda lo antes posible.')
            _notify_installer(
                f"💬 *Mensaje de cliente con cita confirmada*\n\n"
                f"👤 {conversation.get('customer_name', 'Cliente')}\n"
                f"📞 {conversation.get('customer_phone', '')}\n"
                f"💬 \"{raw_msg}\"\n\n"
                f"Este cliente ya tiene cita confirmada pero ha escrito de nuevo. "
                f"Puede ser una cancelación, imprevisto o consulta."
            )

        return str(response)
    except Exception as e:
        print(f'Error in webhook: {e}')
        response = MessagingResponse()
        response.message('Disculpe las molestias, ha ocurrido una incidencia. Me pondré '
                         'en contacto con usted en breve.')
        return str(response)


@app.route('/appointment-status/<conversation_id>', methods=['GET'])
def get_appointment_status(conversation_id):
    for phone, conv in conversations.items():
        if conv['id'] == conversation_id:
            return jsonify({'success': True, 'state': conv['state']})
    return jsonify({'success': False, 'error': 'Conversation not found'}), 404


@app.route('/manual-reply', methods=['POST'])
def manual_reply():
    """Installer sends a custom message or new dates to a client.
    Used via the chatbot when the installer wants to intervene."""
    try:
        data = request.json or {}
        customer_phone = data.get('customer_phone')
        message = data.get('message', '').strip()
        new_slots = data.get('new_slots')  # Optional: list of {date, time} dicts

        if not customer_phone:
            return jsonify({'success': False, 'error': 'customer_phone is required'}), 400
        if not customer_phone.startswith('+'):
            customer_phone = f'+34{customer_phone}'

        conversation = conversations.get(customer_phone)

        # If new_slots provided, format them and send as options
        if new_slots and conversation:
            from datetime import datetime as dt
            slots = []
            for i, s in enumerate(new_slots):
                slot_date = dt.strptime(s['date'], '%Y-%m-%d')
                slot_time = s.get('time', '08:00')
                duration_min = int(conversation.get('duration_hours', 1.5) * 60)
                end_h, end_m = divmod(_time_to_minutes(slot_time) + duration_min, 60)
                slot_formatted = f"{format_es(slot_date.replace(hour=int(slot_time.split(':')[0]), minute=int(slot_time.split(':')[1])))} (aprox. hasta las {end_h:02d}:{end_m:02d})"
                slots.append({
                    'date': s['date'],
                    'start_time': slot_time,
                    'end_time': f"{end_h:02d}:{end_m:02d}",
                    'formatted': format_es(slot_date.replace(hour=int(slot_time.split(':')[0]), minute=int(slot_time.split(':')[1]))),
                    'duration_min': duration_min,
                })
            conversation['time_slots'] = slots
            conversation['state'] = ConversationState.WAITING_CHOICE

            lines = []
            for i, slot in enumerate(slots):
                lines.append(f"{i + 1}. {slot['formatted']} (aprox. hasta las {slot['end_time']})")
            slots_text = '\n'.join(lines)
            msg = (
                f"Disculpe la espera. He revisado mi agenda y le puedo ofrecer estas otras fechas:\n\n"
                f"{slots_text}\n\n"
                f"¿Cuál le viene mejor? Responda con el número de la opción."
            )
            message_sid = send_whatsapp_message(customer_phone, msg)
            if conversation.get('message_log') is not None:
                conversation['message_log'].append({'from': 'bot', 'text': msg, 'time': datetime.now().strftime('%H:%M')})
            return jsonify({'success': True, 'message_sid': message_sid, 'action': 'new_slots_sent'})

        # Otherwise send a free-form message
        if not message:
            return jsonify({'success': False, 'error': 'message or new_slots is required'}), 400

        message_sid = send_whatsapp_message(customer_phone, message)
        if conversation and conversation.get('message_log') is not None:
            conversation['message_log'].append({'from': 'instalador', 'text': message, 'time': datetime.now().strftime('%H:%M')})

        if message_sid:
            return jsonify({'success': True, 'message_sid': message_sid, 'action': 'message_sent'})
        return jsonify({'success': False, 'error': 'Failed to send message'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/conversations', methods=['GET'])
def list_conversations():
    """List all active conversations for the installer to monitor."""
    result = []
    for phone, conv in conversations.items():
        result.append({
            'conversation_id': conv.get('id'),
            'customer_name': conv.get('customer_name'),
            'customer_phone': conv.get('customer_phone'),
            'work_type': conv.get('work_type'),
            'location': conv.get('location'),
            'state': conv.get('state'),
            'state_label': {
                'greeting': 'Saludo enviado',
                'proposing_slots': 'Proponiendo fechas',
                'waiting_choice': 'Esperando respuesta del cliente',
                'confirming_address': 'Confirmando dirección',
                'confirming': 'Esperando confirmación',
                'completed': '✅ Cita confirmada',
                'cancelled': '❌ Cancelada',
                'needs_manual': '⚠️ Necesita intervención manual',
            }.get(conv.get('state'), conv.get('state')),
            'selected_slot': conv.get('selected_slot', {}).get('formatted') if conv.get('selected_slot') else None,
            'created_at': conv.get('created_at'),
            'messages': conv.get('message_log', []),
        })
    # Sort: needs_manual first, then by created_at desc
    result.sort(key=lambda c: (
        0 if c['state'] == 'needs_manual' else
        1 if c['state'] in ('waiting_choice', 'confirming_address', 'confirming', 'proposing_slots', 'greeting') else 2,
        c.get('created_at', '') or ''
    ), reverse=False)
    return jsonify(result)


# ===========================================================================
# Daily reminder
# ===========================================================================

def _build_daily_reminder_message(target_date, appointments):
    header = f"📋 *Resumen de trabajos para mañana*\n🗓️ {format_date_es(target_date)}"
    if not appointments:
        return (f"{header}\n\n✅ No hay trabajos programados para mañana.\n"
                f'¡Disfrute del día!')
    zones = {}
    for appt in appointments:
        zones.setdefault(appt.get('zone') or 'Sin zona', []).append(appt)
    lines = [header, f"Total de citas: *{len(appointments)}*"]
    for zone in sorted(zones.keys()):
        zone_appts = sorted(zones[zone], key=lambda a: a.get('time', ''))
        lines.append('')
        lines.append(f"📍 *{zone}* ({len(zone_appts)})")
        lines.append('──────────────')
        for appt in zone_appts:
            ref = appt.get('reference') or 'N/D'
            name = appt.get('client') or 'Cliente'
            address = appt.get('notes') or appt.get('locality') or 'N/D'
            phone = appt.get('phone') or 'N/D'
            work = appt.get('workType') or 'N/D'
            lines.append(f"🕐 {appt.get('time', '--:--')}  |  WO/Ref: {ref}")
            lines.append(f'👤 {name}')
            lines.append(f'🏠 {address}')
            lines.append(f'📞 {phone}')
            lines.append(f'🔧 {work}')
            lines.append('')
    lines.append('Buen trabajo mañana. 💪')
    return '\n'.join(lines).strip()


@app.route('/send-daily-reminder', methods=['POST'])
def send_daily_reminder():
    try:
        installer_number = INSTALLER_PHONE_NUMBER or TWILIO_WHATSAPP_NUMBER.replace('whatsapp:', '')
        if not installer_number:
            return jsonify({'success': False,
                            'error': 'INSTALLER_PHONE_NUMBER is not configured'}), 400
        data = request.get_json(silent=True) or {}
        if data.get('date'):
            try:
                target_date = datetime.strptime(data['date'], '%Y-%m-%d')
            except ValueError:
                return jsonify({'success': False,
                                'error': "Invalid 'date' format, expected YYYY-MM-DD"}), 400
        else:
            target_date = datetime.now() + timedelta(days=1)
        target_date = target_date.replace(hour=0, minute=0, second=0, microsecond=0)

        appointments = get_day_appointments(target_date)
        message = _build_daily_reminder_message(target_date, appointments)
        message_sid = send_whatsapp_message(installer_number, message)

        if message_sid:
            return jsonify({
                'success': True,
                'date': target_date.strftime('%Y-%m-%d'),
                'appointments_count': len(appointments),
                'message_sid': message_sid,
            })
        return jsonify({'success': False, 'error': 'Failed to send WhatsApp reminder'}), 500
    except Exception as e:
        print(f'Error in send_daily_reminder: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ===========================================================================
# Check slot availability
# ===========================================================================

@app.route('/check-availability', methods=['POST'])
def check_availability():
    """Check if a specific date/time slot is available.
    Useful for the chatbot to verify before initiating."""
    try:
        data = request.json or {}
        preferred_date = data.get('date') or data.get('preferred_date')
        preferred_time = data.get('time') or data.get('preferred_time')
        duration_hours = float(data.get('duration_hours') or DEFAULT_DURATION_HOURS)

        if not preferred_date:
            return jsonify({'success': False, 'error': 'date is required'}), 400

        slot, reason = check_specific_slot(preferred_date, preferred_time, duration_hours)
        if slot:
            return jsonify({
                'success': True,
                'available': True,
                'slot': {
                    'date': slot['date'],
                    'start_time': slot['start_time'],
                    'end_time': slot['end_time'],
                    'formatted': slot['formatted'],
                }
            })
        else:
            return jsonify({
                'success': True,
                'available': False,
                'reason': reason,
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ===========================================================================
# Health check
# ===========================================================================

@app.route('/health', methods=['GET'])
def health_check():
    # Test panel API connectivity
    api_ok = False
    try:
        r = http_requests.get(f"{PANEL_API_URL}/api/stats", timeout=10)
        api_ok = r.status_code == 200
    except Exception:
        pass
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'panel_api': PANEL_API_URL,
        'panel_api_ok': api_ok,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)