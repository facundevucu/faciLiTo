# Asistente Lito — Estado del Proyecto

Fecha: 26/05/2026
Versión: 2.6.0 (bot.py) — nota: el archivo `VERSION` en disco dice v2.1.0, está desactualizado

---

## Estado general

**~85% completo para el caso de uso principal.**

El bot procesa recaudaciones diarias vía foto de WhatsApp, consulta pagos OCA y liquidaciones Fiserv, y expone un dashboard web de administración. Las tres fuentes de datos (SQLite, OCA API, Google Sheets) están operativas. El sistema lleva 11 días corriendo sin reinicios, con 56 recaudaciones registradas y 161 correos Fiserv procesados.

Pendiente principal: la migración de registros históricos `None–None` de Fiserv a la pestaña correcta.

---

## Arquitectura actual

```
WhatsApp (Meta Cloud API)
        │
        │  POST /webhook
        ▼
   FastAPI app  (bot.py · puerto 8000 · uvicorn)
        │
        ├── Mensaje de texto
        │       ├── _handle_combined_query()   ← "cuánto recaudaron los POS"
        │       │       └── OCA API + Google Sheets (Fiserv) en paralelo
        │       ├── _handle_oca_query()         ← "pagos OCA de mayo"
        │       │       └── OCAClient.get_pagos()
        │       ├── _handle_fiserv_query()      ← "comisión Fiserv esta semana"
        │       │       └── fiserv_sync.get_fiserv_data()
        │       └── process_text()              ← Claude Sonnet (chat general + herramientas)
        │               └── tool calls: agregar_chofer, dar_baja_chofer, etc.
        │
        ├── Mensaje con imagen (formulario de recaudación)
        │       └── Claude Sonnet 4.5 Vision → JSON → SQLite
        │
        ├── GET  /webhook          ← verificación inicial de Meta
        ├── GET  /admin/*          ← dashboard (HTTP Basic Auth)
        │
        └── Scheduler (APScheduler)
                └── fiserv_sync.run_fiserv_sync()  cada 6 horas
                        ├── Gmail API → PDF bytes
                        ├── pdf2image → JPEG
                        ├── Claude Sonnet 4.5 Vision → JSON estructurado
                        ├── _classify_pdf() → "liquidacion" | "cargo_terminal"
                        └── Google Sheets API (escritura)
```

---

## Funcionalidades operativas

### Registro de recaudaciones
El padre envía una foto del formulario de recaudación por WhatsApp.

```
→ Foto del formulario
← "Leí los datos. ¿A qué vehículo pertenece esta recaudación?"
→ Selecciona vehículo por lista
← Resumen de la recaudación para confirmar
→ "SI"
← "Recaudación guardada"
```

- Extrae: chofer, fecha, total recaudado, comisión 30%, total POS, fiado, efectivo empresa, combustible
- Calcula: efectivo neto, porcentaje digital, semana, mes, año
- Si el chofer no está registrado → pregunta si agregarlo
- Si ya existe una recaudación del mismo día/chofer → ofrece reemplazar

### Consultas OCA
```
"cuánto depositó OCA esta semana"
"mejor mes OCA 2026"
"comparar abril y mayo"
"resumen del año"
```
Soporta: rango de fechas, mejor/peor mes, resumen anual, comparación de períodos. Maneja paginación y renovación automática de token JWT.

### Consultas Fiserv
```
"comisión Fiserv de mayo"
"cuánto con débito esta semana"
"actualizá Fiserv"
```
Lee de Google Sheets. Filtra por superposición de período (no solo por fechas exactas). Muestra el rango real de los PDFs incluidos.

### Consulta combinada POS
```
"cuánto recaudaron los POS este mes"
"cuánto entraron los terminales en mayo"
```
Responde con OCA + Fiserv sumados:
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
`http://167.126.3.114:8000/admin` — requiere usuario/contraseña del .env

- Selección de mes y chofer
- Tarjetas de resumen (total, días, promedio, mejor día)
- Gráfico de barras apilado diario (POS / Fiado / Efectivo neto)
- Gráfico donut de tipos de cobro
- Tabla de choferes con totales
- Tabla de registros detallada (200 por defecto, máx 500)

---

## Integraciones activas

