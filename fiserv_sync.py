"""fiserv_sync.py — Gmail → PDF (Claude Vision) → Google Sheets pipeline."""
import os
import io
import gc
import re
import json
import time
import base64
import logging
from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler

import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    import pdf2image as _pdf2image
    _PDF2IMAGE_OK = True
except ImportError:
    _PDF2IMAGE_OK = False

from dotenv import load_dotenv
load_dotenv(Path("/home/ubuntu/asistente-lito/.env"))

BASE_DIR         = Path("/home/ubuntu/asistente-lito")
PROCESSED_FILE   = BASE_DIR / "fiserv_processed_emails.json"
TOKEN_FILE       = BASE_DIR / "gmail_token.json"
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_PATH",
                         str(BASE_DIR / "gmail_credentials.json")))
SHEETS_ID_FILE   = BASE_DIR / "fiserv_sheets_id.json"
FISERV_LOG       = BASE_DIR / "fiserv_sync.log"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

FISERV_DOMAINS = ["fiserv.com", "firstdata.com"]
SHEET_TITLE    = "Fiserv Liquidaciones"
TAB_RESUMEN    = "Resumen"
TAB_DETALLE    = "Detalle"
TAB_CARGOS     = "Cargos Terminal"

RESUMEN_HEADERS = [
    "Fecha Proceso", "Período Desde", "Período Hasta",
    "Total Venta", "Total Neto", "Total Comisión", "Total IVA",
    "Cant. Conceptos", "Email ID",
]
DETALLE_HEADERS = [
    "Período Desde", "Período Hasta", "Concepto", "Plan",
    "Cant. Operaciones", "Importe Venta", "Precio Servicio",
    "Porcentaje", "IVA", "Leyes y Cargos", "Neto a Cobrar",
    "Semana Pago Desde", "Semana Pago Hasta",
]
CARGOS_HEADERS = ["Fecha Proceso", "Concepto", "Importe", "Email ID"]

# ── logging ────────────────────────────────────────────────────────────────────

def _setup_log() -> logging.Logger:
    logger = logging.getLogger("fiserv-sync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh  = RotatingFileHandler(FISERV_LOG, maxBytes=2 * 1024 * 1024, backupCount=2)
    fh.setFormatter(fmt)
    sh  = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log        = _setup_log()
_anthropic = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


# ── Google OAuth2 ──────────────────────────────────────────────────────────────

def get_google_creds() -> Credentials:
    """Load and auto-refresh Google OAuth2 credentials."""
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"Token de Google no encontrado en {TOKEN_FILE}.\n"
            "Ejecutá la autenticación inicial (ver instrucciones)."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            log.info("Token de Google refrescado")
        else:
            raise RuntimeError(
                "Token inválido. Re-autenticá con: python3 fiserv_sync.py --auth"
            )
    return creds


# ── processed emails tracking ──────────────────────────────────────────────────

def _load_processed() -> set:
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text()))
        except Exception:
            return set()
    return set()


def _save_processed(ids: set):
    PROCESSED_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ── Google Sheets setup ────────────────────────────────────────────────────────

def _get_sheets_id() -> str:
    sid = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    if sid:
        return sid
    if SHEETS_ID_FILE.exists():
        try:
            return json.loads(SHEETS_ID_FILE.read_text()).get("sheets_id", "")
        except Exception:
            pass
    return ""


def _ensure_sheet(service) -> str:
    """Return sheets ID, creating the spreadsheet + headers if needed."""
    sid = _get_sheets_id()
    if sid:
        _ensure_tabs(service, sid)
        return sid

    spreadsheet = {
        "properties": {"title": SHEET_TITLE},
        "sheets": [
            {"properties": {"title": TAB_RESUMEN}},
            {"properties": {"title": TAB_DETALLE}},
            {"properties": {"title": TAB_CARGOS}},
        ],
    }
    result = service.spreadsheets().create(body=spreadsheet).execute()
    sid    = result["spreadsheetId"]
    log.info(f"Planilla creada: https://docs.google.com/spreadsheets/d/{sid}")

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{TAB_RESUMEN}!A1", "values": [RESUMEN_HEADERS]},
                {"range": f"{TAB_DETALLE}!A1",  "values": [DETALLE_HEADERS]},
                {"range": f"{TAB_CARGOS}!A1",   "values": [CARGOS_HEADERS]},
            ],
        },
    ).execute()

    SHEETS_ID_FILE.write_text(json.dumps({"sheets_id": sid}, indent=2))
    return sid


