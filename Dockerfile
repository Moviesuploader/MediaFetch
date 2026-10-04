FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates chromium build-essential git \
    && git clone --depth 1 --branch 2.0.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && deno install --node-modules-dir=auto --allow-scripts=npm:canvas --frozen \
    && cd /app \
    && rm -rf /opt/bgutil-ytdlp-pot-provider/.git \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/mediafetch \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /tmp/mediafetch /opt/bgutil-ytdlp-pot-provider

USER appuser

EXPOSE 8000

CMD ["python", "run.py"]
