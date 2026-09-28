#!/usr/bin/env python3
"""Replay durable raw-frame JSONL records into the configured database."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from app import ApiError, RawFrameStore, validate_raw_frame_payload


def payload_from_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return validate_raw_frame_payload(
        {
            "site_id": record.get("site_id"),
            "device_id": record.get("device_id"),
            "measurement_point_id": record.get("measurement_point_id"),
            "protocol": record.get("protocol"),
            "frames": [
                {
                    "sequence": record.get("sequence"),
                    "captured_at": record.get("captured_at"),
                    "direction": record.get("direction"),
                    "frame_hex": record.get("frame_hex"),
                }
            ],
        }
    )


def replay(stream: TextIO, store: Optional[RawFrameStore], *, start_line: int = 1) -> Dict[str, int]:
    scanned = 0
    inserted = 0
    duplicates = 0
    for line_number, line in enumerate(stream, start=1):
        if line_number < start_line or not line.strip():
            continue
        scanned += 1
        try:
            record = json.loads(line)
            payload = payload_from_record(record)
            request_id = str(record["request_id"])
            received_at = str(record["received_at"])
        except (ApiError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid archive record at line {line_number}: {error}") from error
        if store is None:
            continue
        accepted = store.ingest(request_id, received_at, payload)
        inserted += accepted
        duplicates += len(payload["frames"]) - accepted
    return {"scanned": scanned, "inserted": inserted, "duplicates": duplicates}


def parse_args() -> argparse.Namespace:
    database_path = os.environ.get("POWER_MONITOR_DATABASE", str(Path(__file__).parent / "data" / "power-monitor.db"))
    default_archive = os.environ.get(
        "POWER_MONITOR_RAW_ARCHIVE",
        str(Path(database_path).expanduser().with_name("dlt645-frames.jsonl")),
    )
    parser = argparse.ArgumentParser(description="Replay durable IoT raw-frame JSONL records")
    parser.add_argument("--archive", default=default_archive, help="JSONL archive path")
    parser.add_argument("--database", default=database_path, help="SQLite database path")
    parser.add_argument("--database-url", default=os.environ.get("POWER_MONITOR_DATABASE_URL"), help=argparse.SUPPRESS)
    parser.add_argument("--start-line", type=int, default=1, help="first archive line to process")
    parser.add_argument("--dry-run", action="store_true", help="validate records without writing the database")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_line < 1:
        print("--start-line must be at least 1", file=sys.stderr)
        return 2
    archive_path = Path(args.archive).expanduser()
    store = None
    try:
        if not args.dry_run:
            store = RawFrameStore(
                args.database,
                str(archive_path),
                monitor_database_url=args.database_url,
            )
        with archive_path.open("r", encoding="utf-8") as stream:
            result = replay(stream, store, start_line=args.start_line)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"replay failed: {error}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
