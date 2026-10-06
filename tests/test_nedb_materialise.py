"""NEDB seeding regressions; no running database required."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stealth.forge import _materialise_nedb, schema_catalog
from stealth.schemas import CATALOG


class MaterialiseTests(unittest.TestCase):
    def test_catalog_serials_nulls_and_empty_tables(self):
        calls = []
        def send(req, **kwargs):
            body = json.loads(req.data) if req.data else None
            calls.append((req.method, req.full_url, body))
            return io.BytesIO(b'{"ok":true}')
        with patch("stealth.forge.urllib.request.urlopen", side_effect=send):
            for schema in CATALOG.values():
                self.assertEqual(_materialise_nedb(schema, "stealth_" + schema.key),
                                 "stealth_" + schema.key)
        puts = [body for method, url, body in calls if method == "POST" and url.endswith("/put")]
        self.assertTrue(puts)
        self.assertTrue(any(any(v is None for v in p["doc"].values()) for p in puts))
        for p in puts:
            if "id" in p["doc"] and p["id"] != "__stealth_empty_fixture__":
                self.assertIsInstance(p["doc"]["id"], int)
        self.assertTrue(any(method == "DELETE" and "/rows/" in url
                            for method, url, _ in calls))
        self.assertEqual(schema_catalog(CATALOG["shop"])["orders"]["id"], "int")
        self.assertEqual(schema_catalog(CATALOG["telemetry"])["samples"]["taken_at"], "timestamp")

    def test_refuses_non_fixture_database(self):
        with patch("stealth.forge.urllib.request.urlopen") as send:
            with self.assertRaises(ValueError):
                _materialise_nedb(CATALOG["shop"], "shop")
            send.assert_not_called()

    def test_tampered_seed_fails(self):
        def send(req, **kwargs):
            return io.BytesIO(b'{"ok":false}' if req.full_url.endswith("/verify") else b'{}')
        with patch("stealth.forge.urllib.request.urlopen", side_effect=send):
            with self.assertRaises(RuntimeError):
                _materialise_nedb(CATALOG["shop"], "stealth_shop")


if __name__ == "__main__":
    unittest.main()
