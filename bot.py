import os
import json
import re
import traceback
import unicodedata
import requests
import anthropic
import sqlite3
import base64
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from dashboard import setup_dashboard, dashboard_router

load_dotenv()

WHATSAPP_TOKEN    = os.environ["WHATSAPP_TOKEN"]
PHONE_NUMBER_ID   = os.environ["PHONE_NUMBER_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
VERIFY_TOKEN      = os.environ["VERIFY_TOKEN"]
ALLOWED_NUMBERS   = set(filter(None, os.getenv("ALLOWED_NUMBERS", "").split(",")))

BASE_DIR   = "/home/ubuntu/asistente-lito"
DB_PATH    = os.path.join(BASE_DIR, "recaudaciones.db")
LOG_PATH   = os.path.join(BASE_DIR, "bot.log")
VERSION    = "2.6.0"
START_TIME = time.time()

DIAS_ES          = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
INITIAL_EMPRESAS = ["PUL", "Lumin Civil", "Lumin Energia", "MIDES", "Hospital"]
UY_TZ            = ZoneInfo("America/Montevideo")
RESPUESTAS_AFIRMATIVAS = {"SI", "SÍ", "DALE", "OK", "YES", "CONFIRMAR"}

VEHICULOS = {
    "1": "Terminal",
    "2": "Plaza",
    "3": "Sanatorio",
    "4": "Particular",
    "terminal": "Terminal",
    "plaza": "Plaza",
    "sanatorio": "Sanatorio",
    "particular": "Particular",
}
VEHICLE_TIMEOUT = 600  # 10 minutes
CHOFER_TIMEOUT  = 600  # 10 minutes

# sender → {parsed, derived, existing_id, existing_total}
PENDING_REPLACEMENTS: dict = {}
# sender → {"nombre": str}  (waiting for RUT input)
PENDING_EMPRESA: dict = {}
# sender → {parsed, derived, ts}  (waiting for vehicle selection)
PENDING_VEHICLE: dict = {}
# sender → {parsed, fecha_str, matches, ts}  (waiting for chofer disambiguation)
PENDING_CHOFER: dict = {}
# sender → {parsed, fecha_str, ts}  (waiting for user to provide unreadable chofer name)
PENDING_CHOFER_NAME: dict = {}
# sender → {parsed, fecha_str, nombre, ts}  (waiting for user to confirm OCR-read chofer name)
PENDING_CHOFER_OCR: dict = {}
# sender → {parsed, fecha_str, nombre, ts}  (waiting for SI/NO to register a new chofer)
PENDING_CHOFER_CONFIRM: dict = {}
# sender → {parsed, derived, ts}  (waiting for pre-save confirmation / field corrections)
PENDING_CONFIRM: dict = {}

BOT_TOOLS = [
    {
        "name": "agregar_chofer",
        "description": (
            "Registra un nuevo chofer en el sistema o reactiva uno inactivo. "
            "Usá esta herramienta cuando el usuario quiera agregar, dar de alta "
            "o incorporar a un nuevo conductor o chofer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del chofer"}
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "dar_baja_chofer",
        "description": (
            "Da de baja a un chofer activo. Sus registros históricos se mantienen. "
            "Usá esta herramienta cuando el usuario indique que un chofer ya no trabaja, "
            "se fue, fue despedido, o quiere desactivarlo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del chofer a dar de baja"}
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "listar_choferes",
        "description": (
            "Lista todos los choferes activos del sistema. "
            "Usá esta herramienta cuando el usuario pregunte quiénes son los choferes, "
            "quiera ver la lista de conductores activos, o pregunte cuántos choferes hay."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agregar_empresa",
        "description": (
            "Registra una nueva empresa de recaudación o reactiva una inactiva. "
            "Usá cuando el usuario quiera agregar, dar de alta o incorporar una empresa "
            "(ej. nueva empresa de transporte, servicios, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la empresa"}
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "dar_baja_empresa",
        "description": (
            "Da de baja a una empresa activa. Los registros históricos se mantienen. "
            "Usá cuando el usuario indique que una empresa ya no opera, fue cerrada, "
            "o quiere desactivarla."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la empresa a dar de baja"}
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "actualizar_rut_empresa",
        "description": (
            "Actualiza el RUT de una empresa registrada. "
            "Usá cuando el usuario quiera registrar o corregir el número de RUT de una empresa."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la empresa"},
                "rut":    {"type": "string", "description": "Nuevo RUT de la empresa"},
            },
            "required": ["nombre", "rut"],
        },
    },
    {
        "name": "listar_empresas",
        "description": (
            "Lista todas las empresas registradas en el sistema. "
            "Usá cuando el usuario pregunte qué empresas hay, "
            "o quiera ver el listado de empresas activas."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

PERSONA_PROMPT = """Sos Lito, el asistente secretarial de Jorge. Trabajás para su empresa de transporte uruguaya y tu función es ayudarlo a registrar y consultar las recaudaciones diarias.

IDENTIDAD Y TONO:
- Tratá a Jorge siempre de "usted" y usá su nombre con naturalidad
- Sé cálido y profesional, como un secretario de confianza — nunca como un robot
- Al saludar usá siempre el saludo indicado en el contexto (campo "Saludo actual")
- Si Jorge te saluda o manda un mensaje casual al arrancar el día, respondé el saludo antes de cualquier dato
- Usá frases cortas y directas — una o dos líneas por dato, sin explicaciones de más

REGLAS DE DATOS:
- Reportá SOLO los datos que están en la base de datos. No proyectés, no inventés, no estimés
- Si un monto parece inusual (muy alto o muy bajo respecto al historial reciente), mencionálo sin alarmar
- Cuando se guarda una recaudación, siempre confirmá todos los datos extraídos para que Jorge los pueda verificar
- Si te preguntan algo fuera del tema de recaudaciones, decíle amablemente que solo podés ayudar con eso

IDIOMA: español rioplatense formal. Moneda: pesos uruguayos ($)."""


# ── helpers ───────────────────────────────────────────────────────────────────

def _empresa_to_key(nombre: str) -> str:
    return "empresa_" + nombre.lower().replace(" ", "_")


def _build_image_prompt() -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nombre FROM empresas WHERE activo=1 ORDER BY id")
    empresas = [row[0] for row in c.fetchall()]
    conn.close()

    json_fields = {
        "fecha":           "DD/MM/YYYY",
        "chofer":          "nombre del chofer",
        "total_recaudado": 0.0,
        "comision_30":     0.0,
        "total_pos":       0.0,
        "total_fiado":     0.0,
    }
    for e in empresas:
        json_fields[_empresa_to_key(e)] = 0.0
    json_fields["otros_combustible"] = 0.0
    json_fields["efectivo_empresa"]  = 0.0
    json_fields["monto_dudoso"]      = False

    json_str = json.dumps(json_fields, ensure_ascii=False, indent=2)
    return (
        "Sos un asistente financiero. Analizá esta imagen de un formulario de recaudación "
        "y extraé todos los campos visibles.\n\n"
        "ESTRUCTURA DE LA PLANILLA: tiene una tabla con dos columnas — etiquetas a la izquierda "
        "y valores a la derecha dentro de celdas delimitadas. "
        "Extraé ÚNICAMENTE los valores que están dentro de las celdas de la tabla. "
        "Ignorá completamente cualquier texto, número o anotación que esté fuera de la tabla "
        "(notas al margen, cálculos debajo, texto en el borde del papel, etc.). "
        "En esta planilla el único texto fuera de la tabla que puede aparecer son cálculos manuales "
        "como 'Empresa - 1359 / Botella Gomería - 200 / Total - 1159'. Ignoralos.\n\n"
        "IMPORTANTE sobre los montos: en Uruguay el punto es separador de miles y la coma es decimal.\n"
        'Ejemplos de conversión: "6.000" = 6000, "1.500" = 1500, "12.350,50" = 12350.5, "500" = 500.\n'
        "Convertí todos los montos a números sin separadores de miles.\n\n"
        "IMPORTANTE sobre números manuscritos:\n"
        "- El dígito 3 escrito a mano frecuentemente parece un 7. "
        "Para validar el total recaudado, sumá los componentes: "
        "efectivo_empresa + comision_30 + total_pos + total_fiado + (empresa_pul + empresa_lumin_civil "
        "+ empresa_lumin_energia + empresa_mides + empresa_hospital) + otros_combustible. "
        "Si la suma se aproxima a uno de los dos valores posibles, usá ese. "
        "Si no podés validar, devolvé el número que veas pero marcá monto_dudoso: true en el JSON.\n"
        "- El año: '26' siempre es 2026, '25' siempre es 2025.\n\n"
        "IMPORTANTE: el campo 'efectivo_empresa' debe leerse directamente del campo 'EFECTIVO EMPRESA' "
        "de la imagen. No lo calcules ni lo deduzcas — copiá el valor tal como aparece en la planilla.\n\n"
        "IMPORTANTE — últimas dos filas de la tabla: las últimas dos filas son siempre:\n"
        "- EFECTIVO EMPRESA: contiene un número (ej: 1359, 905, 4750)\n"
        "- CHOFER: contiene un nombre de persona (ej: Ale, Facundo, Cristian)\n"
        "Nunca confundas estos dos campos. EFECTIVO EMPRESA siempre es un número. "
        "CHOFER siempre es un nombre. Si ves un número en las últimas filas y un nombre debajo, "
        "el número es efectivo_empresa y el nombre es chofer.\n\n"
        "IMPORTANTE: si el nombre del chofer no es legible con certeza, devolvé chofer: null en lugar "
        "de intentar adivinar. No devuelvas el texto de la etiqueta ('CHOFER') como valor.\n\n"
        "Respondé SOLO con un JSON con esta estructura exacta (usá null para campos vacíos o ilegibles):\n"
        f"{json_str}\n\n"
        "Si la imagen no es un formulario de recaudación o no podés extraer datos básicos respondé:\n"
        '{"error": "No se pudieron extraer los datos"}'
    )


def get_saludo() -> str:
    hora = datetime.now(UY_TZ).hour
    if 6 <= hora < 12:
        return "Buenos días"
    elif 12 <= hora < 20:
        return "Buenas tardes"
    else:
        return "Buenas noches"


# ── logging ───────────────────────────────────────────────────────────────────

def _setup_logging():
    logger = logging.getLogger("asistente-lito")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()
app = FastAPI()
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── DB schema & migration ─────────────────────────────────────────────────────

EXPECTED_COLUMNS = {
    "fecha":               "TEXT",
    "chofer":              "TEXT",
    "total_recaudado":     "REAL",
    "comision_30":         "REAL",
    "total_pos":           "REAL",
    "total_fiado":         "REAL",
    "otros_combustible":   "REAL",
    "efectivo_empresa":    "REAL",
    "dia_semana":          "TEXT",
    "semana_numero":       "INTEGER",
    "mes":                 "INTEGER",
    "anio":                "INTEGER",
    "efectivo_neto":       "REAL",
    "porcentaje_digital":  "REAL",
    "creado_en":           "TEXT",
    # legacy
    "monto":               "REAL",
    "descripcion":         "TEXT",
    "vehiculo":            "TEXT",
}


def _migrate(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("PRAGMA table_info(recaudaciones)")
    existing = {row[1] for row in c.fetchall()}
    for col, col_type in EXPECTED_COLUMNS.items():
        if col not in existing:
            c.execute(f"ALTER TABLE recaudaciones ADD COLUMN {col} {col_type}")
            log.info(f"Migración: columna '{col}' agregada")
    conn.commit()


def _setup_choferes(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS choferes (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre         TEXT UNIQUE,
        activo         INTEGER DEFAULT 1,
        creado_en      TEXT,
        desactivado_en TEXT
    )""")
    conn.commit()
    c.execute("SELECT DISTINCT chofer FROM recaudaciones WHERE chofer IS NOT NULL")
    now = datetime.now().isoformat()
    for (nombre,) in c.fetchall():
        c.execute(
            "INSERT OR IGNORE INTO choferes (nombre, activo, creado_en) VALUES (?,1,?)",
            (nombre, now)
        )
    conn.commit()


def _setup_empresas(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS empresas (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre         TEXT UNIQUE,
        rut            TEXT,
        activo         INTEGER DEFAULT 1,
        creado_en      TEXT,
        desactivado_en TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS recaudacion_empresas (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        recaudacion_id INTEGER NOT NULL REFERENCES recaudaciones(id),
        empresa_id     INTEGER NOT NULL REFERENCES empresas(id),
        monto          REAL,
        UNIQUE(recaudacion_id, empresa_id)
    )""")
    conn.commit()
    now = datetime.now().isoformat()
    for nombre in INITIAL_EMPRESAS:
        c.execute(
            "INSERT OR IGNORE INTO empresas (nombre, activo, creado_en) VALUES (?,1,?)",
            (nombre, now)
        )
    conn.commit()


def _migrate_to_empresas(conn: sqlite3.Connection):
    """Move total_pul/lumin_civil/lumin_energia/mides/hospital into recaudacion_empresas."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(recaudaciones)")
    existing_cols = {row[1] for row in c.fetchall()}
    if "total_pul" not in existing_cols:
        return  # Already migrated

    log.info("Iniciando migración de columnas de empresa a recaudacion_empresas...")

    COL_TO_EMPRESA = {
        "total_pul":           "PUL",
        "total_lumin_civil":   "Lumin Civil",
        "total_lumin_energia": "Lumin Energia",
        "total_mides":         "MIDES",
        "total_hospital":      "Hospital",
    }

    empresa_ids = {}
    for col, nombre in COL_TO_EMPRESA.items():
        c.execute("SELECT id FROM empresas WHERE nombre=?", (nombre,))
        row = c.fetchone()
        if row:
            empresa_ids[col] = row[0]

    # Migrate data from old columns into recaudacion_empresas
    c.execute(
        "SELECT id, total_pul, total_lumin_civil, total_lumin_energia, "
        "total_mides, total_hospital FROM recaudaciones"
    )
    records = c.fetchall()
    col_order = list(COL_TO_EMPRESA.keys())
    for rec in records:
        rec_id = rec[0]
        for i, col in enumerate(col_order):
            monto  = rec[i + 1]
            emp_id = empresa_ids.get(col)
            if emp_id and monto is not None:
                c.execute(
                    "INSERT OR IGNORE INTO recaudacion_empresas "
                    "(recaudacion_id, empresa_id, monto) VALUES (?,?,?)",
                    (rec_id, emp_id, monto)
                )
    conn.commit()

    # Recreate recaudaciones without the 5 empresa columns (SQLite workaround)
    c.execute("PRAGMA table_info(recaudaciones)")
    all_cols   = [(row[1], row[2], row[3], row[4], row[5]) for row in c.fetchall()]
    EMPRESA_COLS = set(COL_TO_EMPRESA.keys())
    keep = [(n, t, nn, d, pk) for n, t, nn, d, pk in all_cols if n not in EMPRESA_COLS]

    col_defs = []
    for name, col_type, notnull, dflt, pk in keep:
        if pk:
            col_defs.append(f"{name} INTEGER PRIMARY KEY AUTOINCREMENT")
        else:
            defn = f"{name} {col_type or 'TEXT'}"
            if notnull:
                defn += " NOT NULL"
            if dflt is not None:
                defn += f" DEFAULT {dflt}"
            col_defs.append(defn)

    col_names = [col[0] for col in keep]
    cols_str  = ", ".join(col_names)

    c.execute(f"CREATE TABLE recaudaciones_new ({', '.join(col_defs)})")
    c.execute(f"INSERT INTO recaudaciones_new ({cols_str}) SELECT {cols_str} FROM recaudaciones")
    c.execute("DROP TABLE recaudaciones")
    c.execute("ALTER TABLE recaudaciones_new RENAME TO recaudaciones")
    conn.commit()

    log.info(
        f"Migración completada: {len(records)} registros procesados, "
        f"columnas eliminadas: {list(EMPRESA_COLS)}"
    )


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS recaudaciones (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha               TEXT,
        chofer              TEXT,
        vehiculo            TEXT,
        total_recaudado     REAL,
        comision_30         REAL,
        total_pos           REAL,
        total_fiado         REAL,
        otros_combustible   REAL,
        efectivo_empresa    REAL,
        dia_semana          TEXT,
        semana_numero       INTEGER,
        mes                 INTEGER,
        anio                INTEGER,
        efectivo_neto       REAL,
        porcentaje_digital  REAL,
        creado_en           TEXT,
        monto               REAL,
        descripcion         TEXT
    )""")
    conn.commit()
    _migrate(conn)
    _setup_choferes(conn)
    _setup_empresas(conn)
    _migrate_to_empresas(conn)
    conn.close()


init_db()
setup_dashboard(VERSION, START_TIME)
app.include_router(dashboard_router)
log.info(f"Bot iniciado — version {VERSION}")


# ── field derivation ──────────────────────────────────────────────────────────

def _derive_fields(fecha_str: str, parsed: dict) -> dict:
    try:
        d             = datetime.strptime(fecha_str, "%d/%m/%Y")
        dia_semana    = DIAS_ES[d.weekday()]
        semana_numero = d.isocalendar()[1]
        mes           = d.month
        anio          = d.year
    except (ValueError, TypeError):
        dia_semana, semana_numero, mes, anio = None, None, None, None

    tr  = parsed.get("total_recaudado") or 0
    pos = parsed.get("total_pos")       or 0

    efectivo_neto      = round(parsed.get("efectivo_empresa") or 0, 2)
    porcentaje_digital = round((pos / tr) * 100, 2) if tr else None

    return {
        "dia_semana":         dia_semana,
        "semana_numero":      semana_numero,
        "mes":                mes,
        "anio":               anio,
        "efectivo_neto":      efectivo_neto,
        "porcentaje_digital": porcentaje_digital,
    }


# ── stats helper ──────────────────────────────────────────────────────────────

def get_monthly_stats(mes: int, anio: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        "SELECT SUM(total_recaudado), COUNT(*) FROM recaudaciones WHERE mes=? AND anio=?",
        (mes, anio)
    )
    total, count = c.fetchone()
    total = total or 0

    c.execute(
        """SELECT chofer, SUM(total_recaudado), COUNT(*), SUM(efectivo_neto)
           FROM recaudaciones WHERE mes=? AND anio=?
           GROUP BY chofer ORDER BY SUM(total_recaudado) DESC""",
        (mes, anio)
    )
    by_chofer = [
        {"chofer": r[0], "total": round(r[1] or 0, 2), "dias": r[2], "efectivo_neto": round(r[3] or 0, 2)}
        for r in c.fetchall()
    ]

    c.execute(
        """SELECT fecha, dia_semana, SUM(total_recaudado) AS t
           FROM recaudaciones WHERE mes=? AND anio=?
           GROUP BY fecha ORDER BY t DESC LIMIT 1""",
        (mes, anio)
    )
    best     = c.fetchone()
    mejor_dia = {"fecha": best[0], "dia": best[1], "total": round(best[2], 2)} if best else None

    c.execute(
        """SELECT SUM(total_pos), SUM(total_fiado), SUM(efectivo_neto), SUM(otros_combustible)
           FROM recaudaciones WHERE mes=? AND anio=?""",
        (mes, anio)
    )
    r = c.fetchone()
    pagos = {
        "pos":           round(r[0] or 0, 2),
        "fiado":         round(r[1] or 0, 2),
        "efectivo_neto": round(r[2] or 0, 2),
        "combustible":   round(r[3] or 0, 2),
    }

    c.execute(
        """SELECT e.nombre, SUM(re.monto)
           FROM recaudacion_empresas re
           JOIN empresas e ON e.id = re.empresa_id
           JOIN recaudaciones r ON r.id = re.recaudacion_id
           WHERE r.mes=? AND r.anio=?
           GROUP BY e.id, e.nombre
           ORDER BY SUM(re.monto) DESC""",
        (mes, anio)
    )
    by_empresa = [{"empresa": r[0], "total": round(r[1] or 0, 2)} for r in c.fetchall()]

    conn.close()
    return {
        "mes":              mes,
        "anio":             anio,
        "total_recaudado":  round(total, 2),
        "dias_trabajados":  count,
        "promedio_por_dia": round(total / count, 2) if count else 0,
        "mejor_dia":        mejor_dia,
        "by_chofer":        by_chofer,
        "pagos":            pagos,
        "by_empresa":       by_empresa,
    }


# ── WhatsApp ──────────────────────────────────────────────────────────────────

def _mask(number: str) -> str:
    return f"****{number[-4:]}" if len(number) >= 4 else "****"


def send_whatsapp_message(to: str, message: str) -> bool:
    url     = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data    = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        r.raise_for_status()
        log.info(f"Mensaje enviado a {_mask(to)} — HTTP {r.status_code}")
        return True
    except requests.RequestException as e:
        log.error(f"Error enviando mensaje a {_mask(to)}: {e}")
        return False


def send_interactive_buttons(to: str, body: str, buttons: list[dict]) -> bool:
    """buttons: [{"id": "...", "title": "..."}] — max 3, title max 20 chars"""
    url     = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data    = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                for b in buttons
            ]},
        },
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        r.raise_for_status()
        log.info(f"Botones interactivos enviados a {_mask(to)} — HTTP {r.status_code}")
        return True
    except requests.RequestException as e:
        log.error(f"Error enviando botones a {_mask(to)}: {e}")
        send_whatsapp_message(to, body)
        return False


def send_interactive_list(to: str, body: str, button_label: str, sections: list[dict]) -> bool:
    """sections: [{"title": "...", "rows": [{"id":"...","title":"...","description":"..."}]}]"""
    url     = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data    = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_label, "sections": sections},
        },
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        r.raise_for_status()
        log.info(f"Lista interactiva enviada a {_mask(to)} — HTTP {r.status_code}")
        return True
    except requests.RequestException as e:
        log.error(f"Error enviando lista a {_mask(to)}: {e}")
        send_whatsapp_message(to, body)
        return False


