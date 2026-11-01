import hashlib
import re
import unittest
from pathlib import Path


DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "site" / "dashboard"


class DashboardAssetTests(unittest.TestCase):
    def test_favicon_is_present_and_referenced_by_site_pages(self) -> None:
        favicon = DASHBOARD_DIR.parent / "favicon.svg"
        self.assertTrue(favicon.is_file())
        pages = {
            DASHBOARD_DIR.parent / "index.html": "./favicon.svg",
            DASHBOARD_DIR.parent / "doc" / "index.html": "../favicon.svg",
            DASHBOARD_DIR / "frames.html": "../favicon.svg",
            DASHBOARD_DIR / "frames-old.html": "../favicon.svg",
        }
        for page, href in pages.items():
            with self.subTest(page=page.name):
                self.assertIn(f'href="{href}"', page.read_text(encoding="utf-8"))
        printer_nginx = (DASHBOARD_DIR.parents[1] / "deploy" / "iot-under-printer.locations.conf").read_text(encoding="utf-8")
        self.assertIn("location = /favicon.ico", printer_nginx)
        self.assertIn("return 302 /iot/favicon.svg", printer_nginx)

    def test_html_asset_versions_match_file_content_hashes(self) -> None:
        html = (DASHBOARD_DIR / "frames.html").read_text(encoding="utf-8")
        for asset_name in ("frames.css", "frames.js"):
            match = re.search(rf"{re.escape(asset_name)}\?v=([0-9a-f]{{12}})", html)
            self.assertIsNotNone(match, f"{asset_name} must use a 12-character content hash")
            expected = hashlib.sha256((DASHBOARD_DIR / asset_name).read_bytes()).hexdigest()[:12]
            self.assertEqual(expected, match.group(1), f"bump {asset_name} cache key after editing it")


if __name__ == "__main__":
    unittest.main()
