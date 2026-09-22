import pytest

from app.bot.application import build_application
from app.core.config import settings


def test_application_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bot_token", "")
    with pytest.raises(RuntimeError, match="BOT_TOKEN is required"):
        build_application()
