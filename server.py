"""
Geofence Map Builder - Python Backend
Provides:
  - MongoDB & MongoDB Compass integration (primary persistent store)
  - Persistent SQLite fallback ('geofences.db')
  - Full REST API (/api/geofences, /api/geofences/<id>, /api/geofences/export, /api/database/status, etc.)
  - Real-time GPS Telemetry Evaluation Engine (/api/telemetry/evaluate)
  - Footprint Recording & Live Event Logging ('footprints.log')
  - Static file serving for the frontend dashboard
  - Dual Engine: Uses Flask if available; falls back automatically to built-in http.server
"""

import os
import sys
import json
import math
import random
import uuid
import sqlite3
from datetime import datetime

# Load environment variables (.env)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# MongoDB Driver (pymongo)
try:
    import pymongo
except ImportError:
    pymongo = None

PORT = int(os.environ.get('PORT', 5000))
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017')
MONGODB_DB_NAME = os.environ.get('MONGODB_DB_NAME', 'virtual_fence')
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'geofences.db')
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
FOOTPRINTS_LOG_PATH = os.path.join(STATIC_DIR, 'footprints.log')
EARTH_RADIUS_METERS = 6371008.8

mongo_client = None
mongo_db = None
use_mongodb = False
device_last_known_geofences = {}

# -----------------------------------------------------------------------------
# DATABASE INITIALIZATION: SQLITE & MONGODB COMPASS INTEGRATION
# -----------------------------------------------------------------------------

