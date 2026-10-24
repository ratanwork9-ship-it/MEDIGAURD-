#!/usr/bin/env python3
"""
Mediguard server
----------------
Receives temperature/humidity readings from the ESP32, stores them in SQLite,
raises alerts when readings leave the allowed range, and serves the dashboard.

Run:        python app.py
Demo mode:  python app.py --demo      (fake readings, no hardware needed)
"""
import argparse
import csv
import io
import math
import os
import random
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MEDIGUARD_DB", os.path.join(BASE, "mediguard.db"))
# Prototype default for local/LAN development only. Set MEDIGUARD_KEY in real deployments.
API_KEY = os.environ.get("MEDIGUARD_KEY", "change-me")
OFFLINE_AFTER = int(os.environ.get("MEDIGUARD_OFFLINE_AFTER", "60"))  # seconds without data = offline

# Default safe limits. Editable from the dashboard. Adjust for your medicines.
# Example prototype thresholds. These are configurable and are NOT universal
# medical-storage recommendations. Set them according to the specific medicine.
DEFAULTS = {"temp_min": 2.0, "temp_max": 8.0, "hum_min": 30.0, "hum_max": 60.0}

FRONTEND = os.path.join(os.path.dirname(BASE), "frontend")
app = Flask(__name__, static_folder=FRONTEND, static_url_path="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,          -- unix time, seconds (UTC)
  device_id   TEXT    NOT NULL,
  temperature REAL    NOT NULL,          -- deg C
  humidity    REAL    NOT NULL,          -- % RH
  cooling     INTEGER NOT NULL DEFAULT 0, -- 1 = cooling fan/airflow active
  servo_angle INTEGER
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS alerts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  device_id   TEXT    NOT NULL,
  metric      TEXT    NOT NULL,          -- temperature | humidity
  direction   TEXT    NOT NULL,          -- high | low
  value       REAL    NOT NULL,
  resolved_ts INTEGER                    -- NULL while still out of range
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL);
"""


# ---------------------------------------------------------------- database
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_limits(conn):
    limits = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in limits:
            limits[row["key"]] = row["value"]
    return limits


def classify(value, lo, hi):
    if value > hi:
        return "high"
    if value < lo:
        return "low"
    return "ok"


def record_reading(conn, device_id, temperature, humidity, cooling, servo_angle, ts=None):
    """Store one reading and open/close alerts as needed. Returns the current limits."""
    ts = int(ts or time.time())
    conn.execute(
        "INSERT INTO readings (ts, device_id, temperature, humidity, cooling, servo_angle) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, device_id, temperature, humidity, 1 if cooling else 0, servo_angle),
    )
    limits = get_limits(conn)
    checks = (
        ("temperature", temperature, limits["temp_min"], limits["temp_max"]),
        ("humidity", humidity, limits["hum_min"], limits["hum_max"]),
    )
    for metric, value, lo, hi in checks:
        state = classify(value, lo, hi)
        open_alert = conn.execute(
            "SELECT id, direction FROM alerts "
            "WHERE device_id=? AND metric=? AND resolved_ts IS NULL ORDER BY id DESC LIMIT 1",
            (device_id, metric),
        ).fetchone()
        if open_alert and open_alert["direction"] != state:
            conn.execute("UPDATE alerts SET resolved_ts=? WHERE id=?", (ts, open_alert["id"]))
            open_alert = None
        if state != "ok" and open_alert is None:
            conn.execute(
                "INSERT INTO alerts (ts, device_id, metric, direction, value) VALUES (?, ?, ?, ?, ?)",
                (ts, device_id, metric, state, value),
            )
    conn.commit()
    return limits


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- API: ESP32 -> server
@app.post("/api/readings")
def post_reading():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify(error="invalid API key"), 401

    body = request.get_json(silent=True) or {}
    try:
        temperature = float(body["temperature"])
        humidity = float(body["humidity"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="temperature and humidity must be numbers"), 400
    if not (-40 <= temperature <= 85) or not (0 <= humidity <= 100):  # also rejects NaN
        return jsonify(error="reading out of sensor range"), 400

    now = int(time.time())
    ts = body.get("ts")
    # Buffered readings from the ESP32 carry their own timestamp (needs NTP on the device).
    if not (isinstance(ts, (int, float)) and 1_600_000_000 < ts <= now + 300):
        ts = now

    servo = body.get("servo_angle")
    servo = int(servo) if isinstance(servo, (int, float)) else None
    device_id = str(body.get("device_id", "mediguard-1"))[:40]

    limits = record_reading(get_db(), device_id, temperature, humidity, body.get("cooling"), servo, ts)
    return jsonify(ok=True, server_time=now, limits=limits)


# ---------------------------------------------------------------- API: dashboard
@app.get("/api/latest")
def latest():
    db = get_db()
    limits = get_limits(db)
    row = db.execute("SELECT * FROM readings ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return jsonify(reading=None, limits=limits)
    age = max(0, int(time.time()) - row["ts"])
    return jsonify(
        reading=dict(row),
        age=age,
        online=age <= OFFLINE_AFTER,
        status={
            "temperature": classify(row["temperature"], limits["temp_min"], limits["temp_max"]),
            "humidity": classify(row["humidity"], limits["hum_min"], limits["hum_max"]),
        },
        limits=limits,
    )


@app.get("/api/readings")
def readings():
    """Time series, averaged into at most ~600 buckets so long ranges stay fast."""
    minutes = request.args.get("minutes", 60, type=int)
    db = get_db()
    now = int(time.time())
    start = 0 if minutes <= 0 else now - minutes * 60
    first = db.execute("SELECT MIN(ts) AS m FROM readings WHERE ts >= ?", (start,)).fetchone()["m"]
    if first is None:
        return jsonify(readings=[])
    bucket = max(1, (now - first) // 600)
    rows = db.execute(
        "SELECT (ts / ?) * ? AS t, AVG(temperature) AS temperature, AVG(humidity) AS humidity, "
        "MAX(cooling) AS cooling, AVG(servo_angle) AS servo_angle "
        "FROM readings WHERE ts >= ? GROUP BY t ORDER BY t",
        (bucket, bucket, start),
    ).fetchall()
    out = [
        {
            "t": r["t"],
            "temperature": round(r["temperature"], 2),
            "humidity": round(r["humidity"], 1),
            "cooling": r["cooling"],
            "servo_angle": None if r["servo_angle"] is None else round(r["servo_angle"]),
        }
        for r in rows
    ]
    return jsonify(readings=out, bucket_seconds=bucket)


@app.get("/api/stats")
def stats():
    minutes = request.args.get("minutes", 60, type=int)
    db = get_db()
    start = 0 if minutes <= 0 else int(time.time()) - minutes * 60
    limits = get_limits(db)
    r = db.execute(
        "SELECT COUNT(*) AS n, MIN(temperature) AS tmin, MAX(temperature) AS tmax, AVG(temperature) AS tavg, "
        "MIN(humidity) AS hmin, MAX(humidity) AS hmax, AVG(humidity) AS havg, AVG(cooling) AS cool "
        "FROM readings WHERE ts >= ?",
        (start,),
    ).fetchone()
    in_range = db.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE ts >= ? "
        "AND temperature BETWEEN ? AND ? AND humidity BETWEEN ? AND ?",
        (start, limits["temp_min"], limits["temp_max"], limits["hum_min"], limits["hum_max"]),
    ).fetchone()["n"]
    n = r["n"]
    return jsonify(
        count=n,
        in_range_pct=None if not n else round(100 * in_range / n, 1),
        temperature={"min": r["tmin"], "max": r["tmax"], "avg": r["tavg"]},
        humidity={"min": r["hmin"], "max": r["hmax"], "avg": r["havg"]},
        cooling_pct=None if not n else round(100 * (r["cool"] or 0), 1),
    )


@app.get("/api/alerts")
def alerts():
    limit = max(1, min(request.args.get("limit", 20, type=int), 200))
    rows = get_db().execute("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify(alerts=[dict(r) for r in rows])


@app.route("/api/settings", methods=["GET", "PUT"])
def settings():
    db = get_db()
    if request.method == "GET":
        return jsonify(limits=get_limits(db))

    body = request.get_json(silent=True) or {}
    new = get_limits(db)
    try:
        for key in DEFAULTS:
            if key in body:
                new[key] = float(body[key])
    except (TypeError, ValueError):
        return jsonify(error="limits must be numbers"), 400
    if not all(math.isfinite(v) for v in new.values()):
        return jsonify(error="limits must be finite numbers"), 400
    if new["temp_min"] >= new["temp_max"]:
        return jsonify(error="Lowest temperature must be below the highest."), 400
    if new["hum_min"] >= new["hum_max"]:
        return jsonify(error="Lowest humidity must be below the highest."), 400
    if not (-20 <= new["temp_min"] and new["temp_max"] <= 60):
        return jsonify(error="Temperature limits must be between -20 and 60 °C."), 400
    if not (0 <= new["hum_min"] and new["hum_max"] <= 100):
        return jsonify(error="Humidity limits must be between 0 and 100 %."), 400

    for key, value in new.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    db.commit()
    return jsonify(ok=True, limits=new)


@app.get("/api/export.csv")
def export_csv():
    minutes = request.args.get("minutes", 0, type=int)  # 0 = everything
    start = 0 if minutes <= 0 else int(time.time()) - minutes * 60
    rows = get_db().execute(
        "SELECT ts, device_id, temperature, humidity, cooling, servo_angle "
        "FROM readings WHERE ts >= ? ORDER BY ts",
        (start,),
    ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "unix_ts", "device_id", "temperature_c", "humidity_pct", "cooling", "servo_angle"])
    for r in rows:
        w.writerow([
            datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            r["ts"], r["device_id"], r["temperature"], r["humidity"], r["cooling"], r["servo_angle"],
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mediguard-readings.csv"},
    )


# ---------------------------------------------------------------- demo mode
class Simulator:
    """Generate simulated readings for the dashboard preview; no hardware is connected."""

    def __init__(self):
        self.temp, self.hum, self.cooling = 5.8, 46.0, False

    def step(self, ts, dt):
        ambient = 28 + 5 * math.sin(ts / 2400.0)
        target = 5.5 if self.cooling else ambient
        k = min(0.25, 0.006 * dt)
        self.temp += (target - self.temp) * k + random.gauss(0, 0.05)
        if not self.cooling and self.temp >= 6.5:
            self.cooling = True
        elif self.cooling and self.temp <= 5.5:
            self.cooling = False
        hum_target = 44 + 4 * math.sin(ts / 3100.0) + (8 if self.cooling else 0)
        self.hum += (hum_target - self.hum) * k + random.gauss(0, 0.15)
        if random.random() < 0.001 * dt:      # occasional "lid left open" humidity spike
            self.hum += random.uniform(12, 20)
        self.hum = max(5.0, min(95.0, self.hum))
        return round(self.temp, 2), round(self.hum, 1), self.cooling, (90 if self.cooling else 0)


def start_demo():
    conn = connect()
    sim = Simulator()
    now = int(time.time())
    have = conn.execute("SELECT COUNT(*) AS n FROM readings WHERE ts >= ?", (now - 3600,)).fetchone()["n"]
    if have == 0:  # backfill 3 hours so the charts aren't empty
        for ts in range(now - 3 * 3600, now, 30):
            record_reading(conn, "demo-esp32", *sim.step(ts, 30), ts=ts)
    conn.close()

    def loop():
        thread_conn = connect()  # SQLite connections can't be shared across threads
        while True:
            ts = int(time.time())
            record_reading(thread_conn, "demo-esp32", *sim.step(ts, 2), ts=ts)
            time.sleep(2)

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- main
init_db()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="generate fake readings (no hardware needed)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    if args.demo:
        start_demo()
    ip = lan_ip()
    print(f"\n  Dashboard:  http://localhost:{args.port}")
    print(f"  ESP32 URL:  http://{ip}:{args.port}/api/readings   (put this in the firmware)")
    print("  API key:    configured from MEDIGUARD_KEY (default is development-only)\n")
    app.run(host=args.host, port=args.port, threaded=True)
