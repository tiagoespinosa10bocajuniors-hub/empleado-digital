# whatsapp_bot.py - Empleado Digital (MOTOR multi-negocio)
# Un solo servidor que atiende a muchos negocios.
# - Cada negocio se registra, entra y carga SUS productos.
# - Cuando llega un WhatsApp, busca de que negocio es y responde con SU lista.
# - Guarda los mensajes que entran (bandeja) y TOMA PEDIDOS (primera accion del bot).
# - Maneja CLIENTES con cuenta corriente (conciliacion: quien debe y cuanto).

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
from google.genai import types
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
# Modo prueba: si WA_TEST_FROM tiene numeros, el bot SOLO responde a esos (por Meta).
WA_TEST_FROM = os.environ.get("WA_TEST_FROM", "").strip()


def permitido(de):
    """En modo prueba, solo responde a los numeros autorizados (ignora al resto)."""
    if not WA_TEST_FROM:
        return True
    autorizados = ["".join(c for c in n if c.isdigit()) for n in WA_TEST_FROM.split(",")]
    return "".join(c for c in de if c.isdigit()) in autorizados


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


# ------------------------------------------------------------------
# 3) Modelos (cada tabla de la base de datos)
# ------------------------------------------------------------------
class Negocio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    clave_hash = db.Column(db.String(255), nullable=False)
    whatsapp_to = db.Column(db.String(40))          # numero de Twilio (sandbox)
    wa_phone_id = db.Column(db.String(40))          # ID del numero propio (Meta)
    productos = db.Column(db.Text, default="")
    creado = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Cliente(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    negocio_id = db.Column(db.Integer, db.ForeignKey("negocio.id"), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    telefono = db.Column(db.String(40), default="")
    notas = db.Column(db.Text, default="")
    creado = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    movimientos = db.relationship("Movimiento", backref="cliente", lazy=True)

    def saldo(self):
        """Positivo = el cliente te debe. Negativo = tiene saldo a favor."""
        s = 0.0
        for m in self.movimientos:
            if m.tipo == "cargo":
                s += m.monto
            else:
                s -= m.monto
        return s


class Movimiento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("cliente.id"), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)   # 'cargo' (compra) o 'pago'
    monto = db.Column(db.Float, nullable=False, default=0.0)
    detalle = db.Column(db.String(200), default="")
    fecha = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Mensaje(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    negocio_id = db.Column(db.Integer, db.ForeignKey("negocio.id"), nullable=False)
    de = db.Column(db.String(60), default="")
    texto = db.Column(db.Text, default="")
    fecha = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    leido = db.Column(db.Boolean, default=False)


class Pedido(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    negocio_id = db.Column(db.Integer, db.ForeignKey("negocio.id"), nullable=False)
    de = db.Column(db.String(60), default="")
    detalle = db.Column(db.Text, default="")
    total = db.Column(db.Float)                       # puede venir vacio
    estado = db.Column(db.String(20), default="nuevo")  # nuevo / entregado
    fecha = db.Column(db.DateTime, default=datetime.datetime.utcnow)


def log_mensaje(negocio, de, texto):
    """Guarda en la bandeja cada mensaje que entra (sin romper la respuesta)."""
    try:
        db.session.add(Mensaje(negocio_id=negocio.id, de=de or "", texto=texto or ""))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("log msg:", e)


# ------------------------------------------------------------------
# 4) Inteligencia: responde, toma pedidos y lee fotos
# ------------------------------------------------------------------
def instrucciones(negocio):
    return f"""Sos el empleado de atencion al cliente de "{negocio.nombre}", atendiendo por WhatsApp.
Respondes en espanol rioplatense, amable y al grano (mensajes cortos para chat).
Solo podes usar la informacion de esta lista de productos:

{negocio.productos or '(todavia no cargaron productos)'}

Reglas:
- Si el producto esta en la lista, deci el precio y si hay stock.
- Si no esta, deci que no lo manejas y ofrece uno parecido si existe.
- Nunca inventes precios ni productos.
- Cuando el cliente CONFIRME un pedido (te diga que si, dale, anotamelo), agrega al FINAL,
  en una linea NUEVA y aparte, una linea EXACTA asi:
  #PEDIDO# detalle: <productos y cantidades> | total: <monto en numeros>
  Si el cliente todavia NO confirmo, NO pongas esa linea. Esa linea es solo para el sistema."""


def responder_ia(negocio, texto):
    try:
        return cliente.models.generate_content(
            model="gemini-2.5-flash",
            contents=instrucciones(negocio) + "\n\nConsulta del cliente: " + texto,
        ).text
    except Exception as e:
        print("ERROR:", e)
        return "Perdon, tuve un problemita. Proba de nuevo en un rato."


def extraer_pedido(negocio, de, texto):
    """Si la IA marco un pedido confirmado, lo guarda y limpia la respuesta al cliente."""
    if not texto or "#PEDIDO#" not in texto:
        return texto
    limpio = []
    detalle = ""
    total = None
    for linea in texto.splitlines():
        if "#PEDIDO#" in linea:
            cuerpo = linea.split("#PEDIDO#", 1)[1].strip()
            d = cuerpo
            if "total:" in cuerpo.lower():
                idx = cuerpo.lower().rfind("total:")
                d = cuerpo[:idx]
                t = cuerpo[idx + len("total:"):]
                num = "".join(ch for ch in t if ch.isdigit() or ch == ".")
                try:
                    total = float(num) if num else None
                except ValueError:
                    total = None
            if "detalle:" in d.lower():
                d = d[d.lower().find("detalle:") + len("detalle:"):]
            detalle = d.strip(" |")
        else:
            limpio.append(linea)
    try:
        db.session.add(Pedido(negocio_id=negocio.id, de=de or "",
                              detalle=detalle, total=total))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("pedido:", e)
    return "\n".join(limpio).strip()


def leer_lista_foto(img_bytes, mime):
    """Le pasa una foto de la lista de precios a la IA y devuelve texto plano."""
    prompt = ("Esta es la foto de una lista de precios de un negocio. "
              "Devolve SOLO la lista en texto plano, un producto por linea, "
              "con formato 'Producto - $precio'. Si ves el stock, agregalo. "
              "No agregues comentarios, titulos ni explicaciones.")
    parte = types.Part.from_bytes(data=img_bytes, mime_type=mime)
    resp = cliente.models.generate_content(
        model="gemini-2.5-flash",
        contents=[parte, prompt],
    )
    return (resp.text or "").strip()


# ------------------------------------------------------------------
# 5) Webhook de Twilio (numero de prueba compartido)
# ------------------------------------------------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    pregunta = request.form.get("Body", "")
    para = request.form.get("To", "")
    de = request.form.get("From", "")
    twiml = MessagingResponse()

    negocio = Negocio.query.filter_by(whatsapp_to=para).first()
    if not negocio:
        negocio = Negocio.query.filter_by(whatsapp_to=NUMERO_SANDBOX).first()
    if not negocio:
        twiml.message("Este numero todavia no esta conectado a ningun negocio.")
        return str(twiml)

    log_mensaje(negocio, de, pregunta)
    respuesta = responder_ia(negocio, pregunta)
    respuesta = extraer_pedido(negocio, de, respuesta)
    twiml.message(respuesta)
    return str(twiml)


# ------------------------------------------------------------------
# 5b) Webhook de WhatsApp Cloud API (Meta) - numero propio por negocio
# ------------------------------------------------------------------
@app.route("/meta", methods=["GET"])
def meta_verificar():
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
        return "ok", 200

    if not permitido(de):
        return "ok", 200

    negocio = Negocio.query.filter_by(wa_phone_id=phone_id).first()
    if not negocio:
        return "ok", 200

    log_mensaje(negocio, de, texto)
    respuesta = responder_ia(negocio, texto)
    respuesta = extraer_pedido(negocio, de, respuesta)
    enviar_wa(phone_id, de, respuesta)
    return "ok", 200


# ------------------------------------------------------------------
# 6) Registro / entrar / salir
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


def negocio_actual():
    nid = session.get("negocio_id")
    if not nid:
        return None
    return db.session.get(Negocio, nid)


# ------------------------------------------------------------------
# 7) Panel principal (Inicio, Productos, Clientes, Pedidos, Mi numero)
# ------------------------------------------------------------------
@app.route("/panel", methods=["GET", "POST"])
def panel():
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))

    if request.method == "POST":
        n.productos = request.form.get("productos", "").strip()
        db.session.commit()
        flash("Guardado. Tu empleado ya usa la lista nueva.")
        return redirect(url_for("panel") + "#productos")

    clientes = Cliente.query.filter_by(negocio_id=n.id).order_by(Cliente.nombre).all()
    total_cobrar = sum(c.saldo() for c in clientes if c.saldo() > 0)
    mensajes = (Mensaje.query.filter_by(negocio_id=n.id)
                .order_by(Mensaje.fecha.desc()).limit(50).all())
    nuevos = Mensaje.query.filter_by(negocio_id=n.id, leido=False).count()
    pedidos = (Pedido.query.filter_by(negocio_id=n.id)
               .order_by(Pedido.fecha.desc()).limit(50).all())
    pedidos_nuevos = Pedido.query.filter_by(negocio_id=n.id, estado="nuevo").count()
    return render_template("panel.html", n=n, sandbox=NUMERO_SANDBOX,
                           clientes=clientes, total_cobrar=total_cobrar,
                           mensajes=mensajes, nuevos=nuevos,
                           pedidos=pedidos, pedidos_nuevos=pedidos_nuevos)


# --- Clientes ---
@app.route("/clientes/nuevo", methods=["POST"])
def clientes_nuevo():
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    nombre = request.form.get("nombre", "").strip()
    if nombre:
        c = Cliente(negocio_id=n.id, nombre=nombre,
                    telefono=request.form.get("telefono", "").strip(),
                    notas=request.form.get("notas", "").strip())
        db.session.add(c)
        db.session.commit()
        flash("Cliente agregado.")
    return redirect(url_for("panel") + "#clientes")


@app.route("/cliente/<int:cid>")
def cliente_detalle(cid):
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    c = db.session.get(Cliente, cid)
    if not c or c.negocio_id != n.id:
        return redirect(url_for("panel"))
    movs = (Movimiento.query.filter_by(cliente_id=c.id)
            .order_by(Movimiento.fecha.desc()).all())
    return render_template("cliente.html", n=n, c=c, movs=movs, saldo=c.saldo())


@app.route("/cliente/<int:cid>/mov", methods=["POST"])
def cliente_mov(cid):
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    c = db.session.get(Cliente, cid)
    if not c or c.negocio_id != n.id:
        return redirect(url_for("panel"))
    tipo = "pago" if request.form.get("tipo") == "pago" else "cargo"
    try:
        monto = float(request.form.get("monto", "0").replace(",", "."))
    except ValueError:
        monto = 0.0
    if monto > 0:
        db.session.add(Movimiento(cliente_id=c.id, tipo=tipo, monto=monto,
                                  detalle=request.form.get("detalle", "").strip()))
        db.session.commit()
        flash("Movimiento registrado.")
    return redirect(url_for("cliente_detalle", cid=c.id))


# --- Pedidos (bandeja) ---
@app.route("/pedidos/vistos", methods=["POST"])
def pedidos_vistos():
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    Mensaje.query.filter_by(negocio_id=n.id, leido=False).update({"leido": True})
    db.session.commit()
    return redirect(url_for("panel") + "#pedidos")


@app.route("/pedido/<int:pid>/entregado", methods=["POST"])
def pedido_entregado(pid):
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    p = db.session.get(Pedido, pid)
    if p and p.negocio_id == n.id:
        p.estado = "entregado"
        db.session.commit()
    return redirect(url_for("panel") + "#pedidos")


# --- Productos por foto ---
@app.route("/panel/foto", methods=["POST"])
def panel_foto():
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    f = request.files.get("foto")
    if f and f.filename:
        try:
            raw = f.read()
            mime = f.mimetype or "image/jpeg"
            texto = leer_lista_foto(raw, mime)
            if texto:
                if n.productos and n.productos.strip():
                    n.productos = n.productos.strip() + "\n" + texto
                else:
                    n.productos = texto
                db.session.commit()
                flash("Lei la foto y agregue los productos. Revisalos abajo y guarda.")
            else:
                flash("No pude leer la foto. Proba con una mas clara.")
        except Exception as e:
            print("foto:", e)
            flash("No pude leer la foto. Proba de nuevo en un rato.")
    return redirect(url_for("panel") + "#productos")


# --- Mi numero propio (Meta) ---
@app.route("/panel/numero", methods=["POST"])
def panel_numero():
    n = negocio_actual()
    if not n:
        return redirect(url_for("entrar"))
    n.wa_phone_id = (request.form.get("wa_phone_id", "").strip() or None)
    db.session.commit()
    flash("Guardado. Avisanos para terminar de activar tu numero.")
    return redirect(url_for("panel") + "#numero")


# ------------------------------------------------------------------
# 8) Landing y archivos
# ------------------------------------------------------------------
@app.route("/")
def home():
    return send_from_directory(CARPETA, "index.html")


@app.route("/logo.svg")
def logo():
    return send_from_directory(CARPETA, "logo.svg")


@app.route("/precios")
def precios():
    return send_from_directory(CARPETA, "precios.html")


# ------------------------------------------------------------------
# 9) Despertador: el server se pinga a si mismo para no dormirse
# ------------------------------------------------------------------
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()


def mantener_despierto():
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(SELF_URL, timeout=20)
        except Exception as e:
            print("keepalive:", e)


if SELF_URL:
    threading.Thread(target=mantener_despierto, daemon=True).start()


# ------------------------------------------------------------------
# 10) Crear las tablas + un negocio demo al arrancar
# ------------------------------------------------------------------
def iniciar():
    with app.app_context():
        db.create_all()
        if db.engine.url.get_backend_name().startswith("postgres"):
            try:
                from sqlalchemy import text
                db.session.execute(text(
                    "ALTER TABLE negocio ADD COLUMN IF NOT EXISTS wa_phone_id VARCHAR(40)"))
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print("migracion:", e)
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
