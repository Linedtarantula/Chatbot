from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import os
import json
from datetime import datetime, timedelta
import threading
import time

app = Flask(__name__)

# Twilio configuration
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886'

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# In-memory storage for conversations (use Redis/DB in production)
conversations = {}
appointment_status = {}

class ConversationState:
    GREETING = 'greeting'
    PROPOSING_SLOTS = 'proposing_slots'
    WAITING_CHOICE = 'waiting_choice'
    CONFIRMING = 'confirming'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'

def send_whatsapp_message(to_number, message):
    """Send WhatsApp message via Twilio"""
    try:
        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'
        
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            body=message,
            to=to_number
        )
        return message.sid
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

def generate_time_slots(duration_hours=2, num_slots=3):
    """Generate available time slots (mock - integrate with Google Calendar)"""
    slots = []
    start_date = datetime.now() + timedelta(days=1)
    
    for i in range(num_slots):
        slot_date = start_date + timedelta(days=i)
        # Skip weekends
        while slot_date.weekday() >= 5:
            slot_date += timedelta(days=1)
        
        # Morning slot (9:00-11:00 or based on duration)
        slot_time = slot_date.replace(hour=9, minute=0, second=0, microsecond=0)
        slots.append({
            'datetime': slot_time,
            'formatted': slot_time.strftime('%A %d de %B a las %H:%M'),
            'end_time': (slot_time + timedelta(hours=duration_hours)).strftime('%H:%M')
        })
    
    return slots

