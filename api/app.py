#!/usr/bin/env python3
"""HTTP API for receiving power telemetry from collection devices."""

import argparse
import hmac
import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_PATH = "/api/v1/iotreport"
RAW_FRAME_PATH = "/api/v1/dlt645/frame"
HEALTH_PATH = "/api/v1/health"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RECORDS_PER_BATCH = 300
MAX_RAW_FRAMES_PER_BATCH = 100
MAX_RAW_FRAME_HEX_CHARS = 4096
MAX_RAW_FRAME_QUERY_LIMIT = 100
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
HEX_FRAME_PATTERN = re.compile(r"^[0-9A-Fa-f]+$")
QUALITY_VALUES = {"ok", "estimated", "invalid", "offline_gap"}
RAW_FRAME_DIRECTIONS = {"tx", "rx"}
RAW_FRAME_PROTOCOL = "DL/T 645-2007"
RAW_METRIC_CONFIG = {
    "voltage-a": {"name": "A 相电压", "unit": "V", "scale": 0.1},
    "current-a": {"name": "A 相电流", "unit": "A", "scale": 0.001},
    "instantaneous-active-power": {"name": "瞬时有功功率", "unit": "W", "scale": 0.1},
}
RAW_METRIC_DI = {
    (0x00, 0x01, 0x01, 0x02): "voltage-a",
    (0x00, 0x01, 0x02, 0x02): "current-a",
    (0x00, 0x00, 0x03, 0x02): "instantaneous-active-power",
}


try:
    from http.server import ThreadingHTTPServer
except ImportError:  # Python 3.6 compatibility on the deployment host.
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True


