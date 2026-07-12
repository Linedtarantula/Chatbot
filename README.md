# Sistema de Gestión de Citas por WhatsApp

Sistema automatizado para gestionar citas de instalación mediante WhatsApp usando Twilio y integración con chatbot de Abacus.AI.

## 🚀 Características

- ✅ Contacto automático con clientes por WhatsApp
- ✅ Conversación natural y profesional en español (trato de "usted", instalador de Leroy Merlin)
- ✅ Propuesta de franjas horarias disponibles (bloques de 1,5 h, L-V de 08:00 a 16:00)
- ✅ Confirmación bidireccional
- ✅ **Base de datos propia (SQLite)**: control total de la agenda, sin dependencias externas
- ✅ Detección de disponibilidad y evita solapamientos consultando la base de datos
- ✅ Agrupación por zona/localidad (Islantilla, Lepe, La Antilla, Ayamonte...) para optimizar desplazamientos
- ✅ Diferencia trabajos de **Leroy Merlin**, **personales** y **días bloqueados**
- ✅ **API REST** (con CORS) para un panel web de administración
- ✅ Notificación al instalador con el resumen de citas del día siguiente

> **Base de datos:** se usa un fichero SQLite local (`appointments.db`). Se inicializa automáticamente al arrancar. También puede ejecutarse `python init_db.py` (con `--seed` para datos de ejemplo). Ya no se necesita Google Calendar.

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
| `/appointment-status/<id>` | GET | Consultar estado de conversación |
| `/webhook/whatsapp` | POST | Webhook de Twilio (recibir mensajes) |
| `/send-daily-reminder` | POST | Enviar al instalador el resumen de citas del día siguiente |
| `/api/appointments` | GET | Listar citas (filtros: `date`, `date_from`, `date_to`, `zone`, `source`, `status`) |
| `/api/appointments` | POST | Crear cita manual |
| `/api/appointments/<id>` | PUT | Actualizar cita |
| `/api/appointments/<id>` | DELETE | Eliminar cita |
| `/api/block-day` | POST | Bloquear un día completo (`source=blocked_day`) |
| `/api/availability` | GET | Consultar horarios disponibles (usado por el bot/panel) |
| `/api/appointments/tomorrow` | GET | Citas del día siguiente |

### 🗄️ API REST y base de datos

Todas las operaciones se guardan en una base de datos **SQLite** propia (`appointments.db`) mediante SQLAlchemy. La tabla `appointments` distingue el tipo de entrada mediante los campos:

- **`status`**: `pending`, `confirmed`, `completed`, `cancelled`, `blocked`
- **`source`**: `leroy_merlin`, `personal`, `blocked_day`

Los endpoints `/api/*` tienen **CORS habilitado** para que un panel web pueda consumirlos. Opcionalmente pueden protegerse con un token simple: define la variable `API_TOKEN` y envía la cabecera `X-API-Token: <token>` (o `?token=<token>`). Si `API_TOKEN` está vacío, la API queda abierta.

Ejemplos:

```bash
# Crear una cita manual (trabajo personal)
curl -X POST https://tu-dominio.railway.app/api/appointments \
  -H "Content-Type: application/json" \
  -d '{"customer_name":"María","appointment_date":"2026-07-20","start_time":"09:00","duration_hours":1.5,"location":"Cartaya","work_type":"Montaje","source":"personal"}'

# Bloquear un día completo (vacaciones)
curl -X POST https://tu-dominio.railway.app/api/block-day \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-07-21","reason":"Vacaciones"}'

# Consultar disponibilidad para una zona
curl "https://tu-dominio.railway.app/api/availability?location=Lepe&num_slots=3"

# Listar citas de Leroy Merlin de una fecha
curl "https://tu-dominio.railway.app/api/appointments?source=leroy_merlin&date=2026-07-20"
```

## 🔔 Recordatorio diario al instalador

El endpoint `/send-daily-reminder` consulta la base de datos, obtiene **todas las citas del día siguiente**, las **agrupa por zona geográfica** (ordenadas por zona y hora) y envía un mensaje de WhatsApp profesional y fácil de leer desde el móvil al número del instalador vía Twilio.