@app.route('/initiate-appointment', methods=['POST'])
def initiate_appointment():
    """
    Endpoint called by Abacus.AI chatbot to initiate appointment scheduling
    Expected payload:
    {
        "customer_name": "Juan Pérez",
        "customer_phone": "+34612345678",
        "work_type": "Instalación de armarios",
        "duration_hours": 2,
        "reference": "LM-12345",
        "address": "Calle Mayor 10, Madrid",
        "installer_id": "installer_123"
    }
    """
    try:
        data = request.json
        customer_phone = data.get('customer_phone')
        
        if not customer_phone:
            return jsonify({'success': False, 'error': 'customer_phone is required'}), 400
        
        # Normalize phone number
        if not customer_phone.startswith('+'):
            customer_phone = f'+34{customer_phone}'
        
        # Generate time slots
        duration = float(data.get('duration_hours', 2))
        time_slots = generate_time_slots(duration_hours=duration, num_slots=3)
        
        # Store conversation data
        conversation_id = f"{customer_phone}_{int(time.time())}"
        conversations[customer_phone] = {
            'id': conversation_id,
            'customer_name': data.get('customer_name'),
            'customer_phone': customer_phone,
            'work_type': data.get('work_type'),
            'duration_hours': duration,
            'reference': data.get('reference', ''),
            'address': data.get('address', ''),
            'installer_id': data.get('installer_id'),
            'state': ConversationState.GREETING,
            'time_slots': time_slots,
            'selected_slot': None,
            'created_at': datetime.now().isoformat()
        }
        
        # Send initial greeting
        customer_name = data.get('customer_name', 'cliente')
        work_type = data.get('work_type', 'instalación')
        
        greeting_message = f"""¡Hola {customer_name}! 👋

Soy el asistente de instalaciones. Me pongo en contacto contigo para coordinar la {work_type} que tienes pendiente.

¿Te viene bien que te proponga algunas fechas disponibles?"""
        
        message_sid = send_whatsapp_message(customer_phone, greeting_message)
        
        if message_sid:
            conversations[customer_phone]['state'] = ConversationState.PROPOSING_SLOTS
            return jsonify({
                'success': True,
                'conversation_id': conversation_id,
                'message': 'Appointment scheduling initiated',
                'message_sid': message_sid
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to send WhatsApp message'}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/webhook/whatsapp', methods=['POST'])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages from Twilio"""
    try:
        incoming_msg = request.values.get('Body', '').strip().lower()
        from_number = request.values.get('From', '')
        
        # Remove 'whatsapp:' prefix
        customer_phone = from_number.replace('whatsapp:', '')
        
        response = MessagingResponse()
        
        if customer_phone not in conversations:
            response.message("Lo siento, no tengo ninguna cita pendiente de coordinar contigo. Si necesitas ayuda, contacta directamente con tu instalador.")
            return str(response)
        
        conversation = conversations[customer_phone]
        state = conversation['state']
        
        # Handle conversation flow
        if state == ConversationState.PROPOSING_SLOTS:
            # Customer responded to greeting, send time slots
            slots = conversation['time_slots']
            slots_text = "\n".join([
                f"{i+1}. {slot['formatted']} (hasta las {slot['end_time']})"
                for i, slot in enumerate(slots)
            ])
            
            message = f"""Perfecto, estas son las opciones disponibles:

{slots_text}

¿Cuál te viene mejor? Responde con el número de la opción (1, 2 o 3).

Si ninguna te viene bien, dímelo y busco otras fechas. 😊"""
            
            response.message(message)
            conversation['state'] = ConversationState.WAITING_CHOICE
            
        elif state == ConversationState.WAITING_CHOICE:
            # Customer is choosing a slot
            if incoming_msg in ['1', '2', '3']:
                slot_index = int(incoming_msg) - 1
                selected_slot = conversation['time_slots'][slot_index]
                conversation['selected_slot'] = selected_slot
                
                # Confirm details
                work_type = conversation['work_type']
                address = conversation['address']
                duration = conversation['duration_hours']
                
                confirmation_msg = f"""¡Genial! 👍

Confirmo entonces:
📅 {selected_slot['formatted']}
⏱️ Duración aproximada: {duration} horas
📍 Dirección: {address}
🔧 Trabajo: {work_type}

¿Todo correcto? Responde SÍ para confirmar o NO si hay que cambiar algo."""
                
                response.message(confirmation_msg)
                conversation['state'] = ConversationState.CONFIRMING
                
            elif 'no' in incoming_msg or 'ninguna' in incoming_msg:
                response.message("Entendido. Déjame consultar otras fechas disponibles y te contacto en breve. 📅")
                conversation['state'] = ConversationState.CANCELLED
                appointment_status[conversation['id']] = 'needs_rescheduling'
                
            else:
                response.message("Por favor, responde con el número de la opción que prefieres (1, 2 o 3), o dime si ninguna te viene bien.")
                
        elif state == ConversationState.CONFIRMING:
            # Customer is confirming
            if 'si' in incoming_msg or 'sí' in incoming_msg or 'vale' in incoming_msg or 'ok' in incoming_msg:
                response.message(f"""¡Perfecto! ✅ Cita confirmada.

Te esperamos el {conversation['selected_slot']['formatted']}.

Recibirás un recordatorio un día antes. Si surge cualquier imprevisto, avísanos con antelación.

¡Hasta pronto! 👋""")
                
                conversation['state'] = ConversationState.COMPLETED
                appointment_status[conversation['id']] = {
                    'status': 'confirmed',
                    'slot': conversation['selected_slot'],
                    'customer_phone': customer_phone,
                    'customer_name': conversation['customer_name'],
                    'work_type': conversation['work_type'],
                    'address': conversation['address'],
                    'reference': conversation['reference'],
                    'duration_hours': conversation['duration_hours']
                }
                
            else:
                response.message("¿Qué necesitas cambiar? Dime y lo ajustamos. 😊")
                
        return str(response)
        
    except Exception as e:
        print(f"Error in webhook: {e}")
        response = MessagingResponse()
        response.message("Disculpa, ha ocurrido un error. Te contactaremos pronto.")
        return str(response)

@app.route('/appointment-status/<conversation_id>', methods=['GET'])
def get_appointment_status(conversation_id):
    """Check status of an appointment (called by Abacus.AI chatbot)"""
    if conversation_id in appointment_status:
        return jsonify({
            'success': True,
            'status': appointment_status[conversation_id]
        })
    else:
        # Check if conversation exists and get current state
        for phone, conv in conversations.items():
            if conv['id'] == conversation_id:
                return jsonify({
                    'success': True,
                    'status': 'in_progress',
                    'state': conv['state']
                })
        
        return jsonify({
            'success': False,
            'error': 'Conversation not found'
        }), 404

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
