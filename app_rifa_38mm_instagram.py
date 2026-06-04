import base64
import html
import io
import os
import random
import re
import sqlite3
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rifa.db"
STATIC_DIR = BASE_DIR / "static"
LOGOS_DIR = STATIC_DIR / "logos"
LOGOS_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))
QR_LINK = "https://www.instagram.com/asgarageasuncion?utm_source=qr"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(value):
    try:
        n = float(value or 0)
    except Exception:
        n = 0.0
    return f"{n:,.0f}".replace(",", ".")


def esc(value):
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def generate_ticket_code(conn):
    for _ in range(300):
        code = str(random.randint(100000, 999999))
        exists = conn.execute("SELECT 1 FROM boletas WHERE codigo_ticket = ?", (code,)).fetchone()
        if not exists:
            return code
    return uuid.uuid4().hex[:10].upper()


def normalize_ticket_code(value):
    return re.sub(r"[^A-Za-z0-9]", "", value or "")


def public_base_url(handler):
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or f"{HOST}:{PORT}"
    proto = handler.headers.get("X-Forwarded-Proto")
    if not proto:
        proto = "https" if "onrender.com" in host else "http"
    return f"{proto}://{host}"


def qr_data_uri(data):
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M

        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=6,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return ""


def qr_img_html(data):
    uri = qr_data_uri(data)
    if uri:
        return f"<img class='ticket-qr' src='{uri}' alt='QR de Instagram'>"
    return "<div class='qr-fallback'>QR no disponible<br>Instale qrcode[pil]</div>"


def optimize_logo_bytes(raw_bytes):
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.thumbnail((320, 120), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), ".png"
    except Exception:
        return raw_bytes, None


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campanias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                descripcion TEXT,
                costo_boleta REAL NOT NULL DEFAULT 0,
                premio TEXT,
                fecha_sorteo TEXT,
                logo_path TEXT,
                activa INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                apellido TEXT NOT NULL,
                direccion TEXT,
                telefono TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ventas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campania_id INTEGER NOT NULL,
                cliente_id INTEGER NOT NULL,
                cantidad INTEGER NOT NULL,
                costo_unitario REAL NOT NULL,
                total REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (campania_id) REFERENCES campanias(id),
                FOREIGN KEY (cliente_id) REFERENCES clientes(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS boletas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campania_id INTEGER NOT NULL,
                venta_id INTEGER NOT NULL,
                cliente_id INTEGER NOT NULL,
                numero INTEGER NOT NULL,
                codigo_ticket TEXT,
                estado TEXT NOT NULL DEFAULT 'EMITIDA',
                created_at TEXT NOT NULL,
                UNIQUE(campania_id, numero),
                FOREIGN KEY (campania_id) REFERENCES campanias(id),
                FOREIGN KEY (venta_id) REFERENCES ventas(id),
                FOREIGN KEY (cliente_id) REFERENCES clientes(id)
            )
            """
        )
        if not column_exists(conn, "boletas", "codigo_ticket"):
            cur.execute("ALTER TABLE boletas ADD COLUMN codigo_ticket TEXT")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sorteos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campania_id INTEGER NOT NULL,
                boleta_id INTEGER NOT NULL,
                cliente_id INTEGER NOT NULL,
                numero INTEGER NOT NULL,
                ganador_nombre TEXT NOT NULL,
                ganador_telefono TEXT,
                fecha_sorteo TEXT NOT NULL,
                FOREIGN KEY (campania_id) REFERENCES campanias(id),
                FOREIGN KEY (boleta_id) REFERENCES boletas(id),
                FOREIGN KEY (cliente_id) REFERENCES clientes(id)
            )
            """
        )
        pendientes = conn.execute(
            "SELECT id FROM boletas WHERE codigo_ticket IS NULL OR TRIM(codigo_ticket) = ''"
        ).fetchall()
        for row in pendientes:
            cur.execute(
                "UPDATE boletas SET codigo_ticket = ? WHERE id = ?",
                (generate_ticket_code(conn), row["id"]),
            )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_boletas_codigo_ticket ON boletas(codigo_ticket)")
        conn.commit()


