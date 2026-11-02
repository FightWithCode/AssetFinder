from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import re
import socket
import threading
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import requests
from ddgs import DDGS
from PIL import Image
from pydantic import BaseModel, Field, field_validator


class SearchOptions(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=100)
    count: int = Field(default=20, ge=1, le=500)
    engine: Literal['auto', 'bing', 'duckduckgo'] = 'auto'
    license: Literal['all', 'any', 'Public', 'Share', 'ShareCommercially',
                     'Modify', 'ModifyCommercially'] = 'all'
    safesearch: Literal['on', 'moderate', 'off'] = 'moderate'
    min_width: int = Field(default=0, ge=0, le=20000)
    min_height: int = Field(default=0, ge=0, le=20000)
    max_mb: int = Field(default=20, ge=1, le=100)
    collection: str = Field(default='collection', max_length=100)

    @field_validator('keywords')
    @classmethod
    def clean_keywords(cls, value):
        result = list(dict.fromkeys(word.strip() for word in value if word.strip()))
        if not result:
            raise ValueError('Enter at least one keyword.')
        if any(len(word) > 300 for word in result):
            raise ValueError('Each keyword must be at most 300 characters.')
        return result


def slug(value: str) -> str:
    value = re.sub(r'[^\w -]', '_', value, flags=re.UNICODE)
    value = re.sub(r'[\s_]+', '_', value).strip(' ._-')[:70] or 'images'
    # A prefix also avoids Windows device names such as CON and NUL.
    return 'set_' + value


def public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only public HTTP(S) image URLs are supported.')
    if parsed.port and parsed.port not in (80, 443):
        raise ValueError('Nonstandard image server port.')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Private or local network image URLs are blocked.')


class Cancelled(Exception):
    pass


def fetch_image(url: str, options: SearchOptions, cancel: threading.Event):
    """Fetch bounded raster data; inspect actual bytes instead of trusting extensions."""
    start = time.monotonic()
    max_bytes = options.max_mb * 1024 * 1024
    with requests.Session() as session:
        session.headers['User-Agent'] = 'ImageSearch/1.0 (local image downloader)'
        for _ in range(6):
            if cancel.is_set():
                raise Cancelled()
            public_url(url)
            with session.get(url, stream=True, timeout=(6, 10), allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers['Location'])
                    continue
                response.raise_for_status()
                if int(response.headers.get('Content-Length', 0)) > max_bytes:
                    raise ValueError('Image exceeds file size limit.')
                data = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if cancel.is_set():
                        raise Cancelled()
                    if time.monotonic() - start > 40:
                        raise ValueError('Image download exceeded 40 seconds.')
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError('Image exceeds file size limit.')
                break
        else:
            raise ValueError('Too many redirects.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            extension = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif',
                         'BMP': 'bmp', 'TIFF': 'tiff', 'AVIF': 'avif'}.get(image.format)
            if not extension:
                raise ValueError('Unsupported image format.')
            image.verify()
        # Verify pixel decoding as well: some truncated JPEGs pass verify().
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    if width < options.min_width or height < options.min_height:
        raise ValueError('Image is below minimum dimensions.')
    return bytes(data), extension, width, height, url


def search_images(keyword: str, options: SearchOptions, page: int):
    # None means unrestricted; "any" means Creative Commons in DDGS.
    # DDGS 9.16's Bing backend ignores license_image. Never silently drop it.
    backend = 'duckduckgo' if options.license != 'all' else options.engine
    return DDGS(timeout=15).images(
        keyword, backend=backend, safesearch=options.safesearch,
        license_image=None if options.license == 'all' else options.license,
        max_results=min(max(options.count * 3, 30), 300), page=page,
    )


