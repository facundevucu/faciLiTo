import os
import sqlite3
import secrets
import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

ADMIN_USER     = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

BASE_DIR = "/home/ubuntu/asistente-lito"
DB_PATH  = os.path.join(BASE_DIR, "recaudaciones.db")

_VERSION    = "unknown"
_START_TIME = time.time()

security = HTTPBasic()
dashboard_router = APIRouter(prefix="/admin", tags=["admin"])


def setup_dashboard(version: str, start_time: float):
    global _VERSION, _START_TIME
    _VERSION    = version
    _START_TIME = start_time


def _auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD no configurado en .env")
    ok = (
        secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode()) and
        secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": 'Basic realm="Lito Admin"'},
        )


def _db():
    return sqlite3.connect(DB_PATH)


# ── endpoints ─────────────────────────────────────────────────────────────────

@dashboard_router.get("", response_class=HTMLResponse, dependencies=[Depends(_auth)])
async def admin_page():
    return HTML_TEMPLATE


@dashboard_router.get("/api/health", dependencies=[Depends(_auth)])
async def admin_health():
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM recaudaciones")
        count = c.fetchone()[0]
        c.execute("SELECT creado_en FROM recaudaciones ORDER BY creado_en DESC LIMIT 1")
        last = c.fetchone()
        conn.close()
        db_ok = True
    except Exception:
        count, last, db_ok = 0, None, False
    return {
        "status": "ok" if db_ok else "degraded",
        "db_records": count,
        "version": _VERSION,
        "uptime_seconds": round(time.time() - _START_TIME),
        "last_record": last[0] if last else None,
    }


@dashboard_router.get("/api/months", dependencies=[Depends(_auth)])
async def admin_months():
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT mes, anio FROM recaudaciones WHERE mes IS NOT NULL "
        "ORDER BY anio DESC, mes DESC"
    )
    rows = c.fetchall()
    conn.close()
    return [{"mes": r[0], "anio": r[1]} for r in rows]


@dashboard_router.get("/api/choferes", dependencies=[Depends(_auth)])
async def admin_choferes():
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT nombre, activo FROM choferes ORDER BY activo DESC, nombre")
    registered = c.fetchall()
    c.execute(
        "SELECT DISTINCT chofer FROM recaudaciones WHERE chofer IS NOT NULL "
        "AND UPPER(chofer) NOT IN (SELECT UPPER(nombre) FROM choferes)"
    )
    legacy = c.fetchall()
    conn.close()
    result  = [{"nombre": r[0], "activo": r[1]} for r in registered]
    result += [{"nombre": r[0], "activo": 1} for r in legacy]
    return result


@dashboard_router.get("/api/empresas", dependencies=[Depends(_auth)])
async def admin_empresas():
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT nombre, rut, activo FROM empresas ORDER BY activo DESC, nombre")
    rows = c.fetchall()
    conn.close()
    return [{"nombre": r[0], "rut": r[1], "activo": r[2]} for r in rows]


