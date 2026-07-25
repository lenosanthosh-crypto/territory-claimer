import os, json, hashlib, sqlite3, time, math
from flask import Flask, jsonify, request, send_from_directory
from shapely.geometry import Polygon, mapping, shape
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'territory.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
        total_area REAL DEFAULT 0, ride_count INTEGER DEFAULT 0, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS rides (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
        time TEXT, points_json TEXT NOT NULL, stats_json TEXT NOT NULL,
        route_polygon_json TEXT, claimed_area_json TEXT, claimed_area_m2 REAL DEFAULT 0,
        created_at REAL)''')
    return conn

def init_db():
    db = get_db()
    db.commit()
    db.close()

def project_area_m2(poly):
    if poly is None or poly.is_empty:
        return 0
    coords = list(poly.exterior.coords)
    if len(coords) < 4:
        return 0
    clat = sum(c[1] for c in coords[:-1]) / max(len(coords) - 1, 1)
    cos_lat = math.cos(math.radians(clat))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * cos_lat
    projected = Polygon([(c[0] * m_per_deg_lon, c[1] * m_per_deg_lat) for c in coords])
    return abs(projected.area)

def user_color(name):
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16)
    hue = h % 360
    return f'hsl({hue}, 70%, 55%)'

def make_user_id(name):
    return hashlib.md5(name.strip().lower().encode()).hexdigest()[:12]

def build_route_polygon(points):
    if len(points) < 3:
        return None
    start = points[0]
    end = points[-1]
    dlat = (end['lat'] - start['lat']) * 111320.0
    dlon = (end['lon'] - start['lon']) * 111320.0 * math.cos(math.radians(start['lat']))
    close_dist = math.sqrt(dlat * dlat + dlon * dlon)
    if close_dist > 500:
        return None
    coords = [(p['lon'], p['lat']) for p in points]
    coords.append(coords[0])
    try:
        poly = Polygon(coords)
        if poly.is_valid and not poly.is_empty and project_area_m2(poly) > 10:
            return poly
    except Exception:
        pass
    return None

def recompute_claims(db, new_ride_id, new_user_id, new_poly, new_time):
    all_rides = db.execute(
        'SELECT id, user_id, claimed_area_json, time, created_at FROM rides WHERE id != ?',
        (new_ride_id,)
    ).fetchall()

    updates = []
    new_claimed = new_poly

    for ride in all_rides:
        if ride['claimed_area_json'] is None:
            continue
        try:
            existing_poly = shape(json.loads(ride['claimed_area_json']))
        except Exception:
            continue
        if existing_poly.is_empty:
            continue

        existing_time = ride['time'] or ''
        ride_is_older = existing_time < new_time if new_time else (ride['created_at'] or 0) < (new_time or 0)

        if existing_poly.intersects(new_poly):
            overlap = existing_poly.intersection(new_poly)
            if overlap.is_empty:
                continue

            if ride_is_older:
                shrunk = existing_poly.difference(new_poly)
                if shrunk.is_empty:
                    shrunk = None
                updates.append({
                    'id': ride['id'],
                    'claimed_area': shrunk,
                    'area_m2': project_area_m2(shrunk) if shrunk else 0,
                    'user_id': ride['user_id']
                })
            else:
                new_claimed = new_claimed.difference(existing_poly)
                if new_claimed.is_empty:
                    new_claimed = None

    for u in updates:
        if u['claimed_area'] is not None:
            db.execute(
                'UPDATE rides SET claimed_area_json = ?, claimed_area_m2 = ? WHERE id = ?',
                (json.dumps(mapping(u['claimed_area'])), u['area_m2'], u['id'])
            )
        else:
            db.execute(
                'UPDATE rides SET claimed_area_json = NULL, claimed_area_m2 = 0 WHERE id = ?',
                (u['id'],)
            )

    affected_user_ids = set()
    for u in updates:
        affected_user_ids.add(u['user_id'])
    affected_user_ids.add(new_user_id)

    for uid in affected_user_ids:
        row = db.execute('SELECT COALESCE(SUM(claimed_area_m2), 0) as total FROM rides WHERE user_id = ?', (uid,)).fetchone()
        db.execute('UPDATE users SET total_area = ? WHERE id = ?', (row['total'], uid))

    return new_claimed

@app.route('/')
def index():
    return send_from_directory('.', 'territory.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    uid = make_user_id(name)
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    if not user:
        color = user_color(name)
        db.execute('INSERT INTO users (id, name, color, created_at) VALUES (?, ?, ?, ?)',
                   (uid, name, color, datetime.utcnow().isoformat()))
        db.commit()
        user = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    db.close()
    return jsonify({
        'id': user['id'], 'name': user['name'], 'color': user['color'],
        'total_area': user['total_area'], 'ride_count': user['ride_count']
    })

@app.route('/api/rides', methods=['GET'])
def get_rides():
    db = get_db()
    rides = db.execute('SELECT * FROM rides ORDER BY created_at DESC').fetchall()
    result = []
    for r in rides:
        pts = json.loads(r['points_json'])
        stats = json.loads(r['stats_json'])
        result.append({
            'id': r['id'], 'user_id': r['user_id'], 'user_name': r['user_name'],
            'time': r['time'], 'points': pts, 'stats': stats,
            'claimed_area': json.loads(r['claimed_area_json']) if r['claimed_area_json'] else None,
            'claimed_area_m2': r['claimed_area_m2'] or 0
        })
    db.close()
    return jsonify(result)

@app.route('/api/rides', methods=['POST'])
def upload_ride():
    data = request.json
    user_id = data.get('user_id')
    user_name = data.get('user_name')
    points = data.get('points', [])
    stats = data.get('stats', {})
    ride_time = data.get('time', '')

    if not user_id or len(points) < 2:
        return jsonify({'error': 'Invalid data'}), 400

    db = get_db()
    poly = build_route_polygon(points)
    claimed_area = poly
    claimed_m2 = project_area_m2(poly) if poly else 0

    cur = db.execute(
        'INSERT INTO rides (user_id, user_name, time, points_json, stats_json, route_polygon_json, claimed_area_json, claimed_area_m2, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (user_id, user_name, ride_time, json.dumps(points), json.dumps(stats),
         json.dumps(mapping(poly)) if poly else None,
         json.dumps(mapping(claimed_area)) if claimed_area else None,
         claimed_m2, time.time())
    )
    ride_id = cur.lastrowid

    if poly:
        final_claimed = recompute_claims(db, ride_id, user_id, poly, ride_time)
        if final_claimed is not None and not final_claimed.is_empty:
            db.execute(
                'UPDATE rides SET claimed_area_json = ?, claimed_area_m2 = ? WHERE id = ?',
                (json.dumps(mapping(final_claimed)), project_area_m2(final_claimed), ride_id)
            )
            claimed_m2 = project_area_m2(final_claimed)
        else:
            db.execute('UPDATE rides SET claimed_area_json = NULL, claimed_area_m2 = 0 WHERE id = ?', (ride_id,))
            claimed_m2 = 0

    db.execute('UPDATE users SET ride_count = ride_count + 1 WHERE id = ?', (user_id,))
    row = db.execute('SELECT COALESCE(SUM(claimed_area_m2), 0) as total FROM rides WHERE user_id = ?', (user_id,)).fetchone()
    db.execute('UPDATE users SET total_area = ? WHERE id = ?', (row['total'], user_id))

    db.commit()
    db.close()

    loop_detected = poly is not None and claimed_m2 > 0
    return jsonify({
        'id': ride_id, 'user_id': user_id, 'user_name': user_name,
        'time': ride_time, 'points': points, 'stats': stats,
        'claimed_area_m2': claimed_m2, 'loop_detected': loop_detected
    })

@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    db = get_db()
    users = db.execute(
        'SELECT id, name, color, total_area, ride_count FROM users ORDER BY total_area DESC'
    ).fetchall()
    result = [{'id': u['id'], 'name': u['name'], 'color': u['color'],
               'total_area': u['total_area'], 'ride_count': u['ride_count']} for u in users]
    db.close()
    return jsonify(result)

@app.route('/api/territories', methods=['GET'])
def territories():
    db = get_db()
    rides = db.execute(
        'SELECT id, user_id, user_name, claimed_area_json, claimed_area_m2, time FROM rides WHERE claimed_area_json IS NOT NULL AND claimed_area_m2 > 0'
    ).fetchall()
    users = {}
    for u in db.execute('SELECT id, color FROM users').fetchall():
        users[u['id']] = u['color']
    db.close()
    result = []
    for r in rides:
        result.append({
            'ride_id': r['id'], 'user_id': r['user_id'], 'user_name': r['user_name'],
            'color': users.get(r['user_id'], '#888'),
            'claimed_area': json.loads(r['claimed_area_json']),
            'claimed_area_m2': r['claimed_area_m2'], 'time': r['time']
        })
    return jsonify(result)

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    print(f"Territory Claimer running at http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
