# 📦 Guía de Despliegue Paso a Paso

## 🎯 Opción Recomendada: Railway

Railway es la opción más sencilla y ofrece un plan gratuito generoso.

### Paso 1: Preparar el Código

1. **Crear repositorio en GitHub**:
   ```bash
   cd /home/ubuntu/whatsapp-appointment-system
   git init
   git add .
   git commit -m "Initial commit - WhatsApp appointment system"
   ```

2. **Subir a GitHub**:
   - Crear un nuevo repositorio en https://github.com/new
   - Seguir las instrucciones para push:
   ```bash
   git remote add origin https://github.com/TU_USUARIO/whatsapp-appointments.git
   git branch -M main
   git push -u origin main
   ```

### Paso 2: Desplegar en Railway

1. **Ir a Railway**: https://railway.app/

2. **Crear cuenta** (puedes usar GitHub para login rápido)

3. **Nuevo Proyecto**:
   - Click en "New Project"
   - Seleccionar "Deploy from GitHub repo"
   - Autorizar Railway a acceder a tu GitHub
   - Seleccionar el repositorio `whatsapp-appointments`

4. **Configurar Variables de Entorno**:
   - En el dashboard del proyecto, ir a "Variables"
   - Añadir:
     ```
     TWILIO_ACCOUNT_SID = tu_account_sid_aqui
     TWILIO_AUTH_TOKEN = tu_auth_token_aqui
     ```

5. **Despliegue Automático**:
   - Railway detectará el `Procfile` y `requirements.txt`
   - El despliegue comenzará automáticamente
   - Espera 2-3 minutos

6. **Obtener URL Pública**:
   - En "Settings" → "Networking"
   - Click en "Generate Domain"
   - Copia la URL (ej: `https://whatsapp-appointments-production.up.railway.app`)

### Paso 3: Configurar Twilio

1. **Ir a Twilio Console**: https://console.twilio.com/

2. **Obtener Credenciales**:
   - Account SID y Auth Token están en el Dashboard principal
   - Cópialos y úsalos en Railway (Paso 2.4)

3. **Configurar WhatsApp Sandbox** (para pruebas):
   - Ir a: Messaging → Try it out → Send a WhatsApp message
   - Verás un número de WhatsApp y un código (ej: "join happy-dog")
   - Desde tu WhatsApp, envía ese mensaje al número mostrado
   - Recibirás confirmación de que estás conectado al sandbox

4. **Configurar Webhook**:
   - En la misma página del sandbox
   - En "WHEN A MESSAGE COMES IN":
     ```
     https://TU-URL-DE-RAILWAY.up.railway.app/webhook/whatsapp
     ```
   - Método: HTTP POST
   - Click "Save"

### Paso 4: Probar el Sistema

1. **Health Check**:
   ```bash
   curl https://TU-URL-DE-RAILWAY.up.railway.app/health
   ```
   Deberías ver: `{"status":"healthy","timestamp":"..."}`

2. **Prueba de Cita** (reemplaza el número con tu WhatsApp):
   ```bash
   curl -X POST https://TU-URL-DE-RAILWAY.up.railway.app/initiate-appointment \
     -H "Content-Type: application/json" \
     -d '{
       "customer_name": "Tu Nombre",
       "customer_phone": "+34TU_NUMERO",
       "work_type": "Instalación de prueba",
       "duration_hours": 2,
       "reference": "TEST-001",
       "address": "Calle Test 1, Madrid",
       "installer_id": "test"
     }'
   ```

3. **Verificar WhatsApp**:
   - Deberías recibir un mensaje del bot en tu WhatsApp
   - Responde para probar la conversación

### Paso 5: Integrar con Chatbot de Abacus.AI

Ahora que el servidor está desplegado, necesitas la URL para configurar el chatbot.

**URL del servidor**: `https://TU-URL-DE-RAILWAY.up.railway.app`

Guarda esta URL, la necesitarás para el siguiente paso.

---

## 🔧 Alternativa: Render

Si prefieres Render:

1. **Ir a Render**: https://render.com/
2. **New → Web Service**
3. **Conectar GitHub repo**
4. **Configuración**:
   - Name: `whatsapp-appointments`
   - Environment: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
5. **Variables de entorno**: Añadir `TWILIO_ACCOUNT_SID` y `TWILIO_AUTH_TOKEN`
6. **Create Web Service**

---

## ✅ Checklist Final

- [ ] Código subido a GitHub
- [ ] Proyecto desplegado en Railway/Render
- [ ] Variables de entorno configuradas
- [ ] URL pública obtenida
- [ ] Webhook configurado en Twilio
- [ ] WhatsApp sandbox activado (si aplica)
- [ ] Health check funciona
- [ ] Prueba de mensaje enviada y recibida
- [ ] URL guardada para integración con chatbot

---

## 🆘 Problemas Comunes

**"Application failed to respond"**
- Verifica que las variables de entorno estén configuradas
- Revisa los logs en Railway/Render

**"No recibo mensajes de WhatsApp"**
- Verifica que el webhook esté configurado correctamente
- Asegúrate de que la URL es HTTPS
- Comprueba que estás en el sandbox (si aplica)

**"Error al enviar mensaje"**
- Verifica las credenciales de Twilio
- Comprueba el formato del número (+34...)
- Revisa el balance de tu cuenta Twilio

---

## 📞 Siguiente Paso

Una vez completado todo esto, proporciona la URL de tu servidor para modificar el chatbot de Abacus.AI y conectarlo con este sistema.
