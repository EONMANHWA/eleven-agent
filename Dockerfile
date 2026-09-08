FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright NODE_OPTIONS=--max-old-space-size=96
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium && chmod -R a+rX /ms-playwright
RUN useradd --create-home --uid 10001 bot
COPY --chown=bot:bot . .
USER bot
EXPOSE 10000
CMD ["python", "app.py"]