class ApiError(Exception):
    """An error that can be returned safely to an API client."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class PowerReportStore:
    """SQLite-backed telemetry store with record-level idempotency."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS power_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    measurement_point_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    measured_at TEXT NOT NULL,
                    power_w REAL NOT NULL,
                    energy_wh REAL,
                    voltage_v REAL,
                    current_a REAL,
                    power_factor REAL,
                    quality TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE (site_id, device_id, measurement_point_id, sequence)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_power_reports_point_time
                ON power_reports (site_id, measurement_point_id, measured_at)
                """
            )

    def ingest(
        self,
        request_id: str,
        site_id: str,
        device_id: str,
        measurement_point_id: str,
        records: List[Dict[str, Any]],
        received_at: str,
    ) -> Tuple[int, int]:
        accepted = 0
        duplicates = 0
        columns = (
            "measured_at",
            "power_w",
            "energy_wh",
            "voltage_v",
            "current_a",
            "power_factor",
            "quality",
        )

        with self._connect() as connection:
            for record in records:
                cursor = connection.execute(
                    """
                    INSERT INTO power_reports (
                        request_id, site_id, device_id, measurement_point_id,
                        sequence, measured_at, power_w, energy_wh, voltage_v,
                        current_a, power_factor, quality, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (site_id, device_id, measurement_point_id, sequence) DO NOTHING
                    """,
                    (
                        request_id,
                        site_id,
                        device_id,
                        measurement_point_id,
                        record["sequence"],
                        record["measured_at"],
                        record["power_w"],
                        record.get("energy_wh"),
                        record.get("voltage_v"),
                        record.get("current_a"),
                        record.get("power_factor"),
                        record["quality"],
                        received_at,
                    ),
                )
                if cursor.rowcount == 1:
                    accepted += 1
                    continue

                # The unique constraint may have been hit by a concurrent retry.
                existing = connection.execute(
                    """
                    SELECT measured_at, power_w, energy_wh, voltage_v, current_a,
                           power_factor, quality
                    FROM power_reports
                    WHERE site_id = ? AND device_id = ? AND measurement_point_id = ? AND sequence = ?
                    """,
                    (site_id, device_id, measurement_point_id, record["sequence"]),
                ).fetchone()
                if existing is not None and all(existing[column] == record.get(column) for column in columns):
                    duplicates += 1
                    continue
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "sequence_conflict",
                    f"sequence {record['sequence']} was already stored with different data",
                )

        return accepted, duplicates


class RawFrameStore:
    """Archives raw frames asynchronously and stores queryable copies in SQLite."""

    def __init__(self, database_path: str, archive_path: Optional[str], *, enable_database: bool = True,
                 monitor_database_url: Optional[str] = None, monitor_api_url: Optional[str] = None,
                 monitor_api_key: Optional[str] = None) -> None:
        self.database_path = database_path
        self.archive_path = archive_path or str(Path(database_path).with_name("dlt645-frames.jsonl"))
        self.enable_database = enable_database
        self.monitor_database_url = monitor_database_url
        self.monitor_api_url = monitor_api_url
        self.monitor_api_key = monitor_api_key
        self._archive_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._archive_thread = threading.Thread(target=self._archive_worker, name="dlt645-archive", daemon=True)
        if enable_database and database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(self.archive_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if enable_database:
            self._initialize()
        self._archive_thread.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        if self.monitor_database_url:
            with self._postgres_connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS iot_dlt645_frames (
                            id BIGSERIAL PRIMARY KEY,
                            request_id UUID NOT NULL,
                            site_id TEXT NOT NULL,
                            device_id TEXT NOT NULL,
                            measurement_point_id TEXT NOT NULL,
                            protocol TEXT NOT NULL,
                            sequence BIGINT NOT NULL,
                            captured_at TIMESTAMPTZ NOT NULL,
                            direction TEXT NOT NULL,
                            frame_hex TEXT NOT NULL,
                            received_at TIMESTAMPTZ NOT NULL,
                            metric_key TEXT,
                            metric_value DOUBLE PRECISION,
                            metric_unit TEXT
                        )
                        """
                    )
                    cursor.execute(
                        """CREATE INDEX IF NOT EXISTS iot_dlt645_frames_received_idx
                           ON iot_dlt645_frames (received_at DESC)"""
                    )
                    cursor.execute(
                        """CREATE INDEX IF NOT EXISTS iot_dlt645_frames_metric_time_idx
                           ON iot_dlt645_frames (metric_key, captured_at DESC)"""
                    )
                connection.commit()
            return
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    measurement_point_id TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    frame_hex TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_raw_frames_received
                ON raw_frames (received_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_raw_frames_device_time
                ON raw_frames (site_id, device_id, captured_at DESC)
                """
            )

    def _postgres_connect(self) -> Any:
        try:
            import psycopg2
        except ImportError as error:
            raise RuntimeError("psycopg2 is required for Monitor Center storage") from error
        return psycopg2.connect(self.monitor_database_url, connect_timeout=5, options="-c timezone=UTC")

    def enqueue_archive(self, request_id: str, received_at: str, payload: Dict[str, Any]) -> int:
        """Queue JSONL writes before the synchronous database transaction starts."""
        for frame in payload["frames"]:
            line = json.dumps(
                {
                    "request_id": request_id,
                    "received_at": received_at,
                    "site_id": payload["site_id"],
                    "device_id": payload["device_id"],
                    "measurement_point_id": payload["measurement_point_id"],
                    "protocol": payload["protocol"],
                    **frame,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._archive_queue.put(line)
        return len(payload["frames"])

    def _archive_worker(self) -> None:
        try:
            with open(self.archive_path, "a", encoding="utf-8") as archive:
                while True:
                    line = self._archive_queue.get()
                    try:
                        if line is None:
                            return
                        archive.write(line + "\n")
                        archive.flush()
                    finally:
                        self._archive_queue.task_done()
        except OSError:
            # SQLite remains the authoritative query store; log the archive failure.
            print(json.dumps({"event": "dlt645_archive_error", "archive_path": self.archive_path}), file=sys.stderr, flush=True)

    def ingest(self, request_id: str, received_at: str, payload: Dict[str, Any]) -> int:
        if not self.enable_database:
            return len(payload["frames"])
        if self.monitor_database_url:
            with self._postgres_connect() as connection:
                with connection.cursor() as cursor:
                    for frame in payload["frames"]:
                        decoded = decode_raw_metric({"measurement_point_id": payload["measurement_point_id"], **frame})
                        cursor.execute(
                            """
                            INSERT INTO iot_dlt645_frames (
                                request_id, site_id, device_id, measurement_point_id,
                                protocol, sequence, captured_at, direction, frame_hex,
                                received_at, metric_key, metric_value, metric_unit
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                request_id, payload["site_id"], payload["device_id"], payload["measurement_point_id"],
                                payload["protocol"], frame["sequence"], frame["captured_at"], frame["direction"],
                                frame["frame_hex"], received_at, decoded["metric"] if decoded else None,
                                decoded["value"] if decoded else None, decoded["unit"] if decoded else None,
                            ),
                        )
                connection.commit()
            return len(payload["frames"])
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO raw_frames (
                    request_id, site_id, device_id, measurement_point_id,
                    protocol, sequence, captured_at, direction, frame_hex, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        request_id,
                        payload["site_id"],
                        payload["device_id"],
                        payload["measurement_point_id"],
                        payload["protocol"],
                        frame["sequence"],
                        frame["captured_at"],
                        frame["direction"],
                        frame["frame_hex"],
                        received_at,
                    )
                    for frame in payload["frames"]
                ],
            )
        return len(payload["frames"])

    def list_recent(self, *, limit: int, site_id: Optional[str] = None, device_id: Optional[str] = None,
                    direction: Optional[str] = None) -> Dict[str, Any]:
        if self.monitor_api_url:
            query = [("limit", str(limit))]
            if site_id:
                query.append(("site_id", site_id))
            if device_id:
                query.append(("device_id", device_id))
            if direction:
                query.append(("direction", direction))
            from urllib.parse import urlencode
            request = Request(self.monitor_api_url + "?" + urlencode(query), headers={"X-Monitor-Center-Key": self.monitor_api_key or ""})
            try:
                with urlopen(request, timeout=8) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Monitor Center response is invalid")
                return payload
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("Monitor Center query failed") from error
        conditions: List[str] = []
        parameters: List[Any] = []
        for column, value in (("site_id", site_id), ("device_id", device_id), ("direction", direction)):
            if value:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        if self.monitor_database_url:
            pg_where = where.replace("?", "%s")
            with self._postgres_connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""SELECT id, request_id::text, site_id, device_id, measurement_point_id,
                                   protocol, sequence, captured_at, direction, frame_hex, received_at,
                                   metric_key, metric_value, metric_unit
                            FROM iot_dlt645_frames {pg_where} ORDER BY id DESC LIMIT %s""",
                        [*parameters, limit],
                    )
                    columns = [description[0] for description in cursor.description]
                    frames = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    cursor.execute(f"SELECT COUNT(*) FROM iot_dlt645_frames {pg_where}", parameters)
                    total = cursor.fetchone()[0]
            for frame in frames:
                for field in ("captured_at", "received_at"):
                    frame[field] = frame[field].isoformat().replace("+00:00", "Z")
        elif self.enable_database:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""SELECT id, request_id, site_id, device_id, measurement_point_id,
                               protocol, sequence, captured_at, direction, frame_hex, received_at
                        FROM raw_frames {where} ORDER BY id DESC LIMIT ?""",
                    [*parameters, limit],
                ).fetchall()
                total = connection.execute(f"SELECT COUNT(*) FROM raw_frames {where}", parameters).fetchone()[0]
            frames = [dict(row) for row in rows]
        else:
            # Log-only deployments still expose the local JSONL archive to the page.
            frames = []
            try:
                with open(self.archive_path, "r", encoding="utf-8") as archive:
                    for line in archive:
                        try:
                            frame = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(frame, dict):
                            continue
                        if site_id and frame.get("site_id") != site_id:
                            continue
                        if device_id and frame.get("device_id") != device_id:
                            continue
                        if direction and frame.get("direction") != direction:
                            continue
                        frames.append(frame)
            except OSError:
                frames = []
            frames = list(reversed(frames[-limit:]))
            total = len(frames)
        metrics: Dict[str, List[Dict[str, Any]]] = {name: [] for name in RAW_METRIC_CONFIG}
        collection_buckets: Dict[str, Dict[str, Any]] = {}
        for frame in reversed(frames):
            decoded = decode_raw_metric(frame)
            if decoded is not None:
                frame["metric_key"] = decoded["metric"]
                frame["metric_value"] = decoded["value"]
                frame["metric_unit"] = decoded["unit"]
                metrics[decoded["metric"]].append(
                    {"captured_at": frame["captured_at"], "sequence": frame["sequence"], "value": decoded["value"]}
                )
            timestamp = str(frame.get("captured_at", ""))
            bucket = timestamp[:19] + "Z" if len(timestamp) >= 19 else timestamp
            item = collection_buckets.setdefault(bucket, {"captured_at": bucket, "success": 0, "failure": 0})
            item["success" if decoded is not None else "failure"] += 1
        return {
            "frames": frames,
            "count": len(frames),
            "total": total,
            "metrics": [
                {"key": key, "name": config["name"], "unit": config["unit"], "points": metrics[key]}
                for key, config in RAW_METRIC_CONFIG.items()
            ],
            "collection": {"interval": "1s", "points": list(collection_buckets.values())},
        }

    def close(self) -> None:
        if self._archive_thread.is_alive():
            self._archive_queue.put(None)
            self._archive_thread.join(timeout=2)


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be 1-64 characters using letters, numbers, '.', '_', ':' or '-'",
        )
    return value


