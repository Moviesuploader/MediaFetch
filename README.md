# MediaFetch 🚀

A modular Telegram media downloader built with **Python, FastAPI, Telegram Bot API, yt-dlp, and FFmpeg**.

## Current milestone

The bot currently supports this flow:

1. User sends a public media URL.
2. MediaFetch detects the platform.
3. User chooses **Best / 720p / 480p / MP3**.
4. yt-dlp downloads the media.
5. Progress is shown in Telegram.
6. The file is sent back to the user.
7. Temporary files are cleaned up.

Current platform detection covers **YouTube, Instagram, Facebook, Reddit, X/Twitter, TikTok, Pinterest, and Threads**. Actual download support depends on yt-dlp and the target platform.

### Reliability and protection already included

- Per-user request rate limiting
- One active download per user
- Global download concurrency limit
- Configurable Telegram upload-size limit
- 30-second downloader socket timeout
- Temporary-file cleanup
- Docker image with FFmpeg
- `/health` endpoint for deployment health checks
- Automated compile/test CI

## Project structure

```
MediaFetch/
├── app/
│   ├── api/
│   │   └── main.py
│   ├── bot/
│   │   ├── application.py
│   │   └── handlers.py
│   ├── core/
│   │   ├── config.py
│   │   └── rate_limit.py
│   └── downloader/
│       ├── detector.py
│       └── service.py
├── tests/
├── .env.example
├── .dockerignore
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt
└── run.py
```

## Local setup

1. Install **Python 3.12+** and **FFmpeg**.
2. Create a Telegram bot with BotFather and obtain its token.
3. Copy `.env.example` to `.env`.
4. Set `BOT_TOKEN`.
5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Start the application:

```bash
python run.py
```

Health check: `GET /health`

For development/testing:

```bash
pip install -r requirements-dev.txt
python -m compileall -q app run.py
pytest
```

## Environment

| Variable | Required | Default |
|---|---|---|
| `BOT_TOKEN` | Yes | — |
| `DOWNLOAD_DIR` | No | `/tmp/mediafetch` |
| `MAX_FILE_MB` | No | `50` |
| `MAX_CONCURRENT_DOWNLOADS` | No | `2` |

## Docker

The included Dockerfile installs FFmpeg and starts MediaFetch with `python run.py`.

The service listens on port **8000**. Set the deployment platform's health check to `/health` and provide `BOT_TOKEN` as a secret/environment variable.

## Roadmap

### Next
- Real Telegram end-to-end testing
- Better media metadata/type detection
- Stronger error classification and user-facing messages
- More automated downloader tests

### Later
- MongoDB
- Download history
- Telegram `file_id` caching
- Admin controls
- Premium subscriptions
- Web dashboard
- Object storage for larger media
- Public API
- Monitoring and analytics

## Important

MediaFetch is intended for content the user is authorized to download. Private, DRM-protected, login-required, or otherwise restricted content may not be downloadable, and platform terms and applicable laws still apply.