class DownloadJob:
    def __init__(self, job_id: str, options: SearchOptions, output_root: Path):
        self.id = job_id
        self.options = options
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.folder = output_root / f'{slug(options.collection)}_{stamp}_{job_id[:8]}'
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.state = {
            'id': job_id, 'status': 'queued', 'folder': str(self.folder.resolve()),
            'target': len(options.keywords) * options.count, 'downloaded': 0,
            'skipped': 0, 'current_keyword': '', 'message': 'Waiting to start',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'keywords': [{'keyword': k, 'downloaded': 0, 'target': options.count, 'status': 'queued'}
                         for k in options.keywords],
            'images': [], 'errors': [], 'options': options.model_dump(),
        }

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def record_error(self, keyword, message, url=''):
        with self.lock:
            self.state['skipped'] += 1
            self.state['errors'].append({'keyword': keyword, 'url': url, 'message': str(message)[:500]})

    def save(self):
        snapshot = self.snapshot()
        temp = self.folder / 'manifest.tmp'
        temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.folder / 'manifest.json')
        fields = ['keyword', 'file', 'title', 'image_url', 'resolved_url', 'source_url',
                  'engine', 'source_label', 'width', 'height', 'sha256', 'license', 'license_filter']
        with (self.folder / 'sources.csv').open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            # Prefix spreadsheet formula characters in third-party metadata.
            for item in snapshot['images']:
                writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value)
                                 for key, value in item.items() if key in fields})

    def run(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=False)
            self.update(status='running', message='Searching for images')
            for index, keyword in enumerate(self.options.keywords):
                if self.cancel.is_set():
                    break
                self.update(current_keyword=keyword, message=f'Searching: {keyword}')
                with self.lock:
                    self.state['keywords'][index]['status'] = 'running'
                target_folder = self.folder / f'{index + 1:03d}_{slug(keyword)}'
                target_folder.mkdir()
                hashes, seen_urls = set(), set()
                # Fetch extra pages when duplicates, tiny images, and unavailable hosts
                # leave a shortfall. Bounded to avoid infinite requests.
                for page in range(1, 11):
                    if self.cancel.is_set() or len(hashes) >= self.options.count:
                        break
                    try:
                        results = search_images(keyword, self.options, page)
                    except Exception as exc:
                        self.record_error(keyword, f'Search failed: {exc}')
                        break
                    fresh = 0
                    for result in results:
                        if self.cancel.is_set() or len(hashes) >= self.options.count:
                            break
                        url = result.get('image', '')
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        fresh += 1
                        self.update(message=f'Downloading: {keyword} ({len(hashes)}/{self.options.count})')
                        try:
                            data, extension, width, height, resolved = fetch_image(url, self.options, self.cancel)
                            digest = hashlib.sha256(data).hexdigest()
                            if digest in hashes:
                                self.record_error(keyword, 'Duplicate image content', url)
                                continue
                            file = target_folder / f'{len(hashes) + 1:04d}_{digest[:12]}.{extension}'
                            file.write_bytes(data)
                            hashes.add(digest)
                            record = {
                                'keyword': keyword, 'file': file.relative_to(self.folder).as_posix(),
                                'title': result.get('title', ''), 'image_url': url,
                                'resolved_url': resolved, 'source_url': result.get('url', ''),
                                'engine': 'duckduckgo' if self.options.license != 'all' else self.options.engine,
                                'source_label': result.get('source', ''),
                                'width': width, 'height': height, 'sha256': digest,
                                'license': result.get('license') or 'Unknown; check source',
                                'license_filter': self.options.license,
                            }
                            with self.lock:
                                self.state['images'].append(record)
                                self.state['downloaded'] += 1
                                self.state['keywords'][index]['downloaded'] = len(hashes)
                            self.save()
                        except Cancelled:
                            break
                        except Exception as exc:
                            self.record_error(keyword, exc, url)
                    if not fresh:
                        break
                with self.lock:
                    self.state['keywords'][index]['status'] = (
                        'cancelled' if self.cancel.is_set() else
                        'complete' if len(hashes) == self.options.count else 'partial')
                self.save()
            snapshot = self.snapshot()
            if self.cancel.is_set():
                self.update(status='cancelled', message='Cancelled. Downloaded files have been kept.')
                with self.lock:
                    for item in self.state['keywords']:
                        if item['status'] == 'queued':
                            item['status'] = 'cancelled'
            elif snapshot['downloaded'] == snapshot['target']:
                self.update(status='complete', message='All requested images downloaded.')
            else:
                self.update(status='partial', message='Search finished with fewer images than requested. See errors and sources for details.')
        except Exception as exc:
            self.update(status='failed', message=str(exc))
        finally:
            self.update(finished_at=datetime.now(timezone.utc).isoformat())
            if self.folder.is_dir():
                try:
                    self.save()
                except OSError as exc:
                    self.update(status='failed', message=f'Could not write manifest: {exc}')