def _number(
    value: Any,
    field_name: str,
    minimum: float,
    maximum: float,
    *,
    required: bool = False,
) -> Optional[float]:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be a number",
        )
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be between {minimum:g} and {maximum:g}",
        )
    return normalized


def _timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be an RFC 3339 timestamp with a timezone",
        )
    if value.endswith("Z"):
        normalized = value[:-1] + "+0000"
    elif len(value) >= 6 and value[-6] in "+-" and value[-3] == ":":
        normalized = value[:-3] + value[-2:]
    else:
        normalized = value
    try:
        format_string = "%Y-%m-%dT%H:%M:%S.%f%z" if "." in normalized else "%Y-%m-%dT%H:%M:%S%z"
        parsed = datetime.strptime(normalized, format_string)
    except ValueError as error:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be an RFC 3339 timestamp with a timezone",
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must include a timezone",
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "request body must be a JSON object",
        )

    site_id = _identifier(payload.get("site_id"), "site_id")
    device_id = _identifier(payload.get("device_id"), "device_id")
    measurement_point_id = _identifier(payload.get("measurement_point_id"), "measurement_point_id")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or not 1 <= len(raw_records) <= MAX_RECORDS_PER_BATCH:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"records must contain between 1 and {MAX_RECORDS_PER_BATCH} items",
        )

    records: List[Dict[str, Any]] = []
    sequences: Set[int] = set()
    for index, raw_record in enumerate(raw_records):
        prefix = f"records[{index}]"
        if not isinstance(raw_record, dict):
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix} must be a JSON object",
            )

        sequence = raw_record.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 2**63 - 1:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix}.sequence must be an integer between 0 and {2**63 - 1}",
            )
        if sequence in sequences:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix}.sequence is duplicated within this request",
            )
        sequences.add(sequence)

        quality = raw_record.get("quality", "ok")
        if not isinstance(quality, str) or quality not in QUALITY_VALUES:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix}.quality must be one of {', '.join(sorted(QUALITY_VALUES))}",
            )

        records.append(
            {
                "sequence": sequence,
                "measured_at": _timestamp(raw_record.get("measured_at"), f"{prefix}.measured_at"),
                "power_w": _number(
                    raw_record.get("power_w"),
                    f"{prefix}.power_w",
                    -100_000_000,
                    100_000_000,
                    required=True,
                ),
                "energy_wh": _number(
                    raw_record.get("energy_wh"), f"{prefix}.energy_wh", 0, 1_000_000_000_000_000
                ),
                "voltage_v": _number(raw_record.get("voltage_v"), f"{prefix}.voltage_v", 0, 100_000),
                "current_a": _number(raw_record.get("current_a"), f"{prefix}.current_a", 0, 1_000_000),
                "power_factor": _number(
                    raw_record.get("power_factor"), f"{prefix}.power_factor", -1, 1
                ),
                "quality": quality,
            }
        )

    return {
        "site_id": site_id,
        "device_id": device_id,
        "measurement_point_id": measurement_point_id,
        "records": records,
    }


