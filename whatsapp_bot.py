# whatsapp_bot.py - Empleado Digital por WhatsApp (version para la nube)
# Servidor web que recibe mensajes de Twilio, pregunta a Gemini y responde.

import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from google import genai

CARPETA = os.path.dirname(os.path.abspath(__file__))

# La clave viene de una VARIABLE DE ENTORNO (segura, NO se sube al repo).
# Si no esta, intenta leer clave.txt (para correr en tu compu).
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not API_KEY:
    try:
        with open(os.path.join(CARPETA, "clave.txt"), "r", encoding="utf-8-sig") as f:
            API_KEY = f.read().strip()
    except FileNotFoundError:
        API_KEY = ""

with open(os.path.join(CARPETA, "productos.txt"), "r", encoding="utf-8-sig") as f:
    PRODUCTOS = f.read()

cliente = genai.Client(api_key=API_KEY)

INSTRUCCIONES = f"""Sos el empleado de atencion al cliente de una distribuidora, atendiendo por WhatsApp.
Respondes en espanol rioplatense, amable y al grano (mensajes cortos para chat).
Solo podes usar la informacion de esta lista de productos:

{PRODUCTOS}

Reglas:
- Si el producto esta en la lista, deci el precio y si hay stock.
- Si no esta, deci que no lo manejas y ofrece uno parecido si existe.
- Nunca inventes precios ni productos."""

app = Flask(__name__)


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    pregunta = request.form.get("Body", "")
    twiml = MessagingResponse()
    try:
        respuesta = cliente.models.generate_content(
            model="gemini-2.5-flash",
            contents=INSTRUCCIONES + "\n\nConsulta del cliente: " + pregunta,
        )
        twiml.message(respuesta.text)
    except Exception as e:
        twiml.message("Perdon, tuve un problemita. Proba de nuevo en un rato.")
        print("ERROR:", e)
    return str(twiml)


@app.route("/", methods=["GET"])
def home():
    return "Empleado Digital activo."


# Solo para correr en tu compu. En la nube lo arranca gunicorn.
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
