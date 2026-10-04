import logging
import os
import subprocess
from pathlib import Path

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("mediafetch.bootstrap")
_provider_process: subprocess.Popen | None = None


def _start_pot_provider() -> None:
    """Start the bundled bgutil HTTP POT provider for YouTube."""
    global _provider_process

    enabled = os.getenv("YOUTUBE_POT_PROVIDER_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    provider_url = os.getenv(
        "YOUTUBE_POT_PROVIDER_URL",
        "http://127.0.0.1:4416",
    ).strip().rstrip("/")

    # If an external provider URL is configured, leave that provider under the
    # operator's control and do not start a second local server.
    if not enabled or provider_url not in {
        "http://127.0.0.1:4416",
        "http://localhost:4416",
    }:
        logger.info(
            "YouTube POT provider: local disabled/external url=%s",
            provider_url or "none",
        )
        return

    deno = Path("/usr/local/bin/deno")
    server = Path("/opt/bgutil-ytdlp-pot-provider/server/src/main.ts")
    node_modules = Path("/opt/bgutil-ytdlp-pot-provider/server/node_modules")
    if not deno.is_file() or not server.is_file() or not node_modules.is_dir():
        logger.warning(
            "YouTube POT provider files unavailable; continuing without local provider"
        )
        return

    try:
        _provider_process = subprocess.Popen(
            [
                str(deno),
                "run",
                "--allow-env",
                "--allow-net",
                f"--allow-ffi={node_modules}",
                f"--allow-read={node_modules}",
                str(server),
                "--host",
                "127.0.0.1",
                "--port",
                "4416",
            ],
            cwd=str(server.parent),
        )
        logger.info(
            "YouTube POT provider started pid=%s url=%s",
            _provider_process.pid,
            provider_url,
        )
    except Exception:
        logger.exception("Failed to start YouTube POT provider")


def _stop_pot_provider() -> None:
    global _provider_process
    if _provider_process is None:
        return
    if _provider_process.poll() is None:
        try:
            _provider_process.terminate()
            _provider_process.wait(timeout=5)
        except Exception:
            try:
                _provider_process.kill()
            except Exception:
                pass
    _provider_process = None


if __name__ == "__main__":
    _start_pot_provider()
    try:
        uvicorn.run(
            "app.api.main:app",
            host="0.0.0.0",
            port=int(os.getenv("PORT", "8000")),
            reload=False,
        )
    finally:
        _stop_pot_provider()
