#!/usr/bin/env python3
"""Replay durable raw-frame JSONL records into the configured database."""

import argparse
import gzip
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

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


def discover_archive_paths(base_path: Path) -> List[Path]:
    """Return dated rotations from oldest to newest, followed by the active archive."""
    base_path = base_path.expanduser()
    rotated_name = re.compile(
        rf"^{re.escape(base_path.name)}\.\d{{4}}-\d{{2}}-\d{{2}}(?:\.gz)?$"
    )
    if base_path.name.endswith(".gz"):
        return [base_path] if base_path.is_file() else []
    rotations = sorted(
        path for path in base_path.parent.glob(f"{base_path.name}.*")
        if path.is_file() and rotated_name.fullmatch(path.name)
    )
    if base_path.is_file():
        rotations.append(base_path)
    return rotations


def open_archive(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(str(path), "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def replay_archives(paths: List[Path], store: Optional[RawFrameStore], *, start_line: int = 1) -> Dict[str, int]:
    scanned = 0
    inserted = 0
    duplicates = 0
    global_line = 0
    for path in paths:
        with open_archive(path) as stream:
            for local_line, line in enumerate(stream, start=1):
                global_line += 1
                if global_line < start_line or not line.strip():
                    continue
                scanned += 1
                try:
                    record = json.loads(line)
                    payload = payload_from_record(record)
                    request_id = str(record["request_id"])
                    received_at = str(record["received_at"])
                except (ApiError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValueError(f"invalid archive record at {path}:{local_line}: {error}") from error
                if store is None:
                    continue
                accepted = store.ingest(request_id, received_at, payload)
                inserted += accepted
                duplicates += len(payload["frames"]) - accepted
    return {
        "files": len(paths),
        "scanned": scanned,
        "inserted": inserted,
        "duplicates": duplicates,
    }


def parse_args() -> argparse.Namespace:
    database_path = os.environ.get("POWER_MONITOR_DATABASE", str(Path(__file__).parent / "data" / "power-monitor.db"))
    default_archive = os.environ.get(
        "POWER_MONITOR_RAW_ARCHIVE",
        str(Path(database_path).expanduser().with_name("dlt645-frames.jsonl")),
    )
    parser = argparse.ArgumentParser(description="Replay durable IoT raw-frame JSONL records")
    parser.add_argument(
        "--archive",
        default=default_archive,
        help="active JSONL path, or one specific plain/.gz archive; active path includes dated rotations",
    )
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
        archive_paths = discover_archive_paths(archive_path)
        if not archive_paths:
            raise OSError(f"archive not found: {archive_path}")
        if not args.dry_run:
            store = RawFrameStore(
                args.database,
                str(archive_path),
                monitor_database_url=args.database_url,
            )
        result = replay_archives(archive_paths, store, start_line=args.start_line)
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
