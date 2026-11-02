import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient
from PIL import Image

from imagesearch.core import DownloadJob, SearchOptions, fetch_image, public_url, search_images, slug
from imagesearch.web import create_app


def png(color='red', size=(20, 15)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


def candidate(name):
    return {'image': f'https://example.com/{name}.jpg', 'title': name,
            'url': f'https://example.com/page/{name}', 'source': 'Bing'}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        # Keep temporary files under the writable workspace on Windows.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **options):
        return DownloadJob('test123456', SearchOptions(keywords=['red pandas'], **options), self.root)

    @patch('imagesearch.core.DDGS')
    def test_unrestricted_is_not_creative_commons(self, ddgs):
        search_images('cats', SearchOptions(keywords=['cats']), 1)
        self.assertIsNone(ddgs.return_value.images.call_args.kwargs['license_image'])
        search_images('cats', SearchOptions(keywords=['cats'], license='any'), 1)
        self.assertEqual(ddgs.return_value.images.call_args.kwargs['license_image'], 'any')
        self.assertEqual(ddgs.return_value.images.call_args.kwargs['backend'], 'duckduckgo')
        search_images('cats', SearchOptions(keywords=['cats'], engine='bing', license='Public'), 1)
        self.assertEqual(ddgs.return_value.images.call_args.kwargs['backend'], 'duckduckgo')

    @patch('imagesearch.core.fetch_image')
    @patch('imagesearch.core.search_images')
    def test_replaces_duplicates_and_failures_and_writes_sources(self, search, fetch):
        search.side_effect = [[candidate('one'), candidate('same'), candidate('bad')], [candidate('two')]]
        fetch.side_effect = [
            (png('red'), 'png', 20, 15, 'https://example.com/one.jpg'),
            (png('red'), 'png', 20, 15, 'https://example.com/same.jpg'),
            requests.HTTPError('403 Forbidden'),
            (png('blue'), 'png', 20, 15, 'https://example.com/two.jpg'),
        ]
        job = self.job(count=2)
        job.run()
        snapshot = job.snapshot()
        self.assertEqual(snapshot['status'], 'complete')
        self.assertEqual(snapshot['downloaded'], 2)
        self.assertEqual(snapshot['skipped'], 2)
        self.assertEqual(search.call_count, 2)
        manifest = json.loads((job.folder / 'manifest.json').read_text('utf-8'))
        self.assertEqual(manifest['status'], 'complete')
        self.assertEqual(len(list(job.folder.rglob('*.png'))), 2)
        self.assertIn('https://example.com/page/one', (job.folder / 'sources.csv').read_text('utf-8-sig'))
        self.assertEqual(manifest['images'][0]['license'], 'Unknown; check source')

    @patch('imagesearch.core.search_images', return_value=[])
    def test_shortfall_is_partial(self, search):
        job = self.job(count=3)
        job.run()
        self.assertEqual(job.snapshot()['status'], 'partial')
        self.assertEqual(search.call_count, 1)

    @patch('imagesearch.core.search_images', side_effect=[RuntimeError('rate limited'), []])
    def test_failed_keyword_does_not_stop_next_keyword(self, search):
        job = DownloadJob('test', SearchOptions(keywords=['first', 'second']), self.root)
        job.run()
        self.assertEqual(search.call_count, 2)
        self.assertEqual(job.snapshot()['status'], 'partial')
        self.assertIn('rate limited', job.snapshot()['errors'][0]['message'])

    @patch('imagesearch.core.fetch_image')
    @patch('imagesearch.core.search_images')
    def test_cancellation_retains_images_and_marks_remaining(self, search, fetch):
        job = DownloadJob('cancel', SearchOptions(keywords=['one', 'two'], count=4), self.root)
        search.return_value = [candidate('one'), candidate('two')]
        def download(*args):
            job.cancel.set()
            return png(), 'png', 20, 15, 'https://example.com/one.jpg'
        fetch.side_effect = download
        job.run()
        self.assertEqual(job.snapshot()['status'], 'cancelled')
        self.assertEqual(job.snapshot()['downloaded'], 1)
        self.assertTrue(all(k['status'] == 'cancelled' for k in job.snapshot()['keywords']))
        self.assertEqual(len(list(job.folder.rglob('*.png'))), 1)

    def test_path_sanitization_and_keyword_validation(self):
        self.assertNotIn('/', slug('../../CON'))
        self.assertNotEqual(slug('CON'), 'CON')
        with self.assertRaises(ValueError):
            SearchOptions(keywords=['  '])
        self.assertEqual(SearchOptions(keywords=[' cat ', 'cat']).keywords, ['cat'])


class FetchTests(unittest.TestCase):
    @patch('imagesearch.core.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 80))])
    def test_blocks_private_hosts(self, dns):
        with self.assertRaises(ValueError):
            public_url('http://localhost/test.jpg')

    def test_blocks_non_http(self):
        with self.assertRaises(ValueError):
            public_url('file:///C:/Windows/test.jpg')

    def fetch_mock(self, responses, **options):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = responses
        with patch('imagesearch.core.requests.Session', return_value=session), patch('imagesearch.core.public_url') as public:
            result = fetch_image('https://example.com/a.jpg', SearchOptions(keywords=['a'], **options), threading.Event())
        return result, public

    def response(self, data=b'', code=200, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = code
        response.headers = headers or {}
        response.iter_content.return_value = iter([data])
        return response

    def test_detects_actual_format_and_checks_redirect_destination(self):
        result, public = self.fetch_mock([
            self.response(code=302, headers={'Location': 'https://other.example/image'}),
            self.response(png()),
        ])
        self.assertEqual(result[1:4], ('png', 20, 15))
        self.assertEqual(public.call_args_list[1].args[0], 'https://other.example/image')

    def test_rejects_html_response_and_small_image(self):
        with self.assertRaises(Exception):
            self.fetch_mock([self.response(b'<html>Access denied</html>')])
        with self.assertRaisesRegex(ValueError, 'minimum dimensions'):
            self.fetch_mock([self.response(png())], min_width=100)

    def test_enforces_size_even_without_content_length(self):
        with self.assertRaisesRegex(ValueError, 'file size limit'):
            self.fetch_mock([self.response(b'x' * (1024 * 1024 + 1))], max_mb=1)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.client = TestClient(create_app(Path(self.temp.name)))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_home_and_validation(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)
        self.assertEqual(self.client.post('/api/jobs', json={'keywords': ['cats'], 'count': 0}).status_code, 422)
        self.assertEqual(self.client.get('/api/jobs/missing').status_code, 404)

    def test_cross_origin_writes_are_rejected(self):
        response = self.client.post('/api/jobs', json={'keywords': ['cats']}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    @patch('imagesearch.web.threading.Thread')
    def test_job_creation_concurrency_and_file_access(self, worker):
        first = self.client.post('/api/jobs', json={'keywords': ['cats'], 'count': 1})
        self.assertEqual(first.status_code, 202)
        job = first.json()
        self.assertEqual(self.client.post('/api/jobs', json={'keywords': ['dogs']}).status_code, 409)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/files/secret.txt").status_code, 404)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/cancel").status_code, 200)
        self.assertEqual(self.client.get('/api/jobs').json()[0]['id'], job['id'])


if __name__ == '__main__':
    unittest.main()
