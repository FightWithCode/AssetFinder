from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, field_validator
from yt_dlp import YoutubeDL

from .core import Cancelled


class VideoOptions(BaseModel):
    url: str = Field(max_length=2048)
    quality: Literal['best', '1080', '720', '480'] = 'best'

    @field_validator('url')
    @classmethod
    def youtube_url(cls, value):
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)):
            raise ValueError('Enter a YouTube video link.')
        host = (parsed.hostname or '').lower()
        parts = parsed.path.strip('/').split('/')
        video_id = ''
        if host == 'youtu.be' and len(parts) == 1:
            video_id = parts[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if parsed.path == '/watch':
                video_id = parse_qs(parsed.query).get('v', [''])[0]
            elif len(parts) == 2 and parts[0] in ('shorts', 'embed', 'live'):
                video_id = parts[1]
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError('Enter a single YouTube video link (watch, Shorts, or youtu.be).')
        # Canonicalize to a single video; discard playlist and other URL options.
        return f'https://www.youtube.com/watch?v={video_id}'


class VideoJob:
    def __init__(self, job_id: str, options: VideoOptions, output_root: Path):
        self.id = job_id
        self.options = options
        self.folder = output_root / f'video_{datetime.now():%Y%m%d_%H%M%S}_{job_id[:8]}'
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.state = {
            'id': job_id, 'kind': 'video', 'status': 'queued',
            'folder': str(self.folder.resolve()), 'target': 1, 'downloaded': 0,
            'message': 'Waiting to start', 'progress': 0,
            'downloaded_bytes': 0, 'total_bytes': None,
            'images': [], 'videos': [], 'keywords': [], 'errors': [],
            'options': options.model_dump(),
            'created_at': datetime.now(timezone.utc).isoformat(),
        }

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def progress(self, data):
        self.check_cancel()
        downloaded = data.get('downloaded_bytes', 0)
        total = data.get('total_bytes') or data.get('total_bytes_estimate')
        title = data.get('info_dict', {}).get('title') or 'video'
        self.update(downloaded_bytes=downloaded, total_bytes=total,
                    progress=min(99, downloaded / total * 100) if total else 0,
                    message=f'Downloading: {title}' if data['status'] == 'downloading'
                    else 'Processing downloaded video…')

    def postprocess(self, data):
        self.check_cancel()
        self.update(message='Processing downloaded video…')

    def filter_video(self, info, *, incomplete=False):
        self.check_cancel()
        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
            return 'Live streams and upcoming videos are not supported. Try after the stream ends.'

    def run(self):
        try:
            self.check_cancel()
            self.folder.mkdir(parents=True, exist_ok=False)
            self.update(status='running', message='Fetching YouTube video information…')
            cap = '' if self.options.quality == 'best' else f'[height<={self.options.quality}]'
            ffmpeg = bool(shutil.which('ffmpeg'))
            selector = f'bv*{cap}+ba/b{cap}' if ffmpeg else f'b{cap}'
            settings = {
                'format': selector, 'noplaylist': True,
                'outtmpl': str(self.folder / '%(title).140B [%(id)s].%(ext)s'),
                'windowsfilenames': True, 'quiet': True, 'noprogress': True,
                'js_runtimes': {name: {} for name in ('deno', 'node') if shutil.which(name)},
                'socket_timeout': 15, 'retries': 3, 'fragment_retries': 3,
                'progress_hooks': [self.progress], 'postprocessor_hooks': [self.postprocess],
                'match_filter': self.filter_video,
                'merge_output_format': 'mp4/mkv',
            }
            with YoutubeDL(settings) as downloader:
                info = downloader.extract_info(self.options.url, download=True)
                self.check_cancel()
                if not info or info.get('_type') in ('playlist', 'multi_video'):
                    raise ValueError('The link did not return a single downloadable video.')
                # The final extension can change after merging or remuxing.
                files = [file for file in self.folder.iterdir()
                         if file.is_file() and file.suffix.lower() in ('.mp4', '.webm', '.mkv', '.3gp', '.mov', '.flv')]
                if len(files) != 1:
                    raise ValueError('No finished video file was produced. The video may be unavailable or live.')
                file = files[0]
                self.update(videos=[{'file': file.name, 'title': info.get('title') or file.stem,
                                     'source_url': self.options.url, 'size': file.stat().st_size}],
                            downloaded=1, progress=100, status='complete',
                            message='Video downloaded.' if ffmpeg else
                            'Video downloaded using a combined audio/video format. Install FFmpeg for higher resolutions.')
        except Exception as exc:
            if self.cancel.is_set() or isinstance(exc, Cancelled):
                self.update(status='cancelled', message='Cancelled. Incomplete files remain in the output folder.')
            else:
                message = str(exc)[:1500]
                self.update(status='failed', message=message,
                            errors=[{'keyword': 'YouTube', 'message': message}])
        finally:
            self.update(finished_at=datetime.now(timezone.utc).isoformat())
            if self.folder.is_dir():
                try:
                    temp = self.folder / 'manifest.tmp'
                    temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding='utf-8')
                    temp.replace(self.folder / 'manifest.json')
                except OSError as exc:
                    self.update(status='failed', message=f'Could not write manifest: {exc}')