# ── Claude ────────────────────────────────────────────────────────────────────

def process_image(image_url: str) -> str:
    img_resp = requests.get(
        image_url,
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        timeout=30
    )
    img_resp.raise_for_status()
    img_b64  = base64.b64encode(img_resp.content).decode()
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": _build_image_prompt()},
            ]
        }]
    )
    return response.content[0].text


def process_text(message_text: str) -> tuple:
    """Returns (text_response, tool_call_dict) — exactly one of them is None."""
    now   = datetime.now(UY_TZ)
    stats = get_monthly_stats(now.month, now.year)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT fecha, dia_semana, chofer, total_recaudado, comision_30,
                        total_pos, total_fiado, efectivo_neto, porcentaje_digital,
                        semana_numero, vehiculo
                 FROM recaudaciones ORDER BY creado_en DESC LIMIT 60""")
    rows = c.fetchall()
    conn.close()

    registros = "\n".join([
        f"- {r[0]} ({r[1] or '?'}) chofer:{r[2] or '?'} vehiculo:{r['vehiculo'] or '?'} "
        f"total:${r[3] or 0:.0f} comision:${r[4] or 0:.0f} "
        f"pos:${r[5] or 0:.0f} fiado:${r[6] or 0:.0f} "
        f"neto:${r[7] or 0:.0f} digital:{r[8] or 0:.1f}% sem:{r[9] or '?'}"
        for r in rows
    ])

    empresa_str = ", ".join(
        f"{e['empresa']}:${e['total']:.0f}" for e in stats.get("by_empresa", [])
    )

    context = (
        f"Saludo actual: {get_saludo()}\n"
        f"Fecha y hora actual: {now.strftime('%A %d/%m/%Y %H:%M')} (Uruguay, UTC-3)\n\n"
        f"ESTADÍSTICAS DEL MES ACTUAL ({now.strftime('%m/%Y')}):\n"
        f"- Total recaudado: ${stats['total_recaudado']:.0f}\n"
        f"- Días trabajados: {stats['dias_trabajados']}\n"
        f"- Promedio por día: ${stats['promedio_por_dia']:.0f}\n"
        f"- Mejor día: {stats['mejor_dia']}\n"
        f"- POS: ${stats['pagos']['pos']:.0f} | Fiado: ${stats['pagos']['fiado']:.0f}"
        f" | Efectivo neto: ${stats['pagos']['efectivo_neto']:.0f}\n"
        f"- Por empresa: {empresa_str or 'sin datos'}\n"
        f"- Por chofer: {json.dumps(stats['by_chofer'], ensure_ascii=False)}\n\n"
        f"ÚLTIMOS 60 REGISTROS:\n{registros}\n\n"
        f"Mensaje de Jorge: {message_text}"
    )

    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1000,
                system=PERSONA_PROMPT,
                tools=BOT_TOOLS,
                messages=[{"role": "user", "content": context}]
            )
            break
        except anthropic.InternalServerError:
            if attempt == 0:
                log.warning("Anthropic 500 transitorio, reintentando en 3s...")
                time.sleep(3)
            else:
                raise

    for block in response.content:
        if block.type == "tool_use":
            return None, {"name": block.name, "input": block.input}

    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    return text, None


# ── message handlers ──────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s.upper().strip())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _chofer_es_ilegible(valor: str | None) -> bool:
    """True when the value extracted from the image is clearly not a real chofer name."""
    if not valor:
        return True
    norm = _normalize(valor.strip())
    if not norm:
        return True
    _EXACTOS   = {"CHOFER", "ILEGIBLE", "NO SE", "?", "N/A", "S/D", "NULL", "NONE"}
    _PALABRAS  = {"CHOFER", "ILEGIBLE"}
    if norm in {_normalize(w) for w in _EXACTOS}:
        return True
    return bool(set(norm.split()) & {_normalize(w) for w in _PALABRAS})


def _match_choferes(nombre: str) -> list:
    """Returns active chofer names that share at least one word with `nombre`."""
    if not nombre:
        return []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nombre FROM choferes WHERE activo=1")
    todos = [row[0] for row in c.fetchall()]
    conn.close()
    palabras_input = set(_normalize(nombre).split())
    matches = []
    for chofer in todos:
        palabras_chofer = set(_normalize(chofer).split())
        if palabras_input & palabras_chofer:
            matches.append(chofer)
    return matches


def _get_canonical_chofer(nombre: str) -> str | None:
    if not nombre:
        return None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT nombre FROM choferes WHERE UPPER(nombre)=UPPER(?) AND activo=1",
        (nombre,)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def _cmd_list_choferes(sender: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nombre FROM choferes WHERE activo=1 ORDER BY nombre")
    activos = [r[0] for r in c.fetchall()]
    c.execute("SELECT COUNT(*) FROM choferes WHERE activo=0")
    n_inactivos = c.fetchone()[0]
    conn.close()
    if not activos:
        msg = "No hay choferes activos registrados."
    else:
        msg = "Choferes activos:\n" + "\n".join(f"- {n}" for n in activos)
        if n_inactivos:
            msg += f"\n\n(inactivos: {n_inactivos})"
    send_whatsapp_message(sender, msg)


def _cmd_add_chofer(sender: str, nombre: str):
    if not nombre:
        send_whatsapp_message(sender, "Indicá el nombre del chofer.")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, activo FROM choferes WHERE UPPER(nombre)=UPPER(?)", (nombre,))
        existing = c.fetchone()
        if existing:
            if existing[1] == 1:
                conn.close()
                send_whatsapp_message(sender, f"{nombre} ya está registrado como chofer activo.")
                return
            c.execute(
                "UPDATE choferes SET activo=1, desactivado_en=NULL WHERE id=?",
                (existing[0],)
            )
            conn.commit()
            conn.close()
            log.info(f"Chofer reactivado: {nombre}")
            send_whatsapp_message(sender, f"Chofer {nombre} reactivado correctamente.")
            return
        c.execute(
            "INSERT INTO choferes (nombre, activo, creado_en) VALUES (?,1,?)",
            (nombre, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        log.info(f"Chofer nuevo agregado: {nombre}")
        send_whatsapp_message(sender, f"Chofer {nombre} agregado correctamente.")
    except sqlite3.Error as e:
        log.error(f"Error agregando chofer '{nombre}': {e}")
        send_whatsapp_message(sender, "Hubo un error al agregar el chofer. Intentá de nuevo.")


def _cmd_deactivate_chofer(sender: str, nombre: str):
    if not nombre:
        send_whatsapp_message(sender, "Indicá el nombre del chofer.")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, activo FROM choferes WHERE UPPER(nombre)=UPPER(?)", (nombre,))
        existing = c.fetchone()
        if not existing:
            conn.close()
            send_whatsapp_message(sender, f"No existe ningún chofer con el nombre {nombre}.")
            return
        if existing[1] == 0:
            conn.close()
            send_whatsapp_message(sender, f"{nombre} ya estaba inactivo.")
            return
        c.execute(
            "UPDATE choferes SET activo=0, desactivado_en=? WHERE id=?",
            (datetime.now().isoformat(), existing[0])
        )
        conn.commit()
        conn.close()
        log.info(f"Chofer dado de baja: {nombre}")
        send_whatsapp_message(sender, f"Chofer {nombre} dado de baja. Sus registros históricos se mantienen.")
    except sqlite3.Error as e:
        log.error(f"Error dando de baja chofer '{nombre}': {e}")
        send_whatsapp_message(sender, "Hubo un error al dar de baja el chofer. Intentá de nuevo.")


def _cmd_init_add_empresa(sender: str, nombre: str):
    if not nombre:
        send_whatsapp_message(sender, "Indicá el nombre de la empresa.")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, activo FROM empresas WHERE UPPER(nombre)=UPPER(?)", (nombre,))
        existing = c.fetchone()
        if existing:
            if existing[1] == 1:
                conn.close()
                send_whatsapp_message(sender, f"{nombre} ya está registrada como empresa activa.")
                return
            c.execute(
                "UPDATE empresas SET activo=1, desactivado_en=NULL WHERE id=?",
                (existing[0],)
            )
            conn.commit()
            conn.close()
            log.info(f"Empresa reactivada: {nombre}")
            send_whatsapp_message(sender, f"Empresa {nombre} reactivada correctamente.")
            return
        conn.close()
        PENDING_EMPRESA[sender] = {"nombre": nombre}
        send_whatsapp_message(sender, f"¿Cuál es el RUT de {nombre}? Respondé SKIP si no lo tiene.")
    except sqlite3.Error as e:
        log.error(f"Error iniciando alta de empresa '{nombre}': {e}")
        send_whatsapp_message(sender, "Hubo un error. Intentá de nuevo.")


def _cmd_add_empresa(sender: str, nombre: str, rut: str | None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO empresas (nombre, rut, activo, creado_en) VALUES (?,?,1,?)",
            (nombre, rut, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        log.info(f"Empresa agregada: {nombre} RUT:{rut}")
        rut_info = f" (RUT: {rut})" if rut else ""
        send_whatsapp_message(sender, f"Empresa {nombre}{rut_info} agregada correctamente.")
    except sqlite3.Error as e:
        log.error(f"Error agregando empresa '{nombre}': {e}")
        send_whatsapp_message(sender, "Hubo un error al agregar la empresa. Intentá de nuevo.")


def _cmd_deactivate_empresa(sender: str, nombre: str):
    if not nombre:
        send_whatsapp_message(sender, "Indicá el nombre de la empresa.")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, activo FROM empresas WHERE UPPER(nombre)=UPPER(?)", (nombre,))
        existing = c.fetchone()
        if not existing:
            conn.close()
            send_whatsapp_message(sender, f"No existe ninguna empresa con el nombre {nombre}.")
            return
        if existing[1] == 0:
            conn.close()
            send_whatsapp_message(sender, f"{nombre} ya estaba inactiva.")
            return
        c.execute(
            "UPDATE empresas SET activo=0, desactivado_en=? WHERE id=?",
            (datetime.now().isoformat(), existing[0])
        )
        conn.commit()
        conn.close()
        log.info(f"Empresa dada de baja: {nombre}")
        send_whatsapp_message(sender, f"Empresa {nombre} dada de baja. Los registros históricos se mantienen.")
    except sqlite3.Error as e:
        log.error(f"Error dando de baja empresa '{nombre}': {e}")
        send_whatsapp_message(sender, "Hubo un error. Intentá de nuevo.")


def _cmd_update_rut(sender: str, nombre: str, rut: str):
    if not nombre or not rut:
        send_whatsapp_message(sender, "Indicá el nombre de la empresa y el nuevo RUT.")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM empresas WHERE UPPER(nombre)=UPPER(?)", (nombre,))
        existing = c.fetchone()
        if not existing:
            conn.close()
            send_whatsapp_message(sender, f"No existe ninguna empresa con el nombre {nombre}.")
            return
        c.execute("UPDATE empresas SET rut=? WHERE id=?", (rut, existing[0]))
        conn.commit()
        conn.close()
        log.info(f"RUT actualizado — {nombre}: {rut}")
        send_whatsapp_message(sender, f"RUT de {nombre} actualizado a {rut}.")
    except sqlite3.Error as e:
        log.error(f"Error actualizando RUT de '{nombre}': {e}")
        send_whatsapp_message(sender, "Hubo un error al actualizar el RUT. Intentá de nuevo.")


def _cmd_list_empresas(sender: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nombre, rut, activo FROM empresas ORDER BY activo DESC, nombre")
    rows = c.fetchall()
    conn.close()
    if not rows:
        send_whatsapp_message(sender, "No hay empresas registradas.")
        return
    activas   = [(n, r) for n, r, a in rows if a == 1]
    inactivas = [n for n, r, a in rows if a == 0]
    msg = ("Empresas activas:\n" + "\n".join(
        f"- {n}" + (f" (RUT: {r})" if r else "") for n, r in activas
    )) if activas else "No hay empresas activas."
    if inactivas:
        msg += "\n\n(inactivas: " + ", ".join(inactivas) + ")"
    send_whatsapp_message(sender, msg)


def _find_existing(fecha: str, chofer: str) -> dict | None:
    if not fecha or not chofer:
        return None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, total_recaudado FROM recaudaciones WHERE fecha=? AND chofer=?",
        (fecha, chofer)
    )
    row = c.fetchone()
    conn.close()
    return {"id": row[0], "total": row[1]} if row else None


def _do_insert(parsed: dict, derived: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO recaudaciones (
        fecha, chofer, vehiculo,
        total_recaudado, comision_30, total_pos, total_fiado,
        otros_combustible, efectivo_empresa,
        dia_semana, semana_numero, mes, anio,
        efectivo_neto, porcentaje_digital, creado_en
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        parsed.get("fecha"), parsed.get("chofer"), parsed.get("vehiculo"),
        parsed.get("total_recaudado"), parsed.get("comision_30"),
        parsed.get("total_pos"), parsed.get("total_fiado"),
        parsed.get("otros_combustible"), parsed.get("efectivo_empresa"),
        derived["dia_semana"], derived["semana_numero"], derived["mes"], derived["anio"],
        derived["efectivo_neto"], derived["porcentaje_digital"],
        datetime.now().isoformat(),
    ))
    record_id = c.lastrowid

    c.execute("SELECT id, nombre FROM empresas WHERE activo=1")
    for emp_id, emp_nombre in c.fetchall():
        monto = parsed.get(_empresa_to_key(emp_nombre))
        if monto is not None:
            c.execute(
                "INSERT OR IGNORE INTO recaudacion_empresas "
                "(recaudacion_id, empresa_id, monto) VALUES (?,?,?)",
                (record_id, emp_id, monto)
            )
    conn.commit()
    conn.close()


def _do_update(record_id: int, parsed: dict, derived: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""UPDATE recaudaciones SET
        chofer=?, vehiculo=?, total_recaudado=?, comision_30=?, total_pos=?, total_fiado=?,
        otros_combustible=?, efectivo_empresa=?,
        dia_semana=?, semana_numero=?, mes=?, anio=?,
        efectivo_neto=?, porcentaje_digital=?, creado_en=?
        WHERE id=?""", (
        parsed.get("chofer"), parsed.get("vehiculo"), parsed.get("total_recaudado"),
        parsed.get("comision_30"), parsed.get("total_pos"), parsed.get("total_fiado"),
        parsed.get("otros_combustible"), parsed.get("efectivo_empresa"),
        derived["dia_semana"], derived["semana_numero"], derived["mes"], derived["anio"],
        derived["efectivo_neto"], derived["porcentaje_digital"],
        datetime.now().isoformat(), record_id,
    ))

    c.execute("DELETE FROM recaudacion_empresas WHERE recaudacion_id=?", (record_id,))
    c.execute("SELECT id, nombre FROM empresas WHERE activo=1")
    for emp_id, emp_nombre in c.fetchall():
        monto = parsed.get(_empresa_to_key(emp_nombre))
        if monto is not None:
            c.execute(
                "INSERT INTO recaudacion_empresas (recaudacion_id, empresa_id, monto) VALUES (?,?,?)",
                (record_id, emp_id, monto)
            )
    conn.commit()
    conn.close()