def _ensure_tabs(service, sheets_id: str):
    """Add any missing tabs and headers to an existing spreadsheet."""
    meta         = service.spreadsheets().get(spreadsheetId=sheets_id).execute()
    existing     = {s["properties"]["title"] for s in meta.get("sheets", [])}
    add_requests = []
    header_data  = []
    tabs = [
        (TAB_RESUMEN, RESUMEN_HEADERS),
        (TAB_DETALLE, DETALLE_HEADERS),
        (TAB_CARGOS,  CARGOS_HEADERS),
    ]
    for tab, headers in tabs:
        if tab not in existing:
            add_requests.append({"addSheet": {"properties": {"title": tab}}})
            header_data.append({"range": f"{tab}!A1", "values": [headers]})
    if add_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheets_id, body={"requests": add_requests}
        ).execute()
    if header_data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheets_id,
            body={"valueInputOption": "RAW", "data": header_data},
        ).execute()


def _get_tab_id(service, sid: str, tab_title: str) -> int:
    """Return the numeric sheetId for a given tab title."""
    meta = service.spreadsheets().get(spreadsheetId=sid).execute()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab_title:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Pestaña '{tab_title}' no encontrada")


# ── Gmail fetch ────────────────────────────────────────────────────────────────

def _walk_parts(service, msg_id: str, parts: list, pdfs: list):
    """Recursively walk MIME parts and collect PDF bytes."""
    for part in parts:
        mime     = part.get("mimeType", "")
        filename = part.get("filename", "")
        if "pdf" in mime.lower() or filename.lower().endswith(".pdf"):
            body    = part.get("body", {})
            raw     = body.get("data")
            att_id  = body.get("attachmentId")
            if raw:
                pdfs.append(base64.urlsafe_b64decode(raw))
            elif att_id:
                att = service.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=att_id
                ).execute()
                pdfs.append(base64.urlsafe_b64decode(att["data"]))
        sub = part.get("parts", [])
        if sub:
            _walk_parts(service, msg_id, sub, pdfs)


def fetch_fiserv_emails() -> list[dict]:
    """Return [{id, pdfs:[bytes]}] for unprocessed Fiserv emails with PDF attachments."""
    creds     = get_google_creds()
    service   = build("gmail", "v1", credentials=creds, cache_discovery=False)
    processed = _load_processed()

    sender_q  = " OR ".join(f"from:{d}" for d in FISERV_DOMAINS)
    query     = f"({sender_q}) has:attachment"
    log.info(f"Buscando en Gmail: {query}")

    new_ids: list[str] = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for m in resp.get("messages", []):
            if m["id"] not in processed:
                new_ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info(f"Emails nuevos encontrados: {len(new_ids)}")
    result = []
    for msg_id in new_ids:
        msg  = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
        pdfs: list[bytes] = []
        payload = msg.get("payload", {})
        _walk_parts(service, msg_id, payload.get("parts", [payload]), pdfs)
        if pdfs:
            result.append({"id": msg_id, "pdfs": pdfs})
        else:
            log.warning(f"Email {msg_id[:10]} sin adjuntos PDF — omitiendo")
            result.append({"id": msg_id, "pdfs": [], "_skip": True})
    return result


# ── PDF parsing ────────────────────────────────────────────────────────────────