@dashboard_router.get("/api/summary", dependencies=[Depends(_auth)])
async def admin_summary(mes: int = None, anio: int = None):
    now  = datetime.now()
    mes  = mes  or now.month
    anio = anio or now.year
    conn = _db()
    c    = conn.cursor()

    c.execute(
        "SELECT SUM(total_recaudado), COUNT(*), AVG(total_recaudado) "
        "FROM recaudaciones WHERE mes=? AND anio=?", (mes, anio)
    )
    r = c.fetchone()
    total, count, avg = (r[0] or 0), (r[1] or 0), (r[2] or 0)

    c.execute(
        "SELECT fecha, dia_semana, SUM(total_recaudado) AS t FROM recaudaciones "
        "WHERE mes=? AND anio=? GROUP BY fecha ORDER BY t DESC LIMIT 1", (mes, anio)
    )
    best = c.fetchone()

    c.execute(
        "SELECT chofer, SUM(total_recaudado), COUNT(*), SUM(efectivo_neto), AVG(total_recaudado) "
        "FROM recaudaciones WHERE mes=? AND anio=? GROUP BY chofer ORDER BY SUM(total_recaudado) DESC",
        (mes, anio)
    )
    choferes = [
        {"chofer": r[0] or "Sin nombre", "total": round(r[1] or 0), "dias": r[2],
         "efectivo_neto": round(r[3] or 0), "promedio": round(r[4] or 0)}
        for r in c.fetchall()
    ]

    c.execute(
        "SELECT SUM(total_pos), SUM(total_fiado), SUM(efectivo_neto), SUM(otros_combustible) "
        "FROM recaudaciones WHERE mes=? AND anio=?", (mes, anio)
    )
    p = c.fetchone()

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
    by_empresa = [{"empresa": r[0], "total": round(r[1] or 0)} for r in c.fetchall()]

    conn.close()
    return {
        "mes": mes, "anio": anio,
        "total_recaudado":  round(total),
        "dias_trabajados":  count,
        "promedio_por_dia": round(avg),
        "mejor_dia": {"fecha": best[0], "dia": best[1], "total": round(best[2])} if best else None,
        "choferes": choferes,
        "pagos": {
            "pos":           round(p[0] or 0),
            "fiado":         round(p[1] or 0),
            "efectivo_neto": round(p[2] or 0),
            "combustible":   round(p[3] or 0),
        },
        "by_empresa": by_empresa,
    }


@dashboard_router.get("/api/daily", dependencies=[Depends(_auth)])
async def admin_daily(mes: int = None, anio: int = None):
    now  = datetime.now()
    mes  = mes  or now.month
    anio = anio or now.year
    conn = _db()
    c    = conn.cursor()
    c.execute(
        "SELECT fecha, dia_semana, SUM(total_recaudado), SUM(total_pos), "
        "SUM(total_fiado), SUM(efectivo_neto) "
        "FROM recaudaciones WHERE mes=? AND anio=? GROUP BY fecha ORDER BY fecha",
        (mes, anio)
    )
    rows = c.fetchall()
    conn.close()
    return [
        {"fecha": r[0], "dia": r[1], "total": round(r[2] or 0),
         "pos": round(r[3] or 0), "fiado": round(r[4] or 0), "efectivo_neto": round(r[5] or 0)}
        for r in rows
    ]


@dashboard_router.get("/api/records", dependencies=[Depends(_auth)])
async def admin_records(
    mes: int = None,
    anio: int = None,
    chofer: str = None,
    limit: int = Query(default=200, le=500),
):
    now  = datetime.now()
    mes  = mes  or now.month
    anio = anio or now.year
    conn = _db()
    c    = conn.cursor()
    sql = (
        "SELECT id, fecha, dia_semana, chofer, "
        "total_recaudado, comision_30, total_pos, total_fiado, "
        "efectivo_neto, porcentaje_digital, "
        "otros_combustible, efectivo_empresa, creado_en "
        "FROM recaudaciones WHERE mes=? AND anio=?"
    )
    params = [mes, anio]
    if chofer:
        sql += " AND chofer=?"
        params.append(chofer)
    sql += " ORDER BY fecha DESC, creado_en DESC LIMIT ?"
    params.append(limit)
    c.execute(sql, params)
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    return rows