def _build_confirm_msg(parsed: dict, derived: dict, modified_field: str = None) -> str:
    fecha    = parsed.get("fecha") or "?"
    chofer   = parsed.get("chofer") or "sin nombre"
    vehiculo = parsed.get("vehiculo") or "sin asignar"
    total    = parsed.get("total_recaudado") or 0
    ef_emp   = parsed.get("efectivo_empresa") or 0
    pos      = parsed.get("total_pos") or 0
    dudoso   = parsed.get("monto_dudoso")

    def _e(campo):
        return "→ " if modified_field == campo else ""

    total_suffix = " *(verificar)*" if dudoso else ""
    return (
        f"*Resumen de la recaudación*\n"
        f"*Fecha:* {_e('fecha')}{fecha}\n"
        f"*Chofer:* {_e('chofer')}{chofer}\n"
        f"*Vehículo:* {_e('vehiculo')}{vehiculo}\n"
        f"*Total:* {_e('total_recaudado')}${total:.0f}{total_suffix}\n"
        f"*Efectivo empresa:* {_e('efectivo_empresa')}${ef_emp:.0f}\n"
        f"*POS:* ${pos:.0f}\n\n"
        f"¿Todo correcto? Respondé *SI* para guardar, un número para corregir o *CANCELAR*."
    )


