from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import DownloadJob, SearchOptions
from .video import VideoJob, VideoOptions

STATIC = Path(__file__).parent / 'static'


def create_app(output_root: Path | None = None):
    root = (output_root or Path(__file__).resolve().parent.parent / 'downloads').resolve()
    app = FastAPI(title='ImageSearch', docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    jobs: dict[str, DownloadJob | VideoJob] = {}
    jobs_lock = threading.Lock()

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        # Reject cross-origin writes to the local downloader.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD') and origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin requests are not allowed.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def find(job_id):
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, 'Job not found in this server session.')
        return job

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/config')
    def config():
        return {'output_root': str(root)}

    @app.get('/api/jobs')
    def list_jobs():
        with jobs_lock:
            return [dict(id=job.id, status=job.snapshot()['status']) for job in reversed(list(jobs.values()))]

    def start_job(options, job_type):
        with jobs_lock:
            if any(job.snapshot()['status'] in ('queued', 'running') for job in jobs.values()):
                raise HTTPException(409, 'A download is already running. Cancel it or wait for it to finish.')
            job = job_type(uuid.uuid4().hex, options, root)
            jobs[job.id] = job
            threading.Thread(target=job.run, daemon=True).start()
        return job.snapshot()

    @app.post('/api/jobs', status_code=202)
    def start(options: SearchOptions):
        return start_job(options, DownloadJob)

    @app.post('/api/video-jobs', status_code=202)
    def start_video(options: VideoOptions):
        return start_job(options, VideoJob)

    @app.get('/api/jobs/{job_id}')
    def status(job_id: str):
        return find(job_id).snapshot()

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id: str):
        job = find(job_id)
        job.cancel.set()
        return {'message': 'Cancellation requested. The current search or network read may take a few seconds to finish.'}

    @app.get('/api/jobs/{job_id}/files/{name:path}')
    def image(job_id: str, name: str):
        job = find(job_id)
        snapshot = job.snapshot()
        allowed = {item['file'] for item in snapshot['images'] + snapshot.get('videos', [])} | {'manifest.json'}
        if snapshot.get('kind') != 'video':
            allowed.add('sources.csv')
        file = (job.folder / name).resolve()
        if name not in allowed or not file.is_relative_to(job.folder.resolve()) or not file.is_file():
            raise HTTPException(404, 'File not found.')
        return FileResponse(file, filename=file.name) if snapshot.get('kind') == 'video' else FileResponse(file)

    @app.post('/api/jobs/{job_id}/open-folder')
    def open_folder(job_id: str):
        folder = find(job_id).folder
        if not folder.is_dir():
            raise HTTPException(404, 'The output folder does not exist yet.')
        if os.name != 'nt':
            raise HTTPException(400, f'Open this path in your file manager: {folder}')
        os.startfile(str(folder.resolve()))
        return {'message': 'Folder opened'}

    return app