def sanitize_filename(filename):
    filename = os.path.basename(filename or "")
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    if not filename:
        filename = "logo.png"
    return filename


def parse_multipart(content_type, body):
    result = {}
    files = {}
    match = re.search(r"boundary=(.+)", content_type or "")
    if not match:
        return result, files
    boundary = match.group(1).strip().strip('"')
    boundary_bytes = ("--" + boundary).encode()
    parts = body.split(boundary_bytes)
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, data = part.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = raw_headers.decode("utf-8", errors="ignore")
        name_match = re.search(r'name="([^"]+)"', headers)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', headers)
        if filename_match:
            filename = filename_match.group(1)
            files[name] = {"filename": filename, "content": data}
        else:
            result[name] = data.decode("utf-8", errors="ignore")
    return result, files


def parse_post(handler):
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length)
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" in content_type:
        return parse_multipart(content_type, body)
    data = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in data.items()}, {}


def layout(title, body, extra_head=""):
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ --primary:#0b3d91; --border:#d7dce5; --bg:#f4f6f9; --danger:#b00020; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: Arial, Helvetica, sans-serif; background:var(--bg); color:#1f2937; }}
header {{ background:var(--primary); color:white; padding:14px 20px; }}
header h1 {{ margin:0; font-size:21px; }}
nav {{ background:white; border-bottom:1px solid var(--border); padding:10px 20px; display:flex; gap:10px; flex-wrap:wrap; }}
nav a {{ color:var(--primary); text-decoration:none; font-weight:bold; }}
main {{ padding:20px; max-width:1180px; margin:auto; }}
.card {{ background:white; border:1px solid var(--border); border-radius:10px; padding:18px; margin-bottom:18px; box-shadow:0 1px 3px rgba(0,0,0,.05); }}
.grid {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; }}
.grid3 {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:12px; }}
label {{ display:block; font-size:13px; font-weight:bold; margin-bottom:5px; }}
input, select, textarea {{ width:100%; padding:9px; border:1px solid var(--border); border-radius:7px; font-size:14px; }}
textarea {{ min-height:70px; }}
button, .btn {{ display:inline-block; background:var(--primary); color:white; border:0; border-radius:7px; padding:10px 14px; text-decoration:none; cursor:pointer; font-weight:bold; }}
.btn-secondary {{ background:#374151; }}
.btn-danger {{ background:var(--danger); }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ border-bottom:1px solid var(--border); padding:8px; text-align:left; vertical-align:top; }}
th {{ background:#eef2ff; }}
.badge {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#e8f1ff; color:#0b3d91; font-size:12px; }}
.msg {{ padding:10px 12px; border-radius:8px; background:#ecfdf5; color:#065f46; margin-bottom:12px; }}
.err {{ background:#fef2f2; color:#991b1b; }}
.logo-small {{ max-height:48px; max-width:120px; }}
.search-row {{ display:flex; gap:8px; align-items:end; flex-wrap:wrap; }}
.search-row > div {{ min-width:260px; flex:1; }}
.code {{ font-weight:bold; font-size:16px; color:#0b3d91; white-space:nowrap; }}
@media(max-width:760px) {{ .grid, .grid3 {{ grid-template-columns:1fr; }} }}
</style>
{extra_head}
</head>
<body>
<header><h1>Sistema de Rifas</h1></header>
<nav>
<a href="/">Venta de boletas</a>
<a href="/campanias">Campañas</a>
<a href="/ventas">Boletas emitidas</a>
<a href="/tickets">Buscar tickets</a>
<a href="/sorteo">Sorteo</a>
</nav>
<main>
{body}
</main>
</body>
</html>"""


class App(BaseHTTPRequestHandler):
    def send_html(self, html_text, status=200):
        data = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/static/"):
            self.serve_static(path)
            return
        routes = {
            "/": self.page_home,
            "/campanias": self.page_campanias,
            "/ventas": self.page_ventas,
            "/tickets": self.page_tickets,
            "/sorteo": self.page_sorteo,
            "/boletas/print": self.page_print,
        }
        handler = routes.get(path)
        if handler:
            handler(parsed)
        else:
            self.send_html(layout("No encontrado", "<div class='card err'>Página no encontrada.</div>"), 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/campanias/crear":
            self.post_campania()
        elif path == "/campanias/activar":
            self.post_activar_campania()
        elif path == "/boletas/crear":
            self.post_boletas()
        elif path == "/sorteo/realizar":
            self.post_sorteo()
        else:
            self.send_html(layout("No encontrado", "<div class='card err'>Ruta no encontrada.</div>"), 404)

    def serve_static(self, path):
        rel = path.replace("/static/", "", 1)
        file_path = (STATIC_DIR / rel).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            self.send_response(404)
            self.end_headers()
            return
        content_type = "application/octet-stream"
        if file_path.suffix.lower() in [".png"]:
            content_type = "image/png"
        elif file_path.suffix.lower() in [".jpg", ".jpeg"]:
            content_type = "image/jpeg"
        elif file_path.suffix.lower() == ".gif":
            content_type = "image/gif"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def page_home(self, parsed):
        with get_conn() as conn:
            campanias = conn.execute("SELECT * FROM campanias WHERE activa = 1 ORDER BY id DESC").fetchall()
        opts = "".join(
            f"<option value='{c['id']}'>{esc(c['nombre'])} - Gs. {money(c['costo_boleta'])}</option>"
            for c in campanias
        )
        if not opts:
            opts = "<option value=''>Primero cargue una campaña</option>"
        body = f"""
<div class="card">
<h2>Emitir boletas</h2>
<form method="post" action="/boletas/crear">
<div class="grid">
<div><label>Campaña</label><select name="campania_id" required>{opts}</select></div>
<div><label>Cantidad de boletas</label><input type="number" name="cantidad" min="1" value="1" required></div>
<div><label>Nombre</label><input name="nombre" required></div>
<div><label>Apellido</label><input name="apellido" required></div>
<div><label>Dirección</label><input name="direccion"></div>
<div><label>Nro. teléfono</label><input name="telefono"></div>
</div>
<br>
<button type="submit">Guardar e imprimir</button>
</form>
</div>
<div class="card">
<h3>Notas</h3>
<p>Cada boleta emitida genera un código único de ticket, por ejemplo <b>#118461</b>. El QR del comprobante apunta al Instagram oficial.</p>
</div>
"""
        self.send_html(layout("Venta de boletas", body))

    def page_campanias(self, parsed):
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM campanias ORDER BY id DESC").fetchall()
        trs = ""
        for r in rows:
            logo = f"<img class='logo-small' src='/{esc(r['logo_path'])}'>" if r["logo_path"] else ""
            activo = "Sí" if r["activa"] else "No"
            trs += f"""
<tr>
<td>{r['id']}</td><td>{logo}</td><td>{esc(r['nombre'])}</td><td>Gs. {money(r['costo_boleta'])}</td>
<td>{esc(r['premio'])}</td><td>{activo}</td>
<td>
<form method="post" action="/campanias/activar" style="display:inline">
<input type="hidden" name="id" value="{r['id']}">
<input type="hidden" name="activa" value="{0 if r['activa'] else 1}">
<button type="submit" class="btn-secondary">{'Desactivar' if r['activa'] else 'Activar'}</button>
</form>
</td>
</tr>"""
        body = f"""
<div class="card">
<h2>Nueva campaña</h2>
<form method="post" action="/campanias/crear" enctype="multipart/form-data">
<div class="grid">
<div><label>Nombre de la rifa / campaña</label><input name="nombre" required></div>
<div><label>Costo por boleta</label><input type="number" name="costo_boleta" min="0" step="1" value="0" required></div>
<div><label>Premio</label><input name="premio"></div>
<div><label>Logo</label><input type="file" name="logo" accept="image/*"></div>
<div><label>Estado</label><select name="activa"><option value="1">Activa</option><option value="0">Inactiva</option></select></div>
<div style="grid-column:1/-1"><label>Descripción</label><textarea name="descripcion" placeholder="Opcional. Ejemplo: sorteo al completar los números disponibles."></textarea></div>
</div>
<br><button type="submit">Crear campaña</button>
</form>
</div>
<div class="card">
<h2>Campañas cargadas</h2>
<table><thead><tr><th>ID</th><th>Logo</th><th>Campaña</th><th>Costo</th><th>Premio</th><th>Activa</th><th>Acción</th></tr></thead><tbody>{trs or '<tr><td colspan="7">Sin campañas.</td></tr>'}</tbody></table>
</div>
"""
        self.send_html(layout("Campañas", body))

    def page_ventas(self, parsed):
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT v.*, c.nombre AS campania, cl.nombre, cl.apellido, cl.telefono,
                       (SELECT MIN(numero) FROM boletas WHERE venta_id = v.id) AS desde,
                       (SELECT MAX(numero) FROM boletas WHERE venta_id = v.id) AS hasta,
                       (SELECT GROUP_CONCAT('#' || codigo_ticket, ', ') FROM boletas WHERE venta_id = v.id) AS tickets
                FROM ventas v
                JOIN campanias c ON c.id = v.campania_id
                JOIN clientes cl ON cl.id = v.cliente_id
                ORDER BY v.id DESC
                LIMIT 300
                """
            ).fetchall()
        trs = ""
        for r in rows:
            rango = str(r["desde"]) if r["desde"] == r["hasta"] else f"{r['desde']} - {r['hasta']}"
            tickets = esc(r["tickets"] or "")
            trs += f"""
<tr>
<td>{r['id']}</td><td>{esc(r['created_at'])}</td><td>{esc(r['campania'])}</td>
<td>{esc(r['nombre'])} {esc(r['apellido'])}</td><td>{esc(r['telefono'])}</td>
<td>{r['cantidad']}</td><td>{esc(rango)}</td><td class="code">{tickets}</td><td>Gs. {money(r['costo_unitario'])}</td><td>Gs. {money(r['total'])}</td>
<td><a class="btn" href="/boletas/print?venta_id={r['id']}">Imprimir/PDF</a></td>
</tr>"""
        body = f"""
<div class="card">
<h2>Boletas emitidas</h2>
<p>Para buscar una boleta específica por código, use el buscador de tickets.</p>
<a class="btn btn-secondary" href="/tickets">Buscar ticket</a>
<br><br>
<table>
<thead><tr><th>Venta</th><th>Fecha</th><th>Campaña</th><th>Cliente</th><th>Teléfono</th><th>Cant.</th><th>Boletas</th><th>Tickets</th><th>Costo unit.</th><th>Total</th><th></th></tr></thead>
<tbody>{trs or '<tr><td colspan="11">Sin ventas.</td></tr>'}</tbody>
</table>
</div>
"""
        self.send_html(layout("Ventas", body))

    def page_tickets(self, parsed):
        params = parse_qs(parsed.query)
        q = params.get("q", [""])[0].strip()
        clean = normalize_ticket_code(q)
        rows = []
        with get_conn() as conn:
            if q:
                rows = conn.execute(
                    """
                    SELECT b.id AS boleta_id, b.numero, b.codigo_ticket, b.estado, b.created_at,
                           v.id AS venta_id, v.cantidad, v.costo_unitario, v.total,
                           c.nombre AS campania, c.premio,
                           cl.nombre, cl.apellido, cl.telefono, cl.direccion
                    FROM boletas b
                    JOIN ventas v ON v.id = b.venta_id
                    JOIN campanias c ON c.id = b.campania_id
                    JOIN clientes cl ON cl.id = b.cliente_id
                    WHERE b.codigo_ticket LIKE ?
                       OR CAST(b.numero AS TEXT) LIKE ?
                       OR CAST(v.id AS TEXT) = ?
                       OR cl.nombre LIKE ?
                       OR cl.apellido LIKE ?
                       OR cl.telefono LIKE ?
                       OR c.nombre LIKE ?
                    ORDER BY b.id DESC
                    LIMIT 200
                    """,
                    (
                        f"%{clean}%",
                        f"%{clean}%",
                        clean,
                        f"%{q}%",
                        f"%{q}%",
                        f"%{q}%",
                        f"%{q}%",
                    ),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT b.id AS boleta_id, b.numero, b.codigo_ticket, b.estado, b.created_at,
                           v.id AS venta_id, v.cantidad, v.costo_unitario, v.total,
                           c.nombre AS campania, c.premio,
                           cl.nombre, cl.apellido, cl.telefono, cl.direccion
                    FROM boletas b
                    JOIN ventas v ON v.id = b.venta_id
                    JOIN campanias c ON c.id = b.campania_id
                    JOIN clientes cl ON cl.id = b.cliente_id
                    ORDER BY b.id DESC
                    LIMIT 100
                    """
                ).fetchall()
        trs = ""
        for r in rows:
            code = f"#{r['codigo_ticket']}" if r["codigo_ticket"] else ""
            trs += f"""
<tr>
<td class="code">{esc(code)}</td>
<td>{int(r['numero']):06d}</td>
<td>{esc(r['campania'])}</td>
<td>{esc(r['nombre'])} {esc(r['apellido'])}</td>
<td>{esc(r['telefono'])}</td>
<td>{esc(r['estado'])}</td>
<td>{esc(r['created_at'])}</td>
<td>
<a class="btn" href="/boletas/print?ticket_id={r['boleta_id']}">Imprimir/PDF</a>
<a class="btn btn-secondary" href="/boletas/print?venta_id={r['venta_id']}">Venta completa</a>
</td>
</tr>"""
        notice = ""
        if q and not rows:
            notice = "<div class='card err'>No se encontraron tickets con ese criterio.</div>"
        body = f"""
<div class="card">
<h2>Buscador de tickets emitidos</h2>
<form method="get" action="/tickets" class="search-row">
<div><label>Buscar por código, número, venta, cliente, teléfono o campaña</label><input name="q" value="{esc(q)}" placeholder="#118461, 118461, cliente, teléfono..."></div>
<div><button type="submit">Buscar</button></div>
</form>
</div>
{notice}
<div class="card">
<h2>{'Resultados' if q else 'Últimos tickets emitidos'}</h2>
<table>
<thead><tr><th>Ticket</th><th>Boleta N°</th><th>Campaña</th><th>Cliente</th><th>Teléfono</th><th>Estado</th><th>Emitido</th><th>Acciones</th></tr></thead>
<tbody>{trs or '<tr><td colspan="8">Sin tickets emitidos.</td></tr>'}</tbody>
</table>
</div>
"""
        self.send_html(layout("Buscar tickets", body))

    def page_sorteo(self, parsed):
        query = parse_qs(parsed.query)
        ganador_id = query.get("ganador_id", [""])[0]
        with get_conn() as conn:
            campanias = conn.execute("SELECT * FROM campanias ORDER BY id DESC").fetchall()
            sorteos = conn.execute(
                """
                SELECT s.*, c.nombre AS campania
                FROM sorteos s
                JOIN campanias c ON c.id = s.campania_id
                ORDER BY s.id DESC LIMIT 100
                """
            ).fetchall()
            ganador = None
            if ganador_id:
                ganador = conn.execute(
                    """
                    SELECT s.*, c.nombre AS campania
                    FROM sorteos s
                    JOIN campanias c ON c.id = s.campania_id
                    WHERE s.id = ?
                    """,
                    (ganador_id,),
                ).fetchone()
        opts = "".join(f"<option value='{c['id']}'>{esc(c['nombre'])}</option>" for c in campanias)
        ganador_html = ""
        if ganador:
            ganador_html = f"""
<div class="card msg">
<h2>Ganador</h2>
<p><b>Campaña:</b> {esc(ganador['campania'])}</p>
<p><b>Boleta:</b> {ganador['numero']}</p>
<p><b>Cliente:</b> {esc(ganador['ganador_nombre'])}</p>
<p><b>Teléfono:</b> {esc(ganador['ganador_telefono'])}</p>
<p><b>Fecha del sorteo realizado:</b> {esc(ganador['fecha_sorteo'])}</p>
</div>"""
        trs = ""
        for s in sorteos:
            trs += f"<tr><td>{s['id']}</td><td>{esc(s['fecha_sorteo'])}</td><td>{esc(s['campania'])}</td><td>{s['numero']}</td><td>{esc(s['ganador_nombre'])}</td><td>{esc(s['ganador_telefono'])}</td></tr>"
        body = f"""
{ganador_html}
<div class="card">
<h2>Realizar sorteo</h2>
<form method="post" action="/sorteo/realizar">
<div class="grid">
<div><label>Campaña</label><select name="campania_id" required>{opts}</select></div>
<div style="align-self:end"><button type="submit">Sortear ganador</button></div>
</div>
</form>
</div>
<div class="card">
<h2>Historial de sorteos</h2>
<table><thead><tr><th>ID</th><th>Fecha realizada</th><th>Campaña</th><th>Boleta</th><th>Ganador</th><th>Teléfono</th></tr></thead><tbody>{trs or '<tr><td colspan="6">Sin sorteos.</td></tr>'}</tbody></table>
</div>
"""
        self.send_html(layout("Sorteo", body))

    def page_print(self, parsed):
        query = parse_qs(parsed.query)
        venta_id = query.get("venta_id", [""])[0]
        ticket_id = query.get("ticket_id", [""])[0]
        codigo = normalize_ticket_code(query.get("codigo", [""])[0])
        where = ""
        params = ()
        title_suffix = ""
        if ticket_id:
            where = "b.id = ?"
            params = (ticket_id,)
            title_suffix = f"ticket_{ticket_id}"
        elif codigo:
            where = "b.codigo_ticket = ?"
            params = (codigo,)
            title_suffix = f"ticket_{codigo}"
        elif venta_id:
            where = "v.id = ?"
            params = (venta_id,)
            title_suffix = f"venta_{venta_id}"
        else:
            self.send_html(layout("Imprimir", "<div class='card err'>Debe indicar venta_id o ticket_id.</div>"), 400)
            return

        with get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT b.id AS boleta_id, b.numero, b.codigo_ticket, b.estado, b.created_at AS boleta_created_at,
                       v.id AS venta_id, v.cantidad, v.costo_unitario, v.total, v.created_at AS venta_created_at,
                       c.nombre AS campania, c.costo_boleta, c.logo_path, c.premio,
                       cl.nombre, cl.apellido, cl.direccion, cl.telefono
                FROM boletas b
                JOIN ventas v ON v.id = b.venta_id
                JOIN campanias c ON c.id = b.campania_id
                JOIN clientes cl ON cl.id = b.cliente_id
                WHERE {where}
                ORDER BY b.numero
                """,
                params,
            ).fetchall()
        if not rows:
            self.send_html(layout("Imprimir", "<div class='card err'>Ticket o venta no encontrada.</div>"), 404)
            return

        tickets = ""
        for r in rows:
            logo = f"<img class='ticket-logo' src='/{esc(r['logo_path'])}'>" if r["logo_path"] else ""
            code = r["codigo_ticket"] or ""
            code_display = f"#{code}" if code else "Sin código"
            qr_html = qr_img_html(QR_LINK)
            tickets += f"""
<section class="ticket">
{logo}
<h2>{esc(r['campania'])}</h2>
<div class="ticket-code">{esc(code_display)}</div>
<div class="num">Boleta N° {int(r['numero']):06d}</div>
<div class="qr-wrap">{qr_html}</div>
<div class="line"></div>
<p><b>Venta:</b> {r['venta_id']}</p>
<p><b>Cliente:</b> {esc(r['nombre'])} {esc(r['apellido'])}</p>
<p><b>Tel.:</b> {esc(r['telefono'])}</p>
<p><b>Premio:</b> {esc(r['premio'])}</p>
<p><b>Costo:</b> Gs. {money(r['costo_unitario'])}</p>
<p><b>Emitido:</b> {esc(r['boleta_created_at'])}</p>
<div class="line"></div>
<p class="small">QR Instagram AS Garage.</p>
<p class="small">Conserve este ticket. No reemplaza factura legal.</p>
</section>"""
        safe_title = re.sub(r"[^A-Za-z0-9_-]", "_", title_suffix or "tickets")
        extra = f"""
<style>
body {{ background:white; margin:0; padding:0; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
.no-print {{ margin:12px; }}
.ticket {{ width:54mm; max-width:54mm; padding:1mm 1.5mm; margin:0 auto; border-bottom:1px dashed #000; font-family: Arial, Helvetica, sans-serif; color:#000; text-align:center; overflow:hidden; box-sizing:border-box; }}
.ticket h2 {{ font-size:11px; line-height:1.05; text-align:center; margin:0.6mm auto; font-weight:bold; word-break:break-word; }}
.ticket p {{ font-size:8.5px; line-height:1.08; margin:0.5mm auto; word-break:break-word; text-align:center; }}
.ticket-logo {{ display:block; width:auto; max-width:26mm; max-height:8mm; object-fit:contain; margin:0 auto 0.5mm auto; }}
.ticket-code {{ font-size:18px; line-height:1; text-align:center; font-weight:bold; margin:0.6mm auto 0.3mm auto; letter-spacing:0.3px; }}
.num {{ font-size:10px; line-height:1.05; text-align:center; font-weight:bold; margin:0.3mm auto 0.6mm auto; }}
.qr-wrap {{ display:block; width:100%; text-align:center; margin:0.8mm auto; }}
.ticket-qr {{ width:22mm; height:22mm; display:block; margin:0 auto; image-rendering:pixelated; }}
.qr-fallback {{ width:22mm; min-height:22mm; border:1px solid #000; font-size:6px; overflow:hidden; padding:1mm; text-align:center; word-break:break-all; margin:0 auto; box-sizing:border-box; }}
.line {{ border-top:1px dashed #000; margin:0.8mm auto; width:100%; }}
.small {{ font-size:7px !important; line-height:1.05 !important; text-align:center; }}
.only-wide {{ display:none; }}
@media print {{
  html, body {{ width:58mm !important; height:auto !important; min-height:0 !important; margin:0 !important; padding:0 !important; overflow:visible !important; }}
  header, nav, .no-print {{ display:none !important; }}
  main {{ padding:0 !important; margin:0 !important; max-width:none !important; width:58mm !important; height:auto !important; min-height:0 !important; text-align:center !important; }}
  .ticket {{ width:54mm !important; max-width:54mm !important; margin:0 auto !important; padding-left:1.5mm !important; padding-right:1.5mm !important; page-break-after:auto !important; break-after:auto !important; page-break-inside:auto !important; break-inside:auto !important; }}
  .ticket:last-child {{ border-bottom:none !important; }}
  @page {{ size:58mm auto; margin:0; }}
}}
</style>
<script>
function guardarPDF() {{
  document.title = "{safe_title}";
  window.print();
}}
</script>
"""
        body = f"""
<div class="no-print">
<button onclick="window.print()">Imprimir en térmica</button>
<button onclick="guardarPDF()" class="btn-secondary">Guardar PDF</button>
<a class="btn btn-secondary" href="/tickets">Buscar tickets</a>
<a class="btn btn-secondary" href="/ventas">Volver</a>
<p>Formato ajustado para impresora térmica de <b>58 mm</b>. En Chrome/Edge use margen ninguno, escala 100%, sin encabezado ni pie. En el driver seleccione papel 58 mm y largo automático/recibo/continuo para evitar espacios blancos.</p>
</div>
{tickets}
"""
        self.send_html(layout("Imprimir boletas", body, extra_head=extra))

    def post_campania(self):
        fields, files = parse_post(self)
        nombre = fields.get("nombre", "").strip()
        if not nombre:
            self.redirect("/campanias")
            return
        try:
            costo = float(str(fields.get("costo_boleta", "0")).replace(".", "").replace(",", "."))
        except Exception:
            costo = 0
        logo_path = None
        logo_file = files.get("logo")
        if logo_file and logo_file["filename"] and logo_file["content"]:
            filename = sanitize_filename(logo_file["filename"])
            ext = Path(filename).suffix.lower() or ".png"
            if ext not in [".png", ".jpg", ".jpeg", ".gif"]:
                ext = ".png"
            optimized_content, forced_ext = optimize_logo_bytes(logo_file["content"])
            if forced_ext:
                ext = forced_ext
            final_name = f"{uuid.uuid4().hex}{ext}"
            target = LOGOS_DIR / final_name
            target.write_bytes(optimized_content)
            logo_path = f"static/logos/{final_name}"
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO campanias(nombre, descripcion, costo_boleta, premio, fecha_sorteo, logo_path, activa, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nombre,
                    fields.get("descripcion", ""),
                    costo,
                    fields.get("premio", ""),
                    "",
                    logo_path,
                    int(fields.get("activa", "1") or 1),
                    now_str(),
                ),
            )
            conn.commit()
        self.redirect("/campanias")

    def post_activar_campania(self):
        fields, files = parse_post(self)
        with get_conn() as conn:
            conn.execute("UPDATE campanias SET activa = ? WHERE id = ?", (int(fields.get("activa", "1")), fields.get("id")))
            conn.commit()
        self.redirect("/campanias")

    def post_boletas(self):
        fields, files = parse_post(self)
        campania_id = fields.get("campania_id")
        try:
            cantidad = max(1, int(fields.get("cantidad", "1")))
        except Exception:
            cantidad = 1
        with get_conn() as conn:
            campania = conn.execute("SELECT * FROM campanias WHERE id = ? AND activa = 1", (campania_id,)).fetchone()
            if not campania:
                self.send_html(layout("Error", "<div class='card err'>Campaña inválida o inactiva.</div>"), 400)
                return
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO clientes(nombre, apellido, direccion, telefono, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    fields.get("nombre", "").strip(),
                    fields.get("apellido", "").strip(),
                    fields.get("direccion", "").strip(),
                    fields.get("telefono", "").strip(),
                    now_str(),
                ),
            )
            cliente_id = cur.lastrowid
            costo = float(campania["costo_boleta"] or 0)
            total = costo * cantidad
            cur.execute(
                "INSERT INTO ventas(campania_id, cliente_id, cantidad, costo_unitario, total, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (campania_id, cliente_id, cantidad, costo, total, now_str()),
            )
            venta_id = cur.lastrowid
            row = conn.execute("SELECT COALESCE(MAX(numero), 0) AS max_num FROM boletas WHERE campania_id = ?", (campania_id,)).fetchone()
            inicio = int(row["max_num"] or 0) + 1
            for i in range(cantidad):
                codigo_ticket = generate_ticket_code(conn)
                cur.execute(
                    """
                    INSERT INTO boletas(campania_id, venta_id, cliente_id, numero, codigo_ticket, estado, created_at)
                    VALUES (?, ?, ?, ?, ?, 'EMITIDA', ?)
                    """,
                    (campania_id, venta_id, cliente_id, inicio + i, codigo_ticket, now_str()),
                )
            conn.commit()
        self.redirect(f"/boletas/print?venta_id={venta_id}")

    def post_sorteo(self):
        fields, files = parse_post(self)
        campania_id = fields.get("campania_id")
        with get_conn() as conn:
            boletas = conn.execute(
                """
                SELECT b.*, cl.nombre, cl.apellido, cl.telefono
                FROM boletas b
                JOIN clientes cl ON cl.id = b.cliente_id
                WHERE b.campania_id = ?
                """,
                (campania_id,),
            ).fetchall()
            if not boletas:
                self.send_html(layout("Sorteo", "<div class='card err'>No hay boletas emitidas para esta campaña.</div><a class='btn' href='/sorteo'>Volver</a>"), 400)
                return
            ganador = random.choice(boletas)
            nombre = f"{ganador['nombre']} {ganador['apellido']}"
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO sorteos(campania_id, boleta_id, cliente_id, numero, ganador_nombre, ganador_telefono, fecha_sorteo)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (campania_id, ganador["id"], ganador["cliente_id"], ganador["numero"], nombre, ganador["telefono"], now_str()),
            )
            sorteo_id = cur.lastrowid
            conn.commit()
        self.redirect(f"/sorteo?ganador_id={sorteo_id}")


def run():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), App)
    print(f"Sistema de rifas iniciado en http://{HOST}:{PORT}")
    print("Presione CTRL+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
