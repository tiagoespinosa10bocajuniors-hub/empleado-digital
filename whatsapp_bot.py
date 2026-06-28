# whatsapp_bot.py - Empleado Digital (MOTOR multi-negocio)
# Un solo servidor que atiende a muchos negocios.
# - Cada negocio se registra, entra y carga SUS productos.
# - Cuando llega un WhatsApp, busca de que negocio es y responde con SU lista.

import os
import datetime
import threading
import time
import urllib.request
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

# La base de datos viene de DATABASE_URL (Postgres en la nube, no se borra).
# Si no esta, usa un archivo local (solo para probar en tu compu).
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


# Cada fila = un negocio.
class Negocio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    clave_hash = db.Column(db.String(255), nullable=False)
    whatsapp_to = db.Column(db.String(40))
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


# ------------------------------------------------------------------
# 3) Webhook de WhatsApp: busca el negocio y responde con SU lista
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

    try:
        respuesta = cliente.models.generate_content(
            model="gemini-2.5-flash",
            contents=instrucciones(negocio) + "\n\nConsulta del cliente: " + pregunta,
        )
        twiml.message(respuesta.text)
    except Exception as e:
        twiml.message("Perdon, tuve un problemita. Proba de nuevo en un rato.")
        print("ERROR:", e)
    return str(twiml)


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
# 6) Panel: el negocio edita sus productos
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
# 8) Crear las tablas + un negocio demo al arrancar
# ------------------------------------------------------------------
def iniciar():
    with app.app_context():
        db.create_all()
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

# ------------------------------------------------------------------
# 9) Despertador: el server se pinga a si mismo para no dormirse
#    (Render pone RENDER_EXTERNAL_URL solo en la nube)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
