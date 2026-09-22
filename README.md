# MediaFetch 🚀

A modular Telegram public-media downloader built with Python, FastAPI, Telegram Bot API, yt-dlp and FFmpeg.

## Feature set

### Phase 1 — Core downloader
- Public URL detection for YouTube, Instagram, Facebook, Reddit, X/Twitter, TikTok, Pinterest and Threads.
- Best available video/audio download.
- Dynamic quality buttons based on extracted video heights.
- 2160p / 1440p / 1080p / 720p / 480p / 360p when available.
- MP3 extraction through FFmpeg.
- Highest-resolution photo download.
- Multi-photo/carousel handling with a configurable item limit.
- HD photos are sent as Telegram documents so the original downloaded bytes are preserved.
- Progress updates, upload status and clear failures.
- Per-user active-download protection.
- Global concurrency control.
- Per-user rate limiting.
- File-size enforcement and temporary-file cleanup.

### Phase 2 — Reliability and UX
- Metadata inspection before download: title, duration, uploader, available resolutions and carousel count.
- Supported-platform validation before yt-dlp work.
- Retry settings for transient extractor/download failures.
- Koyeb-compatible FastAPI webhook mode.
- /health endpoint.
- Docker image with FFmpeg and Deno for current yt-dlp JavaScript challenge solving.
- Automated compile/test and Docker-build CI.

### Phase 3 — Service layer
- Telegram file_id cache to avoid re-downloading identical URL + quality requests.
- Configurable cache TTL.
- Optional MongoDB persistence for cache, users, usage and analytics.
- In-memory fallback when MongoDB is not configured.
- Free and premium daily usage limits.
- Admin statistics.
- Admin-granted premium access.
- Maintenance mode.
- Reply-based broadcast.
- Download success/failure/cache-hit analytics.

## Commands

User commands:
- /start
- /help
- /supported
- /about
- /premium

Admin commands (only IDs in ADMIN_IDS):
- /admin
- /premium_grant USER_ID DAYS
- /revoke USER_ID
- /maintenance on|off
- /broadcast — reply to the message that should be broadcast.

## Supported platforms

| Platform | URL examples | Notes |
|---|---|---|
| YouTube | youtube.com, youtu.be | Deno/EJS enabled; some videos may still require access tokens or authentication. |
| Instagram | instagram.com | Public media when yt-dlp can access it. |
| Facebook | facebook.com, fb.watch | Public media where extractable. |
| Reddit | reddit.com, redd.it | Public media where available. |
| X/Twitter | x.com, twitter.com | Public media where extractable. |
| TikTok | tiktok.com, vm.tiktok.com | Public media where available. |
| Pinterest | pinterest.com, pin.it | Pins with downloadable media. |
| Threads | threads.net, threads.com | Public media via the Threads extractor plugin. |

Platform behavior can change. A supported domain does not guarantee that every URL on every platform will work.

## Configuration

Copy .env.example to .env.

| Variable | Default | Purpose |
|---|---:|---|
| BOT_TOKEN | — | Telegram BotFather token |
| DOWNLOAD_DIR | /tmp/mediafetch | Temporary download directory |
| MAX_FILE_MB | 50 | Telegram upload safety limit |
| MAX_CONCURRENT_DOWNLOADS | 1 | Global downloader concurrency |
| MONGODB_URI | empty | Optional MongoDB connection |
| MONGODB_DB | mediafetch | MongoDB database name |
| ADMIN_IDS | empty | Comma-separated Telegram admin IDs |
| FREE_DAILY_LIMIT | 10 | Free successful requests/day/user |
| PREMIUM_DAILY_LIMIT | 100 | Premium successful requests/day/user |
| PREMIUM_MAX_FILE_MB | 50 | Premium upload limit (capped by Telegram's current 50 MB cloud Bot API upload limit) |
| CACHE_TTL_DAYS | 7 | Telegram file-id cache lifetime |
| MAX_CAROUSEL_ITEMS | 10 | Maximum photos handled per post |
| WEBHOOK_MODE | false | Enable Telegram webhook mode |
| WEBHOOK_SECRET | empty | Generated automatically when empty |
| PUBLIC_BASE_URL | empty | Optional explicit webhook base URL |

## Koyeb deployment

Recommended:
- Service type: Web Service
- Repository: Moviesuploader/MediaFetch
- Branch: main
- Build: Dockerfile
- Region: Frankfurt or Washington, D.C.
- Exposed port: 8000 / HTTP
- Health check: GET /health
- WEBHOOK_MODE=true
- MAX_CONCURRENT_DOWNLOADS=1

Koyeb exposes KOYEB_PUBLIC_DOMAIN, which MediaFetch uses automatically for the Telegram webhook.

MongoDB is optional. For a persistent production cache, usage limits and analytics, configure MONGODB_URI.

## Local setup

Install Python 3.12+ and FFmpeg:

~~~bash
pip install -r requirements.txt
python run.py
~~~

Local development defaults to Telegram long polling. Use webhook mode only with a public HTTPS endpoint.

## Tests

~~~bash
pip install -r requirements-dev.txt
python -m compileall -q app run.py
pytest
~~~

CI also builds the Docker image.

## Downloader reliability

yt-dlp supports format selection and metadata extraction through its Python API. Media availability depends on the extractor and source. YouTube in particular may enforce PO-token or authentication requirements that cannot be solved by MediaFetch alone.

## Important

MediaFetch is intended for content the user is authorized to download. Private, DRM-protected, login-required or otherwise restricted content may not be downloadable, and platform terms and applicable laws still apply.