_FISERV_PROMPT = (
    "Analizá esta imagen de una liquidación de Fiserv (procesadora de pagos).\n\n"
    "Extraé todos los datos y respondé SOLO con este JSON (sin texto adicional):\n\n"
    "{\n"
    '  "comercio": "nombre del comercio",\n'
    '  "rut": "número de RUT sin puntos ni guiones",\n'
    '  "moneda": "PESOS o DOLARES",\n'
    '  "periodo_desde": "DD.MM.YYYY",\n'
    '  "periodo_hasta": "DD.MM.YYYY",\n'
    '  "conceptos": [\n'
    "    {\n"
    '      "concepto": "nombre (MASTERCARD, MC DEBIT, VISA CRÉDITO, MAESTRO, VISA DEBITO, PREPAGA-ROU, CARGO TERMINAL FISER, etc)",\n'
    '      "plan": "Contado u otro plan",\n'
    '      "cant_operaciones": 0,\n'
    '      "importe_venta": 0.00,\n'
    '      "precio_servicio": 0.00,\n'
    '      "porcentaje": 0.00,\n'
    '      "iva": 0.00,\n'
    '      "leyes_cargos": 0.00,\n'
    '      "neto_a_cobrar": 0.00,\n'
    '      "semana_pago_desde": "DD.MM.YYYY",\n'
    '      "semana_pago_hasta": "DD.MM.YYYY"\n'
    "    }\n"
    "  ],\n"
    '  "total_venta": 0.00,\n'
    '  "total_precio_servicio": 0.00,\n'
    '  "total_iva": 0.00,\n'
    '  "total_neto": 0.00,\n'
    '  "adelantos": null\n'
    "}\n\n"
    "REGLAS:\n"
    "1. Uruguay: punto=miles, coma=decimal. Convertí a número (ej: '1.234,56' → 1234.56)\n"
    "2. Una fila por cada tarjeta/concepto en la tabla principal\n"
    "3. neto_a_cobrar puede ser negativo si el comercio paga a Fiserv\n"
    "4. Si hay sección 'Adelantos', sumá los importes y ponerlos en 'adelantos'\n"
    "5. null para campos ausentes, 0.0 para campos en cero"
)


def parse_fiserv_pdf(pdf_bytes: bytes) -> dict:
    """Convert PDF → image(s) → Claude Vision → structured dict."""
    if not _PDF2IMAGE_OK:
        raise RuntimeError("pdf2image no está instalado. Ejecutá: pip install pdf2image")

    try:
        images = _pdf2image.convert_from_bytes(
            pdf_bytes,
            dpi=150,
            fmt="jpeg",
            thread_count=1,
        )
    except Exception as e:
        raise ValueError(f"Error convirtiendo PDF a imagen: {e}")
    finally:
        del pdf_bytes
        gc.collect()

    content: list[dict] = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        content.append({
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": "image/jpeg",
                "data":       base64.b64encode(buf.getvalue()).decode(),
            },
        })
        buf.close()
        del img
    del images
    gc.collect()

    content.append({"type": "text", "text": _FISERV_PROMPT})

    response = _anthropic.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    raw   = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Claude no devolvió JSON válido: {raw[:300]}")
    return json.loads(match.group())


def _classify_pdf(data: dict) -> str:
    """Return 'cargo_terminal' or 'liquidacion' based on parsed PDF data."""
    conceptos = data.get("conceptos") or []
    if len(conceptos) == 1:
        nombre = (conceptos[0].get("concepto") or "").upper()
        if "CARGO" in nombre or "TERMINAL" in nombre:
            return "cargo_terminal"
    return "liquidacion"


def _fmt_importe(value: float) -> str:
    """Format as Uruguayan currency string: 1.234,56"""
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


# ── Google Sheets write ────────────────────────────────────────────────────────

