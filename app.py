"""
WhatsApp appointment system — Flask server backed by a local SQLite database.

Google Calendar has been fully replaced by an own SQLite database (SQLAlchemy),
giving the installer total control over their agenda. The database stores every
kind of entry: Leroy Merlin jobs, personal jobs and full-day blocks.

Endpoints
---------
Legacy (kept for compatibility):
  POST /initiate-appointment        Start the WhatsApp scheduling flow.
  POST /webhook/whatsapp            Twilio inbound webhook.
  POST /send-daily-reminder         Send tomorrow's summary to the installer.
  GET  /appointment-status/<id>     Conversation / appointment status.
  GET  /health                      Health check.

REST API (JSON, CORS-enabled, optional token):
  GET    /api/appointments          List appointments (filter: date, zone, source, status).
  POST   /api/appointments          Create a manual appointment.
  PUT    /api/appointments/<id>     Update an appointment.
  DELETE /api/appointments/<id>     Delete an appointment.
  POST   /api/block-day             Block a full day (source=blocked_day).
  GET    /api/availability          Available slots (used by the bot).
  GET    /api/appointments/tomorrow Tomorrow's appointments (for the reminder).
"""

import os
import time
import functools
from datetime import datetime, timedelta, time as dtime

from flask import Flask, request, jsonify
from flask_cors import CORS
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from models import db, Appointment, AppointmentStatus, AppointmentSource
from zones import get_zone_for_location
import db_service
from db_service import (
    DEFAULT_DURATION_HOURS,
    format_es,
    format_date_es,
)

# --- Paths / config ---------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'appointments.db')
# Allow overriding via DATABASE_URL (e.g. Railway). Default: local SQLite file.
DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{DB_PATH}')

# Twilio configuration
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
INSTALLER_PHONE_NUMBER = os.environ.get('INSTALLER_PHONE_NUMBER', '')

# Optional simple token protecting the /api/* endpoints. If unset, API is open.
API_TOKEN = os.environ.get('API_TOKEN', '')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
CORS(app)  # enable CORS for the web admin panel

# Twilio client (created lazily-safe: only fails if actually used without creds).
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# In-memory conversation state for the WhatsApp flow (per phone number).
conversations = {}


def init_database():
    """Create tables if they do not exist yet."""
    with app.app_context():
        db.create_all()


init_database()


class ConversationState:
    GREETING = 'greeting'
    PROPOSING_SLOTS = 'proposing_slots'
    WAITING_CHOICE = 'waiting_choice'
    CONFIRMING = 'confirming'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


# --- Auth -------------------------------------------------------------------