@dashboard_router.get("/api/trend", dependencies=[Depends(_auth)])
async def admin_trend():
    conn = _db()
    c    = conn.cursor()
    c.execute(
        "SELECT mes, anio, SUM(total_recaudado), COUNT(*), SUM(total_pos), SUM(efectivo_neto) "
        "FROM recaudaciones WHERE mes IS NOT NULL "
        "GROUP BY anio, mes ORDER BY anio DESC, mes DESC LIMIT 12"
    )
    rows = c.fetchall()
    conn.close()
    return [
        {"mes": r[0], "anio": r[1], "total": round(r[2] or 0), "dias": r[3],
         "pos": round(r[4] or 0), "efectivo_neto": round(r[5] or 0)}
        for r in reversed(rows)
    ]


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lito Admin</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  .loading { opacity: 0.4; }
</style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen">
<div class="max-w-screen-2xl mx-auto px-6 py-6">

  <!-- Header -->
  <header class="flex items-center justify-between mb-6 pb-5 border-b border-slate-200">
    <div>
      <h1 class="text-xl font-bold text-slate-900">Asistente Lito <span class="text-slate-400 font-normal">/ Admin</span></h1>
    </div>
    <div class="flex items-center gap-3 text-sm">
      <span id="badge-status" class="px-2.5 py-1 rounded-full font-medium bg-slate-100 text-slate-500">● Conectando...</span>
      <span id="badge-version" class="text-slate-400"></span>
      <span id="badge-uptime" class="text-slate-400 hidden sm:inline"></span>
      <span id="badge-db" class="text-slate-400 hidden sm:inline"></span>
    </div>
  </header>

  <!-- Controls -->
  <div class="flex flex-wrap items-center gap-3 mb-6 p-4 bg-white rounded-xl border border-slate-100 shadow-sm">
    <div class="flex items-center gap-2">
      <label class="text-sm font-medium text-slate-500">Período</label>
      <select id="sel-mes" onchange="loadAll()" class="border border-slate-200 bg-white rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer"></select>
    </div>
    <div class="flex items-center gap-2">
      <label class="text-sm font-medium text-slate-500">Chofer</label>
      <select id="sel-chofer" onchange="loadAll()" class="border border-slate-200 bg-white rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer">
        <option value="">Todos</option>
      </select>
    </div>
    <button onclick="loadAll()" class="ml-auto flex items-center gap-1.5 text-sm text-slate-600 border border-slate-200 bg-white rounded-lg px-3 py-1.5 hover:bg-slate-50 transition">
      <span id="refresh-icon">↻</span> Actualizar
    </button>
  </div>

  <!-- Summary cards -->
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    <div class="bg-white rounded-xl p-5 border border-slate-100 shadow-sm">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">Total recaudado</p>
      <p id="c-total" class="text-2xl font-bold text-slate-900">—</p>
    </div>
    <div class="bg-white rounded-xl p-5 border border-slate-100 shadow-sm">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">Días trabajados</p>
      <p id="c-dias" class="text-2xl font-bold text-slate-900">—</p>
    </div>
    <div class="bg-white rounded-xl p-5 border border-slate-100 shadow-sm">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">Promedio / día</p>
      <p id="c-promedio" class="text-2xl font-bold text-slate-900">—</p>
    </div>
    <div class="bg-white rounded-xl p-5 border border-slate-100 shadow-sm">
      <p class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">Mejor día</p>
      <p id="c-mejor" class="text-base font-bold text-slate-900">—</p>
      <p id="c-mejor-total" class="text-sm text-emerald-600 font-semibold mt-0.5">—</p>
    </div>
  </div>

  <!-- Charts row -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
    <div class="lg:col-span-2 bg-white rounded-xl p-5 border border-slate-100 shadow-sm">
      <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Recaudación diaria</h2>
      <canvas id="chart-daily"></canvas>
      <p id="chart-daily-empty" class="text-center text-slate-400 text-sm py-8 hidden">Sin datos para este período</p>
    </div>
    <div class="bg-white rounded-xl p-5 border border-slate-100 shadow-sm flex flex-col">
      <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Tipos de cobro</h2>
      <div class="relative flex-1" style="min-height:160px">
        <canvas id="chart-donut"></canvas>
      </div>
      <div id="donut-legend" class="mt-4 space-y-2 text-sm"></div>
    </div>
  </div>

  <!-- Chofer breakdown -->
  <div class="bg-white rounded-xl border border-slate-100 shadow-sm mb-6 overflow-hidden">
    <div class="px-5 py-4 border-b border-slate-100">
      <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wide">Desglose por chofer</h2>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-slate-100">
            <th class="text-left px-5 py-3 text-xs font-semibold text-slate-400 uppercase">Chofer</th>
            <th class="text-right px-5 py-3 text-xs font-semibold text-slate-400 uppercase">Total</th>
            <th class="text-right px-5 py-3 text-xs font-semibold text-slate-400 uppercase">Días</th>
            <th class="text-right px-5 py-3 text-xs font-semibold text-slate-400 uppercase">Promedio/día</th>
            <th class="text-right px-5 py-3 text-xs font-semibold text-slate-400 uppercase">Efectivo neto</th>
          </tr>
        </thead>
        <tbody id="tbody-choferes">
          <tr><td colspan="5" class="px-5 py-6 text-center text-slate-400 text-sm">Cargando...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Records table -->
  <div class="bg-white rounded-xl border border-slate-100 shadow-sm overflow-hidden">
    <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
      <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wide">Registros</h2>
      <span id="records-count" class="text-xs text-slate-400 bg-slate-50 px-2.5 py-1 rounded-full"></span>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm whitespace-nowrap">
        <thead>
          <tr class="bg-slate-50 border-b border-slate-100">
            <th class="text-left px-4 py-3 text-xs font-semibold text-slate-400 uppercase sticky left-0 bg-slate-50">Fecha</th>
            <th class="text-left px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Día</th>
            <th class="text-left px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Chofer</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Total</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Comisión</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">POS</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Fiado</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Neto</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">% Digital</th>
            <th class="text-right px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Combustible</th>
            <th class="text-left px-4 py-3 text-xs font-semibold text-slate-400 uppercase">Guardado</th>
          </tr>
        </thead>
        <tbody id="tbody-records">
          <tr><td colspan="11" class="px-5 py-6 text-center text-slate-400 text-sm">Cargando...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>