def save_to_sheets(data: dict, email_id: str = "") -> str:
    """Persist parsed Fiserv liquidación to Google Sheets. Returns the sheet URL."""
    creds   = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sid     = _ensure_sheet(service)
    url     = f"https://docs.google.com/spreadsheets/d/{sid}"

    periodo_desde = data.get("periodo_desde", "")
    periodo_hasta = data.get("periodo_hasta", "")

    # Deduplication check
    existing = service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{TAB_RESUMEN}!B:C"
    ).execute()
    for row in existing.get("values", [])[1:]:
        if len(row) >= 2 and row[0] == periodo_desde and row[1] == periodo_hasta:
            log.info(f"Período {periodo_desde}–{periodo_hasta} ya existe, omitiendo")
            return url

    total_comision = (data.get("total_precio_servicio") or 0) + (data.get("total_iva") or 0)

    service.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"{TAB_RESUMEN}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[
            datetime.now().strftime("%d/%m/%Y %H:%M"),
            periodo_desde,
            periodo_hasta,
            data.get("total_venta")            or 0,
            data.get("total_neto")             or 0,
            round(total_comision, 2),
            data.get("total_iva")              or 0,
            len(data.get("conceptos") or []),
            email_id,
        ]]},
    ).execute()

    detalle_rows = [
        [
            periodo_desde,
            periodo_hasta,
            c.get("concepto",        ""),
            c.get("plan",            ""),
            c.get("cant_operaciones") or 0,
            c.get("importe_venta")    or 0,
            c.get("precio_servicio")  or 0,
            c.get("porcentaje")       or 0,
            c.get("iva")              or 0,
            c.get("leyes_cargos")     or 0,
            c.get("neto_a_cobrar")    or 0,
            c.get("semana_pago_desde", ""),
            c.get("semana_pago_hasta", ""),
        ]
        for c in (data.get("conceptos") or [])
    ]
    if detalle_rows:
        service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"{TAB_DETALLE}!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": detalle_rows},
        ).execute()

    log.info(f"Liquidación guardada: {periodo_desde}–{periodo_hasta}")
    return url


def save_cargo_terminal(data: dict, email_id: str = "") -> str:
    """Persist a cargo terminal PDF to the Cargos Terminal tab. Returns the sheet URL."""
    creds   = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sid     = _ensure_sheet(service)
    url     = f"https://docs.google.com/spreadsheets/d/{sid}"

    conceptos = data.get("conceptos") or []
    concepto  = conceptos[0].get("concepto", "") if conceptos else ""
    importe   = float(
        (conceptos[0].get("neto_a_cobrar") if conceptos else None)
        or data.get("total_neto")
        or 0
    )

    service.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"{TAB_CARGOS}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[
            datetime.now().strftime("%d/%m/%Y %H:%M"),
            concepto,
            importe,
            email_id,
        ]]},
    ).execute()

    log.info(f"Cargo terminal guardado en pestaña separada: ${_fmt_importe(importe)}")
    return url


# ── Sheets read (for WhatsApp queries) ────────────────────────────────────────

_FISERV_CACHE: dict = {}
_CACHE_TTL = 300  # 5 minutes


def get_fiserv_data(start_date: str, end_date: str) -> dict:
    """Query Sheets for Fiserv data overlapping the given date range."""
    cache_key = f"{start_date}|{end_date}"
    cached    = _FISERV_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    sheets_id = _get_sheets_id()
    if not sheets_id:
        return {
            "conceptos": [], "total_venta": 0, "total_neto": 0, "total_comision": 0,
            "sheets_url": "", "error": "Sheets no configurado — ejecutá 'actualizá Fiserv' primero.",
        }

    creds   = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result  = service.spreadsheets().values().get(
        spreadsheetId=sheets_id, range=f"{TAB_DETALLE}!A:M"
    ).execute()
    rows = result.get("values", [])

    d1 = datetime.strptime(start_date, "%Y-%m-%d")
    d2 = datetime.strptime(end_date,   "%Y-%m-%d")

    def _parse_date(s: str) -> datetime | None:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    def _f(row, idx, default=0.0):
        try:
            return float(row[idx]) if len(row) > idx and row[idx] else default
        except (ValueError, TypeError):
            return default

    conceptos      = []
    total_venta    = 0.0
    total_neto     = 0.0
    total_comision = 0.0
    real_desde: datetime | None = None
    real_hasta: datetime | None = None

    for row in rows[1:]:
        if len(row) < 2:
            continue
        row_desde = _parse_date(row[0])
        row_hasta = _parse_date(row[1])
        if not row_desde or not row_hasta:
            continue
        if row_hasta < d1 or row_desde > d2:
            continue
        if real_desde is None or row_desde < real_desde:
            real_desde = row_desde
        if real_hasta is None or row_hasta > real_hasta:
            real_hasta = row_hasta
        c = {
            "concepto":        row[2] if len(row) > 2 else "",
            "plan":            row[3] if len(row) > 3 else "",
            "cant_operaciones": int(_f(row, 4)),
            "importe_venta":   _f(row, 5),
            "precio_servicio": _f(row, 6),
            "porcentaje":      _f(row, 7),
            "iva":             _f(row, 8),
            "neto_a_cobrar":   _f(row, 10),
        }
        conceptos.append(c)
        total_venta    += c["importe_venta"]
        total_neto     += c["neto_a_cobrar"]
        total_comision += c["precio_servicio"] + c["iva"]

    url  = f"https://docs.google.com/spreadsheets/d/{sheets_id}"
    data = {
        "conceptos":      conceptos,
        "total_venta":    round(total_venta,    2),
        "total_neto":     round(total_neto,     2),
        "total_comision": round(total_comision, 2),
        "sheets_url":     url,
        "real_desde":     real_desde.strftime("%d/%m/%Y") if real_desde else "",
        "real_hasta":     real_hasta.strftime("%d/%m/%Y") if real_hasta else "",
    }
    _FISERV_CACHE[cache_key] = {"ts": time.time(), "data": data}
    return data