def require_token(f):
    """Protect an endpoint with a simple token if API_TOKEN is configured."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if API_TOKEN:
            provided = (request.headers.get('X-API-Token')
                        or request.args.get('token', ''))
            if provided != API_TOKEN:
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


# --- Twilio helper ----------------------------------------------------------

def send_whatsapp_message(to_number, message):
    """Send a WhatsApp message via Twilio."""
    try:
        if client is None:
            print("Twilio client not configured; message not sent.")
            return None
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'
        sent = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER, body=message, to=to_number)
        return sent.sid
    except Exception as e:
        print(f"Error sending message: {e}")
        return None


# --- Fallback slot generation (rarely used; DB is normally available) -------

def generate_time_slots(duration_hours=DEFAULT_DURATION_HOURS, num_slots=3):
    """Simple weekday morning slots, used only if the DB query returns nothing."""
    slots = []
    slot_date = (datetime.now() + timedelta(days=1)).replace(
        hour=8, minute=0, second=0, microsecond=0)
    while len(slots) < num_slots:
        while slot_date.weekday() >= 5:
            slot_date += timedelta(days=1)
        slot_time = slot_date.replace(hour=8, minute=0, second=0, microsecond=0)
        end = slot_time + timedelta(hours=duration_hours)
        slots.append({
            'datetime': slot_time,
            'iso': slot_time.isoformat(),
            'date': slot_time.date().isoformat(),
            'start_time': slot_time.strftime('%H:%M'),
            'end_time': end.strftime('%H:%M'),
            'formatted': format_es(slot_time),
            'in_zone': False,
            'zone_note': None,
        })
        slot_date += timedelta(days=1)
    return slots


def build_time_slots(duration_hours, location, num_slots=3):
    """Get available slots from the DB (overlap-aware + zone grouping)."""
    preferred_zone = get_zone_for_location(location) if location else None
    slots = db_service.find_available_slots(
        duration_hours=duration_hours, num_slots=num_slots,
        preferred_zone=preferred_zone)
    if slots:
        return slots
    return generate_time_slots(duration_hours=duration_hours, num_slots=num_slots)


# ===========================================================================
# Legacy WhatsApp flow endpoints
# ===========================================================================

@app.route('/initiate-appointment', methods=['POST'])
def initiate_appointment():
    """Start appointment scheduling via WhatsApp (called by the Abacus.AI bot)."""
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
            'installer_id': data.get('installer_id'),
            'state': ConversationState.GREETING,
            'time_slots': time_slots,
            'selected_slot': None,
            'created_at': datetime.now().isoformat(),
        }

        customer_name = data.get('customer_name', 'cliente')
        work_type = data.get('work_type', 'la instalación')
        greeting_message = (
            f"Buenos días, {customer_name}.\n\n"
            f"Le escribo como instalador de Leroy Merlin. Me pongo en contacto con usted "
            f"porque, debido al alto volumen de trabajo que tenemos estos días, necesitamos "
            f"reorganizar la agenda y concretar una nueva fecha para {work_type}.\n\n"
            f"¿Le parece bien si le propongo algunas fechas disponibles?"
        )
        message_sid = send_whatsapp_message(customer_phone, greeting_message)

        if message_sid:
            conversations[customer_phone]['state'] = ConversationState.PROPOSING_SLOTS
            return jsonify({
                'success': True,
                'conversation_id': conversation_id,
                'message': 'Appointment scheduling initiated',
                'message_sid': message_sid,
                'zone': get_zone_for_location(location) if location else None,
            })
        return jsonify({'success': False, 'error': 'Failed to send WhatsApp message'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _format_slots_message(conversation):
    slots = conversation['time_slots']
    lines = []
    zone_notes = []
    for i, slot in enumerate(slots):
        marker = " ⭐" if slot.get('in_zone') else ""
        lines.append(f"{i + 1}. {slot['formatted']} (aprox. hasta las {slot['end_time']}){marker}")
        if slot.get('zone_note') and slot['zone_note'] not in zone_notes:
            zone_notes.append(slot['zone_note'])
    slots_text = "\n".join(lines)
    zone_block = ""
    if zone_notes:
        zone_block = (
            "\n\nℹ️ Ese/esos días ya tengo desplazamiento a su zona, por lo que me "
            "vendría muy bien poder atenderle también a usted:\n- " + "\n- ".join(zone_notes)
        )
    return (
        f"Le agradezco su disponibilidad. Estas son las fechas que tengo libres:\n\n"
        f"{slots_text}{zone_block}\n\n"
        f"¿Cuál le viene mejor? Puede responderme con el número de la opción (1, 2 o 3).\n\n"
        f"Si ninguna le encaja, indíquemelo y le busco otras fechas sin problema."
    )


@app.route('/webhook/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages from Twilio."""
    try:
        incoming_msg = request.values.get('Body', '').strip().lower()
        from_number = request.values.get('From', '')
        customer_phone = from_number.replace('whatsapp:', '')
        response = MessagingResponse()

        if customer_phone not in conversations:
            response.message(
                "Disculpe, no tengo ninguna cita pendiente de coordinar con usted en este "
                "momento. Si necesita ayuda, puede ponerse en contacto con su tienda "
                "Leroy Merlin. Muchas gracias.")
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
                    response.message("Esa opción no está disponible. Por favor, indíqueme "
                                     "una de las opciones que le he propuesto.")
                    return str(response)
                selected_slot = slots[idx]
                conversation['selected_slot'] = selected_slot
                work_type = conversation['work_type'] or 'la instalación'
                address = conversation['address'] or 'la dirección indicada'
                location = conversation['location']
                duration = conversation['duration_hours']
                address_line = address
                if location and location.lower() not in address.lower():
                    address_line = f"{address} ({location})"
                response.message(
                    f"Perfecto. Le confirmo los datos de la cita:\n\n"
                    f"📅 {selected_slot['formatted']}\n"
                    f"⏱️ Duración aproximada: {duration} horas\n"
                    f"📍 Dirección: {address_line}\n"
                    f"🔧 Trabajo: {work_type}\n\n"
                    f"¿Es todo correcto? Responda SÍ para confirmar la cita o NO si "
                    f"necesita que cambiemos algo.")
                conversation['state'] = ConversationState.CONFIRMING
            elif 'no' in incoming_msg or 'ninguna' in incoming_msg:
                response.message(
                    "Entendido, no se preocupe. Voy a revisar la agenda para buscarle "
                    "otras fechas y me pondré en contacto con usted a la mayor brevedad. "
                    "Gracias por su paciencia.")
                conversation['state'] = ConversationState.CANCELLED
            else:
                response.message(
                    "Disculpe, no le he entendido. ¿Podría indicarme el número de la "
                    "opción que prefiere (1, 2 o 3)? Si ninguna le viene bien, dígamelo "
                    "y le busco otras fechas.")

        elif state == ConversationState.CONFIRMING:
            if any(w in incoming_msg for w in ['si', 'sí', 'vale', 'ok', 'correcto', 'confirmo']):
                selected_slot = conversation['selected_slot']
                try:
                    db_service.create_appointment_from_slot(conversation, selected_slot)
                except Exception as e:
                    print(f"Error saving appointment: {e}")
                response.message(
                    f"Estupendo. Su cita ha quedado confirmada. ✅\n\n"
                    f"Le espero el {selected_slot['formatted']}.\n\n"
                    f"Recibirá un recordatorio el día antes. Si le surgiera cualquier "
                    f"imprevisto, le agradecería que me avisara con antelación.\n\n"
                    f"Muchas gracias por su tiempo. Un saludo.")
                conversation['state'] = ConversationState.COMPLETED
            else:
                response.message("Por supuesto. ¿Qué necesita que modifiquemos? Dígame y "
                                 "lo ajustamos enseguida.")

        return str(response)
    except Exception as e:
        print(f"Error in webhook: {e}")
        response = MessagingResponse()
        response.message("Disculpe las molestias, ha ocurrido una incidencia. Me pondré "
                         "en contacto con usted en breve.")
        return str(response)


