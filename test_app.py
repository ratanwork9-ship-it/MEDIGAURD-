"""Small API regression tests for the MediGuard local prototype."""
import os
import tempfile
import unittest

DB_FILE = tempfile.NamedTemporaryFile(prefix="mediguard-test-", suffix=".db", delete=False)
DB_FILE.close()
os.environ["MEDIGUARD_DB"] = DB_FILE.name
os.environ["MEDIGUARD_KEY"] = "test-key-not-for-production"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app as server  # noqa: E402


class MediGuardApiTests(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()
        with server.connect() as conn:
            conn.execute("DELETE FROM readings")
            conn.execute("DELETE FROM alerts")
            conn.execute("DELETE FROM settings")

    def test_reading_requires_api_key(self):
        response = self.client.post("/api/readings", json={"temperature": 5, "humidity": 45})
        self.assertEqual(response.status_code, 401)

    def test_rejects_invalid_sensor_values(self):
        response = self.client.post(
            "/api/readings",
            headers={"X-API-Key": "test-key-not-for-production"},
            json={"temperature": 500, "humidity": 45},
        )
        self.assertEqual(response.status_code, 400)

    def test_valid_reading_appears_on_latest_endpoint(self):
        response = self.client.post(
            "/api/readings",
            headers={"X-API-Key": "test-key-not-for-production"},
            json={"temperature": 5.2, "humidity": 45, "device_id": "test-device", "cooling": False},
        )
        self.assertEqual(response.status_code, 200)
        latest = self.client.get("/api/latest").get_json()
        self.assertEqual(latest["reading"]["device_id"], "test-device")
        self.assertAlmostEqual(latest["reading"]["temperature"], 5.2)

    def test_invalid_limits_are_rejected(self):
        response = self.client.put("/api/settings", json={"temp_min": 8, "temp_max": 2})
        self.assertEqual(response.status_code, 400)

    def test_alert_limit_is_bounded(self):
        response = self.client.get("/api/alerts?limit=-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["alerts"], [])


if __name__ == "__main__":
    unittest.main()
