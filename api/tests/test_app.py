from __future__ import annotations

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app import (  # noqa: E402
    API_PATH,
    HEALTH_PATH,
    RAW_FRAME_PATH,
    RAW_STATUS_PATH,
    RAW_TRENDS_PATH,
    create_server,
    decode_raw_metric,
    decode_raw_metrics,
    modbus_crc16,
)


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

        archive_path = Path(self.temporary_directory.name) / "dlt645-frames.jsonl"
        archived = json.loads(archive_path.read_text(encoding="utf-8").strip())
        self.assertEqual(1, archived["archive_version"])
        self.assertEqual(body["request_id"], archived["request_id"])
        self.assertEqual("68010203040568910433333333333316", archived["frame_hex"])

    def test_raw_frame_is_durably_archived_before_database_failure(self) -> None:
        original_ingest = self.server.raw_store.ingest

        def fail_ingest(*args: object, **kwargs: object) -> int:
            raise sqlite3.OperationalError("database unavailable")

        self.server.raw_store.ingest = fail_ingest
        try:
            status, body = self.request("POST", RAW_FRAME_PATH, self.valid_raw_payload(), token=self.token)
        finally:
            self.server.raw_store.ingest = original_ingest

        self.assertEqual(503, status)
        self.assertEqual("storage_unavailable", body["error"]["code"])
        archive_path = Path(self.temporary_directory.name) / "dlt645-frames.jsonl"
        archived = json.loads(archive_path.read_text(encoding="utf-8").strip())
        self.assertEqual("esp8266-12f-001", archived["device_id"])

    def test_replaying_same_raw_frame_does_not_increment_stats_twice(self) -> None:
        payload = self.valid_raw_payload()
        request_id = "beec51f8-5f39-426a-b9b8-57aa6c16a712"
        received_at = "2026-08-15T20:01:06.000000Z"

        first = self.server.raw_store.ingest(request_id, received_at, payload)
        second = self.server.raw_store.ingest(request_id, received_at, payload)

        self.assertEqual(1, first)
        self.assertEqual(0, second)
        with sqlite3.connect(self.database_path) as connection:
            frame_count = connection.execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0]
            stats_count = connection.execute("SELECT total_frames FROM iot_frame_stats").fetchone()[0]
        self.assertEqual(1, frame_count)
        self.assertEqual(1, stats_count)

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

    def test_decodes_configured_dlt645_metric_from_data_identifier(self) -> None:
        self.assertEqual(
            {"metric": "instantaneous-active-power", "name": "瞬时有功功率", "unit": "W", "value": 5000.0},
            decode_raw_metric(
                {
                    "measurement_point_id": "inverter-ac-output",
                    "frame_hex": "6800000000000068910733333635333338D716",
                }
            ),
        )

    def test_accepts_and_decodes_im1253b_modbus_frame(self) -> None:
        payload = self.valid_raw_payload()
        payload["protocol"] = "MODBUS-RTU"
        payload["measurement_point_id"] = "inverter-ac-output:instantaneous-active-power"
        payload["frames"][0]["frame_hex"] = "0103040023186001D1"

        status, body = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)

        self.assertEqual(202, status)
        self.assertEqual(1, body["logged_frames"])
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT frame_hex FROM raw_frames").fetchone()
        self.assertEqual(("0103040023186001D1",), row)
        status, body = self.request("GET", f"{RAW_FRAME_PATH}?limit=1")
        self.assertEqual(200, status)
        self.assertEqual(230.0, body["frames"][0]["metric_value"])

    def test_decodes_im1253b_block_from_one_modbus_response(self) -> None:
        frame = {
            "protocol": "MODBUS-RTU",
            "measurement_point_id": "inverter-ac-output:im1253b-block",
            "frame_hex": "010320002318600000FDE800E41E700001E240000003D400000000000009C400001388FB24",
        }

        decoded = {item["metric"]: item["value"] for item in decode_raw_metrics(frame)}

        self.assertEqual(230.0, decoded["voltage-a"])
        self.assertEqual(6.5, decoded["current-a"])
        self.assertEqual(1495.0, decoded["instantaneous-active-power"])
        self.assertAlmostEqual(12.3456, decoded["total-active-energy"])
        self.assertEqual(0.98, decoded["power-factor"])
        self.assertEqual(25.0, decoded["temperature"])
        self.assertEqual(50.0, decoded["frequency"])

    def test_block_response_populates_all_dashboard_metrics(self) -> None:
        payload = self.valid_raw_payload()
        payload["protocol"] = "MODBUS-RTU"
        payload["measurement_point_id"] = "inverter-ac-output:im1253b-block"
        payload["frames"][0]["frame_hex"] = (
            "010320002318600000FDE800E41E700001E240000003D400000000000009C400001388FB24"
        )

        status, _ = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)
        self.assertEqual(202, status)
        status, body = self.request("GET", f"{RAW_FRAME_PATH}?limit=1&summary=0")

        self.assertEqual(200, status)
        self.assertEqual("im1253b-block", body["frames"][0]["metric_key"])
        values = {
            metric["key"]: metric["points"][0]["value"]
            for metric in body["metrics"]
            if metric["points"]
        }
        self.assertEqual(230.0, values["voltage-a"])
        self.assertEqual(1495.0, values["instantaneous-active-power"])
        self.assertEqual("block", body["frames"][0]["metric_unit"])

    def test_temperature_metric_filter_returns_only_temperature_frames(self) -> None:
        payload = self.valid_raw_payload()
        payload["protocol"] = "MODBUS-RTU"
        payload["measurement_point_id"] = "inverter-ac-output:im1253b-block"
        payload["frames"][0]["frame_hex"] = (
            "010320002318600000FDE800E41E700001E240000003D400000000000009C400001388FB24"
        )
        status, _ = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)
        self.assertEqual(202, status)

        status, body = self.request(
            "GET",
            f"{RAW_FRAME_PATH}?limit=1000&view=trend&summary=0&metric_key=temperature",
        )

        self.assertEqual(200, status)
        self.assertEqual(1, body["total"])
        temperature = next(metric for metric in body["metrics"] if metric["key"] == "temperature")
        self.assertEqual(25.0, temperature["points"][0]["value"])

    def test_frame_status_uses_precomputed_counter(self) -> None:
        payload = self.valid_raw_payload()
        status, _ = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)
        self.assertEqual(202, status)

        status, body = self.request("GET", RAW_STATUS_PATH)

        self.assertEqual(200, status)
        self.assertEqual(1, body["total_frames"])
        self.assertEqual(1, body["device_count"])
        self.assertTrue(body["online"])

    def test_recent_frames_can_skip_full_total_count(self) -> None:
        payload = self.valid_raw_payload()
        status, _ = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)
        self.assertEqual(202, status)

        status, body = self.request("GET", f"{RAW_FRAME_PATH}?limit=1&summary=0&total=0")

        self.assertEqual(200, status)
        self.assertIsNone(body["total"])

    def test_metric_trends_endpoint_returns_measurement_points(self) -> None:
        payload = self.valid_raw_payload()
        payload["protocol"] = "MODBUS-RTU"
        payload["measurement_point_id"] = "inverter-ac-output:im1253b-block"
        payload["frames"][0]["frame_hex"] = (
            "010320002318600000FDE800E41E700001E240000003D400000000000009C400001388FB24"
        )
        status, _ = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)
        self.assertEqual(202, status)

        status, body = self.request(
            "GET",
            f"{RAW_TRENDS_PATH}?limit=1000&metric_key=temperature&metric_key=instantaneous-active-power",
        )

        self.assertEqual(200, status)
        metrics = {metric["key"]: metric["points"] for metric in body["metrics"]}
        self.assertEqual(25.0, metrics["temperature"][0]["value"])
        self.assertEqual(1495.0, metrics["instantaneous-active-power"][0]["value"])

    def test_local_energy_summary_uses_daily_meter_counter_delta(self) -> None:
        def block(total_raw: int) -> str:
            encoded = bytearray.fromhex(
                "010320002318600000FDE800E41E700001E240000003D400000000000009C400001388FB24"
            )
            encoded[15:19] = total_raw.to_bytes(4, "big")
            crc = modbus_crc16(encoded[:-2])
            encoded[-2] = crc & 0xFF
            encoded[-1] = crc >> 8
            return encoded.hex().upper()

        frames = [
            {
                "site_id": "home-pv",
                "device_id": "im1253b-001",
                "protocol": "MODBUS-RTU",
                "measurement_point_id": "inverter-ac-output:im1253b-block",
                "captured_at": "2026-09-26T15:59:00Z",
                "frame_hex": block(141510),
            },
            {
                "site_id": "home-pv",
                "device_id": "im1253b-001",
                "protocol": "MODBUS-RTU",
                "measurement_point_id": "inverter-ac-output:im1253b-block",
                "captured_at": "2026-09-27T08:00:00Z",
                "frame_hex": block(141730),
            },
        ]

        result = self.server.raw_store._energy_summary_from_frames(
            frames,
            1,
            start_at="2026-09-26T16:00:00Z",
            end_at="2026-09-27T16:00:00Z",
        )

        self.assertEqual(14.173, result["total_energy_kwh"])
        self.assertAlmostEqual(0.022, result["daily_energy_kwh"])
        self.assertEqual("ok", result["daily_energy_quality"])

    def test_rejects_bad_im1253b_modbus_crc(self) -> None:
        payload = self.valid_raw_payload()
        payload["protocol"] = "MODBUS-RTU"
        payload["measurement_point_id"] = "inverter-ac-output:voltage-a"
        payload["frames"][0]["frame_hex"] = "0103040023186001D0"

        status, body = self.request("POST", RAW_FRAME_PATH, payload, token=self.token)

        self.assertEqual(202, status)
        status, body = self.request("GET", f"{RAW_FRAME_PATH}?limit=1")
        self.assertEqual(200, status)
        self.assertIsNone(body["frames"][0]["metric_value"])

    def test_trend_sampling_covers_the_full_requested_time_range(self) -> None:
        start = datetime(2026, 9, 26, tzinfo=timezone.utc)
        frame_count = 2000
        rows = []
        for index in range(frame_count):
            captured_at = start + timedelta(seconds=index * 86400 / (frame_count - 1))
            rows.append(
                (
                    f"request-{index}",
                    "home-pv",
                    "esp8266-12f-001",
                    "inverter-ac-output:instantaneous-active-power",
                    "DL/T 645-2007",
                    index,
                    captured_at.isoformat().replace("+00:00", "Z"),
                    "rx",
                    "6800000000000068910733333635333338D716",
                    captured_at.isoformat().replace("+00:00", "Z"),
                )
            )
        with sqlite3.connect(self.database_path) as connection:
            connection.executemany(
                """
                INSERT INTO raw_frames (
                    request_id, site_id, device_id, measurement_point_id,
                    protocol, sequence, captured_at, direction, frame_hex, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        end = start + timedelta(days=1, seconds=1)
        status, body = self.request(
            "GET",
            f"{RAW_FRAME_PATH}?limit=1000&view=trend&summary=0"
            f"&start_at={start.isoformat().replace('+00:00', 'Z')}"
            f"&end_at={end.isoformat().replace('+00:00', 'Z')}",
        )

        self.assertEqual(200, status)
        points = next(
            metric["points"]
            for metric in body["metrics"]
            if metric["key"] == "instantaneous-active-power"
        )
        first = datetime.fromisoformat(points[0]["captured_at"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(points[-1]["captured_at"].replace("Z", "+00:00"))
        self.assertEqual(1000, len(points))
        self.assertGreaterEqual((last - first).total_seconds(), 23.9 * 3600)


if __name__ == "__main__":
    unittest.main()