@app.route('/appointment-status/<conversation_id>', methods=['GET'])
def get_appointment_status(conversation_id):
    """Return the current state of a WhatsApp conversation."""
    for phone, conv in conversations.items():
        if conv['id'] == conversation_id:
            return jsonify({'success': True, 'state': conv['state']})
    return jsonify({'success': False, 'error': 'Conversation not found'}), 404


# ===========================================================================
# Daily reminder
# ===========================================================================

def _build_daily_reminder_message(target_date, appointments):
    """Build a clean, mobile-friendly WhatsApp summary grouped by zone."""
    header = f"📋 *Resumen de trabajos para mañana*\n🗓️ {format_date_es(target_date)}"
    if not appointments:
        return (f"{header}\n\n✅ No hay trabajos programados para mañana.\n"
                f"¡Disfrute del día!")
    zones = {}
    for appt in appointments:
        zones.setdefault(appt.get('zone') or 'Sin zona', []).append(appt)
    lines = [header, f"Total de citas: *{len(appointments)}*"]
    for zone in sorted(zones.keys()):
        zone_appts = sorted(zones[zone], key=lambda a: a.get('time', ''))
        lines.append("")
        lines.append(f"📍 *{zone}* ({len(zone_appts)})")
        lines.append("──────────────")
        for appt in zone_appts:
            ref = appt.get('wo_reference') or 'N/D'
            name = appt.get('customer_name') or 'Cliente'
            address = appt.get('notes') or appt.get('location') or 'N/D'
            phone = appt.get('customer_phone') or 'N/D'
            work = appt.get('work_type') or 'N/D'
            lines.append(f"🕐 {appt.get('time', '--:--')}  |  WO/Ref: {ref}")
            lines.append(f"👤 {name}")
            lines.append(f"🏠 {address}")
            lines.append(f"📞 {phone}")
            lines.append(f"🔧 {work}")
            lines.append("")
    lines.append("Buen trabajo mañana. 💪")
    return "\n".join(lines).strip()


