# ImageSearch

A local web app and CLI that search Bing / DuckDuckGo for images and download a requested number **per keyword**, in a separate folder for each keyword. Also download individual YouTube videos from a link. No search API key required.

## Start on Windows

Requires Python 3.10 or newer. Double-click **start.bat**, then open **http://127.0.0.1:8000**. The first start installs dependencies. Keep the terminal open while downloading; press Ctrl+C to stop the server.

Or use PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m imagesearch serve
```

Enter one keyword / phrase per line (or import a `.txt` file), choose the count and search engine, and click **Start collecting**. The page shows progress, image previews, per-keyword counts, failures, and the saved folder. **Open folder** opens Windows Explorer.

Set a different download destination or port:

```powershell
.\.venv\Scripts\python.exe -m imagesearch serve --output "D:\Pictures\Collections" --port 8080
```

The server binds to `127.0.0.1` and handles one collection at a time. Current job state survives page refreshes, but the in-memory job list resets when the server restarts. Existing files and manifests remain on disk. A stopped server does not automatically resume unfinished jobs.

## Command line

```powershell
.\.venv\Scripts\python.exe -m imagesearch download "red pandas" "coastal landscapes" --count 25
.\.venv\Scripts\python.exe -m imagesearch download --file keywords.example.txt --count 10 --engine bing
.\.venv\Scripts\python.exe -m imagesearch download "botanical illustration" --count 30 --license Public --min-width 800 --output "D:\Pictures"
```

Run `python -m imagesearch download --help` for every option. Ctrl+C requests cancellation and keeps downloaded files. CLI exit codes: `0` complete, `2` partial/failed, `130` cancelled.

## YouTube downloads

In the web app, paste a link into **YouTube download**, select a quality limit, and click **Download video**. Watch, Shorts, and `youtu.be` links are accepted. A link containing a playlist downloads only the selected video. Progress, cancellation, the output folder, and a **Save video** link appear in the results panel. Images and videos share the same single-job limit.

From the command line:

```powershell
.\.venv\Scripts\python.exe -m imagesearch video "https://www.youtube.com/watch?v=jNQXAC9IVRw" --quality 720
```

Videos and a source manifest are saved in a unique `downloads/video_<timestamp>_<id>/` folder. The quality option is an upper limit; the actual resolution and container depend on available formats. With the **FFmpeg executable on PATH**, the app can merge separate video and audio streams for higher resolutions. Without FFmpeg, it selects a combined audio/video format; some videos or quality limits may have none available.

YouTube extraction uses [yt-dlp](https://github.com/yt-dlp/yt-dlp#dependencies). Install a supported JavaScript runtime (Deno is recommended by yt-dlp; Node.js is also supported) for YouTube's player challenges. Restart the app after changing PATH. The Python dependencies include yt-dlp's default extras and challenge scripts. Update them when YouTube changes its player:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade "yt-dlp[default]"
```

Only download videos you have permission to save. Playlists, active/upcoming live streams, and login/DRM bypass are not supported. YouTube may block requests or require sign-in; the app reports extraction and download failures. Cancellation takes effect at the next download or processing hook, and incomplete files may remain in the output folder.

## What gets saved

```text
downloads/
  set_My_collection_20261101_120000_a1b2c3d4/
    001_set_red_pandas/
      0001_<hash>.jpg
      0002_<hash>.png
    002_set_coastal_landscapes/
      ...
    manifest.json
    sources.csv
```

Each collection gets a unique directory. Original raster files are retained without resizing or watermark removal. Images are verified, checked against minimum dimensions and file-size limits, and deduplicated by SHA-256 **within each keyword**. Identical images may occur under different keywords. The manifest and CSV record source pages, original and resolved image URLs, dimensions, hashes, engine labels, and any reported license. Unknown licenses remain unknown. The manifest also includes options, per-keyword outcomes, and failure details, and is updated during the run.

## Search and usage rights

**All images / `--license all` is the default**: no license restriction is applied, so copyrighted images may be included. Optional filters include Creative Commons, public domain, and commercial reuse. Filtered searches always use DuckDuckGo, including when `--engine bing` is supplied, because this version of DDGS ignores license filters on Bing. These are search-engine filters, not verified license grants; check the original source before reuse. DDGS calls its Creative Commons filter `any`; this is different from unrestricted `all` in this app.

Search uses [DDGS](https://github.com/deedy5/ddgs), whose image backends are currently Bing and DuckDuckGo. Automatic mode lets DDGS select available backends. Google image search is not implemented. Search results reflect the keyword query; the app does not perform reverse-image search or visual similarity ranking.

Search engines may rate-limit requests, return few results, or change their interfaces. Image hosts may block downloads or return expired URLs. The app tries up to 10 result pages per keyword to make up for failures and duplicates, but cannot guarantee the requested number. A **partial** result is explicit. A failed keyword does not stop later keywords. No login, paywall, CAPTCHA, or access-control bypass is implemented.

Supported image formats: JPEG, PNG, WebP, GIF, BMP, TIFF and AVIF (when supported by Pillow). HTML responses, SVG, corrupt files, oversized files, private-network URLs and tiny images below your configured threshold are skipped. Safe search defaults to Moderate and is adjustable for DuckDuckGo (which treats On and Moderate alike). Bing uses its provider defaults because DDGS does not forward that setting to Bing. Choose DuckDuckGo explicitly when you need the setting applied. Cancellation can take until the current search or network read finishes. The manifest's `engine` records the selected backend (`auto` when automatic selection was used); `source_label` retains the provider's result label, which may be a website name.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use simulated search results and HTTP responses, so they do not depend on live search-engine availability. For a live check, run the CLI with a small count. Live downloads depend on your network and the upstream hosts.

`requirements.lock.txt` captures the exact environment used to verify this project; install it instead of `requirements.txt` if you need those versions.