### WhatsApp (Meta Cloud API)
- **Estado:** Funcionando
- Webhook en `POST /webhook`, verificación en `GET /webhook`
- Soporta texto plano, imágenes, respuestas interactivas (listas, botones)
- Lista blanca de números en `ALLOWED_NUMBERS` (vacía = todos permitidos)
- Variables de entorno:
  - `WHATSAPP_TOKEN` — token de acceso permanente de Meta
  - `PHONE_NUMBER_ID` — ID del número de WhatsApp Business
  - `VERIFY_TOKEN` — token de verificación del webhook

### Claude API (Anthropic)
- **Estado:** Funcionando
- Tres usos diferenciados:
  1. **Procesamiento de imágenes** (recaudaciones): `claude-sonnet-4-5`, `max_tokens=1000`
  2. **Chat de texto** (respuestas generales y herramientas): `claude-sonnet-4-5`, `max_tokens=1000`
  3. **Detección de intención** (OCA, Fiserv, combinado): `claude-haiku-4-5-20251001`, `max_tokens=100–200`
  4. **Parseo de PDFs Fiserv**: `claude-sonnet-4-5`, `max_tokens=2000`
- Variable de entorno:
  - `ANTHROPIC_API_KEY`

### OCA Comercios API
- **Estado:** Funcionando
- Token JWT con vida útil de 300 segundos, se renueva automáticamente
- Paginación via `scroll_id`
- Reintentos automáticos en 401
- Variables de entorno:
  - `OCA_API_URL` — base URL de la API
  - `OCA_ID_KEY` — clave de identificación
  - `OCA_SECRET_KEY` — clave secreta
  - `OCA_RUT` — RUT del comercio

### Fiserv / Gmail / Google Sheets
- **Estado:** Funcionando (con bug de datos históricos — ver Bugs)
- Pipeline: Gmail API → PDF bytes → pdf2image (150 DPI) → Claude Vision → JSON → Google Sheets
- Sincronización automática cada 6 horas via APScheduler
- Clasificación de PDFs: `liquidacion` (múltiples conceptos) vs `cargo_terminal` (1 concepto con "CARGO"/"TERMINAL")
- 161 correos procesados al 26/05/2026
- Variables de entorno (autenticación OAuth2 via archivos):
  - `GMAIL_CREDENTIALS_PATH` — ruta a `gmail_credentials.json` (opcional, default en BASE_DIR)
  - `GOOGLE_SHEETS_ID` — ID de la planilla (opcional, también en `fiserv_sheets_id.json`)
- Archivos de estado:
  - `gmail_token.json` — token OAuth2 de Google (se auto-refresca)
  - `gmail_credentials.json` — credenciales OAuth2 de Google Cloud Console
  - `fiserv_processed_emails.json` — lista de IDs de correos ya procesados (deduplicación)
  - `fiserv_sheets_id.json` — ID de la Google Sheet

---

## Base de datos

### SQLite — `recaudaciones.db`

**Tabla `recaudaciones`** (56 registros)

| Columna | Tipo | Descripción |
|---|---|---|
| id | INTEGER PK | Autoincremental |
| fecha | TEXT | Fecha de la recaudación (YYYY-MM-DD) |
| chofer | TEXT | Nombre del chofer |
| vehiculo | TEXT | Terminal o Plaza |
| total_recaudado | REAL | Total bruto del día |
| comision_30 | REAL | Comisión del 30% |
| total_pos | REAL | Cobros por POS |
| total_fiado | REAL | Cobros fiados |
| efectivo_empresa | REAL | Efectivo de la empresa |
| otros_combustible | REAL | Combustible y otros |
| efectivo_neto | REAL | Calculado: total − comisión − POS − fiado |
| porcentaje_digital | REAL | POS / total_recaudado × 100 |
| dia_semana | TEXT | Lunes, Martes, etc. |
| semana_numero | INTEGER | Número de semana ISO |
| mes | INTEGER | 1–12 |
| anio | INTEGER | Año |
| creado_en | TEXT | Timestamp de guardado |

**Tabla `choferes`** (5 registros)

| Columna | Tipo |
|---|---|
| id | INTEGER PK |
| nombre | TEXT UNIQUE |
| activo | INTEGER (0/1) |
| creado_en | TEXT |
| desactivado_en | TEXT |

**Tabla `empresas`** (5 registros)

| Columna | Tipo |
|---|---|
| id | INTEGER PK |
| nombre | TEXT UNIQUE |
| rut | TEXT |
| activo | INTEGER (0/1) |
| creado_en | TEXT |
| desactivado_en | TEXT |