<script>
const MONTHS = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
const COLORS  = ['#3b82f6','#f59e0b','#10b981','#8b5cf6','#ec4899','#6b7280','#ef4444','#14b8a6','#f97316'];
let chartDaily = null, chartDonut = null;

const fmt = (n, d=0) => n == null ? '—' : '$' + Number(n).toLocaleString('es-UY', {minimumFractionDigits:d, maximumFractionDigits:d});
const pct  = (n)     => n == null ? '—' : n.toFixed(1) + '%';

async function api(path) {
  const r = await fetch('/admin' + path, {credentials:'same-origin'});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function period() {
  const v = document.getElementById('sel-mes').value;
  if (!v) return {};
  const [mes, anio] = v.split('-').map(Number);
  return { mes, anio };
}

async function loadHealth() {
  try {
    const h = await api('/api/health');
    const ok = h.status === 'ok';
    const b  = document.getElementById('badge-status');
    b.textContent = ok ? '● Online' : '● Degradado';
    b.className   = 'px-2.5 py-1 rounded-full font-medium text-sm ' + (ok ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700');
    document.getElementById('badge-version').textContent = 'v' + h.version;
    const u = h.uptime_seconds;
    document.getElementById('badge-uptime').textContent = Math.floor(u/3600) + 'h ' + Math.floor((u%3600)/60) + 'm uptime';
    document.getElementById('badge-db').textContent = h.db_records + ' registros en DB';
  } catch {
    const b = document.getElementById('badge-status');
    b.textContent = '● Sin conexión';
    b.className   = 'px-2.5 py-1 rounded-full font-medium text-sm bg-red-50 text-red-700';
  }
}

async function loadMonths() {
  const data = await api('/api/months');
  const sel  = document.getElementById('sel-mes');
  const now  = new Date();
  const curK = (now.getMonth()+1) + '-' + now.getFullYear();
  const keys  = new Set([curK]);
  data.forEach(m => keys.add(m.mes + '-' + m.anio));
  sel.innerHTML = [...keys].map(k => {
    const [m,y] = k.split('-').map(Number);
    return '<option value="' + k + '">' + MONTHS[m-1] + ' ' + y + '</option>';
  }).join('');
  sel.value = curK;
}

async function loadChoferes() {
  const data = await api('/api/choferes');
  const sel  = document.getElementById('sel-chofer');
  sel.innerHTML = '<option value="">Todos</option>' +
    data.map(c => {
      const label = c.activo ? c.nombre : c.nombre + ' (inactivo)';
      const style = c.activo ? '' : ' style="color:#94a3b8"';
      return '<option value="' + c.nombre + '"' + style + '>' + label + '</option>';
    }).join('');
}

async function loadSummary(mes, anio) {
  const d = await api('/api/summary?mes=' + mes + '&anio=' + anio);
  document.getElementById('c-total').textContent    = fmt(d.total_recaudado);
  document.getElementById('c-dias').textContent     = d.dias_trabajados;
  document.getElementById('c-promedio').textContent = fmt(d.promedio_por_dia);
  if (d.mejor_dia) {
    document.getElementById('c-mejor').textContent       = d.mejor_dia.fecha + (d.mejor_dia.dia ? ' (' + d.mejor_dia.dia + ')' : '');
    document.getElementById('c-mejor-total').textContent = fmt(d.mejor_dia.total);
  } else {
    document.getElementById('c-mejor').textContent       = '—';
    document.getElementById('c-mejor-total').textContent = '';
  }

  // Chofer table
  const chTbody = document.getElementById('tbody-choferes');
  chTbody.innerHTML = d.choferes.length
    ? d.choferes.map(c =>
        '<tr class="border-b border-slate-50 hover:bg-slate-50 transition-colors">' +
        '<td class="px-5 py-3 font-medium">' + c.chofer + '</td>' +
        '<td class="px-5 py-3 text-right font-semibold">' + fmt(c.total) + '</td>' +
        '<td class="px-5 py-3 text-right text-slate-500">' + c.dias + '</td>' +
        '<td class="px-5 py-3 text-right text-slate-500">' + fmt(c.promedio) + '</td>' +
        '<td class="px-5 py-3 text-right text-emerald-600 font-semibold">' + fmt(c.efectivo_neto) + '</td>' +
        '</tr>').join('')
    : '<tr><td colspan="5" class="px-5 py-6 text-center text-slate-400 text-sm">Sin datos para este período</td></tr>';

  // Donut — base + dynamic empresas
  const baseLabels = ['POS', 'Fiado', 'Efectivo neto'];
  const baseVals   = [d.pagos.pos, d.pagos.fiado, d.pagos.efectivo_neto];
  const empLabels  = (d.by_empresa || []).map(e => e.empresa);
  const empVals    = (d.by_empresa || []).map(e => e.total);
  const payLabels  = [...baseLabels, ...empLabels];
  const payVals    = [...baseVals, ...empVals];

  if (chartDonut) chartDonut.destroy();
  chartDonut = new Chart(document.getElementById('chart-donut'), {
    type: 'doughnut',
    data: { labels: payLabels, datasets: [{ data: payVals, backgroundColor: COLORS, borderWidth: 0, hoverOffset: 4 }] },
    options: { plugins: { legend: { display: false } }, cutout: '68%', maintainAspectRatio: false }
  });
  document.getElementById('donut-legend').innerHTML = payLabels.map((l,i) =>
    '<div class="flex items-center justify-between">' +
    '<div class="flex items-center gap-2"><div class="w-2.5 h-2.5 rounded-full" style="background:' + COLORS[i % COLORS.length] + '"></div>' +
    '<span class="text-slate-500">' + l + '</span></div>' +
    '<span class="font-semibold">' + fmt(payVals[i]) + '</span></div>'
  ).join('');
}

async function loadDaily(mes, anio) {
  const data   = await api('/api/daily?mes=' + mes + '&anio=' + anio);
  const canvas = document.getElementById('chart-daily');
  const empty  = document.getElementById('chart-daily-empty');
  if (!data.length) {
    canvas.classList.add('hidden'); empty.classList.remove('hidden');
    if (chartDaily) { chartDaily.destroy(); chartDaily = null; }
    return;
  }
  canvas.classList.remove('hidden'); empty.classList.add('hidden');
  if (chartDaily) chartDaily.destroy();
  chartDaily = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: data.map(d => d.fecha ? d.fecha.slice(0,5) : ''),
      datasets: [
        { label: 'POS',           data: data.map(d => d.pos),           backgroundColor: '#3b82f6', stack: 's' },
        { label: 'Fiado',         data: data.map(d => d.fiado),         backgroundColor: '#f59e0b', stack: 's' },
        { label: 'Efectivo neto', data: data.map(d => d.efectivo_neto), backgroundColor: '#10b981', stack: 's' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { position: 'top', labels: { boxWidth: 10, font: { size: 11 } } } },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 10 } } },
        y: { grid: { color: '#f8fafc' }, ticks: { callback: function(v){ return '$' + Number(v).toLocaleString('es-UY'); }, font: { size: 10 } } }
      }
    }
  });
}