def invalidate_cache():
    _FISERV_CACHE.clear()


# ── migration ──────────────────────────────────────────────────────────────────

def migrate_none_rows():
    """Move None–None rows from Resumen/Detalle tabs to Cargos Terminal."""
    creds   = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sid     = _ensure_sheet(service)

    resumen_resp = service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{TAB_RESUMEN}!A:I"
    ).execute()
    resumen_rows = resumen_resp.get("values", [])

    detalle_resp = service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{TAB_DETALLE}!A:M"
    ).execute()
    detalle_rows = detalle_resp.get("values", [])

    # 0-based array indices of None–None rows (index 0 = header, skip it)
    resumen_none = [
        i for i, row in enumerate(resumen_rows)
        if i > 0
        and not (row[1] if len(row) > 1 else "")
        and not (row[2] if len(row) > 2 else "")
    ]
    detalle_none = [
        i for i, row in enumerate(detalle_rows)
        if i > 0
        and not (row[0] if len(row) > 0 else "")
        and not (row[1] if len(row) > 1 else "")
    ]

    if not resumen_none:
        log.info("No se encontraron registros None–None para migrar")
        return

    log.info(f"Migrando {len(resumen_none)} registros None–None a '{TAB_CARGOS}'")

    # Build rows for Cargos Terminal (match Resumen↔Detalle by position)
    cargo_rows = []
    for pos, res_idx in enumerate(resumen_none):
        row      = resumen_rows[res_idx]
        fecha    = row[0] if len(row) > 0 else ""
        neto     = row[4] if len(row) > 4 else ""
        email_id = row[8] if len(row) > 8 else ""

        concepto = ""
        if pos < len(detalle_none):
            det_row  = detalle_rows[detalle_none[pos]]
            concepto = det_row[2] if len(det_row) > 2 else ""

        cargo_rows.append([fecha, concepto, neto, email_id])

    service.spreadsheets().values().append(
        spreadsheetId=sid,
        range=f"{TAB_CARGOS}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": cargo_rows},
    ).execute()

    # Delete None rows in reverse order to preserve row indices during deletion
    resumen_tab_id = _get_tab_id(service, sid, TAB_RESUMEN)
    detalle_tab_id = _get_tab_id(service, sid, TAB_DETALLE)

    delete_requests = []
    for idx in sorted(resumen_none, reverse=True):
        delete_requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId":    resumen_tab_id,
                    "dimension":  "ROWS",
                    "startIndex": idx,
                    "endIndex":   idx + 1,
                }
            }
        })
    for idx in sorted(detalle_none, reverse=True):
        delete_requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId":    detalle_tab_id,
                    "dimension":  "ROWS",
                    "startIndex": idx,
                    "endIndex":   idx + 1,
                }
            }
        })

    if delete_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": delete_requests}
        ).execute()

    log.info(f"Migración completa: {len(resumen_none)} registros movidos a '{TAB_CARGOS}'")


