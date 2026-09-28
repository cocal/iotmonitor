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
from datetime import datetime, timedelta, timezone
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
RAW_SUMMARY_PATH = "/api/v1/dlt645/summary"
RAW_STATUS_PATH = "/api/v1/dlt645/status"
RAW_TRENDS_PATH = "/api/v1/dlt645/trends"
HEALTH_PATH = "/api/v1/health"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RECORDS_PER_BATCH = 300
MAX_RAW_FRAMES_PER_BATCH = 100
MAX_RAW_FRAME_HEX_CHARS = 4096
MAX_RAW_FRAME_QUERY_LIMIT = 100
MAX_RAW_FRAME_TREND_LIMIT = 1000
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
HEX_FRAME_PATTERN = re.compile(r"^[0-9A-Fa-f]+$")
QUALITY_VALUES = {"ok", "estimated", "invalid", "offline_gap"}
RAW_FRAME_DIRECTIONS = {"tx", "rx"}
RAW_FRAME_PROTOCOL = "DL/T 645-2007"
RAW_FRAME_PROTOCOLS = {RAW_FRAME_PROTOCOL, "MODBUS-RTU"}
RAW_METRIC_CONFIG = {
    "voltage-a": {"name": "A 相电压", "unit": "V", "scale": 0.1},
    "current-a": {"name": "A 相电流", "unit": "A", "scale": 0.001},
    "instantaneous-active-power": {"name": "瞬时有功功率", "unit": "W", "scale": 0.1},
    "total-active-energy": {"name": "累计用电量", "unit": "kWh", "scale": 0.01},
    "power-factor": {"name": "功率因数", "unit": "", "scale": 0.001},
    "temperature": {"name": "温度", "unit": "°C", "scale": 0.01},
    "frequency": {"name": "频率", "unit": "Hz", "scale": 0.01},
}
RAW_METRIC_DI = {
    (0x00, 0x01, 0x01, 0x02): "voltage-a",
    (0x00, 0x01, 0x02, 0x02): "current-a",
    (0x00, 0x00, 0x03, 0x02): "instantaneous-active-power",
    (0x00, 0x01, 0x00, 0x00): "total-active-energy",
    (0x00, 0x00, 0x01, 0x00): "total-active-energy",
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
                            source_id TEXT NOT NULL DEFAULT 'iotmonitor-192',
                            event_id TEXT NOT NULL,
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
                            metric_unit TEXT,
                            raw_archive_path TEXT NOT NULL DEFAULT ''
                        )
                        """
                    )
                    cursor.execute("ALTER TABLE iot_dlt645_frames ADD COLUMN IF NOT EXISTS source_id TEXT NOT NULL DEFAULT 'iotmonitor-192'")
                    cursor.execute("ALTER TABLE iot_dlt645_frames ADD COLUMN IF NOT EXISTS event_id TEXT")
                    cursor.execute("ALTER TABLE iot_dlt645_frames ADD COLUMN IF NOT EXISTS raw_archive_path TEXT NOT NULL DEFAULT ''")
                    cursor.execute(
                        """CREATE INDEX IF NOT EXISTS iot_dlt645_frames_received_idx
                           ON iot_dlt645_frames (received_at DESC)"""
                    )
                    cursor.execute(
                        """CREATE INDEX IF NOT EXISTS iot_dlt645_frames_metric_time_idx
                           ON iot_dlt645_frames (metric_key, captured_at DESC)"""
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS iot_frame_stats (
                            site_id TEXT NOT NULL,
                            device_id TEXT NOT NULL,
                            total_frames BIGINT NOT NULL DEFAULT 0,
                            last_received_at TIMESTAMPTZ,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            PRIMARY KEY (site_id, device_id)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS iot_meter_frame_metrics (
                            frame_id BIGINT NOT NULL,
                            metric_key TEXT NOT NULL,
                            metric_value DOUBLE PRECISION NOT NULL,
                            metric_unit TEXT NOT NULL,
                            PRIMARY KEY (frame_id, metric_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS iot_meter_frame_metrics_key_idx
                        ON iot_meter_frame_metrics (metric_key, frame_id)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS iot_daily_energy (
                            site_id TEXT NOT NULL,
                            device_id TEXT NOT NULL,
                            local_date DATE NOT NULL,
                            baseline_kwh DOUBLE PRECISION,
                            baseline_at TIMESTAMPTZ,
                            latest_total_kwh DOUBLE PRECISION,
                            latest_total_at TIMESTAMPTZ,
                            energy_kwh DOUBLE PRECISION,
                            estimated_energy_kwh DOUBLE PRECISION,
                            last_power_w DOUBLE PRECISION,
                            last_power_at TIMESTAMPTZ,
                            calculation_method TEXT NOT NULL DEFAULT 'meter-counter',
                            quality TEXT NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            PRIMARY KEY (site_id, device_id, local_date)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS iot_daily_energy_date_idx
                        ON iot_daily_energy (local_date DESC, site_id, device_id)
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO iot_meter_frame_metrics
                            (frame_id, metric_key, metric_value, metric_unit)
                        SELECT id, metric_key, metric_value, COALESCE(metric_unit, '')
                        FROM iot_dlt645_frames
                        WHERE metric_key IS NOT NULL
                          AND metric_key NOT IN ('raw-frame', 'im1253b-block')
                          AND metric_value IS NOT NULL
                        ON CONFLICT (frame_id, metric_key) DO NOTHING
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO iot_frame_stats
                            (site_id, device_id, total_frames, last_received_at, updated_at)
                        SELECT site_id, device_id, COUNT(*), MAX(received_at), now()
                        FROM iot_dlt645_frames
                        GROUP BY site_id, device_id
                        ON CONFLICT (site_id, device_id) DO UPDATE SET
                            total_frames = EXCLUDED.total_frames,
                            last_received_at = EXCLUDED.last_received_at,
                            updated_at = now()
                        """
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS iot_frame_stats (
                    site_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    total_frames INTEGER NOT NULL DEFAULT 0,
                    last_received_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (site_id, device_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO iot_frame_stats
                    (site_id, device_id, total_frames, last_received_at, updated_at)
                SELECT site_id, device_id, COUNT(*), MAX(received_at), ?
                FROM raw_frames
                GROUP BY site_id, device_id
                ON CONFLICT (site_id, device_id) DO UPDATE SET
                    total_frames = excluded.total_frames,
                    last_received_at = excluded.last_received_at,
                    updated_at = excluded.updated_at
                """,
                (datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),),
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
            decoded = decode_raw_metric(
                {"measurement_point_id": payload["measurement_point_id"], "protocol": payload["protocol"], **frame}
            )
            metric_fields = {
                "metric_key": decoded["metric"] if decoded else "raw-frame",
                "metric_value": decoded["value"] if decoded else None,
                "metric_unit": decoded["unit"] if decoded else "frame",
            }
            line = json.dumps(
                {
                    "request_id": request_id,
                    "received_at": received_at,
                    "site_id": payload["site_id"],
                    "device_id": payload["device_id"],
                    "measurement_point_id": payload["measurement_point_id"],
                    "protocol": payload["protocol"],
                    **frame,
                    **metric_fields,
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

    @staticmethod
    def _refresh_estimated_daily_energy(
        cursor: Any,
        site_id: str,
        device_id: str,
        captured_at: datetime,
        power_w: float,
    ) -> None:
        local_date = captured_at.astimezone(timezone(timedelta(hours=8))).date()
        cursor.execute(
            """
            SELECT estimated_energy_kwh, last_power_w, last_power_at,
                   quality, calculation_method, energy_kwh
            FROM iot_daily_energy
            WHERE site_id = %s AND device_id = %s AND local_date = %s
            FOR UPDATE
            """,
            (site_id, device_id, local_date),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                INSERT INTO iot_daily_energy
                    (site_id, device_id, local_date, baseline_kwh, baseline_at,
                     latest_total_kwh, latest_total_at, energy_kwh,
                     estimated_energy_kwh, last_power_w, last_power_at,
                     calculation_method, quality, updated_at)
                VALUES (%s, %s, %s, NULL, NULL, NULL, NULL, NULL,
                        0, %s, %s, 'power-integration', 'collecting', now())
                ON CONFLICT (site_id, device_id, local_date) DO NOTHING
                """,
                (site_id, device_id, local_date, power_w, captured_at),
            )
            return

        estimated_energy, previous_power, previous_at, quality, method, current_energy = row
        estimated_energy = float(estimated_energy or 0.0)
        next_power = previous_power
        next_at = previous_at
        if previous_at is None or captured_at > previous_at:
            next_power = power_w
            next_at = captured_at
            if previous_at is not None and previous_power is not None:
                elapsed_seconds = (captured_at - previous_at).total_seconds()
                if 0 < elapsed_seconds <= 120:
                    estimated_energy += (
                        (float(previous_power) + float(power_w))
                        * 0.5
                        * elapsed_seconds
                        / 3600000.0
                    )

        exact_meter_value = method == "meter-counter" and quality == "ok"
        display_energy = current_energy if exact_meter_value else (estimated_energy if estimated_energy > 0 else current_energy)
        display_quality = quality if exact_meter_value else ("estimated" if estimated_energy > 0 else quality)
        display_method = method if exact_meter_value else ("power-integration" if estimated_energy > 0 else method)
        cursor.execute(
            """
            UPDATE iot_daily_energy
            SET estimated_energy_kwh = %s,
                last_power_w = %s,
                last_power_at = %s,
                energy_kwh = %s,
                quality = %s,
                calculation_method = %s,
                updated_at = now()
            WHERE site_id = %s AND device_id = %s AND local_date = %s
            """,
            (
                estimated_energy,
                next_power,
                next_at,
                display_energy,
                display_quality,
                display_method,
                site_id,
                device_id,
                local_date,
            ),
        )

    @staticmethod
    def _refresh_daily_energy_rows(cursor: Any, site_id: str, device_id: str, local_dates: List[Any]) -> None:
        for local_date in local_dates:
            cursor.execute(
                """
                WITH current_reading AS (
                    SELECT metric.metric_value AS latest_total_kwh,
                           frame.captured_at AS latest_total_at
                    FROM iot_dlt645_frames AS frame
                    JOIN iot_meter_frame_metrics AS metric ON metric.frame_id = frame.id
                    WHERE metric.metric_key = 'total-active-energy'
                      AND frame.site_id = %s AND frame.device_id = %s
                      AND (frame.captured_at AT TIME ZONE 'Asia/Shanghai')::date = %s
                    ORDER BY frame.captured_at DESC, frame.id DESC LIMIT 1
                ), previous_reading AS (
                    SELECT metric.metric_value AS baseline_kwh,
                           frame.captured_at AS baseline_at
                    FROM iot_dlt645_frames AS frame
                    JOIN iot_meter_frame_metrics AS metric ON metric.frame_id = frame.id
                    WHERE metric.metric_key = 'total-active-energy'
                      AND frame.site_id = %s AND frame.device_id = %s
                      AND (frame.captured_at AT TIME ZONE 'Asia/Shanghai')::date = %s::date - 1
                    ORDER BY frame.captured_at DESC, frame.id DESC LIMIT 1
                )
                INSERT INTO iot_daily_energy
                    (site_id, device_id, local_date, baseline_kwh, baseline_at,
                     latest_total_kwh, latest_total_at, energy_kwh,
                     calculation_method, quality, updated_at)
                SELECT %s, %s, %s,
                       previous_reading.baseline_kwh, previous_reading.baseline_at,
                       current_reading.latest_total_kwh, current_reading.latest_total_at,
                       CASE
                           WHEN previous_reading.baseline_kwh IS NULL THEN NULL
                           WHEN current_reading.latest_total_kwh < previous_reading.baseline_kwh THEN NULL
                           ELSE current_reading.latest_total_kwh - previous_reading.baseline_kwh
                       END,
                       'meter-counter',
                       CASE
                           WHEN previous_reading.baseline_kwh IS NULL THEN 'missing-baseline'
                           WHEN current_reading.latest_total_kwh < previous_reading.baseline_kwh THEN 'counter-reset'
                           ELSE 'ok'
                       END,
                       now()
                FROM current_reading
                LEFT JOIN previous_reading ON TRUE
                ON CONFLICT (site_id, device_id, local_date) DO UPDATE SET
                    baseline_kwh = EXCLUDED.baseline_kwh,
                    baseline_at = EXCLUDED.baseline_at,
                    latest_total_kwh = EXCLUDED.latest_total_kwh,
                    latest_total_at = EXCLUDED.latest_total_at,
                    energy_kwh = CASE
                        WHEN EXCLUDED.quality = 'ok' THEN EXCLUDED.energy_kwh
                        WHEN iot_daily_energy.estimated_energy_kwh > 0 THEN iot_daily_energy.estimated_energy_kwh
                        ELSE EXCLUDED.energy_kwh
                    END,
                    quality = CASE
                        WHEN EXCLUDED.quality = 'ok' THEN EXCLUDED.quality
                        WHEN iot_daily_energy.estimated_energy_kwh > 0 THEN 'estimated'
                        ELSE EXCLUDED.quality
                    END,
                    calculation_method = CASE
                        WHEN EXCLUDED.quality = 'ok' THEN 'meter-counter'
                        WHEN iot_daily_energy.estimated_energy_kwh > 0 THEN 'power-integration'
                        ELSE EXCLUDED.calculation_method
                    END,
                    updated_at = now()
                """,
                (
                    site_id, device_id, local_date,
                    site_id, device_id, local_date,
                    site_id, device_id, local_date,
                ),
            )

    def ingest(self, request_id: str, received_at: str, payload: Dict[str, Any]) -> int:
        if not self.enable_database:
            return len(payload["frames"])
        if self.monitor_database_url:
            with self._postgres_connect() as connection:
                with connection.cursor() as cursor:
                    inserted_frames = 0
                    frame_stats: Dict[Tuple[str, str], List[Any]] = {}
                    energy_dates: set = set()
                    for frame in payload["frames"]:
                        event_id = f"{request_id}:{frame['sequence']}:{frame['direction']}"
                        decoded = decode_raw_metric(
                            {"measurement_point_id": payload["measurement_point_id"], "protocol": payload["protocol"], **frame}
                        )
                        cursor.execute(
                            """
                            INSERT INTO iot_dlt645_frames (
                                source_id, event_id, request_id, site_id, device_id, measurement_point_id,
                                protocol, sequence, captured_at, direction, frame_hex,
                                received_at, metric_key, metric_value, metric_unit, raw_archive_path
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            RETURNING id
                            """,
                            (
                                "iotmonitor-192", event_id, request_id, payload["site_id"], payload["device_id"], payload["measurement_point_id"],
                                payload["protocol"], frame["sequence"], frame["captured_at"], frame["direction"],
                                frame["frame_hex"], received_at, decoded["metric"] if decoded else "raw-frame",
                                decoded["value"] if decoded else None, decoded["unit"] if decoded else None, self.archive_path,
                            ),
                        )
                        inserted_row = cursor.fetchone()
                        if inserted_row is None:
                            continue
                        inserted_frames += 1
                        frame_id = inserted_row[0]
                        decoded_metrics = decode_raw_metrics(
                            {"measurement_point_id": payload["measurement_point_id"], "protocol": payload["protocol"], **frame}
                        )
                        if decoded_metrics:
                            captured_at = datetime.fromisoformat(str(frame["captured_at"]).replace("Z", "+00:00"))
                            cursor.executemany(
                                """
                                INSERT INTO iot_meter_frame_metrics
                                    (frame_id, metric_key, metric_value, metric_unit)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (frame_id, metric_key) DO UPDATE SET
                                    metric_value = EXCLUDED.metric_value,
                                    metric_unit = EXCLUDED.metric_unit
                                """,
                                [
                                    (frame_id, metric["metric"], metric["value"], metric["unit"])
                                    for metric in decoded_metrics
                                ],
                            )
                            power_metric = next(
                                (metric for metric in decoded_metrics if metric["metric"] == "instantaneous-active-power"),
                                None,
                            )
                            if power_metric is not None:
                                self._refresh_estimated_daily_energy(
                                    cursor,
                                    payload["site_id"],
                                    payload["device_id"],
                                    captured_at,
                                    float(power_metric["value"]),
                                )
                            if any(metric["metric"] == "total-active-energy" for metric in decoded_metrics):
                                local_date = captured_at.astimezone(timezone(timedelta(hours=8))).date()
                                energy_dates.add((payload["site_id"], payload["device_id"], local_date))
                        key = (payload["site_id"], payload["device_id"])
                        aggregate = frame_stats.setdefault(key, [0, received_at])
                        aggregate[0] += 1
                        if aggregate[1] < received_at:
                            aggregate[1] = received_at
                    for site_id, device_id, local_date in energy_dates:
                        self._refresh_daily_energy_rows(
                            cursor, site_id, device_id, [local_date, local_date + timedelta(days=1)]
                        )
                    for (site_id, device_id), (frame_count, last_received_at) in frame_stats.items():
                        cursor.execute(
                            """
                            INSERT INTO iot_frame_stats
                                (site_id, device_id, total_frames, last_received_at, updated_at)
                            VALUES (%s, %s, %s, %s, now())
                            ON CONFLICT (site_id, device_id) DO UPDATE SET
                                total_frames = iot_frame_stats.total_frames + EXCLUDED.total_frames,
                                last_received_at = GREATEST(
                                    iot_frame_stats.last_received_at,
                                    EXCLUDED.last_received_at
                                ),
                                updated_at = now()
                            """,
                            (site_id, device_id, frame_count, last_received_at),
                        )
                connection.commit()
            return inserted_frames
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
            connection.execute(
                """
                INSERT INTO iot_frame_stats
                    (site_id, device_id, total_frames, last_received_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (site_id, device_id) DO UPDATE SET
                    total_frames = iot_frame_stats.total_frames + excluded.total_frames,
                    last_received_at = CASE
                        WHEN iot_frame_stats.last_received_at IS NULL
                          OR excluded.last_received_at > iot_frame_stats.last_received_at
                        THEN excluded.last_received_at
                        ELSE iot_frame_stats.last_received_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["site_id"],
                    payload["device_id"],
                    len(payload["frames"]),
                    received_at,
                    datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                ),
            )
        return len(payload["frames"])

    def list_recent(self, *, limit: int, site_id: Optional[str] = None, device_id: Optional[str] = None,
                    direction: Optional[str] = None, start_at: Optional[str] = None,
                    end_at: Optional[str] = None, include_summary: bool = True,
                    sample_across_range: bool = False, metric_key: Optional[str] = None,
                    include_total: bool = True) -> Dict[str, Any]:
        if self.monitor_api_url:
            query = [
                ("limit", str(limit)),
                ("view", "trend" if sample_across_range else "frames"),
                ("summary", "1" if include_summary else "0"),
                ("total", "1" if include_total else "0"),
            ]
            if site_id:
                query.append(("site_id", site_id))
            if device_id:
                query.append(("device_id", device_id))
            if direction:
                query.append(("direction", direction))
            if start_at:
                query.append(("start_at", start_at))
            if end_at:
                query.append(("end_at", end_at))
            if metric_key:
                query.append(("metric_key", metric_key))
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
        if start_at:
            conditions.append("captured_at >= ?")
            parameters.append(start_at)
        if end_at:
            conditions.append("captured_at < ?")
            parameters.append(end_at)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        if self.monitor_database_url:
            pg_where = where.replace("?", "%s")
            with self._postgres_connect() as connection:
                with connection.cursor() as cursor:
                    select_sql = f"""SELECT id, request_id::text, site_id, device_id, measurement_point_id,
                                             protocol, sequence, captured_at, direction, frame_hex, received_at,
                                             metric_key, metric_value, metric_unit
                                      FROM iot_dlt645_frames {pg_where} ORDER BY id DESC"""
                    if sample_across_range:
                        cursor.execute(select_sql, parameters)
                    else:
                        cursor.execute(select_sql + " LIMIT %s", [*parameters, limit])
                    columns = [description[0] for description in cursor.description]
                    frames = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    if include_total:
                        cursor.execute(f"SELECT COUNT(*) FROM iot_dlt645_frames {pg_where}", parameters)
                        total = cursor.fetchone()[0]
                    else:
                        total = None
            for frame in frames:
                for field in ("captured_at", "received_at"):
                    frame[field] = frame[field].isoformat().replace("+00:00", "Z")
        elif self.enable_database:
            with self._connect() as connection:
                select_sql = f"""SELECT id, request_id, site_id, device_id, measurement_point_id,
                                          protocol, sequence, captured_at, direction, frame_hex, received_at
                                   FROM raw_frames {where} ORDER BY id DESC"""
                rows = connection.execute(
                    select_sql if sample_across_range else select_sql + " LIMIT ?",
                    parameters if sample_across_range else [*parameters, limit],
                ).fetchall()
                total = (
                    connection.execute(f"SELECT COUNT(*) FROM raw_frames {where}", parameters).fetchone()[0]
                    if include_total else None
                )
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
                        captured_at = str(frame.get("captured_at", ""))
                        if start_at and captured_at < start_at:
                            continue
                        if end_at and captured_at >= end_at:
                            continue
                        frames.append(frame)
            except OSError:
                frames = []
            total = len(frames) if include_total else None
            frames = list(reversed(frames))

        if metric_key:
            frames = [
                frame for frame in frames
                if any(decoded.get("metric") == metric_key for decoded in decode_raw_metrics(frame))
            ]
            total = len(frames)

        if sample_across_range and len(frames) > limit:
            # Frames are newest-first. Keep both endpoints and sample evenly so
            # a 24-hour request represents the full window instead of only the
            # latest LIMIT rows.
            if limit == 1:
                frames = [frames[0]]
            else:
                frames = [
                    frames[round(index * (len(frames) - 1) / (limit - 1))]
                    for index in range(limit)
                ]
        elif len(frames) > limit:
            frames = frames[:limit]
        metrics: Dict[str, List[Dict[str, Any]]] = {name: [] for name in RAW_METRIC_CONFIG}
        collection_buckets: Dict[str, Dict[str, Any]] = {}
        for frame in reversed(frames):
            decoded_metrics = decode_raw_metrics(frame)
            if not decoded_metrics:
                frame["metric_key"] = "raw-frame"
                frame["metric_value"] = None
                frame["metric_unit"] = "frame"
            elif len(decoded_metrics) == 1:
                frame["metric_key"] = decoded_metrics[0]["metric"]
                frame["metric_value"] = decoded_metrics[0]["value"]
                frame["metric_unit"] = decoded_metrics[0]["unit"]
            else:
                frame["metric_key"] = "im1253b-block"
                frame["metric_value"] = None
                frame["metric_unit"] = "block"
            for decoded in decoded_metrics:
                metrics[decoded["metric"]].append(
                    {"captured_at": frame["captured_at"], "sequence": frame["sequence"], "value": decoded["value"]}
                )
            timestamp = str(frame.get("captured_at", ""))
            bucket = timestamp[:19] + "Z" if len(timestamp) >= 19 else timestamp
            item = collection_buckets.setdefault(bucket, {"captured_at": bucket, "success": 0, "failure": 0})
            item["success" if decoded_metrics else "failure"] += 1
        payload = {
            "frames": frames,
            "count": len(frames),
            "total": total,
            "metrics": [
                {"key": key, "name": config["name"], "unit": config["unit"], "points": metrics[key]}
                for key, config in RAW_METRIC_CONFIG.items()
            ],
            "collection": {"interval": "1s", "points": list(collection_buckets.values())},
        }
        if include_summary:
            payload.update(self.energy_summary(site_id=site_id, device_id=device_id))
        else:
            payload.update({"total_energy_kwh": None, "daily_energy_kwh": None, "daily_energy": []})
        return payload

    def frame_status(self, *, site_id: Optional[str] = None, device_id: Optional[str] = None,
                     online_threshold_seconds: int = 120) -> Dict[str, Any]:
        if online_threshold_seconds <= 0:
            raise ValueError("online threshold must be positive")
        if self.monitor_api_url:
            query = []
            if site_id:
                query.append(("site_id", site_id))
            if device_id:
                query.append(("device_id", device_id))
            from urllib.parse import urlencode
            request = Request(
                self.monitor_api_url.rsplit("/", 1)[0] + "/status?" + urlencode(query),
                headers={"X-Monitor-Center-Key": self.monitor_api_key or ""},
            )
            try:
                with urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Monitor Center response is invalid")
                return payload
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("Monitor Center status query failed") from error

        conditions = []
        parameters: List[Any] = []
        for column, value in (("site_id", site_id), ("device_id", device_id)):
            if value:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        if self.monitor_database_url:
            pg_where = where.replace("?", "%s")
            with self._postgres_connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT COALESCE(SUM(total_frames), 0),
                               MAX(last_received_at),
                               MAX(updated_at),
                               COUNT(*),
                               COUNT(*) FILTER (
                                   WHERE last_received_at >= now() - (%s * interval '1 second')
                               )
                        FROM iot_frame_stats
                        {pg_where}
                        """,
                        [online_threshold_seconds, *parameters],
                    )
                    total_frames, last_received_at, updated_at, device_count, online_devices = cursor.fetchone()
            last_received = last_received_at.isoformat().replace("+00:00", "Z") if last_received_at else None
            updated = updated_at.isoformat().replace("+00:00", "Z") if updated_at else None
        elif self.enable_database:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=online_threshold_seconds)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z")
            with self._connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT COALESCE(SUM(total_frames), 0),
                           MAX(last_received_at),
                           MAX(updated_at),
                           COUNT(*),
                           COALESCE(SUM(CASE WHEN last_received_at >= ? THEN 1 ELSE 0 END), 0)
                    FROM iot_frame_stats
                    {where}
                    """,
                    [cutoff, *parameters],
                ).fetchone()
            total_frames, last_received, updated, device_count, online_devices = row
        else:
            total_frames, last_received, updated, device_count, online_devices = 0, None, None, 0, 0
        return {
            "site_id": site_id,
            "device_id": device_id,
            "total_frames": int(total_frames),
            "last_received_at": last_received,
            "updated_at": updated,
            "device_count": int(device_count),
            "online_devices": int(online_devices),
            "online": int(online_devices) > 0,
            "online_threshold_seconds": online_threshold_seconds,
        }

    def metric_trends(self, *, limit: int = 1000, site_id: Optional[str] = None,
                      device_id: Optional[str] = None, start_at: Optional[str] = None,
                      end_at: Optional[str] = None,
                      metric_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        selected_keys = metric_keys or [
            "voltage-a",
            "current-a",
            "instantaneous-active-power",
            "temperature",
        ]
        if self.monitor_api_url:
            query = [("limit", str(limit))]
            if site_id:
                query.append(("site_id", site_id))
            if device_id:
                query.append(("device_id", device_id))
            if start_at:
                query.append(("start_at", start_at))
            if end_at:
                query.append(("end_at", end_at))
            query.extend(("metric_key", key) for key in selected_keys)
            from urllib.parse import urlencode
            request = Request(
                self.monitor_api_url.rsplit("/", 1)[0] + "/trends?" + urlencode(query),
                headers={"X-Monitor-Center-Key": self.monitor_api_key or ""},
            )
            try:
                with urlopen(request, timeout=8) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Monitor Center response is invalid")
                return payload
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("Monitor Center trend query failed") from error

        payload = self.list_recent(
            limit=limit,
            site_id=site_id,
            device_id=device_id,
            start_at=start_at,
            end_at=end_at,
            include_summary=False,
            sample_across_range=True,
            include_total=False,
        )
        selected = set(selected_keys)
        return {"metrics": [metric for metric in payload["metrics"] if metric["key"] in selected]}

    def energy_summary(self, site_id: Optional[str] = None, device_id: Optional[str] = None, days: int = 30,
                       start_at: Optional[str] = None, end_at: Optional[str] = None) -> Dict[str, Any]:
        if days <= 0 or days > 366:
            raise ValueError("energy summary days must be between 1 and 366")
        if self.monitor_api_url:
            query = [("days", str(days))]
            if site_id:
                query.append(("site_id", site_id))
            if device_id:
                query.append(("device_id", device_id))
            if start_at:
                query.append(("start_at", start_at))
            if end_at:
                query.append(("end_at", end_at))
            from urllib.parse import urlencode
            request = Request(self.monitor_api_url.rsplit("/", 1)[0] + "/summary?" + urlencode(query), headers={"X-Monitor-Center-Key": self.monitor_api_key or ""})
            try:
                with urlopen(request, timeout=8) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Monitor Center summary response is invalid")
                return payload
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("Monitor Center summary query failed") from error
        frames = []
        try:
            with open(self.archive_path, "r", encoding="utf-8") as archive:
                for line in archive:
                    try:
                        frame = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(frame, dict) and (not site_id or frame.get("site_id") == site_id) and (not device_id or frame.get("device_id") == device_id):
                        frames.append(frame)
        except OSError:
            pass
        return self._energy_summary_from_frames(frames, days, start_at=start_at, end_at=end_at)

    def _energy_summary_from_frames(self, frames: List[Dict[str, Any]], days: int, *, start_at: Optional[str] = None,
                                    end_at: Optional[str] = None) -> Dict[str, Any]:
        local_tz = timezone(timedelta(hours=8))
        today_date = datetime.now(local_tz).date()
        start_date = datetime.fromisoformat(start_at.replace("Z", "+00:00")).astimezone(local_tz).date() if start_at else today_date - timedelta(days=days - 1)
        end_date = (
            (datetime.fromisoformat(end_at.replace("Z", "+00:00")) - timedelta(microseconds=1))
            .astimezone(local_tz)
            .date()
            if end_at else today_date
        )
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        end_timestamp = datetime.fromisoformat(end_at.replace("Z", "+00:00")).timestamp() if end_at else None
        samples_by_device: Dict[Tuple[str, str], List[Tuple[float, float, datetime]]] = {}
        for frame in frames:
            decoded = next(
                (item for item in decode_raw_metrics(frame) if item["metric"] == "total-active-energy"),
                None,
            )
            if not decoded:
                continue
            try:
                timestamp = datetime.fromisoformat(str(frame["captured_at"]).replace("Z", "+00:00"))
                if end_timestamp is not None and timestamp.timestamp() >= end_timestamp:
                    continue
                key = (str(frame.get("site_id", "")), str(frame.get("device_id", "")))
                samples_by_device.setdefault(key, []).append((timestamp.timestamp(), float(decoded["value"]), timestamp))
            except (KeyError, TypeError, ValueError):
                continue
        empty_result = {
            "energy_device_id": None,
            "energy_site_id": None,
            "energy_summary_method": "meter-counter-delta",
            "total_energy_kwh": None,
            "total_energy_at": None,
            "daily_energy_kwh": None,
            "daily_energy_quality": "no-total-energy",
            "daily_energy_baseline_kwh": None,
            "daily_energy_baseline_at": None,
            "daily_energy_latest_kwh": None,
            "daily_energy_latest_at": None,
            "daily_energy": [],
        }
        if not samples_by_device:
            return empty_result
        selected_key = max(samples_by_device, key=lambda key: max(sample[0] for sample in samples_by_device[key]))
        samples = sorted(samples_by_device[selected_key])
        day_ends: Dict[Any, Tuple[float, datetime]] = {}
        for _, value, timestamp in samples:
            day_ends[timestamp.astimezone(local_tz).date()] = (value, timestamp)
        _, total_energy, total_energy_at = samples[-1]
        daily = []
        for index in range((end_date - start_date).days + 1):
            day = start_date + timedelta(days=index)
            baseline = day_ends.get(day - timedelta(days=1))
            latest = day_ends.get(day)
            quality = "ok"
            value = None
            if latest is None:
                quality = "no-reading"
            elif baseline is None:
                quality = "missing-baseline"
            elif latest[0] < baseline[0]:
                quality = "counter-reset"
            else:
                value = round(latest[0] - baseline[0], 6)
            daily.append({
                "date": day.isoformat(),
                "kwh": value,
                "metric_key": "daily-active-energy",
                "quality": quality,
                "baseline_kwh": baseline[0] if baseline else None,
                "baseline_at": baseline[1].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if baseline else None,
                "total_kwh": latest[0] if latest else None,
                "total_at": latest[1].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if latest else None,
            })
        summary_date = end_date if end_at else today_date
        today_point = next((point for point in daily if point["date"] == summary_date.isoformat()), None)
        return {
            "energy_device_id": selected_key[1],
            "energy_site_id": selected_key[0],
            "energy_summary_method": "meter-counter-delta",
            "total_energy_kwh": round(total_energy, 6),
            "total_energy_at": total_energy_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "daily_energy_kwh": today_point["kwh"] if today_point else None,
            "daily_energy_quality": today_point["quality"] if today_point else "no-reading",
            "daily_energy_baseline_kwh": today_point["baseline_kwh"] if today_point else None,
            "daily_energy_baseline_at": today_point["baseline_at"] if today_point else None,
            "daily_energy_latest_kwh": today_point["total_kwh"] if today_point else None,
            "daily_energy_latest_at": today_point["total_at"] if today_point else None,
            "daily_energy": daily,
        }

    @staticmethod
    def _daily_energy_kwh(frames: List[Dict[str, Any]]) -> float:
        points = []
        for frame in frames:
            metric_key = frame.get("metric_key")
            metric_value = frame.get("metric_value")
            if metric_key is None or metric_key == "im1253b-block":
                decoded = next(
                    (item for item in decode_raw_metrics(frame) if item["metric"] == "instantaneous-active-power"),
                    None,
                )
                metric_key = decoded["metric"] if decoded else None
                metric_value = decoded["value"] if decoded else None
            if metric_key != "instantaneous-active-power" or metric_value is None:
                continue
            try:
                timestamp = datetime.fromisoformat(str(frame["captured_at"]).replace("Z", "+00:00"))
                points.append((timestamp.timestamp(), float(metric_value)))
            except (TypeError, ValueError):
                continue
        points.sort()
        energy = 0.0
        for (previous_time, previous_power), (current_time, current_power) in zip(points, points[1:]):
            elapsed = current_time - previous_time
            if 0 < elapsed <= 120:
                energy += (previous_power + current_power) * 0.5 * elapsed / 3600000.0
        return round(max(energy, 0.0), 6)

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


def modbus_crc16(data: bytes) -> int:
    """Return the Modbus-RTU CRC-16 value (wire order is low byte first)."""
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def decode_modbus_metrics(frame: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decode an IM1253B single-item response or one 0x0048-0x004F block."""
    configs = [
        ("voltage-a", "A 相电压", "V", 0.0001, 0),
        ("current-a", "A 相电流", "A", 0.0001, 1),
        ("instantaneous-active-power", "瞬时有功功率", "W", 0.0001, 2),
        ("total-active-energy", "累计有功电量", "kWh", 0.0001, 3),
        ("power-factor", "功率因数", "", 0.001, 4),
        # 0x004D 是二氧化碳排量，当前趋势页面暂不展示。
        ("temperature", "温度", "°C", 0.01, 6),
        ("frequency", "频率", "Hz", 0.01, 7),
    ]
    try:
        encoded = bytes.fromhex(str(frame["frame_hex"]).replace(" ", "").replace(":", ""))
        if len(encoded) < 5 or encoded[1] != 0x03:
            return []
        received_crc = encoded[-2] | (encoded[-1] << 8)
        if modbus_crc16(encoded[:-2]) != received_crc:
            return []

        point_metric = str(frame.get("measurement_point_id", "")).rsplit(":", 1)[-1]
        if len(encoded) == 9 and encoded[2] == 0x04:
            config = next((item for item in configs if item[0] == point_metric), None)
            if config is None:
                return []
            metric, name, unit, scale, _ = config
            raw_value = int.from_bytes(encoded[3:7], "big", signed=False)
            return [{"metric": metric, "name": name, "unit": unit, "value": raw_value * scale}]

        if len(encoded) != 37 or encoded[2] != 0x20:
            return []
        decoded = []
        for metric, name, unit, scale, item_offset in configs:
            start = 3 + item_offset * 4
            raw_value = int.from_bytes(encoded[start : start + 4], "big", signed=False)
            decoded.append({"metric": metric, "name": name, "unit": unit, "value": raw_value * scale})
        return decoded
    except (KeyError, ValueError, IndexError):
        return []