def _raw_frame_hex(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be an even-length hexadecimal string",
        )
    compact = value.replace(" ", "").replace(":", "")
    if (
        not compact
        or len(compact) > MAX_RAW_FRAME_HEX_CHARS
        or len(compact) % 2
        or HEX_FRAME_PATTERN.fullmatch(compact) is None
    ):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"{field_name} must be an even-length hexadecimal string of at most {MAX_RAW_FRAME_HEX_CHARS} characters",
        )
    # This validates transport encoding only. The DL/T 645-2007 payload remains opaque.
    return value


def decode_raw_metric(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Decode the three configured DL/T 645 data identifiers for charting."""
    point = frame.get("measurement_point_id", "")
    suffix = point.rsplit(":", 1)[-1]
    config = RAW_METRIC_CONFIG.get(suffix)
    try:
        encoded = bytes.fromhex(str(frame["frame_hex"]).replace(" ", "").replace(":", ""))
        data_length = encoded[9]
        payload = encoded[10 : 10 + data_length]
        if len(payload) < 5:
            return None
        decoded = bytes((value - 0x33) & 0xFF for value in payload)
        if config is None:
            suffix = RAW_METRIC_DI.get(tuple(decoded[:4]), "")
            config = RAW_METRIC_CONFIG.get(suffix)
        if config is None:
            return None
        # DI is transmitted in line order; the remaining BCD bytes are little-endian.
        data = decoded[4:]
        digits = ""
        for value in reversed(data):
            high, low = value >> 4, value & 0x0F
            if high > 9 or low > 9:
                return None
            digits += f"{high}{low}"
        if not digits:
            return None
        return {"metric": suffix, "name": config["name"], "unit": config["unit"], "value": int(digits) * config["scale"]}
    except (KeyError, ValueError, IndexError):
        return None


def validate_raw_frame_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            "request body must be a JSON object",
        )

    site_id = _identifier(payload.get("site_id"), "site_id")
    device_id = _identifier(payload.get("device_id"), "device_id")
    measurement_point_id = _identifier(payload.get("measurement_point_id"), "measurement_point_id")
    protocol = payload.get("protocol", RAW_FRAME_PROTOCOL)
    if protocol != RAW_FRAME_PROTOCOL:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"protocol must be {RAW_FRAME_PROTOCOL}",
        )

    raw_frames = payload.get("frames")
    if not isinstance(raw_frames, list) or not 1 <= len(raw_frames) <= MAX_RAW_FRAMES_PER_BATCH:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"frames must contain between 1 and {MAX_RAW_FRAMES_PER_BATCH} items",
        )

    frames: List[Dict[str, Any]] = []
    sequences: Set[int] = set()
    for index, raw_frame in enumerate(raw_frames):
        prefix = f"frames[{index}]"
        if not isinstance(raw_frame, dict):
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix} must be a JSON object",
            )

        sequence = raw_frame.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 2**63 - 1:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix}.sequence must be an integer between 0 and {2**63 - 1}",
            )
        if sequence in sequences:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix}.sequence is duplicated within this request",
            )
        sequences.add(sequence)

        direction = raw_frame.get("direction", "rx")
        if not isinstance(direction, str) or direction not in RAW_FRAME_DIRECTIONS:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_error",
                f"{prefix}.direction must be one of tx, rx",
            )

        frames.append(
            {
                "sequence": sequence,
                "captured_at": _timestamp(raw_frame.get("captured_at"), f"{prefix}.captured_at"),
                "direction": direction,
                "frame_hex": _raw_frame_hex(raw_frame.get("frame_hex"), f"{prefix}.frame_hex"),
            }
        )

    return {
        "site_id": site_id,
        "device_id": device_id,
        "measurement_point_id": measurement_point_id,
        "protocol": protocol,
        "frames": frames,
    }


class PowerMonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        store: Optional[PowerReportStore],
        raw_store: RawFrameStore,
        token: str,
        raw_forward_url: Optional[str] = None,
    ) -> None:
        super().__init__(server_address, PowerMonitorHandler)
        self.store = store
        self.raw_store = raw_store
        self.token = token
        self.raw_forward_url = raw_forward_url

    def log_raw_frames(self, request_id: str, received_at: str, payload: Dict[str, Any]) -> None:
        for frame in payload["frames"]:
            print(
                json.dumps(
                    {
                        "event": "dlt645_raw_frame",
                        "request_id": request_id,
                        "received_at": received_at,
                        "site_id": payload["site_id"],
                        "device_id": payload["device_id"],
                        "measurement_point_id": payload["measurement_point_id"],
                        "protocol": payload["protocol"],
                        **frame,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )

    def server_close(self) -> None:
        self.raw_store.close()
        super().server_close()

    def forward_raw_frames(self, payload: Dict[str, Any]) -> None:
        if not self.raw_forward_url:
            return
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.raw_forward_url,
            data=body,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                if response.status != HTTPStatus.ACCEPTED:
                    raise RuntimeError(f"Monitor Center returned HTTP {response.status}")
        except (HTTPError, URLError, OSError) as error:
            raise RuntimeError("Monitor Center synchronization failed") from error


class PowerMonitorHandler(BaseHTTPRequestHandler):
    server: PowerMonitorServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            self._json_response(HTTPStatus.OK, {"status": "ok"})
            return
        if path == RAW_FRAME_PATH:
            self._handle_raw_frame_query()
            return
        if path == API_PATH:
            self._error_response(
                ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "use POST for this endpoint"),
                extra_headers={"Allow": "POST"},
            )
            return
        self._error_response(ApiError(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found"))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == RAW_FRAME_PATH:
            self._handle_raw_frame()
            return
        if path != API_PATH:
            self._error_response(ApiError(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found"))
            return

        try:
            self._authenticate()
            if self.server.store is None:
                raise ApiError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "parsed_ingest_disabled",
                    "parsed telemetry ingestion is disabled in log-only mode",
                )
            payload = validate_payload(self._read_json_body())
            request_id = str(uuid.uuid4())
            received_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            accepted, duplicates = self.server.store.ingest(
                request_id=request_id,
                site_id=payload["site_id"],
                device_id=payload["device_id"],
                measurement_point_id=payload["measurement_point_id"],
                records=payload["records"],
                received_at=received_at,
            )
        except ApiError as error:
            self._error_response(error)
            return
        except sqlite3.Error:
            self.log_error("database operation failed")
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "telemetry storage is unavailable")
            )
            return

        self._json_response(
            HTTPStatus.ACCEPTED,
            {
                "request_id": request_id,
                "status": "accepted",
                "accepted_records": accepted,
                "duplicate_records": duplicates,
                "received_at": received_at,
            },
        )

    def _handle_raw_frame(self) -> None:
        try:
            self._authenticate()
            payload = validate_raw_frame_payload(self._read_json_body())
            request_id = str(uuid.uuid4())
            received_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            self.server.raw_store.enqueue_archive(request_id, received_at, payload)
            self.server.raw_store.ingest(request_id, received_at, payload)
            self.server.forward_raw_frames(payload)
            self.server.log_raw_frames(request_id, received_at, payload)
        except ApiError as error:
            self._error_response(error)
            return
        except sqlite3.Error:
            self.log_error("raw frame database operation failed")
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "raw frame storage is unavailable")
            )
            return
        except RuntimeError:
            self.log_error("raw frame synchronization failed")
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "sync_unavailable", "Monitor Center synchronization is unavailable")
            )
            return

        self._json_response(
            HTTPStatus.ACCEPTED,
            {
                "request_id": request_id,
                "status": "logged",
                "logged_frames": len(payload["frames"]),
                "received_at": received_at,
            },
        )

    def _handle_raw_frame_query(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            raw_limit = query.get("limit", ["50"])[0]
            limit = int(raw_limit)
            if not 1 <= limit <= MAX_RAW_FRAME_QUERY_LIMIT:
                raise ValueError
            direction = query.get("direction", [None])[0]
            if direction is not None and direction not in RAW_FRAME_DIRECTIONS:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "direction must be one of tx, rx")
            result = self.server.raw_store.list_recent(
                limit=limit,
                site_id=query.get("site_id", [None])[0],
                device_id=query.get("device_id", [None])[0],
                direction=direction,
            )
        except ValueError:
            self._error_response(
                ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", f"limit must be between 1 and {MAX_RAW_FRAME_QUERY_LIMIT}")
            )
            return
        except ApiError as error:
            self._error_response(error)
            return
        except sqlite3.Error:
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "raw frame storage is unavailable")
            )
            return
        except RuntimeError:
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "Monitor Center storage is unavailable")
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _authenticate(self) -> None:
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if not hmac.compare_digest(authorization, expected):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized", "a valid bearer token is required")

    def _read_json_body(self) -> Any:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type", "Content-Type must be application/json")

        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else -1
        except ValueError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length is invalid") from error
        if content_length < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "empty_body", "request body is required")
        if content_length > MAX_REQUEST_BYTES:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                f"request body must not exceed {MAX_REQUEST_BYTES} bytes",
            )

        try:
            return json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body must contain valid UTF-8 JSON") from error

    def _error_response(self, error: ApiError, extra_headers: Optional[Dict[str, str]] = None) -> None:
        # Error responses close the connection so an unread request body cannot
        # be interpreted as a second HTTP request by the keep-alive parser.
        self.close_connection = True
        headers = extra_headers or {}
        if error.status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = 'Bearer realm="power-monitor"'
        headers["Connection"] = "close"
        self._json_response(error.status, {"error": {"code": error.code, "message": error.message}}, headers)

    def _json_response(
        self,
        status: int,
        payload: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    database_path: str,
    token: str,
    *,
    log_only: bool = False,
) -> PowerMonitorServer:
    if not token:
        raise ValueError("token must not be empty")
    store = None if log_only else PowerReportStore(database_path)
    archive_path = os.environ.get("POWER_MONITOR_RAW_ARCHIVE")
    monitor_database_url = os.environ.get("POWER_MONITOR_CENTER_DATABASE_URL")
    monitor_api_url = os.environ.get("POWER_MONITOR_CENTER_API_URL")
    monitor_api_key = os.environ.get("POWER_MONITOR_CENTER_API_KEY")
    raw_store = RawFrameStore(
        database_path,
        archive_path,
        enable_database=not log_only or bool(monitor_database_url),
        monitor_database_url=monitor_database_url,
        monitor_api_url=monitor_api_url,
        monitor_api_key=monitor_api_key,
    )
    return PowerMonitorServer(
        (host, port), store, raw_store, token, os.environ.get("POWER_MONITOR_RAW_FORWARD_URL")
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive and store power telemetry reports")
    parser.add_argument("--host", default=os.environ.get("POWER_MONITOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("POWER_MONITOR_PORT", "8090")))
    parser.add_argument(
        "--database",
        default=os.environ.get("POWER_MONITOR_DATABASE", str(Path(__file__).parent / "data" / "power-monitor.db")),
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("POWER_MONITOR_TOKEN", "")
    if not token:
        print("POWER_MONITOR_TOKEN must be set", file=sys.stderr)
        return 2

    log_only = os.environ.get("POWER_MONITOR_LOG_ONLY", "").lower() in {"1", "true", "yes", "on"}
    server = create_server(args.host, args.port, args.database, token, log_only=log_only)
    mode = "raw-frame log-only" if log_only else "parsed telemetry"
    print(f"Power monitor API ({mode}) listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
