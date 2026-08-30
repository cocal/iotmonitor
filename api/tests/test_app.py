from __future__ import annotations

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app import API_PATH, HEALTH_PATH, RAW_FRAME_PATH, create_server  # noqa: E402


class PowerMonitorApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "test.db")
        self.token = "test-device-token"
        self.server = create_server("127.0.0.1", 0, self.database_path, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def valid_payload(self) -> dict:
        return {
            "site_id": "home-pv",
            "device_id": "esp32-c3-001",
            "measurement_point_id": "inverter-ac-output",
            "records": [
                {
                    "sequence": 1042,
                    "measured_at": "2026-08-16T04:01:05+08:00",
                    "power_w": 1487.2,
                    "energy_wh": 128430.5,
                    "voltage_v": 229.6,
                    "current_a": 6.62,
                    "power_factor": 0.98,
                    "quality": "ok",
                }
            ],
        }

    def valid_raw_payload(self) -> dict:
        return {
            "site_id": "home-pv",
            "device_id": "esp8266-12f-001",
            "measurement_point_id": "inverter-ac-output",
            "protocol": "DL/T 645-2007",
            "frames": [
                {
                    "sequence": 1042,
                    "captured_at": "2026-08-16T04:01:05+08:00",
                    "direction": "rx",
                    "frame_hex": "68010203040568910433333333333316",
                }
            ],
        }

    def test_health_check_does_not_require_authentication(self) -> None:
        status, body = self.request("GET", HEALTH_PATH)

        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, body)

    def test_report_requires_bearer_token(self) -> None:
        status, body = self.request("POST", API_PATH, self.valid_payload())

        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["error"]["code"])

    def test_accepts_and_persists_a_power_report(self) -> None:
        status, body = self.request("POST", API_PATH, self.valid_payload(), token=self.token)

        self.assertEqual(202, status)
        self.assertEqual(1, body["accepted_records"])
        self.assertEqual(0, body["duplicate_records"])
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT site_id, device_id, measured_at, power_w, quality FROM power_reports"
            ).fetchone()
        self.assertEqual(
            ("home-pv", "esp32-c3-001", "2026-08-15T20:01:05.000000Z", 1487.2, "ok"),
            row,
        )

    def test_retry_is_counted_as_a_duplicate(self) -> None:
        first_status, _ = self.request("POST", API_PATH, self.valid_payload(), token=self.token)
        second_status, second_body = self.request("POST", API_PATH, self.valid_payload(), token=self.token)

        self.assertEqual(202, first_status)
        self.assertEqual(202, second_status)
        self.assertEqual(0, second_body["accepted_records"])
        self.assertEqual(1, second_body["duplicate_records"])

    def test_conflicting_sequence_rolls_back_the_batch(self) -> None:
        self.request("POST", API_PATH, self.valid_payload(), token=self.token)
        payload = self.valid_payload()
        payload["records"].insert(
            0,
            {
                "sequence": 1041,
                "measured_at": "2026-08-16T04:01:04+08:00",
                "power_w": 1480,
            },
        )
        payload["records"][1]["power_w"] = 999

        status, body = self.request("POST", API_PATH, payload, token=self.token)

        self.assertEqual(409, status)
        self.assertEqual("sequence_conflict", body["error"]["code"])
        with sqlite3.connect(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM power_reports").fetchone()[0]
        self.assertEqual(1, count)

    def test_rejects_invalid_measurement(self) -> None:
        payload = self.valid_payload()
        payload["records"][0]["power_factor"] = 1.5

        status, body = self.request("POST", API_PATH, payload, token=self.token)

        self.assertEqual(422, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_accepts_raw_dlt645_frame_without_parsing(self) -> None:
        status, body = self.request("POST", RAW_FRAME_PATH, self.valid_raw_payload(), token=self.token)

        self.assertEqual(202, status)
        self.assertEqual("logged", body["status"])
        self.assertEqual(1, body["logged_frames"])
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT device_id, direction, frame_hex FROM raw_frames"
            ).fetchone()
        self.assertEqual(("esp8266-12f-001", "rx", "68010203040568910433333333333316"), row)

    def test_lists_recent_raw_frames_without_authentication(self) -> None:
        self.request("POST", RAW_FRAME_PATH, self.valid_raw_payload(), token=self.token)

        status, body = self.request("GET", f"{RAW_FRAME_PATH}?limit=10&direction=rx")

        self.assertEqual(200, status)
        self.assertEqual(1, body["count"])
        self.assertEqual(1, body["total"])
        self.assertEqual("esp8266-12f-001", body["frames"][0]["device_id"])

    def test_log_only_mode_keeps_raw_frame_ingestion_available(self) -> None:
        server = create_server("127.0.0.1", 0, self.database_path, self.token, log_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            body = json.dumps(self.valid_raw_payload()).encode()
            connection.request(
                "POST",
                RAW_FRAME_PATH,
                body,
                {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(202, response.status)
        self.assertEqual(1, result["logged_frames"])

    def test_rejects_malformed_raw_frame_hex(self) -> None:
        payload = self.valid_raw_payload()
        payload["frames"][0]["frame_hex"] = "680"

        status, body = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)

        self.assertEqual(422, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_rejects_non_string_raw_frame_direction(self) -> None:
        payload = self.valid_raw_payload()
        payload["frames"][0]["direction"] = []

        status, body = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)

        self.assertEqual(422, status)
        self.assertEqual("validation_error", body["error"]["code"])


if __name__ == "__main__":
    unittest.main()
