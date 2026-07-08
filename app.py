"""
WhatsApp appointment system — Flask server backed by shared PostgreSQL.

Reads/writes the same database as the NextJS admin panel (Prisma tables).
Tables: Appointment, BlockedDay (PascalCase, matching Prisma schema).

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
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from models import db, Appointment, BlockedDay, AppointmentStatus, AppointmentSource
from zones import get_zone_for_location
import db_service
from db_service import (
    DEFAULT_DURATION_HOURS,
    format_es,
    format_date_es,
)

# --- Config -----------------------------------------------------------------
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL environment variable is required')

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
INSTALLER_PHONE_NUMBER = os.environ.get('INSTALLER_PHONE_NUMBER', '')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_size': 5,
    'max_overflow': 2,
    'connect_args': {'connect_timeout': 15},
}

db.init_app(app)
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


# --- Twilio helper ----------------------------------------------------------

def send_whatsapp_message(to_number, message):
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


# --- Fallback slot generation -----------------------------------------------

def generate_time_slots(duration_hours=DEFAULT_DURATION_HOURS, num_slots=3):
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
    slots = db_service.find_available_slots(
        duration_hours=duration_hours, num_slots=num_slots,
        preferred_zone=preferred_zone)
    if slots:
        return slots
    return generate_time_slots(duration_hours=duration_hours, num_slots=num_slots)


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
                    db_service.create_appointment_from_slot(conversation, selected_slot)
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
            ref = appt.get('reference') or appt.get('wo_reference') or 'N/D'
            name = appt.get('client') or appt.get('customer_name') or 'Cliente'
            address = appt.get('notes') or appt.get('locality') or 'N/D'
            phone = appt.get('phone') or appt.get('customer_phone') or 'N/D'
            work = appt.get('workType') or appt.get('work_type') or 'N/D'
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

        appointments = db_service.get_day_appointments(target_date)
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
    try:
        with app.app_context():
            total = Appointment.query.count()
        db_ok = True
    except Exception as e:
        print(f'Health DB check failed: {e}')
        total = None
        db_ok = False
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': 'postgresql',
        'db_ok': db_ok,
        'appointments_total': total,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
