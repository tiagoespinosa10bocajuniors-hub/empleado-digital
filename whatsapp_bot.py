# whatsapp_bot.py - Empleado Digital (MOTOR multi-negocio)
# Un solo servidor que atiende a muchos negocios.
# - Cada negocio se registra, entra y carga SUS productos.
# - Cuando llega un WhatsApp, busca de que negocio es y responde con SU lista.
# - Soporta el numero de prueba de Twilio (sandbox) Y el numero propio por Meta.

import os
import datetime
import threading
import time
import urllib.request
import json
from flask import (Flask, request, send_from_directory, render_template,
                   redirect, url_for, session, flash)
from twilio.twiml.messaging_response import MessagingResponse
from google import genai
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

CARPETA = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------
# 1) Clave de Gemini (variable de entorno o, si corres en tu compu, clave.txt)
# ------------------------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not API_KEY:
    try:
        with open(os.path.join(CARPETA, "clave.txt"), "r", encoding="utf-8-sig") as f:
            API_KEY = f.read().strip()
    except FileNotFoundError:
        API_KEY = ""
cliente = genai.Client(api_key=API_KEY)

# ------------------------------------------------------------------
# 2) App web + base de datos
# ------------------------------------------------------------------
app = Flask(__name__, template_folder=CARPETA)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-esta-clave-secreta-en-produccion")

DB_URL = os.environ.get("DATABASE_URL", "").strip()
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
if not DB_URL:
    DB_URL = "sqlite:///" + os.path.join(CARPETA, "empleado.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Numero de prueba de Twilio (el "sandbox"). Es compartido por ahora.
NUMERO_SANDBOX = "whatsapp:+14155238886"

# --- WhatsApp Cloud API (Meta): para el numero propio de cada negocio ---
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "empleado-digital-verify").strip()
WA_API = os.environ.get("WHATSAPP_API_VERSION", "v21.0").strip()


def enviar_wa(phone_id, to, body):
    """Envia un mensaje de WhatsApp por la API de Meta."""
    url = f"https://graph.facebook.com/{WA_API}/{phone_id}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": "Bearer " + WHATSAPP_TOKEN,
        "Content-Type": "application/json",
    })
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print("WA send error:", e)


# Cada fila = un negocio.
class Negocio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    clave_hash = db.Column(db.String(255), nullable=False)
    whatsapp_to = db.Column(db.String(40))          # numero de Twilio (sandbox)
    wa_phone_id = db.Column(db.String(40))          # ID del numero propio (Meta)
    productos = db.Column(db.Text, default="")
    creado = db.Column(db.DateTime, default=datetime.datetime.utcnow)


def instrucciones(negocio):
    """Lo que el empleado tiene permitido saber, segun el negocio."""
    return f"""Sos el empleado de atencion al cliente de "{negocio.nombre}", atendiendo por WhatsApp.
Respondes en espanol rioplatense, amable y al grano (mensajes cortos para chat).
Solo podes usar la informacion de esta lista de productos:

{negocio.productos or '(todavia no cargaron productos)'}

Reglas:
- Si el producto esta en la lista, deci el precio y si hay stock.
- Si no esta, deci que no lo manejas y ofrece uno parecido si existe.
- Nunca inventes precios ni productos."""


def responder_ia(negocio, texto):
    """Le pregunta a Gemini con el contexto del negocio. Devuelve el texto."""
    try:
        return cliente.models.generate_content(
            model="gemini-2.5-flash",
            contents=instrucciones(negocio) + "\n\nConsulta del cliente: " + texto,
        ).text
    except Exception as e:
        print("ERROR:", e)
        return "Perdon, tuve un problemita. Proba de nuevo en un rato."


# ------------------------------------------------------------------
# 3) Webhook de Twilio (numero de prueba compartido)
# ------------------------------------------------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    pregunta = request.form.get("Body", "")
    para = request.form.get("To", "")
    twiml = MessagingResponse()

    negocio = Negocio.query.filter_by(whatsapp_to=para).first()
    if not negocio:
        negocio = Negocio.query.filter_by(whatsapp_to=NUMERO_SANDBOX).first()
    if not negocio:
        twiml.message("Este numero todavia no esta conectado a ningun negocio.")
        return str(twiml)

    twiml.message(responder_ia(negocio, pregunta))
    return str(twiml)


# ------------------------------------------------------------------
# 3b) Webhook de WhatsApp Cloud API (Meta) - numero propio por negocio
# ------------------------------------------------------------------
@app.route("/meta", methods=["GET"])
def meta_verificar():
    # Meta valida el webhook con este "apreton de manos"
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "token invalido", 403