def _confirmation_msg(parsed: dict, derived: dict, verb: str) -> str:
    chofer     = parsed.get("chofer") or "sin nombre"
    fecha      = parsed.get("fecha") or "?"
    dia        = derived["dia_semana"] or ""
    total      = parsed.get("total_recaudado") or 0
    ef_empresa = parsed.get("efectivo_empresa") or 0
    pos        = parsed.get("total_pos") or 0
    pct        = derived["porcentaje_digital"] or 0
    vehiculo   = parsed.get("vehiculo") or "sin asignar"
    return (
        f"{verb} {fecha} ({dia})\n"
        f"Chofer: {chofer}\n"
        f"Vehículo: {vehiculo}\n"
        f"Total: ${total:.0f}\n"
        f"Efectivo empresa: ${ef_empresa:.0f}\n"
        f"POS: ${pos:.0f} ({pct:.1f}%)"
    )


def _send_vehicle_list(sender: str, header: str):
    send_interactive_list(
        sender, header, "Seleccionar",
        [{"title": "Vehículos", "rows": [
            {"id": "1", "title": "Terminal"},
            {"id": "2", "title": "Plaza"},
            {"id": "3", "title": "Sanatorio"},
            {"id": "4", "title": "Particular"},
        ]}],
    )


def _send_chofer_confirm_buttons(sender: str, nombre: str):
    send_interactive_buttons(
        sender,
        f"El chofer *{nombre}* no está registrado. ¿Querés agregarlo al sistema?",
        [{"id": "SI", "title": "Si, agregar"}, {"id": "NO", "title": "No"}],
    )


