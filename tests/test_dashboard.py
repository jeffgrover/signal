"""Read-only API and static-file boundary integration check (Python stdlib)."""
import gzip
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from dashboard import FIELDS, load_data, make_handler


class DashboardTest(unittest.TestCase):
    def test_read_only_archive_and_http_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "snapshot.db"
            with sqlite3.connect(db) as conn:
                conn.execute(f"CREATE TABLE observations ({','.join(FIELDS)},server_name,server_location,server_country)")
                conn.execute("INSERT INTO observations(source_rowid,timestamp_local,download_mbps,server_id,server_name) VALUES (1,'2026-01-01T00:00:00',0,'1','Local endpoint')")
                conn.execute("CREATE TABLE analysis_metadata(key,value)")
                conn.execute("INSERT INTO analysis_metadata VALUES ('rule_version','1')")
            before = hashlib.sha256(db.read_bytes()).digest()
            data = load_data(db)
            self.assertEqual(data['rows'][0][2], 0)
            self.assertEqual(data['meta'], {'rule_version': 1})
            self.assertEqual(data['servers'][0][1], 'Local endpoint')
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(data))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                http = HTTPConnection(*server.server_address)
                http.request('GET', '/api/data', headers={'Accept-Encoding': 'gzip'})
                response = http.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Content-Encoding'), 'gzip')
                self.assertEqual(json.loads(gzip.decompress(response.read()))['rows'][0][2], 0)
                for path in ['/', '/app.mjs', '/analytics.mjs', '/app.css']:
                    http.request('GET', path)
                    response = http.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
                for path in ['/analysis/data/speedtests-clean.db', '/../dashboard.py', '/production-review/']:
                    http.request('GET', path)
                    response = http.getresponse()
                    self.assertEqual(response.status, 404)
                    response.read()
                http.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)
            with self.assertRaises(sqlite3.OperationalError):
                load_data(Path(tmp) / 'missing.db')
            self.assertFalse((Path(tmp) / 'missing.db').exists())


if __name__ == '__main__':
    unittest.main()
