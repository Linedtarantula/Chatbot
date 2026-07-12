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

from zones import get_zone_for_location

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

app = Flask(__name__)
CORS(app)

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# In-memory conversation state for the WhatsApp flow.
conversations = {}


class ConversationState:
    GREETING = 'greeting'
    PROPOSING_SLOTS = 'proposing_slots'
    WAITING_CHOICE = 'waiting_choice'
    CONFIRMING = 'confirming'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


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


# --- Availability logic (uses panel API) ------------------------------------

def _time_to_minutes(ts):
    h, m = map(int, ts.split(':'))
    return h * 60 + m


def _minutes_to_time(total_min):
    h, m = divmod(total_min, 60)
    return f"{h:02d}:{m:02d}"


def _overlaps_any(start_min, end_min, day_appts):
    """Check if [start_min, end_min) overlaps any existing appointment."""
    for appt in day_appts:
        appt_start_str = appt.get('timeStart', '')
        if not appt_start_str:
            continue
        appt_start = _time_to_minutes(appt_start_str)
        appt_end = appt_start + (appt.get('duration', 90) or 90)
        if start_min < appt_end and end_min > appt_start:
            return True
    return False


def _free_slots_for_day(day_date, duration_min, day_appts):
    """Return valid start datetimes for a given day (no overlaps)."""
    day_start = WORK_START_HOUR * 60
    day_end = WORK_END_HOUR * 60

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
        if not _overlaps_any(cursor, cursor + duration_min, day_appts):
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
                         days_ahead=14, preferred_zone=None):
    """Find available time slots using the panel API data."""
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

    # Active statuses
    active_statuses = {'pending', 'confirmed', 'en_progreso'}

    # Group appointments by date
    appts_by_day = {}
    for appt in all_appointments:
        if appt.get('status', 'pending') in active_statuses:
            appts_by_day.setdefault(appt['date'], []).append(appt)

    # Blocked days set
    blocked_set = {bd['date'] for bd in all_blocked}

    # Zone appointments per day
    zone_days = {}
    if preferred_zone:
        for appt in all_appointments:
            if appt.get('status', 'pending') in active_statuses:
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
        day_slots = _free_slots_for_day(day, duration_min, day_appts)
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

    # Prioritise same-zone days
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


def build_time_slots(duration_hours, location, num_slots=3):
    preferred_zone = get_zone_for_location(location) if location else None
    slots = find_available_slots(
        duration_hours=duration_hours, num_slots=num_slots,
        preferred_zone=preferred_zone)
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
    active_statuses = {'pending', 'confirmed', 'en_progreso'}
    active = [a for a in appointments if a.get('status', 'pending') in active_statuses]
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
        time_slots = build_time_slots(duration, location, num_slots=3)

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

        if message_sid:
            conversations[customer_phone]['state'] = ConversationState.PROPOSING_SLOTS
            return jsonify({
                'success': True,
                'conversation_id': conversation_id,
                'message': 'Appointment scheduling initiated',
                'message_sid': message_sid,
                'zone': get_zone_for_location(location) if location else None,
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


def _format_slots_message(conversation):
    slots = conversation['time_slots']
    lines = []
    zone_notes = []
    for i, slot in enumerate(slots):
        marker = ' ⭐' if slot.get('in_zone') else ''
        lines.append(f"{i + 1}. {slot['formatted']} (aprox. hasta las {slot['end_time']}){marker}")
        if slot.get('zone_note') and slot['zone_note'] not in zone_notes:
            zone_notes.append(slot['zone_note'])
    slots_text = '\n'.join(lines)
    zone_block = ''
    if zone_notes:
        zone_block = (
            '\n\nℹ️ Ese/esos días ya tengo desplazamiento a su zona, por lo que me '
            'vendría muy bien poder atenderle también a usted:\n- ' + '\n- '.join(zone_notes)
        )
    return (
        f"Le agradezco su disponibilidad. Estas son las fechas que tengo libres:\n\n"
        f"{slots_text}{zone_block}\n\n"
        f"¿Cuál le viene mejor? Puede responderme con el número de la opción (1, 2 o 3).\n\n"
        f"Si ninguna le encaja, indíquemelo y le busco otras fechas sin problema."
    )


@app.route('/webhook/whatsapp', methods=['POST'])
def whatsapp_webhook():
    try:
        incoming_msg = request.values.get('Body', '').strip().lower()
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

        if state == ConversationState.PROPOSING_SLOTS:
            response.message(_format_slots_message(conversation))
            conversation['state'] = ConversationState.WAITING_CHOICE

        elif state == ConversationState.WAITING_CHOICE:
            if incoming_msg in ['1', '2', '3']:
                idx = int(incoming_msg) - 1
                slots = conversation['time_slots']
                if idx >= len(slots):
                    response.message('Esa opción no está disponible. Por favor, indíqueme '
                                     'una de las opciones que le he propuesto.')
                    return str(response)
                selected_slot = slots[idx]
                conversation['selected_slot'] = selected_slot
                work_type = conversation['work_type'] or 'la instalación'
                address = conversation['address'] or 'la dirección indicada'
                location = conversation['location']
                duration = conversation['duration_hours']
                address_line = address
                if location and location.lower() not in address.lower():
                    address_line = f'{address} ({location})'
                response.message(
                    f'Perfecto. Le confirmo los datos de la cita:\n\n'
                    f"📅 {selected_slot['formatted']}\n"
                    f'⏱️ Duración aproximada: {duration} horas\n'
                    f'📍 Dirección: {address_line}\n'
                    f'🔧 Trabajo: {work_type}\n\n'
                    f'¿Es todo correcto? Responda SÍ para confirmar la cita o NO si '
                    f'necesita que cambiemos algo.')
                conversation['state'] = ConversationState.CONFIRMING
            elif 'no' in incoming_msg or 'ninguna' in incoming_msg:
                response.message(
                    'Entendido, no se preocupe. Voy a revisar la agenda para buscarle '
                    'otras fechas y me pondré en contacto con usted a la mayor brevedad. '
                    'Gracias por su paciencia.')
                conversation['state'] = ConversationState.CANCELLED
            else:
                response.message(
                    'Disculpe, no le he entendido. ¿Podría indicarme el número de la '
                    'opción que prefiere (1, 2 o 3)? Si ninguna le viene bien, dígamelo '
                    'y le busco otras fechas.')

        elif state == ConversationState.CONFIRMING:
            if any(w in incoming_msg for w in ['si', 'sí', 'vale', 'ok', 'correcto', 'confirmo']):
                selected_slot = conversation['selected_slot']
                try:
                    create_appointment_via_api(conversation, selected_slot)
                except Exception as e:
                    print(f'Error saving appointment: {e}')
                response.message(
                    f"Estupendo. Su cita ha quedado confirmada. ✅\n\n"
                    f"Le espero el {selected_slot['formatted']}.\n\n"
                    f'Recibirá un recordatorio el día antes. Si le surgiera cualquier '
                    f'imprevisto, le agradecería que me avisara con antelación.\n\n'
                    f'Muchas gracias por su tiempo. Un saludo.')
                conversation['state'] = ConversationState.COMPLETED
            else:
                response.message('Por supuesto. ¿Qué necesita que modifiquemos? Dígame y '
                                 'lo ajustamos enseguida.')

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