def _send_chofer_disambiguation_list(sender: str, matches: list):
    rows = [{"id": str(i + 1), "title": m} for i, m in enumerate(matches)]
    send_interactive_list(
        sender,
        "Encontré varios choferes con ese nombre. ¿A cuál pertenece esta recaudación?",
        "Ver choferes",
        [{"title": "Choferes", "rows": rows}],
    )


def _send_field_select_interactive(sender: str, parsed: dict):
    _fecha = parsed.get("fecha") or "?"
    _total = parsed.get("total_recaudado") or 0
    _ef    = parsed.get("efectivo_empresa") or 0
    _pos   = parsed.get("total_pos") or 0
    _com   = parsed.get("comision_30") or 0
    _fiado = parsed.get("total_fiado") or 0
    _pul   = parsed.get("empresa_pul") or 0
    _mides = parsed.get("empresa_mides") or 0
    _hosp  = parsed.get("empresa_hospital") or 0
    _comb  = parsed.get("otros_combustible") or 0
    send_interactive_list(
        sender,
        "¿Qué campo querés corregir?",
        "Ver campos",
        [{"title": "Campos", "rows": [
            {"id": "1",  "title": "Fecha",       "description": str(_fecha)},
            {"id": "2",  "title": "Total",       "description": f"${_total:.0f}"},
            {"id": "3",  "title": "Ef. empresa", "description": f"${_ef:.0f}"},
            {"id": "4",  "title": "POS",         "description": f"${_pos:.0f}"},
            {"id": "5",  "title": "Comisión",    "description": f"${_com:.0f}"},
            {"id": "6",  "title": "Fiado",       "description": f"${_fiado:.0f}"},
            {"id": "7",  "title": "PUL",         "description": f"${_pul:.0f}"},
            {"id": "8",  "title": "MIDES",       "description": f"${_mides:.0f}"},
            {"id": "9",  "title": "Hospital",    "description": f"${_hosp:.0f}"},
            {"id": "10", "title": "Combustible", "description": f"${_comb:.0f}"},
        ]}],
    )


