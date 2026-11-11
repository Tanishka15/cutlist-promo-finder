"""HTTP contract checks for the loopback editor's explicit route list."""
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from promo_finder.server import _make_handler


class EditorServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.out, cls.web = cls.base / 'out', cls.base / 'web'
        cls.out.mkdir()
        cls.web.mkdir()
        for name, body in {
            'index.html': '<!doctype html><title>Editor test</title>',
            'app.js': 'console.log("synthetic");',
            'style.css': 'body { color: black; }',
            'demo.json': '{"synthetic": true}',
        }.items():
            (cls.web / name).write_text(body)
        for name, body in {
            'data.json': '{"promos": []}',
            'ranked_promos.csv': 'episode,start,end\n',
            'run_log.json': '[]',
        }.items():
            (cls.out / name).write_text(body)
        (cls.base / 'secret.txt').write_text('PRIVATE SENTINEL')
        (cls.out / 'rejections.json').symlink_to(cls.base / 'secret.txt')
        (cls.out / 'raw').mkdir()
        (cls.out / 'raw' / 'episode-003.txt').write_text('PRIVATE SENTINEL')
        handler = _make_handler(cls.out, cls.web)
        handler.log_message = lambda *_: None
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, path, method='GET'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_editor_assets_and_selected_results_are_available(self):
        for path in ('/', '/index.html', '/app.js', '/style.css', '/demo.json', '/data.json', '/run_log.json'):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertTrue(body)

    def test_ranked_csv_download_has_attachment_name(self):
        status, headers, body = self.request('/ranked_promos.csv')
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(body, b'episode,start,end\n')

    def test_arbitrary_paths_raw_prompts_and_traversal_are_not_served(self):
        for path in (
            '/raw/', '/raw/episode-003.txt', '/../secret.txt',
            '/%2e%2e/secret.txt', '/%2e%2e%2fsecret.txt',
            '/promo_finder/parser.py', '/.env', '/out/', '/secret.txt',
        ):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'PRIVATE SENTINEL', body)

    def test_allowlisted_name_cannot_follow_symlink_outside_run_folder(self):
        status, _, body = self.request('/rejections.json')
        self.assertEqual(status, 404)
        self.assertNotIn(b'PRIVATE SENTINEL', body)

    def test_head_matches_get_without_returning_body(self):
        status, headers, body = self.request('/data.json?refresh=1', method='HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(int(headers['Content-Length']), len(b'{"promos": []}'))
        self.assertFalse(body)


if __name__ == '__main__':
    unittest.main()
