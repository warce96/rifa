import html
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rifa.db"
STATIC_DIR = BASE_DIR / "static"
LOGOS_DIR = STATIC_DIR / "logos"
LOGOS_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))


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
                estado TEXT NOT NULL DEFAULT 'EMITIDA',
                created_at TEXT NOT NULL,
                UNIQUE(campania_id, numero),
                FOREIGN KEY (campania_id) REFERENCES campanias(id),
                FOREIGN KEY (venta_id) REFERENCES ventas(id),
                FOREIGN KEY (cliente_id) REFERENCES clientes(id)
            )
            """
        )
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
<p>El costo unitario se toma desde la campaña seleccionada. El sistema guarda la venta, las boletas emitidas, el cliente y el total.</p>
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
<td>{esc(r['premio'])}</td><td>{esc(r['fecha_sorteo'])}</td><td>{activo}</td>
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
<div><label>Fecha de sorteo</label><input type="date" name="fecha_sorteo"></div>
<div><label>Logo</label><input type="file" name="logo" accept="image/*"></div>
<div><label>Estado</label><select name="activa"><option value="1">Activa</option><option value="0">Inactiva</option></select></div>
<div style="grid-column:1/-1"><label>Descripción</label><textarea name="descripcion"></textarea></div>
</div>
<br><button type="submit">Crear campaña</button>
</form>
</div>
<div class="card">
<h2>Campañas cargadas</h2>
<table><thead><tr><th>ID</th><th>Logo</th><th>Campaña</th><th>Costo</th><th>Premio</th><th>Fecha sorteo</th><th>Activa</th><th>Acción</th></tr></thead><tbody>{trs or '<tr><td colspan="8">Sin campañas.</td></tr>'}</tbody></table>
</div>
"""
        self.send_html(layout("Campañas", body))

    def page_ventas(self, parsed):
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT v.*, c.nombre AS campania, cl.nombre, cl.apellido, cl.telefono,
                       (SELECT MIN(numero) FROM boletas WHERE venta_id = v.id) AS desde,
                       (SELECT MAX(numero) FROM boletas WHERE venta_id = v.id) AS hasta
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
            trs += f"""
<tr>
<td>{r['id']}</td><td>{esc(r['created_at'])}</td><td>{esc(r['campania'])}</td>
<td>{esc(r['nombre'])} {esc(r['apellido'])}</td><td>{esc(r['telefono'])}</td>
<td>{r['cantidad']}</td><td>{esc(rango)}</td><td>Gs. {money(r['costo_unitario'])}</td><td>Gs. {money(r['total'])}</td>
<td><a class="btn" href="/boletas/print?venta_id={r['id']}">Imprimir</a></td>
</tr>"""
        body = f"""
<div class="card">
<h2>Boletas emitidas</h2>
<table>
<thead><tr><th>Venta</th><th>Fecha</th><th>Campaña</th><th>Cliente</th><th>Teléfono</th><th>Cant.</th><th>Boletas</th><th>Costo unit.</th><th>Total</th><th></th></tr></thead>
<tbody>{trs or '<tr><td colspan="10">Sin ventas.</td></tr>'}</tbody>
</table>
</div>
"""
        self.send_html(layout("Ventas", body))

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
                    """, (ganador_id,)
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
<p><b>Fecha:</b> {esc(ganador['fecha_sorteo'])}</p>
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
<table><thead><tr><th>ID</th><th>Fecha</th><th>Campaña</th><th>Boleta</th><th>Ganador</th><th>Teléfono</th></tr></thead><tbody>{trs or '<tr><td colspan="6">Sin sorteos.</td></tr>'}</tbody></table>
</div>
"""
        self.send_html(layout("Sorteo", body))

    def page_print(self, parsed):
        query = parse_qs(parsed.query)
        venta_id = query.get("venta_id", [""])[0]
        with get_conn() as conn:
            venta = conn.execute(
                """
                SELECT v.*, c.nombre AS campania, c.costo_boleta, c.logo_path, c.premio, c.fecha_sorteo,
                       cl.nombre, cl.apellido, cl.direccion, cl.telefono
                FROM ventas v
                JOIN campanias c ON c.id = v.campania_id
                JOIN clientes cl ON cl.id = v.cliente_id
                WHERE v.id = ?
                """, (venta_id,)
            ).fetchone()
            boletas = conn.execute("SELECT * FROM boletas WHERE venta_id = ? ORDER BY numero", (venta_id,)).fetchall()
        if not venta:
            self.send_html(layout("Imprimir", "<div class='card err'>Venta no encontrada.</div>"), 404)
            return
        logo = f"<img class='ticket-logo' src='/{esc(venta['logo_path'])}'>" if venta["logo_path"] else ""
        tickets = ""
        for b in boletas:
            tickets += f"""
<section class="ticket">
{logo}
<h2>{esc(venta['campania'])}</h2>
<div class="num">N° {b['numero']:06d}</div>
<div class="line"></div>
<p><b>Cliente:</b> {esc(venta['nombre'])} {esc(venta['apellido'])}</p>
<p><b>Teléfono:</b> {esc(venta['telefono'])}</p>
<p><b>Dirección:</b> {esc(venta['direccion'])}</p>
<p><b>Premio:</b> {esc(venta['premio'])}</p>
<p><b>Fecha sorteo:</b> {esc(venta['fecha_sorteo'])}</p>
<p><b>Costo:</b> Gs. {money(venta['costo_unitario'])}</p>
<p><b>Emitido:</b> {esc(b['created_at'])}</p>
<div class="line"></div>
<p class="small">Conserve esta boleta para participar del sorteo.</p>
</section>"""
        extra = """
<style>
body { background:white; }
.no-print { margin:12px; }
.ticket { width:72mm; padding:4mm; margin:0 auto 6mm auto; border-bottom:1px dashed #000; font-family: Arial, Helvetica, sans-serif; color:#000; }
.ticket h2 { font-size:16px; text-align:center; margin:4px 0; }
.ticket p { font-size:12px; margin:4px 0; }
.ticket-logo { display:block; max-width:42mm; max-height:22mm; margin:0 auto 4px auto; }
.num { font-size:24px; text-align:center; font-weight:bold; margin:6px 0; }
.line { border-top:1px dashed #000; margin:6px 0; }
.small { font-size:10px !important; text-align:center; }
@media print {
  header, nav, .no-print { display:none !important; }
  main { padding:0; max-width:none; }
  .ticket { width:72mm; margin:0; page-break-after:always; }
  @page { size:80mm auto; margin:0; }
}
</style>
"""
        body = f"""
<div class="no-print">
<button onclick="window.print()">Imprimir en térmica</button>
<a class="btn btn-secondary" href="/">Volver</a>
<p>En Chrome/Edge seleccionar impresora térmica, margen ninguno, escala 100%, sin encabezado ni pie.</p>
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
            final_name = f"{uuid.uuid4().hex}{ext}"
            target = LOGOS_DIR / final_name
            target.write_bytes(logo_file["content"])
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
                    fields.get("fecha_sorteo", ""),
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
                cur.execute(
                    "INSERT INTO boletas(campania_id, venta_id, cliente_id, numero, estado, created_at) VALUES (?, ?, ?, ?, 'EMITIDA', ?)",
                    (campania_id, venta_id, cliente_id, inicio + i, now_str()),
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
                """, (campania_id,)
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