def _send_confirm_interactive(sender: str, parsed: dict, derived: dict, modified_field: str = None):
    body = _build_confirm_msg(parsed, derived, modified_field)
    send_interactive_buttons(
        sender, body,
        [
            {"id": "SI",       "title": "Guardar"},
            {"id": "1",        "title": "Corregir"},
            {"id": "CANCELAR", "title": "Cancelar"},
        ],
    )


def _resolve_chofer_and_continue(sender: str, parsed: dict, fecha_str: str, nombre: str):
    matches = _match_choferes(nombre)
    if len(matches) == 0:
        log.warning(f"Chofer no registrado '{nombre}' para {_mask(sender)}")
        PENDING_CHOFER_CONFIRM[sender] = {
            "parsed": parsed, "fecha_str": fecha_str, "nombre": nombre, "ts": time.time(),
        }
        _send_chofer_confirm_buttons(sender, nombre)
    elif len(matches) == 1:
        log.info(f"Chofer '{nombre}' resuelto a '{matches[0]}' para {_mask(sender)}")
        parsed["chofer"] = matches[0]
        derived = _derive_fields(fecha_str, parsed)
        PENDING_VEHICLE[sender] = {"parsed": parsed, "derived": derived, "ts": time.time()}
        total = parsed.get("total_recaudado") or 0
        fecha = parsed.get("fecha") or "?"
        _send_vehicle_list(sender, f"*Recaudación detectada:* ${total:.0f} del {fecha}.\n¿A qué vehículo pertenece?")
    else:
        log.warning(f"Chofer ambiguo '{nombre}' — {len(matches)} matches para {_mask(sender)}")
        PENDING_CHOFER[sender] = {
            "parsed": parsed, "fecha_str": fecha_str, "matches": matches, "ts": time.time(),
        }
        _send_chofer_disambiguation_list(sender, matches)


def _handle_image(sender: str, message: dict):
    image_id = message["image"]["id"]
    try:
        url_resp = requests.get(
            f"https://graph.facebook.com/v18.0/{image_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            timeout=10
        )
        url_resp.raise_for_status()
        image_url = url_resp.json().get("url")

        send_whatsapp_message(sender, "Recibí la imagen, procesando...")
        result = process_image(image_url)

        match = re.search(r'\{.*\}', result, re.DOTALL)
        if not match:
            raise ValueError("Claude no devolvió JSON válido")
        parsed = json.loads(match.group())
        log.info(f"[DEBUG] JSON extraído: {json.dumps(parsed, ensure_ascii=False)}")

        if "error" in parsed:
            log.warning(f"Claude no pudo extraer datos de imagen de {_mask(sender)}")
            send_whatsapp_message(sender, "No pude leer la imagen. Mandá una foto más clara.")
            return

        fecha_str  = parsed.get("fecha", "")
        chofer_raw = parsed.get("chofer")
        if _chofer_es_ilegible(chofer_raw):
            log.warning(f"Chofer ilegible en imagen para {_mask(sender)}: {chofer_raw!r}")
            PENDING_CHOFER_NAME[sender] = {
                "parsed": parsed,
                "fecha_str": fecha_str,
                "ts": time.time(),
            }
            send_whatsapp_message(sender, (
                "No pude leer el nombre del chofer en la imagen.\n"
                "¿Quién es el chofer de esta recaudación?"
            ))
            return
        if chofer_raw:
            log.info(f"Chofer OCR '{chofer_raw}' para {_mask(sender)} — esperando confirmación")
            PENDING_CHOFER_OCR[sender] = {
                "parsed": parsed, "fecha_str": fecha_str, "nombre": chofer_raw, "ts": time.time(),
            }
            send_interactive_buttons(
                sender,
                f"Leí el nombre del chofer como: *{chofer_raw}*\n¿Es correcto?",
                [{"id": "SI", "title": "Si"}, {"id": "NO", "title": "No, corregir"}],
            )
            return

    except Exception as e:
        log.error(f"Error procesando imagen de {_mask(sender)}: {e}\n{traceback.format_exc()}")
        send_whatsapp_message(sender, "Hubo un error procesando la imagen. Intentá de nuevo en unos segundos.")


