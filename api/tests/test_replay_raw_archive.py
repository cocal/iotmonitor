from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from replay_raw_archive import (  # noqa: E402
    discover_archive_paths,
    payload_from_record,
    replay,
    replay_archives,
)


class ReplayRawArchiveTest(unittest.TestCase):
    def record(self) -> dict:
        return {
            "archive_version": 1,
            "request_id": "beec51f8-5f39-426a-b9b8-57aa6c16a712",
            "received_at": "2026-09-28T05:00:00.000000Z",
            "site_id": "home-pv",
            "device_id": "esp8266-12f-001",
            "measurement_point_id": "inverter-ac-output",
            "protocol": "DL/T 645-2007",
            "sequence": 42,
            "captured_at": "2026-09-28T04:59:59.000000Z",
            "direction": "rx",
            "frame_hex": "68010203040568910433333333333316",
        }

    def test_dry_run_validates_archive_without_store(self) -> None:
        stream = io.StringIO(json.dumps(self.record()) + "\n")

        result = replay(stream, None)

        self.assertEqual({"scanned": 1, "inserted": 0, "duplicates": 0}, result)

    def test_record_is_converted_to_single_frame_payload(self) -> None:
        payload = payload_from_record(self.record())

        self.assertEqual("esp8266-12f-001", payload["device_id"])
        self.assertEqual(42, payload["frames"][0]["sequence"])

    def test_discovers_plain_and_gzip_rotations_before_active_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory) / "dlt645-frames.jsonl"
            oldest = Path(str(base) + ".2026-09-26.gz")
            newer = Path(str(base) + ".2026-09-27")
            with gzip.open(str(oldest), "wt", encoding="utf-8") as stream:
                stream.write(json.dumps(self.record()) + "\n")
            newer.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
            base.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
            Path(str(base) + ".unrelated").write_text("ignored", encoding="utf-8")

            paths = discover_archive_paths(base)
            result = replay_archives(paths, None)

        self.assertEqual([oldest.name, newer.name, base.name], [path.name for path in paths])
        self.assertEqual({"files": 3, "scanned": 3, "inserted": 0, "duplicates": 0}, result)


if __name__ == "__main__":
    unittest.main()
