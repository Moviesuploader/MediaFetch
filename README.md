# MediaFetch 🚀

A modular Telegram media downloader built with Python, FastAPI, Telegram Bot API, and yt-dlp.

## Phase 1

The first milestone is intentionally small and testable:

- Telegram `/start`
- Accept a public media URL
- Detect the platform
- Download through yt-dlp
- Return the downloaded file to Telegram
- HTTP `/health` endpoint for deployment platforms
- Docker image with FFmpeg
- Environment-based configuration

### Currently detected platforms

YouTube, Instagram, Facebook, Reddit, X/Twitter, TikTok, Pinterest, and Threads.

Detection is informational; the actual downloader capability depends on yt-dlp and the target platform.

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
│   │   └── config.py
│   └── downloader/
│       ├── detector.py
│       └── service.py
├── .env.example
├── Dockerfile
├── requirements.txt
└── run.py
```

## Local setup

1. Install Python 3.12+ and FFmpeg.
2. Create a Telegram bot with BotFather and obtain its token.
3. Copy `.env.example` to `.env`.
4. Set `BOT_TOKEN`.
5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Start:

```bash
python run.py
```

Health check:

```
GET /health
```

## Environment

| Variable | Required | Default |
|---|---|---|
| `BOT_TOKEN` | Yes | — |
| `DOWNLOAD_DIR` | No | `/tmp/mediafetch` |
| `MAX_FILE_MB` | No | `50` |

## Roadmap

### Phase 2
- Quality selection
- Audio-only downloads
- Better media type detection
- Progress/status updates
- Download queue and concurrency limits
- Better cleanup and error reporting

### Phase 3
- MongoDB
- Download history
- Telegram `file_id` caching
- User limits and rate limiting
- Admin controls

### Phase 4
- Premium subscriptions
- Web dashboard
- Object storage for larger media
- Public API
- Monitoring and analytics

## Important

MediaFetch is intended for content the user is authorized to download. Private, DRM-protected, login-required, or otherwise restricted content may not be downloadable, and platform terms and applicable laws still apply.