**Tabla `recaudacion_empresas`** (29 registros) — relación N:N

| Columna | Tipo |
|---|---|
| id | INTEGER PK |
| recaudacion_id | INTEGER FK → recaudaciones.id |
| empresa_id | INTEGER FK → empresas.id |
| monto | REAL |
| UNIQUE | (recaudacion_id, empresa_id) |

Empresas iniciales: PUL, Lumin Civil, Lumin Energia, MIDES, Hospital.

### Google Sheets — Fiserv Liquidaciones

ID: `1tgKgEXWOMW_BXjrow2SuUnb74oZPydwe1pTMkaAukHo`

**Pestaña "Resumen"**

| Columna | Contenido |
|---|---|
| A | Fecha Proceso |
| B | Período Desde (DD.MM.YYYY) |
| C | Período Hasta (DD.MM.YYYY) |
| D | Total Venta |
| E | Total Neto |
| F | Total Comisión |
| G | Total IVA |
| H | Cant. Conceptos |
| I | Email ID |

**Pestaña "Detalle"**

| Columna | Contenido |
|---|---|
| A | Período Desde |
| B | Período Hasta |
| C | Concepto (VISA DEBITO, MC DEBIT, etc.) |
| D | Plan |
| E | Cant. Operaciones |
| F | Importe Venta |
| G | Precio Servicio |
| H | Porcentaje |
| I | IVA |
| J | Leyes y Cargos |
| K | Neto a Cobrar |
| L | Semana Pago Desde |
| M | Semana Pago Hasta |

**Pestaña "Cargos Terminal"**

| Columna | Contenido |
|---|---|
| A | Fecha Proceso |
| B | Concepto |
| C | Importe |
| D | Email ID |

---

## Archivos del proyecto

| Archivo | Líneas | Descripción |
|---|---|---|
| `bot.py` | 2337 | Aplicación principal: FastAPI, manejo de WhatsApp, intenciones, Claude, SQLite |
| `fiserv_sync.py` | 786 | Pipeline Gmail → PDF → Claude Vision → Google Sheets para liquidaciones Fiserv |
| `oca_client.py` | 75 | Cliente HTTP para la API OCA Comercios con token JWT y paginación |
| `dashboard.py` | 593 | Panel de administración web: API JSON + HTML/JS single-page con Chart.js |
| `backup.sh` | — | Copia `recaudaciones.db` a `/home/ubuntu/backups/`, retiene últimos 30 |
| `commit.sh` | — | `git add` + `git commit` + `git push` de los archivos fuente |
| `recaudaciones.db` | — | Base de datos SQLite local |
| `bot.log` | 523 líneas | Log rotativo del bot (2 MB máx, 2 backups) |
| `fiserv_sync.log` | 1122 líneas | Log rotativo del pipeline Fiserv |
| `fiserv_processed_emails.json` | — | IDs de correos Gmail ya procesados (161 entradas) |
| `fiserv_sheets_id.json` | — | ID de la Google Sheet de Fiserv |
| `gmail_token.json` | — | Token OAuth2 Google (se refresca automáticamente) |
| `gmail_credentials.json` | — | Credenciales OAuth2 de Google Cloud Console |
| `.env` | — | Variables de entorno (no está en git) |
| `.env.example` | — | Plantilla de variables de entorno |
| `VERSION` | — | Dice `v2.1.0` — desactualizado, la versión real es `2.6.0` en bot.py |
| `CONTRIBUTING.md` | — | Convenciones de versioning y guía de commits |
| `.gitignore` | — | Excluye .env, *.db, *.log, *.bak, venv/ |

---

## Bugs conocidos / pendientes

### 1. Datos históricos Fiserv — registros `None–None` sin migrar
Los PDFs de "Cargo Terminal" (factura mensual del POS, ~$943) se guardaron en la pestaña "Resumen" con `periodo_desde` y `periodo_hasta` vacíos antes de implementar la clasificación. Todavía existen en la hoja principal.

**Solución implementada, pendiente de ejecutar:**
```bash
cd ~/asistente-lito && source venv/bin/activate
python3 fiserv_sync.py --migrate
```

### 2. Logs de debug temporales (`[DBG]`) en producción
Durante la sesión de debugging del bug OCA+Fiserv se agregaron líneas `log.info("[DBG] ...")` que siguen activas. No afectan funcionalidad pero agregan ruido al log. Pueden removerse cuando se confirme que el sistema funciona correctamente.