@app.route('/send-daily-reminder', methods=['POST'])
def send_daily_reminder():
    """Send the installer a summary of the next day's appointments from the DB."""
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

        appointments = db_service.get_day_appointments(target_date)
        message = _build_daily_reminder_message(target_date, appointments)
        message_sid = send_whatsapp_message(installer_number, message)

        if message_sid:
            return jsonify({
                'success': True,
                'date': target_date.strftime('%Y-%m-%d'),
                'appointments_count': len(appointments),
                'installer_number': installer_number,
                'message_sid': message_sid,
            })
        return jsonify({'success': False, 'error': 'Failed to send WhatsApp reminder'}), 500
    except Exception as e:
        print(f"Error in send_daily_reminder: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ===========================================================================
# REST API
# ===========================================================================

def _parse_date(value):
    return datetime.strptime(value, '%Y-%m-%d').date()


def _parse_time(value):
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time '{value}', expected HH:MM")


def _apply_appointment_payload(appt, data, partial=False):
    """Apply JSON payload fields onto an Appointment. Raises ValueError on bad data."""
    if 'appointment_date' in data:
        appt.appointment_date = _parse_date(data['appointment_date'])
    if 'start_time' in data:
        appt.start_time = _parse_time(data['start_time']) if data['start_time'] else None
    if 'end_time' in data:
        appt.end_time = _parse_time(data['end_time']) if data['end_time'] else None

    # Derive end_time from duration if only start + duration given.
    if 'duration_hours' in data and data['duration_hours'] is not None:
        appt.duration_hours = float(data['duration_hours'])
    if appt.start_time and appt.end_time is None and appt.duration_hours:
        start_dt = datetime.combine(appt.appointment_date, appt.start_time)
        appt.end_time = (start_dt + timedelta(hours=appt.duration_hours)).time()

    for field in ('customer_name', 'customer_phone', 'location', 'work_type',
                  'notes', 'wo_reference'):
        if field in data:
            setattr(appt, field, data[field])

    if 'status' in data:
        if data['status'] not in AppointmentStatus.ALL:
            raise ValueError(f"Invalid status '{data['status']}'")
        appt.status = data['status']
    if 'source' in data:
        if data['source'] not in AppointmentSource.ALL:
            raise ValueError(f"Invalid source '{data['source']}'")
        appt.source = data['source']


@app.route('/api/appointments', methods=['GET'])
@require_token
def api_list_appointments():
    """List appointments with optional filters: date, date_from, date_to, zone, source, status."""
    try:
        query = Appointment.query
        args = request.args
        if args.get('date'):
            query = query.filter(Appointment.appointment_date == _parse_date(args['date']))
        if args.get('date_from'):
            query = query.filter(Appointment.appointment_date >= _parse_date(args['date_from']))
        if args.get('date_to'):
            query = query.filter(Appointment.appointment_date <= _parse_date(args['date_to']))
        if args.get('source'):
            query = query.filter(Appointment.source == args['source'])
        if args.get('status'):
            query = query.filter(Appointment.status == args['status'])

        query = query.order_by(Appointment.appointment_date.asc(),
                               Appointment.start_time.asc())
        appts = query.all()

        # Zone filter is computed (not a column), so filter in Python.
        zone_filter = args.get('zone')
        result = [a.to_dict() for a in appts
                  if not zone_filter or a.zone.lower() == zone_filter.lower()]
        return jsonify({'success': True, 'count': len(result), 'appointments': result})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/appointments', methods=['POST'])
@require_token
def api_create_appointment():
    """Create a manual appointment."""
    try:
        data = request.get_json(silent=True) or {}
        if not data.get('appointment_date'):
            return jsonify({'success': False, 'error': 'appointment_date is required'}), 400
        appt = Appointment(
            status=data.get('status', AppointmentStatus.CONFIRMED),
            source=data.get('source', AppointmentSource.PERSONAL),
            duration_hours=float(data.get('duration_hours') or DEFAULT_DURATION_HOURS),
        )
        _apply_appointment_payload(appt, data)
        db.session.add(appt)
        db.session.commit()
        return jsonify({'success': True, 'appointment': appt.to_dict()}), 201
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/appointments/<int:appt_id>', methods=['PUT'])
@require_token
def api_update_appointment(appt_id):
    """Update an existing appointment."""
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        return jsonify({'success': False, 'error': 'Appointment not found'}), 404
    try:
        data = request.get_json(silent=True) or {}
        _apply_appointment_payload(appt, data)
        db.session.commit()
        return jsonify({'success': True, 'appointment': appt.to_dict()})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/appointments/<int:appt_id>', methods=['DELETE'])
@require_token
def api_delete_appointment(appt_id):
    """Delete an appointment."""
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        return jsonify({'success': False, 'error': 'Appointment not found'}), 404
    try:
        db.session.delete(appt)
        db.session.commit()
        return jsonify({'success': True, 'deleted_id': appt_id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/block-day', methods=['POST'])
@require_token
def api_block_day():
    """Block one or more full days (source=blocked_day)."""
    try:
        data = request.get_json(silent=True) or {}
        date_str = data.get('date')
        if not date_str:
            return jsonify({'success': False, 'error': 'date is required'}), 400
        block_date = _parse_date(date_str)
        # Avoid duplicate blocks for the same day.
        existing = (Appointment.query
                    .filter(Appointment.appointment_date == block_date)
                    .filter(Appointment.source == AppointmentSource.BLOCKED_DAY)
                    .first())
        if existing:
            return jsonify({'success': True, 'appointment': existing.to_dict(),
                            'message': 'Day already blocked'}), 200
        appt = Appointment(
            appointment_date=block_date,
            start_time=None,
            end_time=None,
            status=AppointmentStatus.BLOCKED,
            source=AppointmentSource.BLOCKED_DAY,
            work_type=data.get('reason') or 'Día bloqueado',
            notes=data.get('notes'),
        )
        db.session.add(appt)
        db.session.commit()
        return jsonify({'success': True, 'appointment': appt.to_dict()}), 201
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/availability', methods=['GET'])
@require_token
def api_availability():
    """Return available slots (used by the bot / admin panel)."""
    try:
        args = request.args
        duration = float(args.get('duration_hours') or DEFAULT_DURATION_HOURS)
        num_slots = int(args.get('num_slots') or 3)
        days_ahead = int(args.get('days_ahead') or 14)
        location = args.get('location', '')
        preferred_zone = get_zone_for_location(location) if location else None
        slots = db_service.find_available_slots(
            duration_hours=duration, num_slots=num_slots,
            days_ahead=days_ahead, preferred_zone=preferred_zone)
        clean = [{k: v for k, v in s.items() if k != 'datetime'} for s in slots]
        return jsonify({'success': True, 'count': len(clean), 'slots': clean,
                        'zone': preferred_zone})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/appointments/tomorrow', methods=['GET'])
@require_token
def api_tomorrow():
    """Return tomorrow's appointments (used by the reminder / panel)."""
    try:
        target = (datetime.now() + timedelta(days=1)).date()
        appts = db_service.get_day_appointments(target)
        return jsonify({'success': True, 'date': target.isoformat(),
                        'count': len(appts), 'appointments': appts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    try:
        with app.app_context():
            total = Appointment.query.count()
        db_ok = True
    except Exception as e:
        print(f"Health DB check failed: {e}")
        total = None
        db_ok = False
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': DATABASE_URL.split('///')[-1] if 'sqlite' in DATABASE_URL else 'external',
        'db_ok': db_ok,
        'appointments_total': total,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
