# Sistema de Gestión de Citas por WhatsApp

Sistema automatizado para gestionar citas de instalación mediante WhatsApp usando Twilio y integración con chatbot de Abacus.AI.

## 🚀 Características

- ✅ Contacto automático con clientes por WhatsApp
- ✅ Conversación natural en español
- ✅ Propuesta de franjas horarias disponibles
- ✅ Confirmación bidireccional
- ✅ Integración con Google Calendar (próximamente)
- ✅ Notificación al instalador cuando la cita está confirmada

## 📋 Requisitos Previos

1. **Cuenta de Twilio** con WhatsApp habilitado
2. **Credenciales de Twilio**:
   - Account SID
   - Auth Token
   - Número de WhatsApp de Twilio

## 🛠️ Instalación y Despliegue

### Opción 1: Despliegue en Railway (Recomendado)

1. **Crear cuenta en Railway**: https://railway.app/

2. **Crear nuevo proyecto**:
   - Click en "New Project"
   - Seleccionar "Deploy from GitHub repo"
   - Conectar tu repositorio (sube estos archivos a GitHub primero)

3. **Configurar variables de entorno** en Railway:
   ```
   TWILIO_ACCOUNT_SID=tu_account_sid
   TWILIO_AUTH_TOKEN=tu_auth_token
   PORT=5000
   ```

4. **Railway detectará automáticamente** el `Procfile` y desplegará la aplicación

5. **Obtener la URL pública** (ej: `https://tu-app.railway.app`)

### Opción 2: Despliegue en Render

1. **Crear cuenta en Render**: https://render.com/

2. **Crear nuevo Web Service**:
   - Click en "New +" → "Web Service"
   - Conectar repositorio de GitHub
   - Configuración:
     - Build Command: `pip install -r requirements.txt`
     - Start Command: `gunicorn app:app`

3. **Configurar variables de entorno**:
   ```
   TWILIO_ACCOUNT_SID=tu_account_sid
   TWILIO_AUTH_TOKEN=tu_auth_token
   ```

4. **Desplegar** y obtener la URL pública

### Opción 3: Despliegue Local (Para pruebas)

```bash
cd whatsapp-appointment-system

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate

# Instalar dependencias
pip install -r requirements.txt

# Configurar variables de entorno
cp .env.example .env
# Editar .env con tus credenciales

# Ejecutar servidor
python app.py
```

Para exponer localmente con ngrok:
```bash
ngrok http 5000
```

## ⚙️ Configuración de Twilio

1. **Ir a Twilio Console**: https://console.twilio.com/

2. **Configurar Webhook de WhatsApp**:
   - Ir a: Messaging → Try it out → Send a WhatsApp message
   - En "Sandbox settings" o en tu número de WhatsApp configurado
   - Configurar "WHEN A MESSAGE COMES IN":
     ```
     https://tu-dominio.railway.app/webhook/whatsapp
     ```
   - Método: POST

3. **Probar el sandbox** (si usas sandbox):
   - Envía el código de activación desde tu WhatsApp al número de Twilio
   - Ejemplo: "join <tu-codigo-sandbox>"

## 🔗 Integración con Chatbot de Abacus.AI

El chatbot de Abacus.AI debe llamar al endpoint `/initiate-appointment` con este payload:

```json
{
  "customer_name": "Juan Pérez",
  "customer_phone": "+34612345678",
  "work_type": "Instalación de armarios",
  "duration_hours": 2,
  "reference": "LM-12345",
  "address": "Calle Mayor 10, Madrid",
  "installer_id": "installer_123"
}
```

Para verificar el estado de una cita:
```
GET https://tu-dominio.railway.app/appointment-status/{conversation_id}
```

## 📡 Endpoints de la API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/initiate-appointment` | POST | Iniciar proceso de cita |
| `/appointment-status/<id>` | GET | Consultar estado de cita |
| `/webhook/whatsapp` | POST | Webhook de Twilio (recibir mensajes) |

## 🧪 Pruebas

1. **Health Check**:
   ```bash
   curl https://tu-dominio.railway.app/health
   ```

2. **Iniciar cita de prueba**:
   ```bash
   curl -X POST https://tu-dominio.railway.app/initiate-appointment \
     -H "Content-Type: application/json" \
     -d '{
       "customer_name": "Test User",
       "customer_phone": "+34600000000",
       "work_type": "Instalación de prueba",
       "duration_hours": 2,
       "reference": "TEST-001",
       "address": "Calle Test 1, Madrid",
       "installer_id": "test_installer"
     }'
   ```

## 🔐 Seguridad

- ⚠️ **Nunca subas credenciales a GitHub**
- Usa variables de entorno para secretos
- En producción, considera añadir autenticación a los endpoints
- Implementa rate limiting para prevenir abuso

## 📝 Próximas Mejoras

- [ ] Integración real con Google Calendar API
- [ ] Base de datos persistente (PostgreSQL/MongoDB)
- [ ] Sistema de recordatorios automáticos
- [ ] Panel de administración web
- [ ] Soporte para múltiples instaladores
- [ ] Métricas y analytics

## 🐛 Troubleshooting

**Problema**: No recibo mensajes de WhatsApp
- Verifica que el webhook esté configurado correctamente en Twilio
- Comprueba los logs del servidor
- Asegúrate de que el número está en el sandbox (si aplica)

**Problema**: Error al enviar mensajes
- Verifica las credenciales de Twilio
- Comprueba que el número de teléfono tiene el formato correcto (+34...)
- Revisa el balance de tu cuenta de Twilio

## 📞 Soporte

Para problemas o preguntas, contacta con el equipo de desarrollo.

## 📄 Licencia

Uso interno - Todos los derechos reservados