def _handle_text(sender: str, message: dict):
    text = message["text"]["body"].strip()

    # Handle pre-save confirmation / field corrections — MUST be first
    if sender in PENDING_CONFIRM:
        pending  = PENDING_CONFIRM[sender]
        if time.time() - pending["ts"] > VEHICLE_TIMEOUT:
            PENDING_CONFIRM.pop(sender)
            log.info(f"Confirmación de guardado expirada para {_mask(sender)}")
            send_whatsapp_message(sender, "La confirmación expiró. Mandá la imagen nuevamente.")
            return
        parsed   = pending["parsed"]
        derived  = pending["derived"]
        substate = pending.get("substate", "confirm")

        # ── substate: waiting for corrected value ─────────────────────
        if substate == "value_input":
            campo_db = pending["campo_db"]
            if campo_db in ("fecha", "chofer", "vehiculo"):
                parsed[campo_db] = text.strip()
            else:
                try:
                    parsed[campo_db] = float(text.strip().replace(",", "."))
                except ValueError:
                    send_whatsapp_message(sender, f"Valor inválido: '{text.strip()}'. Ingresá un número.")
                    return
            derived = _derive_fields(parsed.get("fecha", ""), parsed)
            PENDING_CONFIRM[sender] = {"parsed": parsed, "derived": derived, "ts": time.time(), "substate": "confirm"}
            log.info(f"Campo '{campo_db}' corregido para {_mask(sender)}: {text.strip()}")
            _send_confirm_interactive(sender, parsed, derived, modified_field=campo_db)
            return

        # ── substate: waiting for field selection ─────────────────────
        if substate == "field_select":
            _MENU = {
                "1":  ("fecha",             "Fecha"),
                "2":  ("total_recaudado",   "Total"),
                "3":  ("efectivo_empresa",  "Efectivo empresa"),
                "4":  ("total_pos",         "POS"),
                "5":  ("comision_30",       "Comisión"),
                "6":  ("total_fiado",       "Fiado"),
                "7":  ("empresa_pul",       "PUL"),
                "8":  ("empresa_mides",     "MIDES"),
                "9":  ("empresa_hospital",  "Hospital"),
                "10": ("otros_combustible", "Combustible"),
            }
            if text.strip() in _MENU:
                campo_db, label = _MENU[text.strip()]
                PENDING_CONFIRM[sender] = {
                    "parsed": parsed, "derived": derived,
                    "ts": time.time(), "substate": "value_input", "campo_db": campo_db,
                }
                send_whatsapp_message(sender, f"¿Cuál es el {label.lower()} correcto?")
            else:
                _send_field_select_interactive(sender, parsed)
            return

        # ── substate: confirm (default) ────────────────────────────────
        if text.upper().strip() == "CANCELAR":
            PENDING_CONFIRM.pop(sender)
            log.info(f"Confirmación cancelada por {_mask(sender)}")
            send_whatsapp_message(sender, "Recaudación cancelada. Si querés registrarla, reenviá la foto.")
            return
        if text.upper().strip() in RESPUESTAS_AFIRMATIVAS:
            PENDING_CONFIRM.pop(sender)
            existing = _find_existing(parsed.get("fecha"), parsed.get("chofer"))
            if existing:
                PENDING_REPLACEMENTS[sender] = {
                    "parsed": parsed, "derived": derived,
                    "existing_id": existing["id"], "existing_total": existing["total"],
                }
                chofer = parsed.get("chofer") or "sin nombre"
                fecha  = parsed.get("fecha") or "?"
                log.warning(f"Duplicado detectado para {_mask(sender)}: {chofer} {fecha}")
                send_whatsapp_message(sender, (
                    f"Ya existe un registro para {chofer} del {fecha} "
                    f"con un total de ${existing['total']:.0f}.\n"
                    f"¿Querés reemplazarlo? Respondé SI para confirmar."
                ))
                return
            try:
                _do_insert(parsed, derived)
                log.info(
                    f"Recaudación guardada para {_mask(sender)}: "
                    f"chofer={parsed.get('chofer')} vehiculo={parsed.get('vehiculo')} "
                    f"total=${parsed.get('total_recaudado')}"
                )
            except sqlite3.Error as db_err:
                log.error(f"Error al guardar en DB: {db_err}")
                send_whatsapp_message(sender, "Leí los datos pero hubo un error al guardarlos. Avisale al administrador.")
                return
            send_whatsapp_message(sender, _confirmation_msg(parsed, derived, "*Guardado*"))
            return
        try:
            float(text.strip().replace(",", "."))
            is_number = True
        except ValueError:
            is_number = False
        if is_number:
            PENDING_CONFIRM[sender] = {
                "parsed": parsed, "derived": derived,
                "ts": time.time(), "substate": "field_select",
            }
            _send_field_select_interactive(sender, parsed)
            return
        # Unrecognized response — re-show summary
        _send_confirm_interactive(sender, parsed, derived)
        return

    # Handle pending vehicle selection
    if sender in PENDING_VEHICLE:
        pending = PENDING_VEHICLE[sender]
        if time.time() - pending["ts"] > VEHICLE_TIMEOUT:
            PENDING_VEHICLE.pop(sender)
            log.info(f"Selección de vehículo expirada para {_mask(sender)}")
            send_whatsapp_message(sender, "La selección de vehículo expiró. Mandá la imagen nuevamente.")
            return
        vehiculo = VEHICULOS.get(text.strip().lower())
        if vehiculo is None:
            _send_vehicle_list(sender, "No reconocí el vehículo. ¿A cuál pertenece esta recaudación?")
            return
        PENDING_VEHICLE.pop(sender)
        parsed  = pending["parsed"]
        derived = pending["derived"]
        parsed["vehiculo"] = vehiculo
        PENDING_CONFIRM[sender] = {"parsed": parsed, "derived": derived, "ts": time.time(), "substate": "confirm"}
        log.info(f"Vehículo '{vehiculo}' seleccionado para {_mask(sender)} — esperando confirmación")
        _send_confirm_interactive(sender, parsed, derived)
        return

    # Handle pending chofer name input (image couldn't read it)
    if sender in PENDING_CHOFER_NAME:
        pending = PENDING_CHOFER_NAME[sender]
        if time.time() - pending["ts"] > CHOFER_TIMEOUT:
            PENDING_CHOFER_NAME.pop(sender)
            log.info(f"Espera de nombre de chofer expirada para {_mask(sender)}")
            send_whatsapp_message(sender, "La espera expiró. Mandá la imagen nuevamente.")
            return
        PENDING_CHOFER_NAME.pop(sender)
        parsed    = pending["parsed"]
        fecha_str = pending["fecha_str"]
        log.info(f"Nombre de chofer ingresado por {_mask(sender)}: '{text.strip()}'")
        _resolve_chofer_and_continue(sender, parsed, fecha_str, text.strip())
        return

    # Handle pending chofer OCR name confirmation (was the OCR-read name correct?)
    if sender in PENDING_CHOFER_OCR:
        pending = PENDING_CHOFER_OCR[sender]
        if time.time() - pending["ts"] > CHOFER_TIMEOUT:
            PENDING_CHOFER_OCR.pop(sender)
            log.info(f"Confirmación de nombre OCR expirada para {_mask(sender)}")
            send_whatsapp_message(sender, "La espera expiró. Mandá la imagen nuevamente.")
            return
        respuesta = text.upper().strip()
        if respuesta in RESPUESTAS_AFIRMATIVAS:
            PENDING_CHOFER_OCR.pop(sender)
            parsed    = pending["parsed"]
            fecha_str = pending["fecha_str"]
            nombre    = pending["nombre"]
            log.info(f"Nombre OCR '{nombre}' confirmado por {_mask(sender)}")
            _resolve_chofer_and_continue(sender, parsed, fecha_str, nombre)
        elif respuesta == "NO":
            PENDING_CHOFER_OCR.pop(sender)
            parsed    = pending["parsed"]
            fecha_str = pending["fecha_str"]
            PENDING_CHOFER_NAME[sender] = {"parsed": parsed, "fecha_str": fecha_str, "ts": time.time()}
            send_whatsapp_message(sender, "¿Cuál es el nombre correcto del chofer?")
        else:
            nombre = pending["nombre"]
            send_interactive_buttons(
                sender,
                f"Leí el nombre del chofer como: *{nombre}*\n¿Es correcto?",
                [{"id": "SI", "title": "Si"}, {"id": "NO", "title": "No, corregir"}],
            )
        return

    # Handle pending new chofer confirmation (SI to add / NO to cancel)
    if sender in PENDING_CHOFER_CONFIRM:
        pending = PENDING_CHOFER_CONFIRM[sender]
        if time.time() - pending["ts"] > CHOFER_TIMEOUT:
            PENDING_CHOFER_CONFIRM.pop(sender)
            log.info(f"Confirmación de chofer expirada para {_mask(sender)}")
            send_whatsapp_message(sender, "La confirmación expiró. Mandá la imagen nuevamente.")
            return
        respuesta = text.upper().strip()
        if respuesta in RESPUESTAS_AFIRMATIVAS:
            PENDING_CHOFER_CONFIRM.pop(sender)
            nombre    = pending["nombre"]
            parsed    = pending["parsed"]
            fecha_str = pending["fecha_str"]
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute(
                    "INSERT INTO choferes (nombre, activo, creado_en) VALUES (?,1,?)",
                    (nombre, datetime.now().isoformat())
                )
                conn.commit()
                conn.close()
                log.info(f"Chofer nuevo agregado desde confirmación: {nombre}")
            except sqlite3.Error as e:
                log.error(f"Error agregando chofer '{nombre}': {e}")
                send_whatsapp_message(sender, "Hubo un error al agregar el chofer. Intentá de nuevo.")
                return
            parsed["chofer"] = nombre
            derived = _derive_fields(fecha_str, parsed)
            PENDING_VEHICLE[sender] = {"parsed": parsed, "derived": derived, "ts": time.time()}
            total = parsed.get("total_recaudado") or 0
            fecha = parsed.get("fecha") or "?"
            send_whatsapp_message(sender, f"Chofer {nombre} agregado correctamente.")
            _send_vehicle_list(sender, f"*Recaudación detectada:* ${total:.0f} del {fecha}.\n¿A qué vehículo pertenece?")
        elif respuesta in {"NO", "N", "CANCELAR"}:
            PENDING_CHOFER_CONFIRM.pop(sender)
            nombre = pending["nombre"]
            log.info(f"Alta de chofer '{nombre}' cancelada por {_mask(sender)}")
            send_whatsapp_message(sender, "Entendido. Si querés registrar la recaudación, reenviá la foto.")
        else:
            _send_chofer_confirm_buttons(sender, pending["nombre"])
        return

    # Handle pending chofer disambiguation
    if sender in PENDING_CHOFER:
        pending = PENDING_CHOFER[sender]
        if time.time() - pending["ts"] > CHOFER_TIMEOUT:
            PENDING_CHOFER.pop(sender)
            log.info(f"Selección de chofer expirada para {_mask(sender)}")
            send_whatsapp_message(sender, "La selección de chofer expiró. Mandá la imagen nuevamente.")
            return
        matches = pending["matches"]
        try:
            idx = int(text.strip()) - 1
            if not (0 <= idx < len(matches)):
                raise ValueError
        except ValueError:
            _send_chofer_disambiguation_list(sender, matches)
            return
        PENDING_CHOFER.pop(sender)
        parsed            = pending["parsed"]
        parsed["chofer"]  = matches[idx]
        log.info(f"Chofer seleccionado '{matches[idx]}' para {_mask(sender)}")
        derived = _derive_fields(pending["fecha_str"], parsed)
        PENDING_VEHICLE[sender] = {"parsed": parsed, "derived": derived, "ts": time.time()}
        total = parsed.get("total_recaudado") or 0
        fecha = parsed.get("fecha") or "?"
        _send_vehicle_list(sender, f"*Recaudación detectada:* ${total:.0f} del {fecha}.\n¿A qué vehículo pertenece?")
        return

    # Handle pending empresa RUT flow
    if sender in PENDING_EMPRESA:
        pending = PENDING_EMPRESA.pop(sender)
        rut = None if text.upper() == "SKIP" else text.strip()
        _cmd_add_empresa(sender, pending["nombre"], rut)
        return

    if text.upper().strip() in RESPUESTAS_AFIRMATIVAS and sender in PENDING_REPLACEMENTS:
        pending = PENDING_REPLACEMENTS.pop(sender)
        try:
            _do_update(pending["existing_id"], pending["parsed"], pending["derived"])
            log.info(
                f"Registro reemplazado para {_mask(sender)}: "
                f"id={pending['existing_id']} chofer={pending['parsed'].get('chofer')}"
            )
            send_whatsapp_message(
                sender,
                _confirmation_msg(pending["parsed"], pending["derived"], "Reemplazado!")
            )
        except sqlite3.Error as db_err:
            log.error(f"Error al reemplazar registro: {db_err}")
            send_whatsapp_message(sender, "Hubo un error al reemplazar el registro. Avisale al administrador.")
        return

    if sender in PENDING_REPLACEMENTS:
        PENDING_REPLACEMENTS.pop(sender)
        log.info(f"Reemplazo cancelado para {_mask(sender)}")

    try:
        text_resp, tool_call = process_text(text)
        if tool_call:
            name = tool_call["name"]
            inp  = tool_call["input"]
            log.info(f"Tool call '{name}' para {_mask(sender)}: {inp}")
            if name == "agregar_chofer":
                _cmd_add_chofer(sender, inp.get("nombre", ""))
            elif name == "dar_baja_chofer":
                _cmd_deactivate_chofer(sender, inp.get("nombre", ""))
            elif name == "listar_choferes":
                _cmd_list_choferes(sender)
            elif name == "agregar_empresa":
                _cmd_init_add_empresa(sender, inp.get("nombre", ""))
            elif name == "dar_baja_empresa":
                _cmd_deactivate_empresa(sender, inp.get("nombre", ""))
            elif name == "actualizar_rut_empresa":
                _cmd_update_rut(sender, inp.get("nombre", ""), inp.get("rut", ""))
            elif name == "listar_empresas":
                _cmd_list_empresas(sender)
        else:
            send_whatsapp_message(sender, text_resp)
        log.info(f"Mensaje de texto procesado para {_mask(sender)}")
    except Exception as e:
        log.error(f"Error procesando texto de {_mask(sender)}: {e}\n{traceback.format_exc()}")
        send_whatsapp_message(sender, "Hubo un error procesando tu consulta. Intentá de nuevo.")


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM recaudaciones")
        count = c.fetchone()[0]
        conn.close()
        db_ok = True
    except Exception:
        count = -1
        db_ok = False
    return JSONResponse({
        "status": "ok" if db_ok else "degraded",
        "db_records": count,
        "version": VERSION,
        "uptime_seconds": round(time.time() - START_TIME),
    })


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge"))
    log.warning("Intento de verificación con token inválido")
    return JSONResponse({"error": "Token invalido"}, status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    try:
        data = await request.json()
    except Exception:
        log.warning("Payload inválido recibido en /webhook")
        return {"status": "ok"}

    try:
        entry    = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "ok"}

        message  = entry["messages"][0]
        sender   = message["from"]
        msg_type = message["type"]

        if ALLOWED_NUMBERS and sender not in ALLOWED_NUMBERS:
            log.warning(f"Número no autorizado ignorado: {_mask(sender)}")
            return {"status": "ok"}

        log.info(f"Mensaje recibido de {_mask(sender)} — tipo: {msg_type}")

        if msg_type == "image":
            _handle_image(sender, message)
        elif msg_type == "text":
            _handle_text(sender, message)
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            itype       = interactive.get("type")
            if itype == "button_reply":
                reply_id = interactive["button_reply"]["id"]
            elif itype == "list_reply":
                reply_id = interactive["list_reply"]["id"]
            else:
                reply_id = None
            if reply_id is not None:
                log.info(f"Respuesta interactiva de {_mask(sender)}: {reply_id!r}")
                _handle_text(sender, {"text": {"body": reply_id}})
        else:
            log.info(f"Tipo de mensaje no soportado ignorado: {msg_type}")

    except (KeyError, IndexError) as e:
        log.error(f"Estructura de payload inesperada: {e}")
    except Exception as e:
        log.error(f"Error inesperado en webhook: {e}\n{traceback.format_exc()}")

    return {"status": "ok"}