def get_sqlite_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sqlite_init_db():
    conn = get_sqlite_connection()
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS footprints (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            geofence_id TEXT,
            geofence_name TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            event TEXT NOT NULL,
            source TEXT DEFAULT 'simulation',
            created_at TEXT NOT NULL
        )
    ''')

    if not os.path.exists(FOOTPRINTS_LOG_PATH):
        try:
            with open(FOOTPRINTS_LOG_PATH, 'w', encoding='utf-8') as f:
                f.write(f"# Footprint Activity Log Initialized at {datetime.utcnow().isoformat()}Z\n")
        except Exception:
            pass

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

def init_mongo_connection():
    global mongo_client, mongo_db, use_mongodb
    if pymongo is None:
        print(">> [Database Notice] 'pymongo' not installed. Running on SQLite.")
        return False

    try:
        client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=1800)
        client.admin.command('ping')
        mongo_client = client
        mongo_db = client[MONGODB_DB_NAME]
        use_mongodb = True

        mongo_db.geofences.create_index('id', unique=True)
        mongo_db.footprints.create_index('id', unique=True)
        mongo_db.footprints.create_index([('created_at', pymongo.DESCENDING)])

        print(">> [Database] ==================================================")
        print(">> [Database] MongoDB Connection Established!")
        print(f">> [Database] Database: '{MONGODB_DB_NAME}'")
        print(f">> [Database] MongoDB Compass URI: {MONGODB_URI}")
        print(f">> [Database] Collections: 'geofences', 'footprints'")
        print(">> [Database] ==================================================")

        # If MongoDB geofences is empty, migrate existing fences from SQLite or seed default
        if mongo_db.geofences.count_documents({}) == 0:
            existing_fences = sqlite_list_geofences()
            if existing_fences:
                for f in existing_fences:
                    clean_f = dict(f)
                    clean_f.pop('_id', None)
                    mongo_db.geofences.insert_one(clean_f)
                print(f">> [Database] Migrated {len(existing_fences)} geofence(s) to MongoDB '{MONGODB_DB_NAME}'!")
            else:
                now = datetime.utcnow().isoformat() + 'Z'
                mongo_db.geofences.insert_one({
                    'id': 'geo_default_headquarters',
                    'name': 'Headquarters Perimeter',
                    'type': 'circle',
                    'coordinates': {'lat': 30.123456, 'lng': 78.123456},
                    'radius': 200.0,
                    'status': 'active',
                    'color': '#2563eb',
                    'description': 'Primary facility security perimeter (200m zone)',
                    'created_at': now,
                    'updated_at': now
                })
                print(f">> [Database] Seeded initial default geofence into MongoDB '{MONGODB_DB_NAME}.geofences'")
        return True
    except Exception as e:
        use_mongodb = False
        print(f">> [Database Notice] MongoDB Compass connection '{MONGODB_URI}' unreachable ({e}).")
        print(f">> [Database Notice] Using local SQLite database (geofences.db).")
        print(f">> [Database Notice] In MongoDB Compass, open and connect to: {MONGODB_URI}")
        return False

def init_db():
    sqlite_init_db()
    init_mongo_connection()

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
    return False

# -----------------------------------------------------------------------------
# 1. SQLITE PERSISTENCE LAYER (LOCAL DUAL-STORAGE / FALLBACK)
# -----------------------------------------------------------------------------

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

def sqlite_list_geofences():
    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM geofences ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def sqlite_create_geofence(data):
    now = datetime.utcnow().isoformat() + 'Z'
    fence_id = data.get('id') or f"geo_{int(datetime.utcnow().timestamp()*1000)}"
    name = (data.get('name') or 'Unnamed Geofence').strip()
    ftype = data.get('type')
    coords = data.get('coordinates')
    radius = float(data.get('radius')) if ftype == 'circle' and data.get('radius') is not None else None
    status = data.get('status') or 'active'
    color = data.get('color') or '#2563eb'
    description = (data.get('description') or '').strip()
    coords_json = json.dumps(coords) if not isinstance(coords, str) else coords

    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO geofences (id, name, type, coordinates, radius, status, color, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (fence_id, name, ftype, coords_json, radius, status, color, description, now, now))
    conn.commit()
    c.execute('SELECT * FROM geofences WHERE id = ?', (fence_id,))
    row = c.fetchone()
    conn.close()
    return row_to_dict(row)

def sqlite_update_geofence(fence_id, data):
    now = datetime.utcnow().isoformat() + 'Z'
    conn = get_sqlite_connection()
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
    radius = float(data['radius']) if 'radius' in data and data['radius'] is not None and ftype == 'circle' else (float(existing.get('radius') or 200) if ftype == 'circle' else None)
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

def sqlite_delete_geofence(fence_id):
    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('DELETE FROM geofences WHERE id = ?', (fence_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def sqlite_toggle_geofence(fence_id):
    conn = get_sqlite_connection()
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

def sqlite_clear_all():
    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('DELETE FROM geofences')
    conn.commit()
    conn.close()

def sqlite_record_footprint(record):
    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO footprints (id, device_id, geofence_id, geofence_name, latitude, longitude, event, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (record['id'], record['device_id'], record['geofence_id'], record['geofence_name'],
          record['latitude'], record['longitude'], record['event'], record['source'], record['created_at']))
    conn.commit()
    conn.close()

def sqlite_list_footprints(limit=50):
    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM footprints ORDER BY created_at DESC LIMIT ?', (int(limit),))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def sqlite_clear_footprints():
    conn = get_sqlite_connection()
    c = conn.cursor()
    c.execute('DELETE FROM footprints')
    conn.commit()
    conn.close()

# -----------------------------------------------------------------------------
# 2. MONGODB PERSISTENCE LAYER (COMPASS COMPATIBLE)
# -----------------------------------------------------------------------------

def mongo_list_geofences():
    docs = mongo_db.geofences.find({}, {'_id': 0}).sort('created_at', -1)
    return list(docs)

def mongo_create_geofence(fence_dict):
    doc = dict(fence_dict)
    doc.pop('_id', None)
    mongo_db.geofences.replace_one({'id': doc['id']}, doc, upsert=True)
    return doc

def mongo_update_geofence(fence_id, data):
    existing = mongo_db.geofences.find_one({'id': fence_id}, {'_id': 0})
    if not existing:
        return None
    now = datetime.utcnow().isoformat() + 'Z'
    updated = dict(existing)
    for k in ['name', 'type', 'coordinates', 'radius', 'status', 'color', 'description']:
        if k in data:
            updated[k] = data[k]
    updated['updated_at'] = now
    mongo_db.geofences.replace_one({'id': fence_id}, updated)
    return updated

def mongo_delete_geofence(fence_id):
    res = mongo_db.geofences.delete_one({'id': fence_id})
    return res.deleted_count > 0

def mongo_toggle_geofence(fence_id):
    existing = mongo_db.geofences.find_one({'id': fence_id}, {'_id': 0})
    if not existing:
        return None
    new_status = 'disabled' if existing.get('status') == 'active' else 'active'
    now = datetime.utcnow().isoformat() + 'Z'
    existing['status'] = new_status
    existing['updated_at'] = now
    mongo_db.geofences.replace_one({'id': fence_id}, existing)
    return existing

def mongo_clear_all():
    mongo_db.geofences.delete_many({})

def mongo_record_footprint(record):
    doc = dict(record)
    doc.pop('_id', None)
    mongo_db.footprints.insert_one(doc)

def mongo_list_footprints(limit=50):
    docs = mongo_db.footprints.find({}, {'_id': 0}).sort('created_at', -1).limit(int(limit))
    return list(docs)

def mongo_clear_footprints():
    mongo_db.footprints.delete_many({})

# -----------------------------------------------------------------------------
# 3. UNIFIED DATABASE DISPATCHER (MONGODB PRIMARY + SQLITE DUAL SYNC)
# -----------------------------------------------------------------------------

def db_list_geofences():
    if use_mongodb and mongo_db is not None:
        try:
            return mongo_list_geofences()
        except Exception as e:
            print(f">> [Database Warning] MongoDB read failed: {e}. Falling back to SQLite.")
    return sqlite_list_geofences()

def db_create_geofence(data):
    now = datetime.utcnow().isoformat() + 'Z'
    fence_id = data.get('id') or f"geo_{int(datetime.utcnow().timestamp()*1000)}"
    name = (data.get('name') or 'Unnamed Geofence').strip()
    ftype = data.get('type')
    coords = data.get('coordinates')
    radius = float(data.get('radius')) if ftype == 'circle' and data.get('radius') is not None else None
    status = data.get('status') or 'active'
    color = data.get('color') or '#2563eb'
    description = (data.get('description') or '').strip()

    fence_dict = {
        'id': fence_id,
        'name': name,
        'type': ftype,
        'coordinates': coords,
        'radius': radius,
        'status': status,
        'color': color,
        'description': description,
        'created_at': now,
        'updated_at': now
    }

    # Save to SQLite
    sqlite_create_geofence(fence_dict)

    # Save to MongoDB if available
    if use_mongodb and mongo_db is not None:
        try:
            mongo_create_geofence(fence_dict)
        except Exception as e:
            print(f">> [Database Warning] MongoDB write failed: {e}")

    return fence_dict

def db_update_geofence(fence_id, data):
    sqlite_res = sqlite_update_geofence(fence_id, data)
    if use_mongodb and mongo_db is not None:
        try:
            mongo_res = mongo_update_geofence(fence_id, data)
            if mongo_res:
                return mongo_res
        except Exception as e:
            print(f">> [Database Warning] MongoDB update failed: {e}")
    return sqlite_res

def db_delete_geofence(fence_id):
    if use_mongodb and mongo_db is not None:
        try:
            mongo_delete_geofence(fence_id)
        except Exception:
            pass
    return sqlite_delete_geofence(fence_id)

def db_toggle_geofence(fence_id):
    sqlite_res = sqlite_toggle_geofence(fence_id)
    if use_mongodb and mongo_db is not None:
        try:
            mongo_res = mongo_toggle_geofence(fence_id)
            if mongo_res:
                return mongo_res
        except Exception:
            pass
    return sqlite_res

def db_clear_all():
    if use_mongodb and mongo_db is not None:
        try:
            mongo_clear_all()
        except Exception:
            pass
    sqlite_clear_all()

def db_export_geojson():
    fences = db_list_geofences()
    features = []
    for f in fences:
        geom = None
        if f['type'] == 'circle':
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
# FOOTPRINT RECORDING, LOGGING & DUMMY AREA DATA GENERATOR
# -----------------------------------------------------------------------------

def log_footprint_entry(entry):
    try:
        ts = entry.get('created_at') or datetime.utcnow().isoformat() + 'Z'
        device = entry.get('device_id', 'UNKNOWN_DEVICE')
        fence_name = entry.get('geofence_name') or 'N/A'
        event = entry.get('event', 'INSIDE')
        lat = entry.get('latitude', 0.0)
        lng = entry.get('longitude', 0.0)
        source = entry.get('source', 'general')

        line = f"[{ts}] [{event.upper()}] Device: {device} | Fence: {fence_name} | Lat: {float(lat):.6f}, Lng: {float(lng):.6f} | Source: {source}\n"
        with open(FOOTPRINTS_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line)
            f.flush()
    except Exception as e:
        print(f">> Error logging footprint to file: {e}", file=sys.stderr)

def db_record_footprint(data):
    fid = data.get('id') or f"fp_{int(datetime.utcnow().timestamp()*1000)}_{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow().isoformat() + 'Z'
    device_id = str(data.get('device_id', 'MOUSE_KEY'))
    geofence_id = data.get('geofence_id')
    geofence_name = data.get('geofence_name')
    latitude = float(data.get('latitude', 0.0))
    longitude = float(data.get('longitude', 0.0))
    event = str(data.get('event', 'INSIDE')).upper()
    source = str(data.get('source', 'mouse_or_sim'))

    record = {
        'id': fid,
        'device_id': device_id,
        'geofence_id': geofence_id,
        'geofence_name': geofence_name,
        'latitude': latitude,
        'longitude': longitude,
        'event': event,
        'source': source,
        'created_at': now
    }

    # Record in MongoDB
    if use_mongodb and mongo_db is not None:
        try:
            mongo_record_footprint(record)
        except Exception as e:
            print(f">> [Database Warning] MongoDB footprint write failed: {e}")

    # Record in SQLite & log file
    sqlite_record_footprint(record)
    log_footprint_entry(record)
    return record

def db_list_footprints(limit=50):
    if use_mongodb and mongo_db is not None:
        try:
            return mongo_list_footprints(limit)
        except Exception:
            pass
    return sqlite_list_footprints(limit)

def db_clear_footprints():
    if use_mongodb and mongo_db is not None:
        try:
            mongo_clear_footprints()
        except Exception:
            pass
    sqlite_clear_footprints()
    try:
        with open(FOOTPRINTS_LOG_PATH, 'w', encoding='utf-8') as f:
            f.write(f"# Footprint Activity Log Reset at {datetime.utcnow().isoformat()}Z\n")
    except Exception:
        pass
    return True

def get_database_status():
    fences_count = mongo_db.geofences.count_documents({}) if (use_mongodb and mongo_db is not None) else len(sqlite_list_geofences())
    footprints_count = mongo_db.footprints.count_documents({}) if (use_mongodb and mongo_db is not None) else len(sqlite_list_footprints(10000))
    return {
        'active_database': 'MongoDB' if use_mongodb else 'SQLite',
        'active_engine': 'MongoDB' if use_mongodb else 'SQLite',
        'mongodb_connected': use_mongodb,
        'mongodb_uri': MONGODB_URI,
        'mongodb_database': MONGODB_DB_NAME,
        'compass_connection_string': MONGODB_URI,
        'collections': ['geofences', 'footprints'],
        'geofences_count': fences_count,
        'footprints_count': footprints_count
    }

def generate_dummy_point_inside_fence(fence):
    coords = fence.get('coordinates')
    if isinstance(coords, str):
        try:
            coords = json.loads(coords)
        except Exception:
            return None
    if not coords:
        return None

    ftype = fence.get('type')
    if ftype == 'circle':
        center_lat = float(coords['lat'])
        center_lng = float(coords['lng'])
        radius = float(fence.get('radius') or 200.0)
        # Random point inside 85% of radius to avoid boundary rounding issues
        r = (radius * 0.85) * math.sqrt(0.05 + 0.95 * random.random())
        theta = random.random() * 2 * math.pi
        d_lat = (r * math.cos(theta)) / 111320.0
        d_lng = (r * math.sin(theta)) / (111320.0 * math.cos(math.radians(center_lat)))
        return {'lat': round(center_lat + d_lat, 6), 'lng': round(center_lng + d_lng, 6)}

    elif ftype == 'rectangle':
        north = float(coords['north'])
        south = float(coords['south'])
        east = float(coords['east'])
        west = float(coords['west'])
        lat = south + (north - south) * (0.08 + 0.84 * random.random())
        lng = west + (east - west) * (0.08 + 0.84 * random.random())
        return {'lat': round(lat, 6), 'lng': round(lng, 6)}

    elif ftype == 'polygon':
        pts = coords
        if not isinstance(pts, list) or len(pts) < 3:
            return None
        lats = [p[0] for p in pts]
        lngs = [p[1] for p in pts]
        min_lat, max_lat = min(lats), max(lats)
        min_lng, max_lng = min(lngs), max(lngs)
        for _ in range(100):
            cand_lat = min_lat + (max_lat - min_lat) * random.random()
            cand_lng = min_lng + (max_lng - min_lng) * random.random()
            if is_point_in_polygon((cand_lat, cand_lng), pts):
                return {'lat': round(cand_lat, 6), 'lng': round(cand_lng, 6)}
        avg_lat = sum(lats) / len(lats)
        avg_lng = sum(lngs) / len(lngs)
        return {'lat': round(avg_lat, 6), 'lng': round(avg_lng, 6)}

    return None

def generate_dummy_footprints_in_area(count=5, specific_fence_id=None, client_fences=None):
    all_fences = db_list_geofences()
    
    # If DB is empty but client provided geofences, save them to DB
    if not all_fences and client_fences and isinstance(client_fences, list):
        for cf in client_fences:
            try:
                db_create_geofence(cf)
            except Exception:
                pass
        all_fences = db_list_geofences()

    # If still completely empty, auto-create a default sample geofence so dummy data always works
    if not all_fences:
        default_fence = {
            'name': 'Demo Security Perimeter Alpha',
            'type': 'circle',
            'coordinates': {'lat': 30.123456, 'lng': 78.123456},
            'radius': 350,
            'status': 'active',
            'color': '#2563eb',
            'description': 'Auto-generated demo area for dummy data simulation'
        }
        created = db_create_geofence(default_fence)
        all_fences = [created] if created else []

    active_fences = [f for f in all_fences if f.get('status') == 'active']
    if not active_fences:
        active_fences = all_fences

    if specific_fence_id:
        matched = [f for f in active_fences if f.get('id') == specific_fence_id]
        if matched:
            active_fences = matched

    if not active_fences:
        return []

    device_names = ['PATROL_ALPHA', 'SCOUT_UNIT_02', 'FIELD_RANGER_7', 'DRONE_SURVEILLANCE', 'LOGISTICS_TRUCK_8']
    generated_records = []

    for i in range(int(count)):
        fence = active_fences[i % len(active_fences)]
        pt = generate_dummy_point_inside_fence(fence)
        if pt:
            device_id = device_names[i % len(device_names)]
            rec = db_record_footprint({
                'device_id': device_id,
                'geofence_id': fence['id'],
                'geofence_name': fence['name'],
                'latitude': pt['lat'],
                'longitude': pt['lng'],
                'event': 'INSIDE',
                'source': 'area_dummy_generator'
            })
            generated_records.append(rec)

    return generated_records

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

    @app.route('/api/footprints', methods=['GET'])
    def api_get_footprints():
        limit = request.args.get('limit', 50)
        return jsonify(db_list_footprints(limit))

    @app.route('/api/footprints', methods=['POST'])
    def api_post_footprint():
        data = request.get_json(force=True)
        return jsonify(db_record_footprint(data)), 201

    @app.route('/api/footprints', methods=['DELETE'])
    def api_delete_footprints():
        db_clear_footprints()
        return jsonify({'success': True})

    @app.route('/api/simulation/generate', methods=['POST'])
    def api_generate_dummy():
        data = request.get_json(force=True) or {}
        count = data.get('count', 5)
        fence_id = data.get('geofence_id')
        client_fences = data.get('client_fences') or data.get('fences')
        records = generate_dummy_footprints_in_area(count, fence_id, client_fences)
        return jsonify(records)

    @app.route('/api/database/status', methods=['GET'])
    def api_db_status():
        return jsonify(get_database_status())

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
            elif path == '/api/database/status':
                self.send_json(get_database_status())
            elif path == '/api/footprints':
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int(qs.get('limit', [50])[0])
                self.send_json(db_list_footprints(limit))
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
            elif path == '/api/footprints':
                created = db_record_footprint(body)
                self.send_json(created, status=201)
            elif path == '/api/simulation/generate':
                count = body.get('count', 5) if isinstance(body, dict) else 5
                fence_id = body.get('geofence_id') if isinstance(body, dict) else None
                client_fences = (body.get('client_fences') or body.get('fences')) if isinstance(body, dict) else None
                self.send_json(generate_dummy_footprints_in_area(count, fence_id, client_fences))
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
            elif path == '/api/footprints':
                db_clear_footprints()
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
    db_stat = get_database_status()
    print("=" * 64)
    print(">> GEOFENCE MAP BUILDER - PYTHON BACKEND")
    print(f">> Primary Engine: {db_stat['active_engine']}")
    if db_stat['mongodb_connected']:
        print(f">> MongoDB Compass URI: {db_stat['mongodb_uri']}")
        print(f">> MongoDB Database:    {db_stat['mongodb_database']}")
        print(f">> Compass Collections: geofences ({db_stat['geofences_count']}), footprints ({db_stat['footprints_count']})")
    else:
        print(f">> SQLite Database:     {DB_PATH}")
        print(f">> MongoDB Compass URI: {MONGODB_URI} (Set MONGODB_URI in .env or start mongod)")
    print(f">> Serving Dashboard at: http://localhost:{PORT}")
    print("=" * 64)

    try:
        import flask
        print(">> Web Engine: Flask Active")
        run_flask()
    except ImportError:
        print(">> Web Engine: Built-in Python HTTP Engine (Flask not in this environment)")
        run_builtin()