@app.route("/meta", methods=["POST"])
def meta_mensaje():
    data = request.get_json(silent=True) or {}
    try:
        value = data["entry"][0]["changes"][0]["value"]
        phone_id = str(value["metadata"]["phone_number_id"])
        msg = value["messages"][0]
        de = msg["from"]
        texto = msg.get("text", {}).get("body", "")
    except (KeyError, IndexError, TypeError):
        return "ok", 200  # estados de entrega u otros eventos: ignorar

    negocio = Negocio.query.filter_by(wa_phone_id=phone_id).first()
    if not negocio:
        return "ok", 200

    enviar_wa(phone_id, de, responder_ia(negocio, texto))
    return "ok", 200


# ------------------------------------------------------------------
# 4) Registro de un negocio nuevo
# ------------------------------------------------------------------
@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        clave = request.form.get("clave", "")
        productos = request.form.get("productos", "").strip()

        if not nombre or not email or not clave:
            flash("Completa nombre, email y contrasena.")
            return redirect(url_for("registro"))
        if Negocio.query.filter_by(email=email).first():
            flash("Ese email ya esta registrado. Entra con tu cuenta.")
            return redirect(url_for("entrar"))

        n = Negocio(
            nombre=nombre,
            email=email,
            clave_hash=generate_password_hash(clave),
            productos=productos,
        )
        db.session.add(n)
        db.session.commit()
        session["negocio_id"] = n.id
        return redirect(url_for("panel"))
    return render_template("registro.html")


# ------------------------------------------------------------------
# 5) Iniciar sesion / cerrar sesion
# ------------------------------------------------------------------
@app.route("/entrar", methods=["GET", "POST"])
def entrar():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        clave = request.form.get("clave", "")
        n = Negocio.query.filter_by(email=email).first()
        if n and check_password_hash(n.clave_hash, clave):
            session["negocio_id"] = n.id
            return redirect(url_for("panel"))
        flash("Email o contrasena incorrectos.")
        return redirect(url_for("entrar"))
    return render_template("entrar.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("home"))


# ------------------------------------------------------------------
# 6) Panel: el negocio edita sus productos y conecta su numero
# ------------------------------------------------------------------
@app.route("/panel", methods=["GET", "POST"])
def panel():
    nid = session.get("negocio_id")
    if not nid:
        return redirect(url_for("entrar"))
    n = db.session.get(Negocio, nid)
    if not n:
        session.clear()
        return redirect(url_for("entrar"))

    if request.method == "POST":
        n.productos = request.form.get("productos", "").strip()
        db.session.commit()
        flash("Guardado. Tu empleado ya usa la lista nueva.")
        return redirect(url_for("panel"))
    return render_template("panel.html", n=n, sandbox=NUMERO_SANDBOX)


@app.route("/panel/numero", methods=["POST"])
def panel_numero():
    nid = session.get("negocio_id")
    if not nid:
        return redirect(url_for("entrar"))
    n = db.session.get(Negocio, nid)
    if n:
        n.wa_phone_id = (request.form.get("wa_phone_id", "").strip() or None)
        db.session.commit()
        flash("Guardado. Avisanos para terminar de activar tu numero.")
    return redirect(url_for("panel"))


# ------------------------------------------------------------------
# 7) Landing y archivos
# ------------------------------------------------------------------
@app.route("/")
def home():
    return send_from_directory(CARPETA, "index.html")


@app.route("/logo.svg")
def logo():
    return send_from_directory(CARPETA, "logo.svg")


# ------------------------------------------------------------------
# 8) Despertador: el server se pinga a si mismo para no dormirse
# ------------------------------------------------------------------
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()


def mantener_despierto():
    while True:
        time.sleep(600)  # cada 10 minutos
        try:
            urllib.request.urlopen(SELF_URL, timeout=20)
        except Exception as e:
            print("keepalive:", e)


if SELF_URL:
    threading.Thread(target=mantener_despierto, daemon=True).start()


# ------------------------------------------------------------------
# 9) Crear las tablas + un negocio demo al arrancar
# ------------------------------------------------------------------
def iniciar():
    with app.app_context():
        db.create_all()
        # si la tabla ya existia, agrega la columna nueva (solo Postgres)
        if db.engine.url.get_backend_name().startswith("postgres"):
            try:
                from sqlalchemy import text
                db.session.execute(text(
                    "ALTER TABLE negocio ADD COLUMN IF NOT EXISTS wa_phone_id VARCHAR(40)"))
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print("migracion:", e)
        # negocio demo para que el sandbox siga respondiendo
        if not Negocio.query.filter_by(whatsapp_to=NUMERO_SANDBOX).first():
            try:
                with open(os.path.join(CARPETA, "productos.txt"), "r", encoding="utf-8-sig") as f:
                    prod = f.read()
            except FileNotFoundError:
                prod = ""
            demo = Negocio(
                nombre="Distribuidora Demo",
                email="demo@empleadodigital.app",
                clave_hash=generate_password_hash("demo1234"),
                whatsapp_to=NUMERO_SANDBOX,
                productos=prod,
            )
            db.session.add(demo)
            db.session.commit()


iniciar()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