Ejemplo del mensaje que recibe el instalador:

```
📋 Resumen de trabajos para mañana
🗓️ Martes 7 de julio
Total de citas: 3

📍 Costa Occidental (2)
──────────────
🕐 09:00  |  WO/Ref: WO-123
👤 Juan Pérez
🏠 Calle Mayor 10
📞 +34611111111
🔧 Instalación de armario

🕐 12:00  |  WO/Ref: WO-456
👤 Ana López
🏠 Av. del Mar 5
📞 +34622222222
🔧 Montaje cocina

📍 Huelva Capital (1)
──────────────
🕐 10:00  |  WO/Ref: WO-789
👤 Luis Gil
🏠 Plaza 1
📞 +34633333333
🔧 Reparación

Buen trabajo mañana. 💪
```

### Configurar el número del instalador

Añade la variable de entorno `INSTALLER_PHONE_NUMBER` con el número de WhatsApp que debe recibir el recordatorio (formato E.164, p. ej. `+34612345678`):

```
INSTALLER_PHONE_NUMBER=+34612345678
```

> **Pruebas con sandbox:** durante las pruebas, usa el mismo número que has unido al sandbox de Twilio (el que envió el mensaje `join <código>`). Cuando pases a producción, sustitúyelo por el número real del instalador. Si dejas la variable vacía, se usará `TWILIO_WHATSAPP_NUMBER`.

> **Nota:** las citas se leen de la base de datos SQLite propia.

### Probar manualmente

```bash
# Recordatorio del día siguiente (por defecto)
curl -X POST https://tu-dominio.railway.app/send-daily-reminder

# Forzar una fecha concreta (opcional)
curl -X POST https://tu-dominio.railway.app/send-daily-reminder \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-07-07"}'
```

### ⏰ Automatizar con un cron job gratuito (cron-job.org)

Para que el recordatorio se envíe automáticamente cada tarde (18:00–19:00) sin necesidad de mantener un servidor propio de tareas, usa un servicio de cron gratuito como **[cron-job.org](https://cron-job.org)**:

1. **Crea una cuenta gratuita** en https://cron-job.org y confirma tu email.
2. En el panel, pulsa **"Create cronjob"** (Crear tarea).
3. Rellena los campos:
   - **Title / Título:** `Recordatorio diario WhatsApp`
   - **URL:** `https://tu-dominio.railway.app/send-daily-reminder`
   - **Request method / Método:** selecciona **POST** (en *Advanced* → *Request method*).
4. **Programación (Schedule):**
   - Elige **"Every day"** (todos los días).
   - Hora: **18:30** (o cualquier hora entre las 18:00 y las 19:00).
   - **⚠️ Zona horaria:** en *Settings* del cronjob selecciona **Europe/Madrid** para que la hora sea la local española (si no, cron-job.org usa UTC por defecto).
5. (Opcional) En **Notifications**, activa el aviso por email si la petición falla, para enterarte si algún día no se envía el recordatorio.
6. Guarda con **"Create"**. La tarea llamará al endpoint cada día a la hora indicada y el instalador recibirá el resumen de las citas del día siguiente.

> **Alternativas gratuitas equivalentes:** puedes usar cualquier servicio similar (por ejemplo **EasyCron**, **Cronitor**, **UptimeRobot** con monitor tipo *keyword/POST*, o **GitHub Actions** con un `schedule: cron`). Lo único imprescindible es que hagan una petición **POST** a `/send-daily-reminder` una vez al día entre las 18:00 y las 19:00 (hora de España).
>
> Recuerda que el cron usa habitualmente formato UTC: las **18:30 en Europe/Madrid** equivalen a `30 16 * * *` en invierno (CET, UTC+1) y `30 17 * * *` en horario de verano (CEST, UTC+2). Por eso es más cómodo fijar la zona horaria Europe/Madrid en cron-job.org y olvidarse de la conversión.

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

- [x] Base de datos propia (SQLite) con API REST
- [ ] Base de datos persistente (PostgreSQL/MongoDB)
- [x] Sistema de recordatorios automáticos (resumen diario al instalador)
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
