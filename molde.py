# molde.py - EL MOLDE: un asistente de IA moldeable y agnostico del modelo.
# Idea: un MISMO motor sirve para CUALQUIER asistente. Cambias el "molde"
# (la config) y sale otro asistente. Empleado Digital es solo el molde #1.
#
# Tiene 3 piezas:
#   1) MOTORES   -> un adaptador por modelo (Gemini / Claude / GPT). Agnostico.
#   2) ACCIONES  -> el registro de conectores (lo que el asistente PUEDE hacer).
#   3) MOLDE     -> la config con los huecos: persona, conocimiento, acciones,
#                   permisos, modelo, canal, reglas.
# El modelo pide una accion escribiendo una linea  ##ACCION nombre {args}
# (truco portable: anda con CUALQUIER IA, sin depender de su API de "tools").

import os
import json
import re


# ------------------------------------------------------------------
# 1) MOTORES: un adaptador por proveedor. Mismo "enchufe" para todos.
# ------------------------------------------------------------------
def _motor_gemini(system, user, modelo="gemini-2.5-flash"):
    from google import genai
    cli = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", "").strip())
    r = cli.models.generate_content(model=modelo, contents=system + "\n\n" + user)
    return r.text or ""


def _motor_claude(system, user, modelo="claude-haiku-4-5-20251001"):
    import anthropic
    cli = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    r = cli.messages.create(model=modelo, max_tokens=1024, system=system,
                            messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if getattr(b, "type", None) == "text")


def _motor_gpt(system, user, modelo="gpt-4o-mini"):
    from openai import OpenAI
    cli = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())
    r = cli.chat.completions.create(model=modelo, messages=[
        {"role": "system", "content": system},
        {"role": "user", "content": user}])
    return r.choices[0].message.content or ""


MOTORES = {"gemini": _motor_gemini, "claude": _motor_claude, "gpt": _motor_gpt}


# ------------------------------------------------------------------
# 2) ACCIONES: el registro de conectores. Sumar una accion = una funcion.
#    Cada accion: nombre -> {desc, fn(args, contexto) -> texto}
# ------------------------------------------------------------------
ACCIONES = {}


def accion(nombre, descripcion):
    def deco(fn):
        ACCIONES[nombre] = {"desc": descripcion, "fn": fn}
        return fn
    return deco


@accion("tomar_pedido", 'Anota un pedido confirmado. args: {"detalle","total"}')
def _tomar_pedido(args, ctx):
    ctx.setdefault("pedidos", []).append(args)
    return "pedido anotado"


@accion("consultar_saldo", 'Dice cuanto debe un cliente. args: {"cliente"}')
def _consultar_saldo(args, ctx):
    saldos = ctx.get("saldos", {})
    return f"{args.get('cliente')} debe ${saldos.get(args.get('cliente'), 0)}"


@accion("agendar", 'Agenda algo. args: {"que","cuando"}')
def _agendar(args, ctx):
    ctx.setdefault("agenda", []).append(args)
    return "agendado"


# ------------------------------------------------------------------
# 3) EL MOLDE: la config de un asistente (los huecos que rellenas).
# ------------------------------------------------------------------
def molde(nombre, persona, conocimiento="", acciones=None, modelo="gemini",
          permisos=None, canal="whatsapp", reglas=""):
    return {
        "nombre": nombre,
        "persona": persona,              # quien es y como habla
        "conocimiento": conocimiento,    # que sabe
        "acciones": acciones or [],      # que conectores tiene
        "modelo": modelo,                # gemini / claude / gpt
        "permisos": permisos or {},      # rol -> [acciones permitidas]
        "canal": canal,                  # donde vive
        "reglas": reglas,                # que NO puede hacer
    }


# ------------------------------------------------------------------
# 4) EL MOTOR: arma el prompt desde el molde, llama al modelo elegido,
#    ejecuta SOLO las acciones permitidas y limpia la respuesta.
# ------------------------------------------------------------------
def _permitidas(a, rol):
    # si el rol no esta en permisos, por defecto puede usar todas las del molde
    return a["permisos"].get(rol, a["acciones"])


def _system_prompt(a, rol):
    permitidas = _permitidas(a, rol)
    accs = [f"- {n}: {ACCIONES[n]['desc']}"
            for n in a["acciones"] if n in ACCIONES and n in permitidas]
    bloque = ""
    if accs:
        bloque = ("\n\nPodes ejecutar acciones. Para usar una, escribi una linea EXACTA asi:\n"
                  '##ACCION nombre {"arg": "valor"}\n'
                  "Acciones disponibles:\n" + "\n".join(accs))
    return (f'Sos "{a["nombre"]}". {a["persona"]}\n'
            f'{a["reglas"]}\n\n'
            f'Lo que sabes:\n{a["conocimiento"] or "(nada cargado)"}'
            f'{bloque}')


def pensar(a, mensaje, rol="dueno", contexto=None):
    """Le pasa el mensaje al asistente 'a'. Devuelve texto limpio + acciones hechas."""
    contexto = contexto if contexto is not None else {}
    system = _system_prompt(a, rol)
    motor = MOTORES.get(a["modelo"], _motor_gemini)
    salida = motor(system, mensaje)

    permitidas = _permitidas(a, rol)
    limpio, resultados = [], []
    for linea in salida.splitlines():
        m = re.search(r"##ACCION\s+(\w+)\s*(\{.*\})?", linea)
        if m:
            nombre = m.group(1)
            try:
                args = json.loads(m.group(2) or "{}")
            except Exception:
                args = {}
            if nombre in ACCIONES and nombre in permitidas:
                resultados.append(ACCIONES[nombre]["fn"](args, contexto))
            # si no esta permitida, se ignora (y no se le muestra al cliente)
        else:
            limpio.append(linea)
    return {"texto": "\n".join(limpio).strip(),
            "acciones": resultados, "contexto": contexto}


# ------------------------------------------------------------------
# 5) TEMPLATES: moldes ya rellenos. (Empleado Digital = molde #1)
# ------------------------------------------------------------------
EMPLEADO_DIGITAL = molde(
    nombre="Empleado de la Distribuidora",
    persona="Atendes clientes por WhatsApp, amable y al grano, en espanol rioplatense.",
    conocimiento="Coca 2.25L - $2500\nFernet 750 - $6000",
    acciones=["tomar_pedido", "consultar_saldo"],
    modelo="gemini",
    permisos={"dueno": ["tomar_pedido", "consultar_saldo"],
              "empleado": ["tomar_pedido"]},
    reglas="Nunca inventes precios. No des descuentos.",
)

ASISTENTE_PERSONAL = molde(
    nombre="Tu asistente personal",
    persona="Ayudas a organizar el dia, recordar cosas y agendar.",
    acciones=["agendar"],
    modelo="gemini",
)

PROFE_INGLES = molde(
    nombre="Profe de ingles",
    persona="Ensenas ingles con paciencia, corregis y das ejemplos cortos.",
    modelo="claude",
)
