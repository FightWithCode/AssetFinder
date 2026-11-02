import argparse
import json
import sys
import threading
import uuid
from pathlib import Path

from .core import DownloadJob, SearchOptions
from .video import VideoJob, VideoOptions


def main():
    parser = argparse.ArgumentParser(description='Search for images or download YouTube videos.')
    sub = parser.add_subparsers(dest='command')
    serve = sub.add_parser('serve', help='Start the local web app (default command).')
    serve.add_argument('--port', type=int, default=8000)
    serve.add_argument('--output', type=Path, default=Path('downloads'))
    download = sub.add_parser('download', help='Download directly from the command line.')
    download.add_argument('keywords', nargs='*', help='Quote phrases, e.g. "red pandas"')
    download.add_argument('--file', type=Path, help='UTF-8 text file, one keyword per line.')
    download.add_argument('--count', type=int, default=20, help='Images per keyword (1–500).')
    download.add_argument('--engine', choices=['auto', 'bing', 'duckduckgo'], default='auto')
    download.add_argument('--license', default='all', choices=['all', 'any', 'Public', 'Share', 'ShareCommercially', 'Modify', 'ModifyCommercially'])
    download.add_argument('--safe-search', choices=['on', 'moderate', 'off'], default='moderate')
    download.add_argument('--min-width', type=int, default=0)
    download.add_argument('--min-height', type=int, default=0)
    download.add_argument('--max-mb', type=int, default=20)
    download.add_argument('--collection', default='collection')
    download.add_argument('--output', type=Path, default=Path('downloads'))
    video = sub.add_parser('video', help='Download a single YouTube video from its link.')
    video.add_argument('url', help='YouTube watch, Shorts, or youtu.be link.')
    video.add_argument('--quality', choices=['best', '1080', '720', '480'], default='best')
    video.add_argument('--output', type=Path, default=Path('downloads'))
    args = parser.parse_args()
    if args.command not in ('download', 'video'):
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(getattr(args, 'output', Path('downloads'))), host='127.0.0.1', port=getattr(args, 'port', 8000))
        return
    try:
        if args.command == 'video':
            options = VideoOptions(url=args.url, quality=args.quality)
        else:
            keywords = args.keywords
            if args.file:
                keywords += args.file.read_text(encoding='utf-8-sig').splitlines()
            options = SearchOptions(keywords=keywords, count=args.count, engine=args.engine,
                                    license=args.license, safesearch=args.safe_search,
                                    min_width=args.min_width, min_height=args.min_height,
                                    max_mb=args.max_mb, collection=args.collection)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    job_type = VideoJob if args.command == 'video' else DownloadJob
    job = job_type(uuid.uuid4().hex, options, args.output.resolve())
    worker = threading.Thread(target=job.run, daemon=True)
    worker.start()
    previous = ''
    try:
        while worker.is_alive():
            snapshot = job.snapshot()
            message = f"{snapshot['downloaded']}/{snapshot['target']} | {snapshot['message']}"
            if message != previous:
                print(message, flush=True)
                previous = message
            worker.join(0.5)
    except KeyboardInterrupt:
        print('\nStopping; keeping downloaded files and saving the manifest...')
        job.cancel.set()
        worker.join()
    snapshot = job.snapshot()
    print(json.dumps({key: snapshot[key] for key in ('status', 'downloaded', 'target', 'folder', 'message')}, indent=2))
    sys.exit(0 if snapshot['status'] == 'complete' else 130 if snapshot['status'] == 'cancelled' else 2)


if __name__ == '__main__':
    main()