### 3. `commit.sh` no versiona `fiserv_sync.py` ni `oca_client.py`
El script hace `git add` explícito y no incluye esos dos archivos. Todos los cambios del pipeline Fiserv y del cliente OCA solo están en el servidor, no en el repositorio.

### 4. Archivo `VERSION` desactualizado
`VERSION` en disco dice `v2.1.0`, pero la variable `VERSION` en `bot.py` es `"2.6.0"`. El dashboard muestra la de `bot.py`, pero el archivo de texto es engañoso.

### 5. Google Sheets: datos históricos desde 2024
Los PDFs procesados arrancan desde mayo 2024. Las consultas de Fiserv funcionan bien para ese período, pero no hay datos anteriores.

### 6. Sin webhook de reintento para WhatsApp
Si el servidor cae mientras Meta reintenta un webhook, el mensaje se puede perder. La arquitectura actual no tiene cola de mensajes ni deduplicación por `message_id` de WhatsApp.

---

## Cómo correr el proyecto

### Ver estado del servicio
```bash
sudo systemctl status asistente-lito
```

### Iniciar / detener / reiniciar
```bash
sudo systemctl start asistente-lito
sudo systemctl stop asistente-lito
sudo systemctl restart asistente-lito
```

### Ver logs en tiempo real
```bash
# Log del bot (WhatsApp, recaudaciones, OCA, intenciones)
tail -f ~/asistente-lito/bot.log

# Log del pipeline Fiserv
tail -f ~/asistente-lito/fiserv_sync.log

# Log del sistema (incluye stdout/stderr del proceso)
sudo journalctl -u asistente-lito -f
```

### Forzar sincronización Fiserv manualmente
```bash
cd ~/asistente-lito && source venv/bin/activate
python3 fiserv_sync.py --sync
```

### Migrar registros None–None a pestaña Cargos Terminal
```bash
cd ~/asistente-lito && source venv/bin/activate
python3 fiserv_sync.py --migrate
```

### Testear parseo de un PDF localmente
```bash
cd ~/asistente-lito && source venv/bin/activate
python3 fiserv_sync.py --test-pdf /ruta/archivo.pdf
```

### Re-autenticar Google (si el token expira)
```bash
cd ~/asistente-lito && source venv/bin/activate
python3 fiserv_sync.py --auth
```

### Hacer commit de cambios
```bash
cd ~/asistente-lito
./commit.sh "descripción del cambio"
```

### Hacer backup manual de la DB
```bash
~/asistente-lito/backup.sh
```

### Dashboard web
```
http://167.126.3.114:8000/admin
```

---

## Costos operativos actuales

### Servidor (Oracle Cloud Free Tier)
- **Costo: $0/mes** — instancia ARM en capa gratuita
- 1 vCPU, 1 GB RAM (128 MB en uso activo)
- Ubuntu 22.04, kernel 6.8.0-1049-oracle

### Claude API (Anthropic)
Estimación basada en el uso real observado:

| Uso | Modelo | Frecuencia estimada | Costo aprox/mes |
|---|---|---|---|
| OCR de recaudaciones (imagen → JSON) | Sonnet 4.5 | ~30 imágenes/mes | ~$0.15 |
| Respuestas de texto (chat general) | Sonnet 4.5 | ~50 mensajes/mes | ~$0.10 |
| Detección de intención OCA/Fiserv/combinado | Haiku 4.5 | ~100 llamadas/mes | ~$0.02 |
| Parseo de PDFs Fiserv (imagen → JSON) | Sonnet 4.5 | ~20 PDFs/mes (4 semanas × 2 liquidaciones + 2 cargos terminal) | ~$0.40 |
| **Total estimado** | | | **~$0.70/mes** |

El costo real depende del volumen de recaudaciones y de cuántas consultas hace el padre por WhatsApp. A mayor uso conversacional (consultas de OCA, Fiserv, comparaciones), el costo sube linealmente con los mensajes.

### APIs externas
- **WhatsApp Business API (Meta):** Gratis para conversaciones iniciadas por el usuario (el padre escribe primero). Si el bot iniciara conversaciones, habría costo por plantilla.
- **OCA Comercios API:** Sin costo aparente (acceso con credenciales del comercio).
- **Google Sheets/Gmail API:** Gratis dentro de cuotas de Google (no se acerca a los límites con el volumen actual).
