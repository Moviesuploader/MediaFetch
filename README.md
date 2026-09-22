# MediaFetch 🚀

A modular Telegram media downloader built with Python, FastAPI, Telegram Bot API, yt-dlp, and FFmpeg.

## Current milestone

1. User sends a public media URL.
2. MediaFetch detects the platform.
3. User chooses Best / 720p / 480p / MP3.
4. yt-dlp downloads the media.
5. Progress is shown in Telegram.
6. The file is sent back to the user.
7. Temporary files are cleaned up.

Current platform detection covers YouTube, Instagram, Facebook, Reddit, X/Twitter, TikTok, Pinterest, and Threads. Actual download support depends on yt-dlp and the target platform.

## Reliability and deployment features

- Per-user request rate limiting
- One active download per user
- Global download concurrency limit
- Configurable Telegram upload-size limit
- 30-second downloader socket timeout
- Temporary-file cleanup
- Docker image with FFmpeg
- `/health` endpoint
- Platform-provided `PORT` support
- Telegram webhook mode for Koyeb scale-to-zero
- Automated compile/test CI

## Koyeb deployment

Koyeb Free provides 512 MB RAM, 0.1 vCPU and 2 GB SSD. Its Free Instance is a Web Service and automatically scales to zero after one hour without incoming traffic. MediaFetch therefore supports Telegram webhook mode for Koyeb instead of relying on long polling.

Recommended Koyeb configuration:

- Service type: Web Service
- Deployment: GitHub repository `Moviesuploader/MediaFetch`, branch `main`
- Build: repository Dockerfile
- Region: Frankfurt or Washington, D.C.
- Instance: Free
- Exposed port: 8000 / HTTP
- Health check: HTTP GET `/health`
- `MAX_CONCURRENT_DOWNLOADS=1`

Environment variables:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Telegram BotFather token |
| `WEBHOOK_MODE` | `true` |
| `MAX_CONCURRENT_DOWNLOADS` | `1` |
| `DOWNLOAD_DIR` | `/tmp/mediafetch` |
| `MAX_FILE_MB` | `50` |
| `WEBHOOK_SECRET` | leave empty; generated automatically |
| `PUBLIC_BASE_URL` | leave empty; Koyeb domain is detected automatically |

Koyeb exposes `KOYEB_PUBLIC_DOMAIN`; MediaFetch automatically uses it to register the Telegram webhook at `/telegram/webhook`.

## Local setup

Install Python 3.12+ and FFmpeg, create a Telegram bot with BotFather, copy `.env.example` to `.env`, set `BOT_TOKEN`, then run:

```bash
pip install -r requirements.txt
python run.py
```

Local development defaults to Telegram long polling. Set `WEBHOOK_MODE=true` only when a public HTTPS endpoint is available.

Health check: `GET /health`

Tests:

```bash
pip install -r requirements-dev.txt
python -m compileall -q app run.py
pytest
```

## Docker

The Dockerfile installs FFmpeg and starts MediaFetch with `python run.py`. The application listens on port 8000 by default and honors the platform `PORT` variable.

## Resource note

Koyeb Free has limited CPU/RAM/storage. Large media files and FFmpeg conversions may be slow or fail under the 512 MB / 0.1 vCPU limits. `/tmp/mediafetch` is ephemeral and should not be treated as permanent storage.

## Important

MediaFetch is intended for content the user is authorized to download. Private, DRM-protected, login-required, or otherwise restricted content may not be downloadable, and platform terms and applicable laws still apply.