def decode_modbus_metric(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the requested metric, or power as the primary value of a block."""
    decoded = decode_modbus_metrics(frame)
    point_metric = str(frame.get("measurement_point_id", "")).rsplit(":", 1)[-1]
    return next(
        (item for item in decoded if item["metric"] == point_metric),
        next((item for item in decoded if item["metric"] == "instantaneous-active-power"), decoded[0] if decoded else None),
    )


def decode_raw_metric(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Decode a configured DL/T 645 or IM1253B Modbus metric for charting."""
    if frame.get("protocol") == "MODBUS-RTU":
        return decode_modbus_metric(frame)
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


def decode_raw_metrics(frame: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decode all metrics represented by one raw frame."""
    if frame.get("protocol") == "MODBUS-RTU":
        return decode_modbus_metrics(frame)
    decoded = decode_raw_metric(frame)
    return [decoded] if decoded else []


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
    if protocol not in RAW_FRAME_PROTOCOLS:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            f"protocol must be one of {', '.join(sorted(RAW_FRAME_PROTOCOLS))}",
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
        if path == RAW_SUMMARY_PATH:
            self._handle_raw_summary_query()
            return
        if path == RAW_STATUS_PATH:
            self._handle_raw_status_query()
            return
        if path == RAW_TRENDS_PATH:
            self._handle_raw_trends_query()
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
        view = query.get("view", ["frames"])[0]
        max_limit = MAX_RAW_FRAME_TREND_LIMIT if view == "trend" else MAX_RAW_FRAME_QUERY_LIMIT
        try:
            raw_limit = query.get("limit", ["50"])[0]
            limit = int(raw_limit)
            direction = query.get("direction", [None])[0]
            if direction is not None and direction not in RAW_FRAME_DIRECTIONS:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "direction must be one of tx, rx")
            if view not in {"frames", "trend"}:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "view must be frames or trend")
            if not 1 <= limit <= max_limit:
                raise ValueError
            start_at = query.get("start_at", [None])[0]
            end_at = query.get("end_at", [None])[0]
            if start_at is not None:
                start_at = _timestamp(start_at, "start_at")
            if end_at is not None:
                end_at = _timestamp(end_at, "end_at")
            if start_at and end_at and start_at >= end_at:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "start_at must be before end_at")
            metric_key = query.get("metric_key", [None])[0]
            if metric_key is not None and metric_key not in RAW_METRIC_CONFIG:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "metric_key is not supported")
            result = self.server.raw_store.list_recent(
                limit=limit,
                site_id=query.get("site_id", [None])[0],
                device_id=query.get("device_id", [None])[0],
                direction=direction,
                start_at=start_at,
                end_at=end_at,
                include_summary=query.get("summary", ["1"])[0] not in {"0", "false", "no"},
                sample_across_range=view == "trend",
                metric_key=metric_key,
                include_total=query.get("total", ["1"])[0] not in {"0", "false", "no"},
            )
        except ValueError:
            self._error_response(
                ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", f"limit must be between 1 and {max_limit}")
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

    def _handle_raw_status_query(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            result = self.server.raw_store.frame_status(
                site_id=query.get("site_id", [None])[0],
                device_id=query.get("device_id", [None])[0],
            )
        except (RuntimeError, sqlite3.Error):
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "Monitor Center status is unavailable")
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _handle_raw_trends_query(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            limit = int(query.get("limit", ["1000"])[0])
            if not 1 <= limit <= MAX_RAW_FRAME_TREND_LIMIT:
                raise ValueError
            start_at = query.get("start_at", [None])[0]
            end_at = query.get("end_at", [None])[0]
            if start_at is not None:
                start_at = _timestamp(start_at, "start_at")
            if end_at is not None:
                end_at = _timestamp(end_at, "end_at")
            if start_at and end_at and start_at >= end_at:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "start_at must be before end_at")
            metric_keys = query.get("metric_key") or None
            if metric_keys is not None and any(key not in RAW_METRIC_CONFIG for key in metric_keys):
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "metric_key is not supported")
            result = self.server.raw_store.metric_trends(
                limit=limit,
                site_id=query.get("site_id", [None])[0],
                device_id=query.get("device_id", [None])[0],
                start_at=start_at,
                end_at=end_at,
                metric_keys=metric_keys,
            )
        except ValueError:
            self._error_response(
                ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "limit must be between 1 and 1000")
            )
            return
        except ApiError as error:
            self._error_response(error)
            return
        except (RuntimeError, sqlite3.Error):
            self._error_response(
                ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "Monitor Center trends are unavailable")
            )
            return
        self._json_response(HTTPStatus.OK, result)

    def _handle_raw_summary_query(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            days = int(query.get("days", ["30"])[0])
            if not 1 <= days <= 366:
                raise ValueError
            start_at = query.get("start_at", [None])[0]
            end_at = query.get("end_at", [None])[0]
            if start_at is not None:
                start_at = _timestamp(start_at, "start_at")
            if end_at is not None:
                end_at = _timestamp(end_at, "end_at")
            if start_at and end_at and start_at >= end_at:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "start_at must be before end_at")
            result = self.server.raw_store.energy_summary(
                site_id=query.get("site_id", [None])[0],
                device_id=query.get("device_id", [None])[0],
                days=days,
                start_at=start_at,
                end_at=end_at,
            )
        except ValueError:
            self._error_response(ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", "days must be between 1 and 366"))
            return
        except ApiError as error:
            self._error_response(error)
            return
        except RuntimeError:
            self._error_response(ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "storage_unavailable", "Monitor Center storage is unavailable"))
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
    # The ingress host owns its PostgreSQL database.  Replication to Monitor
    # Center is handled by PostgreSQL logical replication, not by this API.
    monitor_database_url = (
        os.environ.get("POWER_MONITOR_DATABASE_URL")
        or os.environ.get("POWER_MONITOR_CENTER_DATABASE_URL")
    )
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
