# Asistente Lito

![Estado](https://img.shields.io/badge/estado-en%20desarrollo-yellow)
![Versión](https://img.shields.io/badge/versión-v2.6.0-blue)
![Python](https://img.shields.io/badge/python-3.10-blue)

Bot de WhatsApp para gestión de recaudaciones y consultas financieras de una empresa de transporte uruguaya.

---

## ¿Qué es?

Las empresas de transporte en Uruguay trabajan con múltiples fuentes de cobro: efectivo, POS a través de la red OCA y terminales Fiserv (First Data). Reconciliar estos datos manualmente —cruzando PDFs de liquidaciones semanales, consultas a la API de OCA y formularios físicos— consume tiempo y es propenso a errores.

Asistente Lito resuelve esto desde WhatsApp. El operador saca una foto del formulario de recaudación diario y el bot extrae los datos, los guarda en SQLite y los confirma con un resumen. Para consultas financieras, el bot entiende lenguaje natural en español rioplatense: "¿cuánto depositó OCA esta semana?" o "¿cuánto recaudaron los POS este mes?" y responde con datos reales de la API de OCA o de las liquidaciones Fiserv guardadas en Google Sheets.

El sistema corre en una instancia ARM de Oracle Cloud (capa gratuita), procesa imágenes y texto con Claude API, y se mantiene sincronizado con los PDFs de Fiserv vía Gmail cada 6 horas. El costo operativo estimado es de ~$0.70/mes en API calls.

---

## Funcionalidades

### Registro de recaudaciones por foto
El operador envía una foto del formulario físico por WhatsApp:

```
→ [foto del formulario de recaudación]
← "Leí los datos. ¿A qué vehículo pertenece esta recaudación?"
→ [selecciona de la lista]
← Resumen para confirmar (chofer, fecha, total, desglose)
→ "SI"
← "Recaudación guardada"
```

Extrae: chofer, fecha, total recaudado, comisión 30%, POS, fiado, efectivo empresa, combustible.  
Si el chofer no existe, ofrece registrarlo. Si ya hay una recaudación del mismo día, ofrece reemplazarla.

### Consultas OCA

```
"cuánto depositó OCA esta semana"
"mejor mes OCA 2026"
"comparar abril y mayo"
"resumen del año"
```

Rango de fechas, mejor/peor mes del año, resumen anual, comparación de dos períodos. Token JWT con renovación automática y paginación transparente.

### Consultas Fiserv

```
"comisión Fiserv de mayo"
"cuánto con débito esta semana"
"actualizá Fiserv"
```

Lee de Google Sheets. Filtra por superposición de período (los PDFs de Fiserv abarcan semanas que cruzan meses). Muestra el rango real de las liquidaciones incluidas, no el rango solicitado.

### Consulta combinada POS

```
"cuánto recaudaron los POS este mes"
"cuánto entraron los terminales en mayo"
```

Suma OCA + Fiserv en una sola respuesta:

```
*POS — 27/04/2026 al 25/05/2026*

* OCA: $15.640
* Fiserv: $49.605

*Total: $65.245*
```

### Administración de choferes y empresas

```
"agregá a Martínez como chofer"
"dar de baja a González"
"listá los choferes"
"agregá empresa MIDES"
```

### Dashboard web

Panel en `http://<servidor>:8000/admin` (HTTP Basic Auth):

- Resumen mensual por chofer: total, días trabajados, promedio diario, mejor día
- Gráfico de barras diario apilado (POS / Fiado / Efectivo neto)
- Gráfico donut de tipos de cobro
- Tabla detallada de registros (hasta 500 filas)

### Sincronización automática de Fiserv

Cada 6 horas el bot lee los correos de `CFE@fiserv.com` desde Gmail, convierte los PDFs a imágenes, los procesa con Claude Vision y guarda los datos en Google Sheets. Clasifica automáticamente entre liquidaciones semanales y cargos de terminal.

---

## Arquitectura

```
WhatsApp (usuario)
        │
        │  HTTPS / Meta Cloud API
        ▼
┌───────────────────────────────────────────────────────┐
│  FastAPI  ·  bot.py  ·  puerto 8000  ·  uvicorn       │
│                                                       │
│  Texto ──► _handle_combined_query()  ─────────────┐  │
│            _handle_oca_query()       ──────────┐  │  │
│            _handle_fiserv_query()    ───────┐  │  │  │
│            process_text()  (Claude chat)    │  │  │  │
│                                             │  │  │  │
│  Imagen ──► Claude Vision → JSON → SQLite   │  │  │  │
│                                             │  │  │  │
│  APScheduler (cada 6h)                      │  │  │  │
│    Gmail API → pdf2image → Claude Vision    │  │  │  │
│    → Google Sheets                          │  │  │  │
└─────────────────────────────────────────────┼──┼──┼──┘
                                              │  │  │
                         ┌────────────────────┘  │  │
                         │  ┌────────────────────┘  │
                         │  │  ┌────────────────────┘
                         ▼  ▼  ▼
              ┌──────────────────────────────┐
              │  OCA Comercios API  (JWT)    │
              │  Google Sheets  (Fiserv)     │
              │  SQLite  (recaudaciones.db)  │
              │  Claude API  (Anthropic)     │
              └──────────────────────────────┘
```

**Orden de detección de intención** (texto entrante):

1. `_handle_combined_query()` — palabras clave POS/POSNET/TERMINAL sin mención explícita de OCA o Fiserv
2. `_handle_oca_query()` — menciones a OCA o consultas de pagos
3. `_handle_fiserv_query()` — menciones a Fiserv, First Data o liquidaciones
4. `process_text()` — Claude Sonnet como fallback general con herramientas (CRUD de choferes, etc.)

La detección de intención en los pasos 1–3 usa Claude Haiku (rápido y barato); el procesamiento usa Sonnet.

---

## Stack tecnológico

| Componente | Tecnología | Costo |
|---|---|---|
| Servidor | Oracle Cloud ARM (1 vCPU, 1 GB RAM) | $0/mes — capa gratuita |
| Aplicación | FastAPI + uvicorn | — |
| OCR de formularios | Claude Sonnet 4.5 Vision | ~$0.15/mes |
| Chat y herramientas | Claude Sonnet 4.5 | ~$0.10/mes |
| Detección de intención | Claude Haiku 4.5 | ~$0.02/mes |
| Parseo de PDFs Fiserv | Claude Sonnet 4.5 Vision | ~$0.40/mes |
| Pagos OCA | OCA Comercios API (JWT) | $0 |
| Liquidaciones Fiserv | Gmail API + Google Sheets API | $0 (dentro de cuotas) |
| PDF → imagen | pdf2image (Poppler) | $0 |
| Base de datos | SQLite | $0 |
| Mensajería | Meta Cloud API (WhatsApp Business) | $0 (conversaciones inbound) |
| **Total estimado** | | **~$0.70/mes** |

---

## Configuración

### Variables de entorno

Copiar `.env.example` a `.env` y completar:

```bash
# WhatsApp (Meta Cloud API)
WHATSAPP_TOKEN=
PHONE_NUMBER_ID=
VERIFY_TOKEN=

# Claude API
ANTHROPIC_API_KEY=

# OCA Comercios API
OCA_API_URL=
OCA_ID_KEY=
OCA_SECRET_KEY=
OCA_RUT=

# Dashboard (HTTP Basic Auth)
ADMIN_USER=
ADMIN_PASSWORD=

# Números habilitados (vacío = todos)
ALLOWED_NUMBERS=

# Google (opcional si se usan los archivos por defecto)
GMAIL_CREDENTIALS_PATH=
GOOGLE_SHEETS_ID=
```

### Archivos de autenticación Google

Requeridos en el directorio del proyecto:

| Archivo | Descripción |
|---|---|
| `gmail_credentials.json` | Credenciales OAuth2 de Google Cloud Console |
| `gmail_token.json` | Token OAuth2 (se genera con `--auth`, se auto-refresca) |
| `fiserv_sheets_id.json` | ID de la Google Sheet de Fiserv |

### Instalación

```bash
git clone <repo>
cd asistente-lito
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Primera autenticación de Google
python3 fiserv_sync.py --auth

# Arrancar el bot
uvicorn bot:app --host 0.0.0.0 --port 8000
```

### Producción (systemd)

```bash
sudo systemctl start asistente-lito
sudo systemctl stop asistente-lito
sudo systemctl restart asistente-lito
sudo systemctl status asistente-lito
```

### Operaciones útiles

```bash
# Ver logs en tiempo real
tail -f ~/asistente-lito/bot.log
tail -f ~/asistente-lito/fiserv_sync.log

# Forzar sincronización Fiserv
python3 fiserv_sync.py --sync

# Testear parseo de un PDF
python3 fiserv_sync.py --test-pdf /ruta/archivo.pdf

# Backup manual de la base de datos
~/asistente-lito/backup.sh

# Commit y push de cambios
./commit.sh "descripción del cambio"
```

---

## Estado del proyecto

**~85% completo para el caso de uso principal.**

| Área | Estado |
|---|---|
| Registro de recaudaciones por foto | Funcionando |
| Consultas OCA (rango, año, comparación, mejor/peor mes) | Funcionando |
| Consultas Fiserv (liquidaciones semanales) | Funcionando |
| Consulta combinada POS (OCA + Fiserv) | Funcionando |
| Sincronización automática de emails Fiserv | Funcionando |
| Clasificación liquidación vs. cargo terminal | Funcionando |
| Administración de choferes y empresas | Funcionando |
| Dashboard web con gráficos | Funcionando |
| Backups automáticos de SQLite | Funcionando |

### Pendientes conocidos

- **Sin cola de mensajes:** si el servidor cae durante un reintento de webhook de Meta, el mensaje se puede perder. No hay deduplicación por `message_id`.
- **Datos Fiserv desde mayo 2024:** no hay registros anteriores a esa fecha.
- **`VERSION` en disco desactualizado:** el archivo dice `v2.1.0`; la versión real (`2.6.0`) está en `bot.py` y se muestra en el dashboard.

---

## Estructura del proyecto

```
asistente-lito/
├── bot.py                        # Aplicación principal (2337 líneas)
├── fiserv_sync.py                # Pipeline Fiserv: Gmail → PDF → Sheets (786 líneas)
├── oca_client.py                 # Cliente OCA API con JWT (75 líneas)
├── dashboard.py                  # Panel web: API JSON + HTML/JS (593 líneas)
├── backup.sh                     # Backup diario de SQLite (retiene 30 copias)
├── commit.sh                     # git add + commit + push
├── recaudaciones.db              # Base de datos SQLite (no versionada)
├── .env                          # Variables de entorno (no versionada)
├── .env.example                  # Plantilla de variables
├── gmail_credentials.json        # OAuth2 Google (no versionada)
├── gmail_token.json              # Token OAuth2 auto-refrescable (no versionado)
├── fiserv_processed_emails.json  # IDs de correos ya procesados (no versionado)
├── fiserv_sheets_id.json         # ID de la Google Sheet (no versionado)
├── VERSION                       # Versión del proyecto
├── ESTADO_PROYECTO.md            # Documentación técnica detallada del sistema
└── CONTRIBUTING.md               # Convenciones de versioning y commits
```