# ── main orchestrator ──────────────────────────────────────────────────────────

def run_fiserv_sync() -> dict:
    """Fetch → parse → save all new Fiserv emails. Returns summary dict."""
    log.info("=== Iniciando sync Fiserv ===")
    processed     = _load_processed()
    new_processed: list[str] = []
    ok_count      = 0
    err_count     = 0
    sheets_url    = ""

    try:
        emails = fetch_fiserv_emails()
    except Exception as e:
        log.error(f"Error accediendo a Gmail: {e}")
        return {
            "procesados": 0, "errores": 1,
            "sheets_url": "", "mensaje": f"Error de Gmail: {e}",
        }

    if not emails:
        log.info("Sin emails nuevos")
        sid = _get_sheets_id()
        return {
            "procesados": 0, "errores": 0,
            "sheets_url": f"https://docs.google.com/spreadsheets/d/{sid}" if sid else "",
            "mensaje": "No hay liquidaciones nuevas de Fiserv.",
        }

    for email_data in emails:
        email_id = email_data["id"]
        new_processed.append(email_id)

        if email_data.get("_skip"):
            continue

        for pdf_bytes in email_data["pdfs"]:
            try:
                log.info(f"Procesando PDF del email {email_id[:10]}…")
                data     = parse_fiserv_pdf(pdf_bytes)
                pdf_type = _classify_pdf(data)

                if pdf_type == "cargo_terminal":
                    sheets_url = save_cargo_terminal(data, email_id)
                else:
                    sheets_url = save_to_sheets(data, email_id)

                ok_count += 1
            except Exception as e:
                log.error(f"Error en PDF del email {email_id[:10]}: {e}")
                err_count += 1
            finally:
                gc.collect()

    _save_processed(processed | set(new_processed))
    invalidate_cache()

    partes = [f"{ok_count} documentos procesados"]
    if err_count:
        partes.append(f"{err_count} con errores (ver fiserv_sync.log)")
    mensaje = ", ".join(partes)
    log.info(f"=== Sync finalizado: {mensaje} ===")
    return {
        "procesados": ok_count,
        "errores":    err_count,
        "sheets_url": sheets_url,
        "mensaje":    mensaje,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--auth" in sys.argv:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not CREDENTIALS_FILE.exists():
            print(f"ERROR: No se encontró {CREDENTIALS_FILE}")
            print("Descargá las credenciales OAuth2 desde Google Cloud Console")
            print("y subílas al servidor como gmail_credentials.json")
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
        auth_url, _ = flow.authorization_url(prompt="consent")
        print("\nAbri esta URL en tu browser:")
        print(auth_url)
        print()
        code = input('Pega el codigo de autorizacion aqui: ').strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
        TOKEN_FILE.write_text(creds.to_json())
        print(f"\n✓ Token guardado en {TOKEN_FILE}")

    elif "--sync" in sys.argv:
        result = run_fiserv_sync()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif "--migrate" in sys.argv:
        migrate_none_rows()

    elif "--test-pdf" in sys.argv:
        idx = sys.argv.index("--test-pdf")
        if idx + 1 >= len(sys.argv):
            print("Uso: python3 fiserv_sync.py --test-pdf /ruta/archivo.pdf")
            sys.exit(1)
        with open(sys.argv[idx + 1], "rb") as f:
            result = parse_fiserv_pdf(f.read())
        print(json.dumps(result, indent=2, ensure_ascii=False))
        pdf_type = _classify_pdf(result)
        print(f"\nTipo detectado: {pdf_type}")

    else:
        print("Uso:")
        print("  python3 fiserv_sync.py --auth                    # OAuth2 inicial")
        print("  python3 fiserv_sync.py --sync                    # Sincronizar emails")
        print("  python3 fiserv_sync.py --migrate                 # Mover None–None a Cargos Terminal")
        print("  python3 fiserv_sync.py --test-pdf archivo.pdf    # Probar parseo")
