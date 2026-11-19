FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

# Run as an unprivileged user (UID 1000 is what Hugging Face Spaces expects)
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code, UI, vendored JS libraries and the scraped knowledge base (app/data/scraped_data.json).
# No API keys are copied into the image: OPENROUTER_API_KEY is injected at runtime.
COPY --chown=appuser:appuser app/ ./app/

USER appuser
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"7860\")}/api/health', timeout=4)" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
