"""
Geofence Map Builder - Python Backend
Provides:
  - Persistent SQLite Database ('geofences.db')
  - Full REST API (/api/geofences, /api/geofences/<id>, /api/geofences/export, etc.)
  - Real-time GPS Telemetry Evaluation Engine (/api/telemetry/evaluate)
  - Static file serving for the frontend dashboard
  - Dual Engine: Uses Flask if available; falls back automatically to built-in http.server (Zero external dependencies required!)
"""

import os
import sys
import json
import math
import sqlite3
from datetime import datetime

PORT = int(os.environ.get('PORT', 5000))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'geofences.db')
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
EARTH_RADIUS_METERS = 6371008.8

device_last_known_geofences = {}

# -----------------------------------------------------------------------------
# DATABASE INITIALIZATION
# -----------------------------------------------------------------------------

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS geofences (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            coordinates TEXT NOT NULL,
            radius REAL,
            status TEXT NOT NULL DEFAULT 'active',
            color TEXT DEFAULT '#2563eb',
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')

    cursor.execute('SELECT COUNT(*) FROM geofences')
    count = cursor.fetchone()[0]
    if count == 0:
        now = datetime.utcnow().isoformat() + 'Z'
        default_fence = (
            'geo_default_headquarters',
            'Headquarters Perimeter',
            'circle',
            json.dumps({'lat': 30.123456, 'lng': 78.123456}),
            200.0,
            'active',
            '#2563eb',
            'Primary facility security perimeter (200m zone)',
            now,
            now
        )
        cursor.execute('''
            INSERT INTO geofences (id, name, type, coordinates, radius, status, color, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', default_fence)
        conn.commit()

    conn.close()

# -----------------------------------------------------------------------------
# SPATIAL MATHEMATICS ENGINE
# -----------------------------------------------------------------------------

def haversine_distance(lat1, lon1, lat2, lon2):
    to_rad = lambda a: (a * math.pi) / 180.0
    d_lat = to_rad(lat2 - lat1)
    d_lon = to_rad(lon2 - lon1)
    r_lat1 = to_rad(lat1)
    r_lat2 = to_rad(lat2)

    a = (math.sin(d_lat / 2.0) ** 2 +
         math.cos(r_lat1) * math.cos(r_lat2) * (math.sin(d_lon / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_METERS * c

def is_point_in_circle(point, center, radius_meters):
    d = haversine_distance(point[0], point[1], center[0], center[1])
    return d <= radius_meters

def is_point_in_rectangle(point, bounds):
    lat, lng = point
    return bounds.get('south', 0) <= lat <= bounds.get('north', 0) and bounds.get('west', 0) <= lng <= bounds.get('east', 0)

def is_point_in_polygon(point, polygon):
    lat, lng = point
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        intersect = ((yi > lng) != (yj > lng)) and (lat < ((xj - xi) * (lng - yi)) / (yj - yi) + xi)
        if intersect:
            inside = not inside
        j = i
    return inside

def evaluate_point_against_fence(point, fence):
    if fence.get('status') != 'active':
        return False

    ftype = fence.get('type')
    coords = fence.get('coordinates')
    if isinstance(coords, str):
        coords = json.loads(coords)

    if ftype == 'circle':
        center = (coords['lat'], coords['lng'])
        return is_point_in_circle(point, center, fence.get('radius', 0))
    elif ftype == 'rectangle':
        return is_point_in_rectangle(point, coords)
    elif ftype == 'polygon':
        return is_point_in_polygon(point, coords)
    elif ftype == 'flag':
        center = (coords['lat'], coords['lng'])
        return is_point_in_circle(point, center, float(fence.get('radius') or 25.0))
    return False

def row_to_dict(row):
    coords = row['coordinates']
    if isinstance(coords, str):
        try:
            coords = json.loads(coords)
        except Exception:
            pass

    return {
        'id': row['id'],
        'name': row['name'],
        'type': row['type'],
        'coordinates': coords,
        'radius': row['radius'],
        'status': row['status'],
        'color': row['color'],
        'description': row['description'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at']
    }

# -----------------------------------------------------------------------------
# CORE CRUD LOGIC (SHARED)
# -----------------------------------------------------------------------------

def db_list_geofences():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM geofences ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def db_create_geofence(data):
    now = datetime.utcnow().isoformat() + 'Z'
    fence_id = data.get('id') or f"geo_{int(datetime.utcnow().timestamp()*1000)}"
    name = (data.get('name') or 'Unnamed Geofence').strip()
    ftype = data.get('type')
    coords = data.get('coordinates')
    radius = float(data.get('radius')) if (ftype in ['circle', 'flag']) and data.get('radius') is not None else None
    status = data.get('status') or 'active'
    color = data.get('color') or '#2563eb'
    description = (data.get('description') or '').strip()

    coords_json = json.dumps(coords) if not isinstance(coords, str) else coords

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO geofences (id, name, type, coordinates, radius, status, color, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (fence_id, name, ftype, coords_json, radius, status, color, description, now, now))
    conn.commit()

    c.execute('SELECT * FROM geofences WHERE id = ?', (fence_id,))
    row = c.fetchone()
    conn.close()
    return row_to_dict(row)

def db_update_geofence(fence_id, data):
    now = datetime.utcnow().isoformat() + 'Z'
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM geofences WHERE id = ?', (fence_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None

    existing = row_to_dict(row)
    name = (data.get('name') if 'name' in data else existing['name']).strip()
    ftype = data.get('type') if 'type' in data else existing['type']
    coords = data.get('coordinates') if 'coordinates' in data else existing['coordinates']
    if 'radius' in data and data['radius'] is not None and ftype in ['circle', 'flag']:
        radius = float(data['radius'])
    elif ftype in ['circle', 'flag']:
        radius = float(existing.get('radius') or 25)
    else:
        radius = None
    status = data.get('status') if 'status' in data else existing['status']
    color = data.get('color') if 'color' in data else existing['color']
    description = (data.get('description') if 'description' in data else existing['description']).strip()

    coords_json = json.dumps(coords) if not isinstance(coords, str) else coords

    c.execute('''
        UPDATE geofences
        SET name = ?, type = ?, coordinates = ?, radius = ?, status = ?, color = ?, description = ?, updated_at = ?
        WHERE id = ?
    ''', (name, ftype, coords_json, radius, status, color, description, now, fence_id))
    conn.commit()

    c.execute('SELECT * FROM geofences WHERE id = ?', (fence_id,))
    updated = c.fetchone()
    conn.close()
    return row_to_dict(updated)

def db_delete_geofence(fence_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM geofences WHERE id = ?', (fence_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def db_toggle_geofence(fence_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT status FROM geofences WHERE id = ?', (fence_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None

    new_status = 'disabled' if row['status'] == 'active' else 'active'
    now = datetime.utcnow().isoformat() + 'Z'
    c.execute('UPDATE geofences SET status = ?, updated_at = ? WHERE id = ?', (new_status, now, fence_id))
    conn.commit()
    c.execute('SELECT * FROM geofences WHERE id = ?', (fence_id,))
    updated = c.fetchone()
    conn.close()
    return row_to_dict(updated)

def db_clear_all():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM geofences')
    conn.commit()
    conn.close()

def db_export_geojson():
    fences = db_list_geofences()
    features = []
    for f in fences:
        geom = None
        if f['type'] in ('circle', 'flag'):
            geom = {'type': 'Point', 'coordinates': [f['coordinates']['lng'], f['coordinates']['lat']]}
        elif f['type'] == 'rectangle':
            c = f['coordinates']
            geom = {
                'type': 'Polygon',
                'coordinates': [[[c['west'], c['north']], [c['east'], c['north']], [c['east'], c['south']], [c['west'], c['south']], [c['west'], c['north']]]]
            }
        elif f['type'] == 'polygon':
            ring = [[pt[1], pt[0]] for pt in f['coordinates']]
            if ring and ring[0] != ring[-1]:
                ring.append(ring[0])
            geom = {'type': 'Polygon', 'coordinates': [ring]}

        features.append({
            'type': 'Feature',
            'id': f['id'],
            'properties': f,
            'geometry': geom
        })
    return {'type': 'FeatureCollection', 'features': features}

def evaluate_telemetry(data):
    device_id = str(data.get('device_id', 'default_device'))
    lat = float(data['latitude'])
    lng = float(data['longitude'])
    point = (lat, lng)

    all_fences = db_list_geofences()
    currently_inside = set()
    inside_details = []

    for f in all_fences:
        if f.get('status') == 'active' and evaluate_point_against_fence(point, f):
            currently_inside.add(f['id'])
            inside_details.append({
                'id': f['id'],
                'name': f['name'],
                'type': f['type']
            })

    previously_inside = device_last_known_geofences.get(device_id, set())
    device_last_known_geofences[device_id] = currently_inside

    events = []
    for fid in (currently_inside - previously_inside):
        events.append({
            'event': 'ENTER',
            'device_id': device_id,
            'geofence_id': fid,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        })

    for fid in (previously_inside - currently_inside):
        events.append({
            'event': 'EXIT',
            'device_id': device_id,
            'geofence_id': fid,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        })

    return {
        'device_id': device_id,
        'coordinate': {'latitude': lat, 'longitude': lng},
        'inside_geofences': inside_details,
        'events': events
    }

# -----------------------------------------------------------------------------
# ENGINE 1: FLASK IMPLEMENTATION (When Flask is available)
# -----------------------------------------------------------------------------

def run_flask():
    from flask import Flask, request, jsonify, send_from_directory
    app = Flask(__name__, static_folder='.', static_url_path='')

    @app.after_request
    def cors(resp):
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return resp

    @app.route('/api/geofences', methods=['GET'])
    def api_get():
        return jsonify(db_list_geofences())

    @app.route('/api/geofences', methods=['POST'])
    def api_post():
        data = request.get_json(force=True)
        return jsonify(db_create_geofence(data)), 201

    @app.route('/api/geofences/<fid>', methods=['PUT'])
    def api_put(fid):
        data = request.get_json(force=True)
        res = db_update_geofence(fid, data)
        return jsonify(res) if res else (jsonify({'error': 'Not found'}), 404)

    @app.route('/api/geofences/<fid>', methods=['DELETE'])
    def api_del(fid):
        return jsonify({'success': True}) if db_delete_geofence(fid) else (jsonify({'error': 'Not found'}), 404)

    @app.route('/api/geofences/<fid>/toggle', methods=['POST'])
    def api_tog(fid):
        res = db_toggle_geofence(fid)
        return jsonify(res) if res else (jsonify({'error': 'Not found'}), 404)

    @app.route('/api/geofences', methods=['DELETE'])
    def api_clear():
        db_clear_all()
        return jsonify({'success': True})

    @app.route('/api/geofences/export', methods=['GET'])
    def api_export():
        return jsonify(db_export_geojson())

    @app.route('/api/telemetry/evaluate', methods=['POST'])
    def api_telemetry():
        data = request.get_json(force=True)
        return jsonify(evaluate_telemetry(data))

    @app.route('/')
    def root():
        return send_from_directory('.', 'index.html')

    @app.route('/<path:p>')
    def files(p):
        return send_from_directory('.', p)

    app.run(host='0.0.0.0', port=PORT, debug=False)

# -----------------------------------------------------------------------------
# ENGINE 2: BUILT-IN HTTP SERVER FALLBACK (Zero Dependencies)
# -----------------------------------------------------------------------------

def run_builtin():
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    import urllib.parse

    class GeofenceHandler(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == '/api/geofences':
                self.send_json(db_list_geofences())
            elif path == '/api/geofences/export':
                self.send_json(db_export_geojson())
            else:
                if path == '/':
                    self.path = '/index.html'
                super().do_GET()

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self.read_json_body()

            if path == '/api/geofences':
                created = db_create_geofence(body)
                self.send_json(created, status=201)
            elif path == '/api/telemetry/evaluate':
                self.send_json(evaluate_telemetry(body))
            elif path.startswith('/api/geofences/') and path.endswith('/toggle'):
                fid = path.split('/')[3]
                updated = db_toggle_geofence(fid)
                if updated:
                    self.send_json(updated)
                else:
                    self.send_json({'error': 'Not found'}, status=404)
            else:
                self.send_json({'error': 'Unknown endpoint'}, status=404)

        def do_PUT(self):
            path = urllib.parse.urlparse(self.path).path
            body = self.read_json_body()
            if path.startswith('/api/geofences/'):
                fid = path.split('/')[-1]
                updated = db_update_geofence(fid, body)
                if updated:
                    self.send_json(updated)
                else:
                    self.send_json({'error': 'Not found'}, status=404)

        def do_DELETE(self):
            path = urllib.parse.urlparse(self.path).path
            if path == '/api/geofences':
                db_clear_all()
                self.send_json({'success': True})
            elif path.startswith('/api/geofences/'):
                fid = path.split('/')[-1]
                if db_delete_geofence(fid):
                    self.send_json({'success': True})
                else:
                    self.send_json({'error': 'Not found'}, status=404)

        def read_json_body(self):
            length = int(self.headers.get('Content-Length', 0))
            if length > 0:
                raw = self.rfile.read(length).decode('utf-8')
                return json.loads(raw)
            return {}

        def send_json(self, data, status=200):
            content = json.dumps(data).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = HTTPServer(('0.0.0.0', PORT), GeofenceHandler)
    server.serve_forever()

# -----------------------------------------------------------------------------
# MAIN BOOTSTRAP
# -----------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    print("=" * 60)
    print(">> GEOFENCE MAP BUILDER - PYTHON BACKEND")
    print(f">> Database: SQLite ({DB_PATH})")
    print(f">> Serving at: http://localhost:{PORT}")
    print("=" * 60)

    try:
        import flask
        print(">> Engine: Flask Active")
        run_flask()
    except ImportError:
        print(">> Engine: Built-in Python HTTP Engine (Flask not in this environment)")
        run_builtin()
