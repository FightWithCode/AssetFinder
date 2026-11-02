import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from imagesearch.core import Cancelled
from imagesearch.video import VideoJob, VideoOptions
from imagesearch.web import create_app

URL = 'https://www.youtube.com/watch?v=BaW_jenozKc'


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **options):
        return VideoJob('video123', VideoOptions(url=URL, **options), self.root)

    def test_supported_links_canonicalize_and_discard_playlist(self):
        for url in [URL + '&list=PL123', 'https://youtu.be/BaW_jenozKc?t=5',
                    'https://m.youtube.com/shorts/BaW_jenozKc',
                    'https://www.youtube.com/embed/BaW_jenozKc',
                    'https://www.youtube.com/live/BaW_jenozKc']:
            with self.subTest(url=url):
                self.assertEqual(VideoOptions(url=url).url, URL)

    def test_rejects_other_hosts_playlist_only_and_invalid_urls(self):
        for url in ['http://localhost/test', 'file:///video.mp4',
                    'https://youtube.com.evil.example/watch?v=BaW_jenozKc',
                    'https://youtube.com@127.0.0.1/watch?v=BaW_jenozKc',
                    'https://www.youtube.com/playlist?list=PL123',
                    'https://youtu.be/not-an-id', 'https://youtu.be:8080/BaW_jenozKc',
                    'https://youtu.be:invalid/BaW_jenozKc']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                VideoOptions(url=url)
        with self.assertRaises(ValueError):
            VideoOptions(url=URL, quality='4k')

    @patch('imagesearch.video.shutil.which', return_value=None)
    @patch('imagesearch.video.YoutubeDL')
    def test_download_progress_final_file_and_manifest_without_ffmpeg(self, factory, which):
        job = self.job(quality='720')
        def extract(url, download):
            self.assertEqual(url, URL)
            self.assertTrue(download)
            hook = factory.call_args.args[0]['progress_hooks'][0]
            hook({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 100,
                  'info_dict': {'title': 'Test video'}})
            self.assertEqual(job.snapshot()['progress'], 50)
            self.assertEqual(job.snapshot()['status'], 'running')
            (job.folder / 'Test video [BaW_jenozKc].mp4').write_bytes(b'video data')
            return {'title': 'Test video'}
        factory.return_value.__enter__.return_value.extract_info.side_effect = extract
        job.run()
        state = job.snapshot()
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(state['progress'], 100)
        self.assertEqual(state['downloaded'], 1)
        self.assertEqual(state['videos'][0]['size'], 10)
        settings = factory.call_args.args[0]
        self.assertEqual(settings['format'], 'b[height<=720]')
        self.assertTrue(settings['noplaylist'])
        manifest = json.loads((job.folder / 'manifest.json').read_text('utf-8'))
        self.assertEqual(manifest['videos'][0]['source_url'], URL)

    @patch('imagesearch.video.shutil.which', return_value='/bin/tool')
    @patch('imagesearch.video.YoutubeDL')
    def test_ffmpeg_uses_separate_streams_and_final_merged_file(self, factory, which):
        job = self.job(quality='1080')
        def extract(*args, **kwargs):
            (job.folder / 'Merged.mkv').write_bytes(b'video')
            return {'title': 'Merged'}
        factory.return_value.__enter__.return_value.extract_info.side_effect = extract
        job.run()
        settings = factory.call_args.args[0]
        self.assertEqual(settings['format'], 'bv*[height<=1080]+ba/b[height<=1080]')
        self.assertIn('node', settings['js_runtimes'])
        self.assertEqual(job.snapshot()['videos'][0]['file'], 'Merged.mkv')

    @patch('imagesearch.video.YoutubeDL')
    def test_upstream_errors_become_failed_jobs(self, factory):
        factory.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError('Video unavailable')
        job = self.job()
        job.run()
        self.assertEqual(job.snapshot()['status'], 'failed')
        self.assertIn('Video unavailable', job.snapshot()['message'])
        self.assertTrue((job.folder / 'manifest.json').is_file())

    @patch('imagesearch.video.YoutubeDL')
    def test_missing_output_is_failure(self, factory):
        factory.return_value.__enter__.return_value.extract_info.return_value = {'title': 'Nothing'}
        job = self.job()
        job.run()
        self.assertEqual(job.snapshot()['status'], 'failed')
        self.assertEqual(job.snapshot()['videos'], [])

    @patch('imagesearch.video.YoutubeDL')
    def test_cancel_before_start(self, factory):
        job = self.job()
        job.cancel.set()
        job.run()
        self.assertEqual(job.snapshot()['status'], 'cancelled')
        factory.assert_not_called()

    @patch('imagesearch.video.YoutubeDL')
    def test_cancel_during_download_even_if_library_wraps_exception(self, factory):
        job = self.job()
        def extract(*args, **kwargs):
            job.cancel.set()
            with self.assertRaises(Cancelled):
                job.progress({'status': 'downloading'})
            raise RuntimeError('Wrapped cancellation')
        factory.return_value.__enter__.return_value.extract_info.side_effect = extract
        job.run()
        self.assertEqual(job.snapshot()['status'], 'cancelled')

    def test_live_streams_are_rejected(self):
        job = self.job()
        self.assertIsNotNone(job.filter_video({'is_live': True}))
        self.assertIsNotNone(job.filter_video({'live_status': 'is_upcoming'}))
        self.assertIsNone(job.filter_video({'live_status': 'was_live'}))


class VideoWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.client = TestClient(create_app(Path(self.temp.name)))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    @patch('imagesearch.web.threading.Thread')
    def test_create_poll_cancel_and_shared_concurrency_limit(self, worker):
        response = self.client.post('/api/video-jobs', json={'url': URL})
        self.assertEqual(response.status_code, 202)
        job = response.json()
        self.assertEqual(job['kind'], 'video')
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}").json()['options']['url'], URL)
        self.assertEqual(self.client.post('/api/video-jobs', json={'url': URL}).status_code, 409)
        self.assertEqual(self.client.post('/api/jobs', json={'keywords': ['cats']}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/cancel").status_code, 200)

    @patch('imagesearch.web.threading.Thread')
    def test_image_job_also_blocks_video(self, worker):
        self.client.post('/api/jobs', json={'keywords': ['cats']})
        self.assertEqual(self.client.post('/api/video-jobs', json={'url': URL}).status_code, 409)

    def test_validation_and_origin_guard(self):
        self.assertEqual(self.client.post('/api/video-jobs', json={'url': 'http://localhost'}).status_code, 422)
        self.assertEqual(self.client.post('/api/video-jobs', json={'url': URL},
                                         headers={'Origin': 'https://evil.example'}).status_code, 403)

    @patch('imagesearch.video.YoutubeDL')
    @patch('imagesearch.web.threading.Thread')
    def test_download_file_and_block_unlisted_files(self, worker, factory):
        def extract(*args, **kwargs):
            folder = Path(factory.call_args.args[0]['outtmpl']).parent
            (folder / 'video.mp4').write_bytes(b'video data')
            (folder / 'secret.txt').write_text('private')
            (folder / 'unfinished.mp4.part').write_bytes(b'partial')
            return {'title': 'Test'}
        factory.return_value.__enter__.return_value.extract_info.side_effect = extract
        job = self.client.post('/api/video-jobs', json={'url': URL}).json()
        worker.call_args.kwargs['target']()
        base = f"/api/jobs/{job['id']}/files/"
        response = self.client.get(base + 'video.mp4')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'video data')
        self.assertIn('attachment', response.headers['content-disposition'])
        for name in ['secret.txt', 'unfinished.mp4.part', 'sources.csv', '../secret.txt']:
            self.assertEqual(self.client.get(base + name).status_code, 404)
        self.assertEqual(self.client.get(base + 'manifest.json').json()['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