async function loadRecords(mes, anio, chofer) {
  let path = '/api/records?mes=' + mes + '&anio=' + anio;
  if (chofer) path += '&chofer=' + encodeURIComponent(chofer);
  const data  = await api(path);
  const count = data.length;
  document.getElementById('records-count').textContent = count + ' registro' + (count !== 1 ? 's' : '');
  const tbody = document.getElementById('tbody-records');
  if (!count) {
    tbody.innerHTML = '<tr><td colspan="11" class="px-5 py-8 text-center text-slate-400 text-sm">Sin registros para este período</td></tr>';
    return;
  }
  tbody.innerHTML = data.map(r =>
    '<tr class="border-b border-slate-50 hover:bg-blue-50/30 transition-colors">' +
    '<td class="px-4 py-2.5 font-medium sticky left-0 bg-white">' + (r.fecha||'—') + '</td>' +
    '<td class="px-4 py-2.5 text-slate-400 text-xs">' + (r.dia_semana||'—') + '</td>' +
    '<td class="px-4 py-2.5 font-medium">' + (r.chofer||'—') + '</td>' +
    '<td class="px-4 py-2.5 text-right font-bold">' + fmt(r.total_recaudado) + '</td>' +
    '<td class="px-4 py-2.5 text-right text-slate-400">' + fmt(r.comision_30) + '</td>' +
    '<td class="px-4 py-2.5 text-right text-blue-600 font-medium">' + fmt(r.total_pos) + '</td>' +
    '<td class="px-4 py-2.5 text-right text-amber-600 font-medium">' + fmt(r.total_fiado) + '</td>' +
    '<td class="px-4 py-2.5 text-right text-emerald-600 font-bold">' + fmt(r.efectivo_neto) + '</td>' +
    '<td class="px-4 py-2.5 text-right">' + pct(r.porcentaje_digital) + '</td>' +
    '<td class="px-4 py-2.5 text-right text-slate-400">' + fmt(r.otros_combustible) + '</td>' +
    '<td class="px-4 py-2.5 text-xs text-slate-400">' + (r.creado_en ? r.creado_en.slice(0,16).replace('T',' ') : '—') + '</td>' +
    '</tr>'
  ).join('');
}

async function loadAll() {
  const { mes, anio } = period();
  if (!mes || !anio) return;
  const chofer = document.getElementById('sel-chofer').value;
  try {
    await Promise.all([
      loadHealth(),
      loadSummary(mes, anio),
      loadDaily(mes, anio),
      loadRecords(mes, anio, chofer),
    ]);
  } catch(e) { console.error('loadAll error:', e); }
}

async function init() {
  await loadMonths();
  await loadChoferes();
  await loadAll();
  setInterval(loadHealth, 60000);
}

init();
</script>
</body>
</html>"""
